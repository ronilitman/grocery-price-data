"""Refuse to publish a database that is obviously wrong.

The previous pipeline failed silently and shipped four hard-coded sample
products under a green checkmark. Every threshold here exists so that a
failure is visible instead.
"""

import argparse
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description="Sanity-gate the built database.")
    parser.add_argument("--db", default="dist/prices.db")
    parser.add_argument("--min-barcodes", type=int, default=50000)
    parser.add_argument("--min-chains", type=int, default=3)
    parser.add_argument("--min-stores", type=int, default=100)
    parser.add_argument("--min-cities", type=int, default=20)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    query = conn.execute

    checks = [
        ("barcodes", query("SELECT COUNT(*) FROM products").fetchone()[0], args.min_barcodes),
        ("chains with prices",
         query("SELECT COUNT(DISTINCT chain_id) FROM chain_prices").fetchone()[0], args.min_chains),
        ("stores", query("SELECT COUNT(*) FROM stores").fetchone()[0], args.min_stores),
        ("cities",
         query("SELECT COUNT(DISTINCT city) FROM stores WHERE city <> ''").fetchone()[0],
         args.min_cities),
    ]

    failures = []
    for label, actual, minimum in checks:
        status = "ok " if actual >= minimum else "FAIL"
        print(f"[verify] {status} {label:<20}{actual:>10,}  (min {minimum:,})")
        if actual < minimum:
            failures.append(label)

    # A real query has to return real rows, not just non-empty tables.
    sample = query("""
        SELECT p.name, c.name, sp.price
        FROM store_prices sp
        JOIN products p ON p.barcode = sp.barcode
        JOIN chains c ON c.chain_id = sp.chain_id
        WHERE sp.city <> ''
        ORDER BY sp.price
        LIMIT 3
    """).fetchall()
    if not sample:
        failures.append("store_prices view returns nothing")
    else:
        print("[verify] sample rows:")
        for name, chain, price in sample:
            print(f"[verify]   {price:>8.2f}  {chain:<16} {name}")

    orphans = query("""
        SELECT COUNT(*) FROM chain_prices
        WHERE barcode NOT IN (SELECT barcode FROM products)
    """).fetchone()[0]
    print(f"[verify] prices with no product row: {orphans:,}")

    conn.close()
    if failures:
        print("[verify] FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("[verify] passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
