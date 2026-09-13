"""Check data/produce_generic_map.tsv for price-tier violations against a
real, built app.db.

The owner's rule (.claude/skills/unify-produce/SKILL.md): a price tier is a
product. If two candidates differ by more than roughly 2x, they are
different things - organic, a pricier cultivar, or "sold by weight" instead
of a flat unit are all separate products, not the same one at a discount.
KAN-9's review found 33 mapped keys that violate this against their slug's
primary; each was re-read against produce_units.tsv and either removed or
kept with a `tier-ok:` note explaining, by chain and barcode, why the
curator really did decide it is the same product (see the KAN-9 report).

This script re-checks that every remaining outlier still carries that note,
so a future edit to the map can't reintroduce a silent price-tier mismatch.
It needs a *built* app.db (generic_members + chain_prices), not the merged
prices.db - the map was validated against member prices, not producer text.

    python3 scripts/check_produce_tier.py --db /path/to/app.db
"""

import argparse
import collections
import os
import sqlite3

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
MAP_TSV = os.path.join(DATA, "produce_generic_map.tsv")

# "Roughly two times" per the skill - the persistent guard is deliberately a
# little stricter than the wider net (1.8x/0.55x) used to go find candidates
# during the KAN-9 review, so it doesn't nag about borderline cases that
# were already looked at and judged fine.
HIGH = 2.0
LOW = 0.5


def read_tsv(path):
    with open(path, encoding="utf-8") as handle:
        head = handle.readline().rstrip("\n").split("\t")
        return [dict(zip(head, line.rstrip("\n").split("\t")))
                for line in handle if line.strip()]


def median(values):
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def key_median(conn, key):
    """Median price across a generic key's members, joined to chain_prices.

    None when the key has no priced members in this database - a caller
    should treat that as "can't judge", not as a violation.
    """
    rows = conn.execute(
        "SELECT cp.price FROM generic_members gm "
        "JOIN chain_prices cp ON cp.chain_id = gm.chain_id "
        "AND cp.barcode = gm.barcode WHERE gm.key = ?", (key,)).fetchall()
    prices = [r[0] for r in rows]
    return median(prices) if prices else None


def find_tier_violations(conn, gmap):
    """(slug, generic_key, ratio) for every mapped, non-primary key whose
    median price is >=HIGH or <=LOW times its slug's primary median, and
    whose note does not carry a `tier-ok:` marker explaining why.
    """
    by_slug = collections.defaultdict(list)
    for row in gmap:
        by_slug[row["slug"]].append(row)

    violations = []
    for slug, rows in by_slug.items():
        primaries = [r for r in rows if r["primary"] == "yes"]
        if not primaries:
            continue
        primary_med = key_median(conn, primaries[0]["generic_key"])
        if not primary_med:
            continue
        for row in rows:
            if row["primary"] == "yes":
                continue
            med = key_median(conn, row["generic_key"])
            if med is None:
                continue
            ratio = med / primary_med
            if (ratio >= HIGH or ratio <= LOW) and "tier-ok:" not in row["note"]:
                violations.append((slug, row["generic_key"], round(ratio, 2)))
    return violations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="a built app.db")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    gmap = read_tsv(MAP_TSV)
    violations = find_tier_violations(conn, gmap)

    print(f"[produce-tier] {len(gmap)} map rows checked")
    print(f"[produce-tier] {len(violations)} tier violations without a tier-ok note")
    for slug, key, ratio in violations:
        print(f"    ! {slug}: {key!r} is {ratio}x its primary's median price")
    raise SystemExit(1 if violations else 0)


if __name__ == "__main__":
    main()
