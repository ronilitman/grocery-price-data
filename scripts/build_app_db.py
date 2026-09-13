"""Build app.db: the read-optimised copy of prices.db the API will serve.

prices.db (~958 MB) carries everything the nightly pipeline needs, including
promo_stores (10.6M rows) and product_tokens (940k rows) that this database
replaces with a branch bitmap (``store_bits`` + ``deals.branches``, KAN-7) and
an FTS5 index (KAN-8) respectively. This build copies the core catalogue,
adds the columns the browse/category pages need, and now the ``deals`` table
the Discounts page reads. Search comes next; see KAN-8.

Usage:
    python3 scripts/build_app_db.py --db prices.db --out app.db

--db is attached read-only (file:...?mode=ro) so this can run against a copy
someone else still has open, and can never touch the source. The output is
built at <out>.tmp and only os.replace'd onto <out> once everything -
including ANALYZE and VACUUM - has succeeded, so a crash never leaves a
half-built database where the API (or a naive `ls`) would find it.
"""

import argparse
import json
import os
import pathlib
import re
import sqlite3
import sys
import time

import bitmap
import offers as offers_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATEGORIES_JSON = os.path.join(ROOT, "data", "categories.json")
PRODUCT_CATEGORIES_TSV = os.path.join(ROOT, "data", "product_categories.tsv")

# Same shape as merge_db.SCHEMA for every table we carry over, plus three new
# columns on products and the deals/store_bits pair. promo_offers,
# promo_stores and product_tokens are deliberately absent: the first two are
# replaced by store_bits + deals.branches (KAN-7) and the last becomes FTS5
# (KAN-8) - copying them here would just be thrown away.
SCHEMA = """
CREATE TABLE chains(chain_id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE stores(
    chain_id TEXT NOT NULL, store_id TEXT NOT NULL, subchain_id TEXT,
    store_name TEXT, city TEXT, address TEXT, priced_items INTEGER DEFAULT 0,
    PRIMARY KEY (chain_id, store_id));
CREATE TABLE products(
    barcode TEXT PRIMARY KEY, name TEXT, manufacturer TEXT, unit_qty TEXT,
    quantity REAL, unit_of_measure TEXT, is_weighted INTEGER,
    category_id INTEGER,
    -- Whether a product image exists at m.pricez.co.il. Left NULL here on
    -- purpose: probing ~243k image URLs from CI is out of scope for this
    -- build (see KAN-4/KAN-6). The column exists now so a later job can
    -- populate it - true/false, once known - without a schema migration.
    has_image INTEGER,
    -- Whitespace-collapsed, trimmed product name. Keyset pagination over
    -- `products` orders by this instead of `name` so a run of raw double
    -- spaces or leading/trailing junk in the source data can't split a
    -- product's neighbours across pages differently between two builds.
    sort_key TEXT);
CREATE TABLE chain_prices(
    chain_id TEXT NOT NULL, barcode TEXT NOT NULL, price REAL NOT NULL,
    store_count INTEGER, PRIMARY KEY (chain_id, barcode));
CREATE TABLE price_exceptions(
    chain_id TEXT NOT NULL, store_id TEXT NOT NULL, barcode TEXT NOT NULL,
    price REAL NOT NULL, PRIMARY KEY (chain_id, store_id, barcode));
CREATE TABLE chain_products(
    chain_id TEXT NOT NULL, barcode TEXT NOT NULL, name TEXT,
    unit_qty TEXT, unit_of_measure TEXT, is_weighted INTEGER,
    PRIMARY KEY (chain_id, barcode));
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE categories(
    id INTEGER PRIMARY KEY, slug TEXT, name_he TEXT, parent_id INTEGER);
-- Bit i within a chain is branch i, ordered by store_id. Lets a caller turn a
-- deals.branches bitmap back into store ids without re-deriving the order.
CREATE TABLE store_bits(
    chain_id TEXT NOT NULL, store_id TEXT NOT NULL, bit INTEGER NOT NULL,
    PRIMARY KEY (chain_id, store_id));
-- One row per kept offer, after the same merge-and-domination-prune
-- build_catalog.py's promo/ JSON uses (scripts/offers.py, KAN-7) - so the two
-- outputs can never disagree about which offers survive. discount_pct is
-- materialised so the Discounts page can sort/filter without joining
-- chain_prices at query time, and branches is a bitmap into store_bits
-- instead of a promo_stores-shaped link table (10.6M rows, ~380 MB measured)
-- - 28 MB measured on 537k synthetic offers.
CREATE TABLE deals(
    deal_id INTEGER PRIMARY KEY,
    chain_id TEXT NOT NULL, barcode TEXT NOT NULL, category_id INTEGER,
    name TEXT, base_price REAL,
    unit_price REAL NOT NULL, price REAL NOT NULL, min_qty REAL NOT NULL,
    club INTEGER NOT NULL, coupon INTEGER NOT NULL,
    starts TEXT, ends TEXT, description TEXT,
    -- round((1 - unit_price / base_price) * 100, 1); NULL with no baseline
    -- price for that chain+barcode. Rows are kept even when this is <= 0 or
    -- NULL - a multi-buy deal can be no cheaper than the shelf price - the
    -- Discounts page is what filters to > 0, not this build step.
    discount_pct REAL,
    branches BLOB NOT NULL);
"""

INDEXES = [
    "CREATE INDEX idx_products_category_sort "
    "ON products(category_id, sort_key, barcode)",
    "CREATE INDEX idx_chain_prices_barcode ON chain_prices(barcode)",
    "CREATE INDEX idx_stores_chain ON stores(chain_id)",
    "CREATE INDEX idx_deals_discount ON deals(discount_pct DESC, deal_id)",
    "CREATE INDEX idx_deals_chain_cat_discount "
    "ON deals(chain_id, category_id, discount_pct DESC, deal_id)",
    "CREATE INDEX idx_deals_barcode ON deals(barcode)",
]

# Tables copied verbatim from the source. products is handled separately
# because it gains three columns. Named column lists, not SELECT *, so a
# schema drift between prices.db and this script's SCHEMA fails loudly
# instead of silently misaligning columns.
COPY = [
    ("chains",
     "INSERT INTO chains (chain_id, name) "
     "SELECT chain_id, name FROM src.chains"),
    ("stores",
     "INSERT INTO stores "
     "(chain_id, store_id, subchain_id, store_name, city, address, priced_items) "
     "SELECT chain_id, store_id, subchain_id, store_name, city, address, priced_items "
     "FROM src.stores"),
    ("chain_prices",
     "INSERT INTO chain_prices (chain_id, barcode, price, store_count) "
     "SELECT chain_id, barcode, price, store_count FROM src.chain_prices"),
    ("price_exceptions",
     "INSERT INTO price_exceptions (chain_id, store_id, barcode, price) "
     "SELECT chain_id, store_id, barcode, price FROM src.price_exceptions"),
    ("chain_products",
     "INSERT INTO chain_products "
     "(chain_id, barcode, name, unit_qty, unit_of_measure, is_weighted) "
     "SELECT chain_id, barcode, name, unit_qty, unit_of_measure, is_weighted "
     "FROM src.chain_products"),
    ("meta", "INSERT INTO meta (key, value) SELECT key, value FROM src.meta"),
]

WHITESPACE = re.compile(r"\s+")


def sort_key_for(name):
    """Whitespace-collapsed, trimmed name; None/empty pass through unchanged."""
    if not name:
        return name
    return WHITESPACE.sub(" ", name).strip()


def load_category_map(tsv_path):
    """barcode -> category_id, from the no-header, tab-separated TSV."""
    mapping = {}
    with open(tsv_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            barcode, category_id, _source_chain = line.split("\t")
            mapping[barcode] = int(category_id)
    return mapping


def load_categories(json_path):
    with open(json_path, encoding="utf-8") as handle:
        return json.load(handle)


def ro_uri(path):
    """A file: URI SQLite's ATTACH will open strictly read-only."""
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def copy_products(conn, category_map):
    rows = []
    cur = conn.execute(
        "SELECT barcode, name, manufacturer, unit_qty, quantity, "
        "unit_of_measure, is_weighted FROM src.products"
    )
    for barcode, name, manufacturer, unit_qty, quantity, unit_of_measure, is_weighted in cur:
        rows.append((
            barcode, name, manufacturer, unit_qty, quantity, unit_of_measure,
            is_weighted, category_map.get(barcode), None, sort_key_for(name),
        ))
    conn.executemany(
        "INSERT INTO products "
        "(barcode, name, manufacturer, unit_qty, quantity, unit_of_measure, "
        " is_weighted, category_id, has_image, sort_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def copy_categories(conn, categories):
    conn.executemany(
        "INSERT INTO categories (id, slug, name_he, parent_id) VALUES (?,?,?,?)",
        [(c["id"], c["slug"], c["name_he"], c["parent_id"]) for c in categories],
    )
    return len(categories)


def build_store_bits(conn):
    """Assign each chain's branches a 0..n-1 bit, ordered by store_id.

    Chain ids can look like ``7290058160839-001`` (sub-chain splits); they are
    opaque strings here, same as everywhere else - a chain's bit space is
    private to it, so two chains can both use bit 0 for different branches.

    Returns ``{chain_id: {store_id: bit}}`` for build_deals to pack bitmaps
    against, plus the number of store_bits rows written.
    """
    bit_of = {}
    rows = []
    for (chain_id,) in conn.execute(
            "SELECT DISTINCT chain_id FROM src.stores ORDER BY chain_id"):
        store_ids = [sid for (sid,) in conn.execute(
            "SELECT store_id FROM src.stores WHERE chain_id = ? ORDER BY store_id",
            (chain_id,))]
        bit_of[chain_id] = {sid: i for i, sid in enumerate(store_ids)}
        rows.extend((chain_id, sid, i) for i, sid in enumerate(store_ids))
    conn.executemany("INSERT INTO store_bits VALUES (?,?,?)", rows)
    return bit_of, len(rows)


def build_deals(conn, bit_of, category_map):
    """One deals row per offer that survives scripts/offers.py's merge and
    domination-prune - the same function build_catalog.py's promo/ JSON goes
    through, so the two can never disagree about which offers survive.

    ``bit_of`` is build_store_bits' ``{chain_id: {store_id: bit}}``, used to
    pack each offer's branch set into ``deals.branches``.
    """
    base_price = {(c, b): p for c, b, p in conn.execute(
        "SELECT chain_id, barcode, price FROM src.chain_prices")}
    # Only present for chains that priced this barcode by weight (see
    # merge_db.py's chain_products docstring); falls back to the merged
    # products name below when a chain has no name of its own on file.
    chain_name = {(c, b): n for c, b, n in conn.execute(
        "SELECT chain_id, barcode, name FROM src.chain_products") if n}
    product_name = dict(conn.execute("SELECT barcode, name FROM src.products"))
    meta = dict(conn.execute("SELECT key, value FROM src.meta"))
    today = (meta.get("built_at") or "")[:10] or "0000-00-00"

    rows = []
    deal_id = 1
    merged_total = 0
    unmapped = 0
    for (chain_id,) in conn.execute(
            "SELECT DISTINCT chain_id FROM src.promo_offers"):
        merged, _everywhere = offers_mod.merge_chain(
            conn, chain_id, today, table_prefix="src.")
        merged_total += len(merged)
        kept = offers_mod.prune_dominated(merged)
        chain_bits = bit_of.get(chain_id, {})
        n = len(chain_bits)
        for (barcode, club, coupon, min_qty, unit_price), body in kept.items():
            bp = base_price.get((chain_id, barcode))
            discount_pct = (round((1 - unit_price / bp) * 100, 1)
                            if bp and bp > 0 else None)
            name = chain_name.get((chain_id, barcode)) or product_name.get(barcode)
            branch_bits = []
            for store_id in body["where"]:
                bit = chain_bits.get(store_id)
                if bit is None:
                    unmapped += 1
                    continue
                branch_bits.append(bit)
            rows.append((
                deal_id, chain_id, barcode, category_map.get(barcode), name,
                bp, unit_price, body["price"], min_qty, club, coupon,
                body["starts"] or None, body["ends"] or None,
                body["description"] or None, discount_pct,
                bitmap.pack(branch_bits, n),
            ))
            deal_id += 1
    conn.executemany(
        "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    if unmapped:
        print(f"[build_app_db] {unmapped} offer-branch link(s) named a "
              f"store_id absent from stores; dropped from the bitmap",
              file=sys.stderr)
    return len(rows), merged_total


def build(db_path, out_path, categories_json=CATEGORIES_JSON,
          categories_tsv=PRODUCT_CATEGORIES_TSV):
    start = time.perf_counter()
    tmp_path = out_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    conn = sqlite3.connect(tmp_path)
    conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    conn.executescript(SCHEMA)
    conn.execute("ATTACH DATABASE ? AS src", (ro_uri(db_path),))

    counts = {}
    for table, statement in COPY:
        conn.execute(statement)
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    category_map = load_category_map(categories_tsv)
    counts["products"] = copy_products(conn, category_map)

    bit_of, store_bits_rows = build_store_bits(conn)
    counts["store_bits"] = store_bits_rows
    deals_rows, merged_total = build_deals(conn, bit_of, category_map)
    counts["deals"] = deals_rows

    conn.commit()
    conn.execute("DETACH DATABASE src")

    counts["categories"] = copy_categories(conn, load_categories(categories_json))
    conn.commit()

    for statement in INDEXES:
        conn.execute(statement)

    conn.execute("ANALYZE")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    os.replace(tmp_path, out_path)

    elapsed = time.perf_counter() - start
    size = os.path.getsize(out_path)
    return counts, size, elapsed, merged_total


def print_summary(counts, size, elapsed, merged_total):
    print("[build_app_db] rows per table:")
    for table, count in counts.items():
        print(f"[build_app_db]   {table:<18}{count:>12,}")
    print(f"[build_app_db] deals: {counts['deals']:,} kept of {merged_total:,} "
          f"merged offers")
    print(f"[build_app_db] output size: {size / 1e6:.1f} MB")
    print(f"[build_app_db] runtime: {elapsed:.1f}s")


def main():
    parser = argparse.ArgumentParser(
        description="Build app.db, the read-optimised catalogue served by the API.")
    parser.add_argument("--db", required=True, help="merged prices.db (read-only)")
    parser.add_argument("--out", required=True, help="output app.db path")
    args = parser.parse_args()

    counts, size, elapsed, merged_total = build(args.db, args.out)
    print_summary(counts, size, elapsed, merged_total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
