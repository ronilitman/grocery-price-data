"""Build app.db: the read-optimised copy of prices.db the API will serve.

prices.db (~958 MB) carries everything the nightly pipeline needs, including
promo_stores (10.6M rows) and product_tokens (940k rows) that this database
replaces with a branch bitmap (``store_bits`` + ``deals.branches``, KAN-7) and
an FTS5 index (KAN-8) respectively. This build copies the core catalogue,
adds the columns the browse/category pages need, the ``deals`` table the
Discounts page reads, and the ``fts_all``/``fts_deals`` FTS5 search indexes
(KAN-8). The ``/search`` endpoint that queries them is a later task (KAN-12).

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
from collections import defaultdict

import app_search
import bitmap
import offers as offers_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATEGORIES_JSON = os.path.join(ROOT, "data", "categories.json")
PRODUCT_CATEGORIES_TSV = os.path.join(ROOT, "data", "product_categories.tsv")
PRODUCT_NAMES_JSON = os.path.join(ROOT, "data", "product_names.json")

# Same shape as merge_db.SCHEMA for every table we carry over, plus three new
# columns on products and the deals/store_bits pair. promo_offers,
# promo_stores and product_tokens are deliberately absent: the first two are
# replaced by store_bits + deals.branches (KAN-7) and the last becomes FTS5
# (KAN-8) - copying them here would just be thrown away.
SCHEMA = """
CREATE TABLE chains(chain_id TEXT PRIMARY KEY, name TEXT, chain_uid TEXT);
CREATE TABLE stores(
    chain_id TEXT NOT NULL, store_id TEXT NOT NULL, subchain_id TEXT,
    store_name TEXT, city TEXT, city_name TEXT, address TEXT,
    priced_items INTEGER DEFAULT 0, branch_uid TEXT,
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
-- One row per chain with any promo_offers row at all: the "everywhere" set
-- scripts/offers.py's merge_chain returns alongside its merged offers - every
-- branch that ever published ANY promotion for the chain, expired or not
-- (unioned before merge_chain's `ends < today` skip). build_catalog.py's
-- promo/*.json needs exactly this set to choose `s` vs `x` vs neither
-- (KAN-13); it is NOT reconstructable from `deals` alone, which holds only
-- kept, unexpired offers - a branch whose only offer expired would be
-- missing from any union-of-deals reconstruction, silently turning a
-- would-be `x` (or "runs everywhere") into a wrong `s`. Bit order matches
-- store_bits for the same chain, so a caller decodes both bitmaps the same
-- way.
CREATE TABLE promo_everywhere(
    chain_id TEXT PRIMARY KEY, branches BLOB NOT NULL);
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
-- KAN-15: one row per product currently on a real discount - the Discounts
-- page's card list. "One card per product, represented by its best (lowest
-- unit_price) deal" is a GROUP BY barcode over every deals row with
-- discount_pct > 0 (~426k on the real catalogue), and the representative's
-- own discount_pct - not the group's max - decides the card's sort
-- position, so it cannot be found by walking `deals` in discount order and
-- stopping early: the cheapest-unit-price offer for a barcode can sit
-- anywhere in that order. Materialising it here (build_deal_products)
-- turns every /deals request into an index range read over ~94k rows
-- (matching fts_deals' own "distinct barcode with discount_pct > 0" count,
-- KAN-8) instead of a per-request scan of the full deals table - the same
-- trade discount_pct itself already makes, one table up.
CREATE TABLE deal_products(
    barcode TEXT PRIMARY KEY,
    -- The representative deal's id: lowest unit_price wins, ties broken by
    -- the lowest deal_id, so the pick (and the page's sort) is stable.
    deal_id INTEGER NOT NULL,
    chain_id TEXT NOT NULL, category_id INTEGER,
    name TEXT, base_price REAL,
    unit_price REAL NOT NULL, price REAL NOT NULL, min_qty REAL NOT NULL,
    club INTEGER NOT NULL, coupon INTEGER NOT NULL,
    ends TEXT, description TEXT,
    discount_pct REAL NOT NULL,
    -- Distinct chains with a discount_pct > 0 deal on this barcode - never
    -- counts a chain whose only offer here is <= 0 or NULL.
    chains_on_deal INTEGER NOT NULL);
-- Words carried by 1%+ of product names (same rule as
-- build_catalog.NAME_FILLER_AT). NOT dropped from the indexes below - a
-- first cut of this table did that and it deleted real product words along
-- with packaging noise (שוקולד/chocolate, עוף/chicken and יין/wine all
-- clear 1% on the real catalogue), so searching any of them found nothing.
-- This is a query-time table only: scripts/app_search.build_match trims a
-- *query* to its filler words only when every other word in it is also
-- filler, so bm25() - which already ranks a document dominated by a common
-- word below a more distinctive match - gets to do the actual work.
CREATE TABLE fts_filler(token TEXT PRIMARY KEY);
-- One row per product, every token of its name indexed (scripts.
-- app_search.index_text) - filler included, see fts_filler above.
CREATE VIRTUAL TABLE fts_all USING fts5(
    name, barcode UNINDEXED, tokenize='unicode61 remove_diacritics 2');
-- Same shape as fts_all, narrowed to the barcodes currently on a real
-- discount (one row per distinct barcode in `deals` with
-- `discount_pct > 0`) - the Discounts page's own search never has to look
-- past a product that isn't discounted (10ms vs 58ms on `גרם`, measured).
CREATE VIRTUAL TABLE fts_deals USING fts5(
    name, barcode UNINDEXED, tokenize='unicode61 remove_diacritics 2');
-- KAN-17: one row per category (top-level and sub), precomputed at build
-- time so /categories never counts per request. A sub-category's count is
-- its distinct categorised products; a top-level category's is the sum of
-- its children's. Every category gets a row, including count-0 ones - the
-- API filters those out, this table just answers "how many" for any id.
CREATE TABLE category_counts(category_id INTEGER PRIMARY KEY, count INTEGER NOT NULL);
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
    # KAN-15
    "CREATE INDEX idx_deal_products_discount ON deal_products(discount_pct DESC, deal_id)",
    "CREATE INDEX idx_deal_products_chain_cat "
    "ON deal_products(chain_id, category_id, discount_pct DESC, deal_id)",
]

# Tables copied verbatim from the source. products is handled separately
# because it gains extra columns. Named column lists, not SELECT *, so a
# schema drift between prices.db and this script's SCHEMA fails loudly
# instead of silently misaligning columns.
COPY = [
    ("chains",
     "INSERT INTO chains (chain_id, name, chain_uid) "
     "SELECT chain_id, name, chain_uid FROM src.chains"),
    ("stores",
     "INSERT INTO stores "
     "(chain_id, store_id, subchain_id, store_name, city, city_name, address, "
     "priced_items, branch_uid) "
     "SELECT chain_id, store_id, subchain_id, store_name, city, city_name, address, "
     "priced_items, branch_uid FROM src.stores"),
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


def load_product_names(json_path):
    """barcode -> human-decided name, from the product-names JSON table.

    The file is a plain JSON object (``json.load`` in one call, not JSONL):
    ``{"note": ..., "source_build": ..., "products": [{"barcode": ...,
    "name": ..., "source": ..., "decided_at": ...}, ...]}`` (KAN-29). A name
    is decided once, by a human, and kept - it stops the display name
    flipping to whichever chain's file happens to sort last, and stops it
    being one of the eleven chains' 20-character-capped names. Using plain
    JSON rather than a hand-delimited format means the encoder and decoder
    agree about quoting on their own - no name can come back mangled by a
    stray `"` or an embedded newline the way a naively-written TSV could.

    An entry that isn't an object, or has a missing/blank ``barcode`` or
    ``name``, is skipped rather than raising - a hand-reviewed batch is
    exactly the kind of file that picks up a stray bad row - and any key
    besides ``barcode``/``name`` is ignored, so a later field (brand, size,
    unit, image URL, category) can be added to new entries without this
    loader, or any entry written before it existed, breaking; teaching the
    build to use it is a separate change to this function. The file's own
    top-level shape is not optional, though: if it isn't a JSON object with
    a ``products`` array, that is a malformed file and this raises rather
    than silently returning an empty map.
    """
    with open(json_path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ValueError(
            f"{json_path}: expected a JSON object with a 'products' array")

    names = {}
    for entry in data["products"]:
        if not isinstance(entry, dict):
            continue
        barcode = str(entry.get("barcode") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not barcode or not name:
            continue
        names[barcode] = name
    return names


def ro_uri(path):
    """A file: URI SQLite's ATTACH will open strictly read-only."""
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def copy_products(conn, category_map, name_map=None):
    name_map = name_map or {}
    rows = []
    cur = conn.execute(
        "SELECT barcode, name, manufacturer, unit_qty, quantity, "
        "unit_of_measure, is_weighted FROM src.products"
    )
    for barcode, name, manufacturer, unit_qty, quantity, unit_of_measure, is_weighted in cur:
        # KAN-29: a human-decided name (data/product_names.json) wins over
        # whichever chain's file the merge happened to keep. Falls back to
        # today's name when the barcode isn't in the table yet, so partial
        # coverage never breaks a product that hasn't been reviewed.
        display_name = name_map.get(barcode, name)
        rows.append((
            barcode, display_name, manufacturer, unit_qty, quantity, unit_of_measure,
            is_weighted, category_map.get(barcode), None, sort_key_for(display_name),
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

    A branch's only appearance in the source data can be as a
    ``promo_stores.store_id`` with no matching row in ``stores`` - CITY_MARKET_
    SHOPS, YELLOW (which has zero ``stores`` rows at all) and a handful of
    Shufersal/Keshet Teamim branches are all real, on the real prices.db. Each
    chain's bit space is therefore built from the *union* of ``stores`` and
    every ``promo_stores`` id its offers reference, not ``stores`` alone - a
    branch known only from a promotion is still a branch, and build_deals
    below fails the build rather than silently dropping it from a bitmap.

    Chain ids can look like ``7290058160839-001`` (sub-chain splits); they are
    opaque strings here, same as everywhere else - a chain's bit space is
    private to it, so two chains can both use bit 0 for different branches.

    Returns ``{chain_id: {store_id: bit}}`` for build_deals to pack bitmaps
    against, plus the number of store_bits rows written.
    """
    chain_ids = [c for (c,) in conn.execute(
        "SELECT chain_id FROM src.stores "
        "UNION "
        "SELECT chain_id FROM src.promo_offers "
        "ORDER BY chain_id")]

    bit_of = {}
    rows = []
    for chain_id in chain_ids:
        store_ids = [sid for (sid,) in conn.execute(
            "SELECT store_id FROM src.stores WHERE chain_id = ? "
            "UNION "
            "SELECT ps.store_id FROM src.promo_stores ps "
            "JOIN src.promo_offers o ON o.offer_id = ps.offer_id "
            "WHERE o.chain_id = ? "
            "ORDER BY store_id",
            (chain_id, chain_id))]
        bit_of[chain_id] = {sid: i for i, sid in enumerate(store_ids)}
        rows.extend((chain_id, sid, i) for i, sid in enumerate(store_ids))
    conn.executemany("INSERT INTO store_bits VALUES (?,?,?)", rows)
    return bit_of, len(rows)


def build_category_counts(conn):
    """KAN-17: one row per category in ``categories``, precomputed so
    /categories never runs a COUNT(*) per request.

    A sub-category's count is its distinct categorised products; a
    top-level category's is the sum of its children's.
    """
    sub_counts = dict(conn.execute(
        "SELECT category_id, COUNT(*) FROM products "
        "WHERE category_id IS NOT NULL "
        "GROUP BY category_id"))
    parent_of = dict(conn.execute("SELECT id, parent_id FROM categories"))

    rows = []
    top_totals = defaultdict(int)
    for category_id, parent_id in parent_of.items():
        if parent_id is None:
            continue
        count = sub_counts.get(category_id, 0)
        rows.append((category_id, count))
        top_totals[parent_id] += count
    for category_id, parent_id in parent_of.items():
        if parent_id is None:
            rows.append((category_id, top_totals.get(category_id, 0)))

    conn.executemany("INSERT INTO category_counts VALUES (?,?)", rows)
    return len(rows)


def build_deals(conn, bit_of, category_map):
    """One deals row per offer that survives scripts/offers.py's merge and
    domination-prune - the same function build_catalog.py's promo/ JSON goes
    through, so the two can never disagree about which offers survive.

    Also writes ``promo_everywhere``: each chain's ``everywhere`` set from
    ``merge_chain`` (KAN-13) - every branch that ever published ANY
    promotion for the chain, expired or not. build_catalog.py needs exactly
    that set, not ``deals``' kept-only branches, to choose ``s``/``x``/
    neither the same way it always has - a branch whose only offer expired
    is in ``everywhere`` but would be invisible to anything reconstructed
    from ``deals`` alone.

    ``bit_of`` is build_store_bits' ``{chain_id: {store_id: bit}}``, used to
    pack both ``deals.branches`` and ``promo_everywhere.branches``.
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
    everywhere_rows = []
    deal_id = 1
    merged_total = 0
    for (chain_id,) in conn.execute(
            "SELECT DISTINCT chain_id FROM src.promo_offers"):
        merged, everywhere = offers_mod.merge_chain(
            conn, chain_id, today, table_prefix="src.")
        merged_total += len(merged)
        kept = offers_mod.prune_dominated(merged)
        chain_bits = bit_of.get(chain_id, {})
        n = len(chain_bits)

        def pack_branches(store_ids, chain_id=chain_id, chain_bits=chain_bits, n=n):
            bits = []
            for store_id in store_ids:
                bit = chain_bits.get(store_id)
                if bit is None:
                    # build_store_bits derives each chain's bit space from the
                    # union of stores and promo_stores, so this can only mean
                    # that invariant broke - never silently drop a branch
                    # (that is exactly the bug KAN-7's review fix closed:
                    # 61,089 real deals losing every branch this way).
                    raise ValueError(
                        f"store_bits has no bit for chain {chain_id!r} store "
                        f"{store_id!r} - build_store_bits must cover every "
                        f"promo_stores branch")
                bits.append(bit)
            return bitmap.pack(bits, n)

        everywhere_rows.append((chain_id, pack_branches(everywhere)))

        for (barcode, club, coupon, min_qty, unit_price), body in kept.items():
            bp = base_price.get((chain_id, barcode))
            discount_pct = (round((1 - unit_price / bp) * 100, 1)
                            if bp and bp > 0 else None)
            name = chain_name.get((chain_id, barcode)) or product_name.get(barcode)
            rows.append((
                deal_id, chain_id, barcode, category_map.get(barcode), name,
                bp, unit_price, body["price"], min_qty, club, coupon,
                body["starts"] or None, body["ends"] or None,
                body["description"] or None, discount_pct,
                pack_branches(body["where"]),
            ))
            deal_id += 1
    conn.executemany(
        "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO promo_everywhere VALUES (?,?)", everywhere_rows)
    return len(rows), merged_total


def build_deal_products(conn):
    """KAN-15: one deal_products row per barcode with a real (> 0) discount,
    representing it by the deal with the lowest unit_price (owner's rule:
    rank by the promotion's per-unit price regardless of minimum quantity),
    ties broken by the lowest deal_id. Must run after build_deals - it reads
    `deals` directly rather than re-deriving offers.

    Ordering the source rows by (barcode, unit_price, deal_id) and keeping
    the first row seen per barcode is the same "first-seen wins" trick
    build_catalog.py's own candidate-resolution code uses elsewhere - no
    window function needed, and it is O(rows) with rows already sorted by
    SQLite's own index, not O(rows^2).

    Expiry is not re-checked here: scripts/offers.py's merge_chain already
    drops `ends < today` (the build's "today") before a row ever reaches
    `deals`, so nothing in `deals` is expired to begin with - see build_deals.
    """
    rows = conn.execute(
        "SELECT deal_id, chain_id, barcode, category_id, name, base_price, "
        "unit_price, price, min_qty, club, coupon, ends, description, "
        "discount_pct FROM deals WHERE discount_pct > 0 "
        "ORDER BY barcode, unit_price ASC, deal_id ASC"
    ).fetchall()

    best = {}
    chains = {}
    for row in rows:
        barcode, chain_id = row[2], row[1]
        chains.setdefault(barcode, set()).add(chain_id)
        best.setdefault(barcode, row)

    out = []
    for barcode, row in best.items():
        (deal_id, chain_id, _barcode, category_id, name, base_price,
         unit_price, price, min_qty, club, coupon, ends, description,
         discount_pct) = row
        out.append((
            barcode, deal_id, chain_id, category_id, name, base_price,
            unit_price, price, min_qty, club, coupon, ends, description,
            discount_pct, len(chains[barcode]),
        ))
    conn.executemany(
        "INSERT INTO deal_products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        out)
    return len(out)


def build_fts(conn):
    """Populate fts_filler, fts_all and fts_deals (KAN-8).

    Every token of every name is indexed, filler included (2026-09-14
    reviewer override - the original spec stripped filler from the indexed
    text too, which on real data deleted words like שוקולד/chocolate and
    עוף/chicken, not just packaging noise). Filler is still computed and
    stored in fts_filler: it is what ``app_search.build_match`` trims a
    *query* down to, at query time, when doing so leaves at least one more
    distinctive token behind. ``deals`` must already be populated
    (build_deals runs first) - fts_deals reads it directly rather than
    re-deriving "on offer" itself.
    """
    names = [name for (name,) in conn.execute(
        "SELECT name FROM products WHERE name <> ''")]
    filler = app_search.compute_filler(names)
    conn.executemany(
        "INSERT INTO fts_filler VALUES (?)", [(t,) for t in sorted(filler)])

    all_rows = [
        (app_search.index_text(name), barcode)
        for barcode, name in conn.execute(
            "SELECT barcode, name FROM products WHERE name <> ''")
    ]
    conn.executemany("INSERT INTO fts_all (name, barcode) VALUES (?,?)", all_rows)
    conn.execute("INSERT INTO fts_all(fts_all) VALUES ('optimize')")

    name_of = dict(conn.execute("SELECT barcode, name FROM products"))
    deal_barcodes = [barcode for (barcode,) in conn.execute(
        "SELECT DISTINCT barcode FROM deals WHERE discount_pct > 0")]
    deals_rows = [
        (app_search.index_text(name_of.get(barcode) or ""), barcode)
        for barcode in deal_barcodes
    ]
    conn.executemany("INSERT INTO fts_deals (name, barcode) VALUES (?,?)", deals_rows)
    conn.execute("INSERT INTO fts_deals(fts_deals) VALUES ('optimize')")

    sizes = {}
    for table in ("fts_all", "fts_deals"):
        total, = conn.execute(
            "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat "
            "WHERE name = ? OR name GLOB ?", (table, table + "_*")).fetchone()
        sizes[table] = total

    print(f"[build_app_db] {len(filler)} filler words: {' '.join(sorted(filler))}")
    print(f"[build_app_db] fts_all: {len(all_rows):,} rows, "
          f"{sizes['fts_all'] / 1e6:.1f} MB")
    print(f"[build_app_db] fts_deals: {len(deals_rows):,} rows, "
          f"{sizes['fts_deals'] / 1e6:.1f} MB")

    return {"fts_all": len(all_rows), "fts_deals": len(deals_rows)}


def build(db_path, out_path, categories_json=CATEGORIES_JSON,
          categories_tsv=PRODUCT_CATEGORIES_TSV, names_json=PRODUCT_NAMES_JSON):
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
    name_map = load_product_names(names_json)
    counts["products"] = copy_products(conn, category_map, name_map)

    bit_of, store_bits_rows = build_store_bits(conn)
    counts["store_bits"] = store_bits_rows
    deals_rows, merged_total = build_deals(conn, bit_of, category_map)
    counts["deals"] = deals_rows
    counts["promo_everywhere"] = conn.execute(
        "SELECT COUNT(*) FROM promo_everywhere").fetchone()[0]
    counts["deal_products"] = build_deal_products(conn)

    conn.commit()
    conn.execute("DETACH DATABASE src")

    counts["categories"] = copy_categories(conn, load_categories(categories_json))
    conn.commit()

    # KAN-17: needs categories (just copied above) and products.category_id
    # (copy_products, above).
    counts["category_counts"] = build_category_counts(conn)
    conn.commit()

    counts.update(build_fts(conn))
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
