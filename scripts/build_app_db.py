"""Build app.db: the read-optimised copy of prices.db the API will serve.

prices.db (~958 MB) carries everything the nightly pipeline needs, including
promo_stores (10.6M rows) and product_tokens (940k rows) that this database
replaces with a branch bitmap and an FTS5 index respectively - in later
subtasks. This one only copies the core catalogue and adds the columns the
browse/category pages need. Deals and search come next; see KAN-7 and KAN-8.

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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATEGORIES_JSON = os.path.join(ROOT, "data", "categories.json")
PRODUCT_CATEGORIES_TSV = os.path.join(ROOT, "data", "product_categories.tsv")

# Same shape as merge_db.SCHEMA for every table we carry over, plus three new
# columns on products. promo_offers, promo_stores and product_tokens are
# deliberately absent: the first two become a bitmap-backed deals table
# (KAN-7) and the last becomes FTS5 (KAN-8) - copying them here would just be
# thrown away.
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
"""

INDEXES = [
    "CREATE INDEX idx_products_category_sort "
    "ON products(category_id, sort_key, barcode)",
    "CREATE INDEX idx_chain_prices_barcode ON chain_prices(barcode)",
    "CREATE INDEX idx_stores_chain ON stores(chain_id)",
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
    return counts, size, elapsed


def print_summary(counts, size, elapsed):
    print("[build_app_db] rows per table:")
    for table, count in counts.items():
        print(f"[build_app_db]   {table:<18}{count:>12,}")
    print(f"[build_app_db] output size: {size / 1e6:.1f} MB")
    print(f"[build_app_db] runtime: {elapsed:.1f}s")


def main():
    parser = argparse.ArgumentParser(
        description="Build app.db, the read-optimised catalogue served by the API.")
    parser.add_argument("--db", required=True, help="merged prices.db (read-only)")
    parser.add_argument("--out", required=True, help="output app.db path")
    args = parser.parse_args()

    counts, size, elapsed = build(args.db, args.out)
    print_summary(counts, size, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
