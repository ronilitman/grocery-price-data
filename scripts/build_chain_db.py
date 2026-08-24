"""outputs/*.csv  ->  one small SQLite file for a single chain.

The collapse that happens here is what keeps the whole thing free to host.
Raw per-store prices are roughly 80 million rows nationally, which is a
multi-gigabyte database. But Israeli chains price almost everything
chain-wide, so we store:

  chain_prices      one baseline price per (chain, barcode) - the modal price
  price_exceptions  only the stores that actually differ from that baseline

which is ~50x smaller and answers the same questions exactly.
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csvutil import find_csvs, read_rows, pick, digits, to_float  # noqa: E402

BATCH = 20000

SCHEMA = """
CREATE TABLE IF NOT EXISTS chains(
    chain_id TEXT PRIMARY KEY,
    name     TEXT
);
CREATE TABLE IF NOT EXISTS stores(
    chain_id     TEXT NOT NULL,
    store_id     TEXT NOT NULL,
    subchain_id  TEXT,
    store_name   TEXT,
    city         TEXT,
    address      TEXT,
    -- How many distinct products this branch actually published a price for.
    -- Nothing downstream can work this out: a branch that charges the chain
    -- baseline for everything is absent from price_exceptions, so "no rows in
    -- the exceptions table" and "published nothing at all" look identical
    -- once _raw is gone. Counted here while _raw still exists.
    priced_items INTEGER DEFAULT 0,
    PRIMARY KEY (chain_id, store_id)
);
CREATE TABLE IF NOT EXISTS products(
    barcode         TEXT PRIMARY KEY,
    name            TEXT,
    manufacturer    TEXT,
    unit_qty        TEXT,
    quantity        REAL,
    unit_of_measure TEXT,
    is_weighted     INTEGER
);
CREATE TABLE IF NOT EXISTS chain_prices(
    chain_id    TEXT NOT NULL,
    barcode     TEXT NOT NULL,
    price       REAL NOT NULL,
    store_count INTEGER,
    PRIMARY KEY (chain_id, barcode)
);
CREATE TABLE IF NOT EXISTS price_exceptions(
    chain_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    barcode  TEXT NOT NULL,
    price    REAL NOT NULL,
    PRIMARY KEY (chain_id, store_id, barcode)
);
CREATE TABLE IF NOT EXISTS promos(
    chain_id    TEXT NOT NULL,
    store_id    TEXT,
    promo_id    TEXT,
    barcode     TEXT NOT NULL,
    description TEXT,
    price       REAL,
    starts      TEXT,
    ends        TEXT
);
"""


def connect(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    conn.executescript(SCHEMA)
    return conn


def load_stores(conn, outputs):
    rows = []
    for path in find_csvs(outputs, "STORE_FILE"):
        for row in read_rows(path):
            chain_id = digits(pick(row, "chainid", "chain_id"))
            store_id = pick(row, "storeid", "store_id")
            if not chain_id or not store_id:
                continue
            rows.append((
                chain_id,
                str(store_id).lstrip("0") or "0",
                pick(row, "subchainid", "subchain_id"),
                pick(row, "storename", "store_name"),
                pick(row, "city", "cityname"),
                pick(row, "address"),
            ))
            chain_name = pick(row, "chainname", "chain_name")
            if chain_name:
                conn.execute("INSERT OR REPLACE INTO chains VALUES (?,?)", (chain_id, chain_name))
    conn.executemany(
        "INSERT OR REPLACE INTO stores "
        "(chain_id, store_id, subchain_id, store_name, city, address) "
        "VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def load_prices(conn, outputs):
    """Stream price rows into a staging table, then collapse in SQL."""
    conn.execute("CREATE TABLE _raw(chain_id TEXT, store_id TEXT, barcode TEXT, price REAL)")
    raw_batch, product_batch = [], []
    seen_chains = set()
    total = 0

    def flush():
        if raw_batch:
            conn.executemany("INSERT INTO _raw VALUES (?,?,?,?)", raw_batch)
            raw_batch.clear()
        if product_batch:
            conn.executemany(
                "INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?)", product_batch)
            product_batch.clear()

    for path in find_csvs(outputs, "PRICE_FULL_FILE"):
        print(f"[build] reading {os.path.basename(path)}")
        for row in read_rows(path):
            barcode = digits(pick(row, "itemcode", "item_code"))
            if len(barcode) < 6:
                continue
            price = to_float(pick(row, "itemprice", "item_price"))
            if price is None or price <= 0:
                continue
            chain_id = digits(pick(row, "chainid", "chain_id"))
            store_id = str(pick(row, "storeid", "store_id")).lstrip("0") or "0"
            if not chain_id:
                continue
            seen_chains.add(chain_id)

            raw_batch.append((chain_id, store_id, barcode, price))
            name = pick(row, "itemname", "item_name", "manufactureitemdescription")
            if name:
                product_batch.append((
                    barcode,
                    name.strip(),
                    pick(row, "manufacturername", "manufacturename"),
                    pick(row, "unitqty", "unit_qty"),
                    to_float(pick(row, "quantity")),
                    pick(row, "unitofmeasure", "unit_of_measure"),
                    1 if str(pick(row, "bisweighted", "isweighted", default="0")).strip() in ("1", "true", "True") else 0,
                ))
            total += 1
            if len(raw_batch) >= BATCH:
                flush()
    flush()
    conn.commit()
    return total, seen_chains


def collapse(conn):
    # One PriceFull publish per store is the norm, but chains sometimes publish
    # several a day. Keep the last row seen for each (store, barcode).
    conn.execute("""
        DELETE FROM _raw WHERE rowid NOT IN (
            SELECT MAX(rowid) FROM _raw GROUP BY chain_id, store_id, barcode
        )
    """)
    # Baseline = the price charged by the most stores; ties broken by the
    # lower price so the baseline never overstates.
    conn.execute("""
        INSERT OR REPLACE INTO chain_prices (chain_id, barcode, price, store_count)
        SELECT chain_id, barcode, price, cnt FROM (
            SELECT chain_id, barcode, price, COUNT(*) AS cnt,
                   ROW_NUMBER() OVER (
                       PARTITION BY chain_id, barcode
                       ORDER BY COUNT(*) DESC, price ASC
                   ) AS rn
            FROM _raw
            GROUP BY chain_id, barcode, price
        ) WHERE rn = 1
    """)
    conn.execute("""
        INSERT OR REPLACE INTO price_exceptions (chain_id, store_id, barcode, price)
        SELECT r.chain_id, r.store_id, r.barcode, r.price
        FROM _raw r
        JOIN chain_prices c ON c.chain_id = r.chain_id AND c.barcode = r.barcode
        WHERE ABS(r.price - c.price) > 0.001
    """)
    # Must run before the DROP: _raw is the only place per-store coverage
    # exists. A branch with no row here published no prices at all, and a cart
    # priced against it would be pure chain baseline - a plausible-looking
    # total invented from nothing.
    conn.execute("""
        UPDATE stores SET priced_items = COALESCE((
            SELECT COUNT(*) FROM _raw r
            WHERE r.chain_id = stores.chain_id AND r.store_id = stores.store_id
        ), 0)
    """)
    conn.execute("DROP TABLE _raw")
    conn.commit()


def _barcodes_from_promo_row(row):
    """Promo files nest their items; the CSV writer stores that as JSON."""
    direct = digits(pick(row, "itemcode", "item_code"))
    if len(direct) >= 6:
        return [direct]
    found = []
    for key, value in row.items():
        if "item" not in key or not value or value[0] not in "[{":
            continue
        try:
            blob = json.loads(value)
        except (ValueError, TypeError):
            continue
        stack = [blob]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for inner_key, inner_value in node.items():
                    if isinstance(inner_value, (dict, list)):
                        stack.append(inner_value)
                    elif "itemcode" in inner_key.lower():
                        code = digits(inner_value)
                        if len(code) >= 6:
                            found.append(code)
            elif isinstance(node, list):
                stack.extend(node)
    return found


def load_promos(conn, outputs):
    batch = []
    for path in find_csvs(outputs, "PROMO_FULL_FILE"):
        print(f"[build] reading {os.path.basename(path)}")
        for row in read_rows(path):
            chain_id = digits(pick(row, "chainid", "chain_id"))
            if not chain_id:
                continue
            barcodes = _barcodes_from_promo_row(row)
            if not barcodes:
                continue
            record = (
                chain_id,
                str(pick(row, "storeid", "store_id")).lstrip("0") or "0",
                pick(row, "promotionid", "promo_id"),
                None,
                pick(row, "promotiondescription", "description"),
                to_float(pick(row, "discountedprice", "discountedpricepermeasureunit")),
                # Super-Pharm spells these ...DateTime; other chains ...Date.
                pick(row, "promotionstartdate", "promostartdate", "promotionstartdatetime"),
                pick(row, "promotionenddate", "promoenddate", "promotionenddatetime"),
            )
            for barcode in barcodes:
                batch.append(record[:3] + (barcode,) + record[4:])
            if len(batch) >= BATCH:
                conn.executemany("INSERT INTO promos VALUES (?,?,?,?,?,?,?,?)", batch)
                batch.clear()
    if batch:
        conn.executemany("INSERT INTO promos VALUES (?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM promos").fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description="Build a per-chain SQLite file from parsed CSVs.")
    parser.add_argument("--chain", required=True, help="ScraperFactory name, used for the filename")
    parser.add_argument("--outputs", default="outputs")
    parser.add_argument("--out-dir", default="chain_dbs")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    target = os.path.join(args.out_dir, f"{args.chain.lower()}.db")
    conn = connect(target)

    store_count = load_stores(conn, args.outputs)
    price_rows, chain_ids = load_prices(conn, args.outputs)
    if not price_rows:
        print(f"[build] {args.chain}: no usable price rows.", file=sys.stderr)
        return 1
    collapse(conn)
    promo_rows = load_promos(conn, args.outputs)

    for chain_id in chain_ids:
        conn.execute("INSERT OR IGNORE INTO chains VALUES (?,?)", (chain_id, args.chain))
    conn.commit()

    stats = {
        name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in ("stores", "products", "chain_prices", "price_exceptions", "promos")
    }
    conn.execute("VACUUM")
    conn.close()

    print(f"[build] {args.chain}: {price_rows:,} raw price rows -> "
          f"{stats['chain_prices']:,} baselines + {stats['price_exceptions']:,} exceptions")
    print(f"[build] stores={store_count:,} products={stats['products']:,} promos={promo_rows:,}")
    print(f"[build] wrote {target} ({os.path.getsize(target) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
