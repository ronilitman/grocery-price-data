"""Merge the per-chain SQLite files into the single database we publish.

Page size is forced to 1 KiB. That is what makes the file cheap to query over
HTTP range requests: a browser fetching one index page pulls 1 KiB, not the
4 KiB SQLite would otherwise default to.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE chains(chain_id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE stores(
    chain_id TEXT NOT NULL, store_id TEXT NOT NULL, subchain_id TEXT,
    store_name TEXT, city TEXT, address TEXT, priced_items INTEGER DEFAULT 0,
    PRIMARY KEY (chain_id, store_id));
CREATE TABLE products(
    barcode TEXT PRIMARY KEY, name TEXT, manufacturer TEXT, unit_qty TEXT,
    quantity REAL, unit_of_measure TEXT, is_weighted INTEGER);
CREATE TABLE chain_prices(
    chain_id TEXT NOT NULL, barcode TEXT NOT NULL, price REAL NOT NULL,
    store_count INTEGER, PRIMARY KEY (chain_id, barcode));
CREATE TABLE price_exceptions(
    chain_id TEXT NOT NULL, store_id TEXT NOT NULL, barcode TEXT NOT NULL,
    price REAL NOT NULL, PRIMARY KEY (chain_id, store_id, barcode));
-- Keyed on the offer's terms, not its PromotionID; see build_chain_db.py.
CREATE TABLE promo_offers(
    offer_id INTEGER PRIMARY KEY, chain_id TEXT NOT NULL, promo_id TEXT NOT NULL,
    barcode TEXT NOT NULL, club INTEGER NOT NULL, min_qty REAL NOT NULL,
    price REAL NOT NULL, unit_price REAL NOT NULL, description TEXT,
    starts TEXT, ends TEXT);
CREATE TABLE promo_stores(
    offer_id INTEGER NOT NULL, store_id TEXT NOT NULL,
    PRIMARY KEY (offer_id, store_id));
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE product_tokens(token TEXT NOT NULL, barcode TEXT NOT NULL);
"""

# The price a given store charges: its own exception if it has one, else the
# chain baseline. Every "cheapest in city" query goes through this.
VIEW = """
CREATE VIEW store_prices AS
SELECT s.chain_id, s.store_id, s.city, b.barcode,
       COALESCE(e.price, b.price) AS price
FROM stores s
JOIN chain_prices b ON b.chain_id = s.chain_id
LEFT JOIN price_exceptions e
       ON e.chain_id = s.chain_id AND e.store_id = s.store_id AND e.barcode = b.barcode;
"""

INDEXES = [
    "CREATE INDEX idx_prices_barcode ON chain_prices(barcode, price)",
    "CREATE INDEX idx_exceptions_barcode ON price_exceptions(barcode)",
    "CREATE INDEX idx_stores_city ON stores(city, chain_id, store_id)",
    "CREATE INDEX idx_products_name ON products(name)",
    "CREATE INDEX idx_tokens ON product_tokens(token, barcode)",
    "CREATE INDEX idx_offers_barcode ON promo_offers(barcode, unit_price)",
    "CREATE INDEX idx_offers_chain ON promo_offers(chain_id, ends)",
]

COPY = [
    ("chains", "INSERT OR REPLACE INTO chains SELECT * FROM src.chains"),
    # Named, not SELECT *: a chain database built before priced_items existed
    # has one column fewer, and the positional form would silently skip the
    # whole stores table for that chain - losing every branch it holds.
    ("stores", "INSERT OR REPLACE INTO stores "
               "(chain_id, store_id, subchain_id, store_name, city, address, priced_items) "
               "SELECT chain_id, store_id, subchain_id, store_name, city, address, "
               "{priced} FROM src.stores"),
    ("products", "INSERT OR REPLACE INTO products SELECT * FROM src.products"),
    ("chain_prices", "INSERT OR REPLACE INTO chain_prices SELECT * FROM src.chain_prices"),
    ("price_exceptions", "INSERT OR REPLACE INTO price_exceptions SELECT * FROM src.price_exceptions"),
    # offer_id is assigned per chain database, so ids collide across chains.
    # {offset} shifts each source past everything merged so far - exact, unlike
    # re-joining on a key that contains REALs.
    ("promo_offers",
     "INSERT INTO promo_offers SELECT offer_id + {offset}, chain_id, promo_id, "
     "barcode, club, min_qty, price, unit_price, description, starts, ends "
     "FROM src.promo_offers"),
    ("promo_stores",
     "INSERT INTO promo_stores SELECT offer_id + {offset}, store_id "
     "FROM src.promo_stores"),
]


TOKEN_SPLIT = re.compile(r"[^\w\u0590-\u05FF]+", re.UNICODE)


def build_tokens(conn):
    """Word index over product names.

    Product search needs to match a word anywhere in the name, and
    ``LIKE '%milk%'`` cannot use an index - over HTTP range requests that means
    pulling the whole products table for every keystroke. Indexing the
    individual words turns it back into a prefix seek.
    """
    rows = []
    for barcode, name in conn.execute("SELECT barcode, name FROM products WHERE name <> ''"):
        for token in set(TOKEN_SPLIT.split(name.lower())):
            if len(token) >= 2:
                rows.append((token, barcode))
        if len(rows) >= 100000:
            conn.executemany("INSERT INTO product_tokens VALUES (?,?)", rows)
            rows.clear()
    if rows:
        conn.executemany("INSERT INTO product_tokens VALUES (?,?)", rows)
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Merge per-chain databases into one.")
    parser.add_argument("--in-dir", default="chain_dbs")
    parser.add_argument("--out", default="dist/prices.db")
    args = parser.parse_args()

    parts = sorted(glob.glob(os.path.join(args.in_dir, "*.db")))
    if not parts:
        print(f"[merge] nothing to merge in {args.in_dir}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)

    conn = sqlite3.connect(args.out)
    conn.execute("PRAGMA page_size=1024")   # must precede any table creation
    conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    conn.executescript(SCHEMA)

    # backfill_chains.py records when each chain's data was actually built. A
    # chain carried forward from an earlier run is real data, just older, and
    # the client has to be able to tell the difference.
    freshness = {}
    fresh_path = os.path.join(args.in_dir, "_freshness.json")
    if os.path.exists(fresh_path):
        with open(fresh_path, encoding="utf-8") as handle:
            freshness = json.load(handle)

    merged = []
    chain_as_of = {}
    for part in parts:
        conn.execute("ATTACH DATABASE ? AS src", (part,))
        # NULL, not 0, when the source predates the column: "we did not measure"
        # and "this branch published nothing" must not look the same downstream.
        src_columns = {row[1] for row in conn.execute("PRAGMA src.table_info(stores)")}
        priced = "priced_items" if "priced_items" in src_columns else "NULL"
        offset = conn.execute(
            "SELECT COALESCE(MAX(offer_id), 0) FROM promo_offers").fetchone()[0]
        for table, statement in COPY:
            try:
                conn.execute(statement.format(priced=priced, offset=offset))
            except sqlite3.Error as err:
                print(f"[merge] {os.path.basename(part)}:{table}: {err}", file=sys.stderr)
        conn.commit()
        # Chain ids live in the source, the timestamp is keyed by file name, so
        # the two are married here while src is still attached.
        as_of = freshness.get(os.path.basename(part)[:-3].upper())
        if as_of:
            for (chain_id,) in conn.execute("SELECT chain_id FROM src.chains"):
                chain_as_of[chain_id] = as_of
        conn.execute("DETACH DATABASE src")
        merged.append(os.path.basename(part))
        print(f"[merge] merged {os.path.basename(part)}")

    build_tokens(conn)
    conn.executescript(VIEW)
    for statement in INDEXES:
        conn.execute(statement)

    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("chains", "stores", "products", "chain_prices",
                      "price_exceptions", "promo_offers", "promo_stores",
                      "product_tokens")
    }
    built_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("built_at", built_at),
        ("sources", json.dumps(merged)),
        ("counts", json.dumps(counts)),
        ("chain_as_of", json.dumps(chain_as_of)),
    ])
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.execute("VACUUM")          # applies page_size and compacts
    conn.close()

    size = os.path.getsize(args.out)
    print(f"[merge] {args.out}  {size / 1e6:.1f} MB")
    for table, count in counts.items():
        print(f"[merge]   {table:<18}{count:>12,}")

    with open(os.path.join(os.path.dirname(args.out) or ".", "metadata.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"built_at": built_at, "bytes": size, "counts": counts,
                   "sources": merged, "chain_as_of": chain_as_of}, handle,
                  ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
