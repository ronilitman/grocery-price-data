"""Check data/produce_units.tsv against a merged database.

There is no builder for that file. Every row in it was chosen by reading the
chain's own weighted rows and deciding which one is the product - the first
attempt did it with match patterns, price bands and a scoring rule, and filed
fennel as garlic, beef as cherry tomato and a steak as lychee. Patterns cannot
tell those apart; reading can.

So this only checks. It re-reads every row against the database and complains
if a barcode has stopped existing, stopped being weighted, changed price, or if
one product ends up listed twice for the same chain.

    python3 scripts/check_produce_units.py --db /tmp/prices.db

Prices drift daily, so --tolerance is a fraction, not a promise. A row whose
price has moved a long way is worth looking at: it may now be a different
product under the same code.
"""

import argparse
import collections
import os
import sqlite3

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
UNITS = os.path.join(DATA, "produce_units.tsv")


def read(path):
    with open(path, encoding="utf-8") as handle:
        head = handle.readline().rstrip("\n").split("\t")
        return [dict(zip(head, line.rstrip("\n").split("\t"))) for line in handle]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--tolerance", type=float, default=0.5,
                        help="how far a price may drift before it is reported")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    name = {(c, b): n.strip() for c, b, n in
            conn.execute("SELECT chain_id, barcode, name FROM chain_products")}
    weighted = {(c, b): w for c, b, w in
                conn.execute("SELECT chain_id, barcode, is_weighted FROM chain_products")}
    price = {(c, b): p for c, b, p in
             conn.execute("SELECT chain_id, barcode, price FROM chain_prices")}

    rows = read(UNITS)
    problems, drifted, seen = [], [], set()
    for row in rows:
        key = (row["chain_id"], row["barcode"])
        pair = (row["slug"], row["chain_id"])
        if pair in seen:
            problems.append(f"{row['slug']}: {row['chain_name']} listed twice")
            continue
        seen.add(pair)
        if key not in price:
            problems.append(f"{row['slug']} {row['chain_name']}: "
                            f"{row['barcode']} is no longer priced")
            continue
        if weighted.get(key) != 1:
            problems.append(f"{row['slug']} {row['chain_name']}: "
                            f"{name.get(key, row['barcode'])} is no longer sold by weight")
            continue
        was, now = float(row["price"]), price[key]
        if abs(now - was) > was * args.tolerance:
            drifted.append(f"{row['slug']} {row['chain_name']}: "
                           f"₪{was:g} -> ₪{now:g}  {name.get(key, '')}")

    items = collections.Counter(r["slug"] for r in rows)
    chains = collections.Counter(r["chain_name"] for r in rows)
    print(f"[produce] {len(rows):,} rows, {len(items)} products, {len(chains)} chains")
    print(f"[checked] {len(problems)} problems, {len(drifted)} prices drifted "
          f"more than {args.tolerance:.0%}")
    for line in problems:
        print(f"    ! {line}")
    for line in drifted[:20]:
        print(f"    ~ {line}")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
