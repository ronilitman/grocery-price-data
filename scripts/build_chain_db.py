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
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promos  # noqa: E402
from csvutil import find_csvs, read_rows, pick, digits, to_float  # noqa: E402

BATCH = 20000

# Chain ids whose store file never names them. Without this the fallback is the
# scraper's enum name, and the app - which prints whatever it is given - shows
# "HAZI_HINAM" in a list of Hebrew chain names. Keyed by chain id rather than
# by scraper: City Market publishes under two ids and only one of them is the
# branded chain.
DISPLAY_NAMES = {
    "7290700100008": "חצי חינם",
}

# Chain ids a chain publishes by mistake, and the id the rows belong to.
# City Market zeroes the ChainID in some of its price files - 51,372 products
# worth - while the StoreIDs in them stay its own: all four store ids in those
# files resolve to named branches under 7290000000003. Left alone they surface
# as a second, branchless chain; re-attributed they are simply that chain's
# prices. Keyed by scraper as well as id, because an all-zeros ChainID is a
# generic mistake and another chain's would not belong here.
CHAIN_ID_ALIASES = {
    ("CITY_MARKET_SHOPS", "0000000000000"): "7290000000003",
}

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
-- One row per distinct offer, not per (promotion x item x branch). Every
-- branch republishes the whole chain's promotions, so the published form is
-- ~20x larger than the information in it: Keshet ships 206,519 item rows
-- carrying 10,486 offers. Which branches honour an offer lives in
-- promo_stores, because that genuinely varies - Keshet prices this box at
-- 11.90 in eighteen branches and 9.90 in three.
CREATE TABLE IF NOT EXISTS promo_offers(
    offer_id    INTEGER PRIMARY KEY,
    chain_id    TEXT NOT NULL,
    promo_id    TEXT NOT NULL,
    barcode     TEXT NOT NULL,
    -- 0 = anyone, 1 = needs a loyalty card. Half of Yellow's promotions are
    -- club-only; showing those as the shelf price advertises a number most
    -- shoppers cannot get.
    club        INTEGER NOT NULL,
    -- The pack size the price is for. Without it "34.00" reads as the price of
    -- one Toffifee when it is the price of two.
    min_qty     REAL NOT NULL,
    price       REAL NOT NULL,
    unit_price  REAL NOT NULL,
    description TEXT,
    starts      TEXT,
    ends        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_key
    ON promo_offers(chain_id, promo_id, barcode, club, min_qty, price);
CREATE TABLE IF NOT EXISTS promo_stores(
    offer_id INTEGER NOT NULL,
    store_id TEXT NOT NULL,
    PRIMARY KEY (offer_id, store_id)
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


def load_promos(conn, dumps):
    """Collapse every branch's PromoFull into distinct offers plus a branch list.

    Read from the XML dumps, not from ``outputs/``: see scripts/promos.py for
    why the parser package's CSV cannot represent a promotion and why building
    it cost 1,970 MB for a chain whose prices take 76 MB.
    """
    files = promos.find_promo_files(dumps)
    if not files:
        print("[build] no PromoFull dumps found")
        return 0, 0
    print(f"[build] reading {len(files)} PromoFull dumps")

    branches = defaultdict(set)             # offer -> {store_id}
    seen_rows = 0
    for store_id, offer in promos.read_offers(dumps):
        branches[offer].add(store_id)
        seen_rows += 1

    conn.executemany(
        "INSERT OR IGNORE INTO promo_offers"
        "(chain_id, promo_id, barcode, club, min_qty, price, unit_price,"
        " description, starts, ends) VALUES (?,?,?,?,?,?,?,?,?,?)",
        list(branches))
    conn.commit()

    # The first six fields are the unique key the index above is built on;
    # unit_price and the labels hang off them.
    keyed = {row[1:]: row[0] for row in conn.execute(
        "SELECT offer_id, chain_id, promo_id, barcode, club, min_qty, price "
        "FROM promo_offers")}
    links = [(keyed[offer[:6]], store_id)
             for offer, stores in branches.items() for store_id in stores]
    conn.executemany("INSERT OR IGNORE INTO promo_stores VALUES (?,?)", links)
    conn.commit()

    print(f"[build] promotions: {seen_rows:,} published item rows -> "
          f"{len(branches):,} distinct offers "
          f"({seen_rows / max(len(branches), 1):.1f}x)")
    return len(branches), len(links)


def apply_chain_aliases(conn, scraper, chain_ids, tables=("_raw",)):
    """Re-attribute rows a chain published under a mistaken chain id.

    Runs against ``_raw`` before collapse() so the baseline is the modal price
    across every branch, including the ones that arrived mislabelled - fixing
    it afterwards would leave two half-populated chains to reconcile.
    """
    updated = set(chain_ids)
    for (alias_scraper, wrong_id), right_id in CHAIN_ID_ALIASES.items():
        if alias_scraper != scraper:
            continue
        for table in tables:
            conn.execute(f"UPDATE {table} SET chain_id = ? WHERE chain_id = ?",
                         (right_id, wrong_id))
        if conn.total_changes:
            print(f"[build] re-attributed {wrong_id} -> {right_id}")
        conn.execute("DELETE FROM chains WHERE chain_id = ?", (wrong_id,))
        conn.execute("DELETE FROM stores WHERE chain_id = ?", (wrong_id,))
        updated.discard(wrong_id)
        updated.add(right_id)
    conn.commit()
    return updated


def drop_priceless_chains(conn):
    """Remove chain ids that have branches but no prices behind them.

    One company can publish the same shops under several chain ids. Meshmat
    Yosef ships three - 7290058289400 with 9,411 products, and 5144744100001
    and 2222222 with the same four branches and no prices at all. All three
    reach the app as separate chains, so the picker offers the same shop three
    times and a cart can be compared against itself.

    The test is prices, not the shape of the id: a chain with prices and no
    store file (Fresh Market, when it published an empty one) is kept, because
    losing its prices is the worse failure.
    """
    priceless = [row[0] for row in conn.execute(
        "SELECT chain_id FROM chains WHERE chain_id NOT IN "
        "(SELECT DISTINCT chain_id FROM chain_prices)")]
    if not priceless:
        return 0

    marks = ",".join("?" * len(priceless))
    conn.execute(
        f"DELETE FROM promo_stores WHERE offer_id IN "
        f"(SELECT offer_id FROM promo_offers WHERE chain_id IN ({marks}))", priceless)
    for table in ("stores", "price_exceptions", "promo_offers", "chains"):
        conn.execute(f"DELETE FROM {table} WHERE chain_id IN ({marks})", priceless)
    conn.commit()
    for chain_id in priceless:
        print(f"[build] dropped {chain_id}: branches but no prices")
    return len(priceless)


def main():
    parser = argparse.ArgumentParser(description="Build a per-chain SQLite file from parsed CSVs.")
    parser.add_argument("--chain", required=True, help="ScraperFactory name, used for the filename")
    parser.add_argument("--outputs", default="outputs")
    parser.add_argument("--dumps", default="dumps",
                        help="Where the PromoFull XML dumps are; promotions are read from\n         the XML, never from the parser package's CSV.")
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
    chain_ids = apply_chain_aliases(conn, args.chain, chain_ids)
    collapse(conn)
    offer_count, link_count = load_promos(conn, args.dumps)
    apply_chain_aliases(conn, args.chain, chain_ids, tables=("promo_offers",))

    # OR IGNORE: only chains the store file did not name reach this.
    for chain_id in chain_ids:
        conn.execute("INSERT OR IGNORE INTO chains VALUES (?,?)",
                     (chain_id, DISPLAY_NAMES.get(chain_id, args.chain)))
    conn.commit()

    # Last, so it sees the complete chain list.
    drop_priceless_chains(conn)

    stats = {
        name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in ("stores", "products", "chain_prices", "price_exceptions",
                     "promo_offers", "promo_stores")
    }
    conn.execute("VACUUM")
    conn.close()

    print(f"[build] {args.chain}: {price_rows:,} raw price rows -> "
          f"{stats['chain_prices']:,} baselines + {stats['price_exceptions']:,} exceptions")
    print(f"[build] stores={store_count:,} products={stats['products']:,} "
          f"offers={stats['promo_offers']:,} offer-branch links={stats['promo_stores']:,}")
    print(f"[build] wrote {target} ({os.path.getsize(target) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
