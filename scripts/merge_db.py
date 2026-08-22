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
    store_name TEXT, city TEXT, address TEXT,
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
CREATE TABLE promos(
    chain_id TEXT NOT NULL, store_id TEXT, promo_id TEXT, barcode TEXT NOT NULL,
    description TEXT, price REAL, starts TEXT, ends TEXT);
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
    "CREATE INDEX idx_promos_barcode ON promos(barcode)",
    "CREATE INDEX idx_promos_chain ON promos(chain_id, ends)",
]

COPY = [
    ("chains", "INSERT OR REPLACE INTO chains SELECT * FROM src.chains"),
    ("stores", "INSERT OR REPLACE INTO stores SELECT * FROM src.stores"),
    ("products", "INSERT OR REPLACE INTO products SELECT * FROM src.products"),
    ("chain_prices", "INSERT OR REPLACE INTO chain_prices SELECT * FROM src.chain_prices"),
    ("price_exceptions", "INSERT OR REPLACE INTO price_exceptions SELECT * FROM src.price_exceptions"),
    ("promos", "INSERT INTO promos SELECT * FROM src.promos"),
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

    merged = []
    for part in parts:
        conn.execute("ATTACH DATABASE ? AS src", (part,))
        for table, statement in COPY:
            try:
                conn.execute(statement)
            except sqlite3.Error as err:
                print(f"[merge] {os.path.basename(part)}:{table}: {err}", file=sys.stderr)
        conn.commit()
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
                      "price_exceptions", "promos", "product_tokens")
    }
    built_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("built_at", built_at),
        ("sources", json.dumps(merged)),
        ("counts", json.dumps(counts)),
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
        json.dump({"built_at": built_at, "bytes": size,
                   "counts": counts, "sources": merged}, handle,
                  ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
