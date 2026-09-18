"""Propose rows for data/product_names.json (KAN-29), for a human to review.

Run ON DEMAND ONLY - never from the nightly pipeline - and it commits nothing.

Where the names come from
-------------------------
The per-chain databases the build produces (``chain_dbs/db-<CHAIN>/*.db``),
one per chain, each with its own ``products(barcode, name, manufacturer)``.
That is the only place every chain's own wording for a barcode survives.

It is NOT ``prices.db``'s ``chain_products``: that table is built by
``scripts/merge_db.py`` with ``WHERE p.is_weighted = 1``, so it holds only
weighed goods - loose produce and meat by the kilo. It covered 23,343 of
243,428 barcodes (9.6%) on a September build and by construction contains no
packaged product at all, which is most of the catalogue.

Capping chains
--------------
Some chains truncate every name at 20 characters. That is derived here from
the data rather than hard-coded: a chain whose longest name across its whole
products table is exactly 20 is capping. Their names are still collected -
for a barcode nobody else stocks, a truncated name beats nothing - but the
proposal is marked ``capping-fallback`` so a reviewer knows to distrust it.

The proposal
------------
The most common name among the chains that publish full names. A tie is
broken by taking the longest of the tied names, on the grounds that it
carries the most information, and marked ``tie-longest`` so the reviewer can
see that frequency did not actually decide. Choosing *between* genuinely
different names - a manufacturer's wording against a supermarket's paraphrase
- is left to the reviewer, which is what the review file is for.

Usage:
    python3 scripts/propose_names.py --chain-dbs chain_dbs --out review.tsv
    python3 scripts/propose_names.py --chain-dbs chain_dbs --out review.tsv \
        --barcodes 7290011723200 8714789733296
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import sqlite3

CAP_LENGTH = 20


def normalize_name(name):
    """Undo a retailer feed's own CSV-escaping artifact, not edit real text.

    Some chains export their catalogue through something that CSV-quoted a
    name and never got un-quoted again before it landed in the per-chain
    db: the whole field wrapped in a pair of double quotes, with every
    literal `"` inside doubled. That wrapping is stripped, and a doubled
    ``""`` collapses to a single ``"`` - but a genuine gershayim (``מ"ל``,
    ``ק"ג``, ``בד"צ``, ...), a lone `"` with real text on both sides and
    never doubled, is left exactly as it is. This runs before any length
    comparison or frequency count, so the corrupted, artificially-long
    candidate can no longer out-vote or out-length a clean one for the same
    product.
    """
    name = name.strip()
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        name = name[1:-1]
    while '""' in name:
        name = name.replace('""', '"')
    return name.strip()


def chain_dbs(root):
    """Yield (chain_id, chain_label, db_path, capped) for every chain db."""
    for path in sorted(glob.glob(os.path.join(root, "db-*", "*.db"))):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT chain_id, name FROM chains LIMIT 1").fetchone()
            if row is None:
                continue
            longest = conn.execute(
                "SELECT MAX(LENGTH(name)) FROM products WHERE name <> ''"
            ).fetchone()[0]
        finally:
            conn.close()
        label = row[1] or os.path.basename(os.path.dirname(path))
        yield row[0], label, path, longest == CAP_LENGTH


def gather(root, barcodes=None):
    """barcode -> [(chain_label, name, manufacturer, capped)], across all chains."""
    found = collections.defaultdict(list)
    for _chain_id, label, path, capped in chain_dbs(root):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if barcodes:
                rows = []
                # Chunked so a long favourites list can't blow SQLite's
                # variable limit.
                todo = list(barcodes)
                for i in range(0, len(todo), 500):
                    chunk = todo[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    rows += conn.execute(
                        "SELECT barcode, name, manufacturer FROM products "
                        f"WHERE name <> '' AND barcode IN ({marks})", chunk
                    ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT barcode, name, manufacturer FROM products "
                    "WHERE name <> ''"
                ).fetchall()
        finally:
            conn.close()
        for barcode, name, manufacturer in rows:
            found[barcode].append(
                (label, normalize_name(name), manufacturer or "", capped))
    return found


def propose_one(candidates):
    """(proposed_name, source, brand) from one barcode's candidate list."""
    full = [c for c in candidates if not c[3]]
    pool, source = (full, "trusted") if full else (candidates, "capping-fallback")
    counts = collections.Counter(name for _, name, _, _ in pool)
    best = counts.most_common()
    if len(best) > 1 and best[0][1] == best[1][1]:
        tied = [name for name, n in best if n == best[0][1]]
        name = max(tied, key=len)
        source += "/tie-longest"
    else:
        name = best[0][0]
    brands = collections.Counter(
        m for _, _, m, _ in pool if m and m.strip())
    brand = brands.most_common(1)[0][0] if brands else ""
    return name, source, brand


def propose(found, existing):
    for barcode in sorted(found):
        if barcode in existing:
            continue
        candidates = found[barcode]
        if not candidates:
            continue
        name, source, brand = propose_one(candidates)
        yield barcode, name, source, brand, candidates


def write_review(path, proposals):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["barcode", "proposed_name", "source", "brand", "chains", "candidates"])
        for barcode, name, source, brand, rows in proposals:
            candidates = " | ".join(
                f"{label}: {cname}" for label, cname, _, _ in rows)
            writer.writerow([barcode, name, source, brand, len(rows), candidates])


def load_existing(path):
    """The set of barcodes already decided, from data/product_names.json.

    A missing file returns an empty set - correct for a first run, before
    any name has ever been decided. A file that exists but is not the
    expected JSON object with a ``products`` array is a malformed file and
    raises, rather than silently returning an empty set and re-proposing
    everything that was already decided.
    """
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ValueError(
            f"{path}: expected a JSON object with a 'products' array")
    return {
        str(entry.get("barcode") or "").strip()
        for entry in data["products"]
        if isinstance(entry, dict) and str(entry.get("barcode") or "").strip()
    }


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(
        description="Propose data/product_names.json rows for review (KAN-29). "
                    "Writes a review TSV; never commits.")
    parser.add_argument("--chain-dbs", required=True,
                        help="directory of per-chain databases (chain_dbs/)")
    parser.add_argument("--out", required=True, help="review TSV to write")
    parser.add_argument("--barcodes", nargs="*",
                        help="only these barcodes (default: every barcode)")
    parser.add_argument("--names-json",
                        default=os.path.join(root, "data", "product_names.json"),
                        help="existing product_names.json, to skip decided rows")
    args = parser.parse_args()

    found = gather(args.chain_dbs, args.barcodes)
    existing = load_existing(args.names_json)
    proposals = list(propose(found, existing))
    write_review(args.out, proposals)

    by_source = collections.Counter(p[2].split("/")[0] for p in proposals)
    ties = sum(1 for p in proposals if "tie-longest" in p[2])
    print(f"[propose_names] {len(proposals)} proposals -> {args.out} "
          f"({by_source['trusted']} from full-name chains, "
          f"{by_source['capping-fallback']} capping-fallback, "
          f"{ties} decided by tie-break rather than frequency)")


if __name__ == "__main__":
    main()
