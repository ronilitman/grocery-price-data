"""Propose rows for data/product_names.tsv (KAN-29), for a human to review.

Run ON DEMAND ONLY - never from the nightly pipeline - and it commits
nothing. For every barcode not already decided in data/product_names.tsv, it
looks at every name a chain filed for that barcode in prices.db's
chain_products table and writes one proposal, plus every candidate it saw
and which chain gave it, to a review TSV. A human reads that file and copies
the rows they accept into data/product_names.tsv by hand.

Candidate chains split two ways:

TRUSTED_CHAINS publish real, uncapped names. CAPPING_CHAINS are the eleven
whose names never exceed 20 characters - a hard cap, not a coincidence of
short products, confirmed by the per-chain max-length table in this
change's commit message. A barcode's proposal is the most common name among
whichever pool has any candidates for it - the trusted chains if any of them
stock it, the capping chains only when nobody else does (a truncated name is
still a hint, marked "capping-fallback" so a reviewer knows to weight it
less, or check an outside source per KAN-29 part 3, before accepting it).

Picking *between* disagreeing candidates - a manufacturer's own name over a
supermarket's paraphrase, say - is deliberately left to the reviewer: this
script only breaks ties by raw frequency, which is arithmetic, not
judgement.

Usage:
    python3 scripts/propose_names.py --db prices.db --out names_review.tsv
"""

import argparse
import collections
import csv
import os
import pathlib
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES_TSV = os.path.join(ROOT, "data", "product_names.tsv")

# chain_id -> a short label for the review file. Chain ids are the
# barcode-shaped legal identifiers CLAUDE.md describes (see SUBCHAIN_SPLITS
# for why Netiv Hesed's carries a "-006" suffix); they came from a build's
# own `chains` table, except Super Sapir, which is absent from the
# smoke-test build this module's CAPPING_CHAINS table was read off of and
# was instead taken from the scraper library's own
# SuperSapir(Bina, chain_id="7290058156016").
TRUSTED_CHAINS = {
    "7290027600007": "שופרסל",  # Shufersal
    "7290875100001": "סופר ברקת",  # Bareket
    "7290172900007": "סופר פארם",  # Super Pharm
    "7290873255550": "טיב טעם",  # Tiv Taam
    "7290700100008": "חצי חינם",  # Hazi Hinam
    "7290696200003": "ויקטורי",  # Victory
    "7290058159628": "מעיין 2000",  # Maayan 2000
    "7290058134977": "שפע ברכת השם",  # Shefa Barkat
    "7290058108879": "קינג סטור",  # King Store
    "7290661400001": "מחסני השוק",  # Mahsani Ashuk
    "7290058197699": "גוד פארם",  # Good Pharm
    "7290058160839-006": "נתיב החסד",  # Netiv Hesed
    "7290058148776": "שוק העיר",  # Shuk Ahir
    "7290058156016": "סופר ספיר",  # Super Sapir
    "7290455000004": "האחים כהן",  # Het Cohen
    "5144744100002": "משנת יוסף",  # Meshmat Yosef
    "7290058249350": "וולט",  # Wolt
}

# The eleven chains whose chain_products.name never exceeds 20 characters
# (verified 2026-09-17 against a local build: every one of these had
# max(len(name)) == 20 across its rows, where every other chain went well
# past it - see the commit message for the full table). Ignored for
# candidates unless a barcode has no TRUSTED_CHAINS candidate at all.
CAPPING_CHAINS = {
    "7290055700007",  # קרפור / Carrefour
    "7290103152017",  # אושר עד / Osher Ad
    "7290639000004",  # סטופמרקט / Stop Market
    "7290876100000",  # פרש מרקט / Fresh Market
    "7290785400000",  # קשת טעמים / Keshet Teamim
    "7291059100008",  # פוליצר / Politzer
    "7290058140886",  # רמי לוי שיווק השקמה / Rami Levy
    "7290492000005",  # Dor Alon
    "7290058177776",  # סופר יודה / Super Yuda
    "7290526500006",  # Dabach
    "7290644700005",  # YELLOW
}


def ro_uri(path):
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def load_existing_barcodes(tsv_path):
    """Barcodes data/product_names.tsv already has a decided name for."""
    barcodes = set()
    if not os.path.exists(tsv_path):
        return barcodes
    with open(tsv_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            barcode = line.split("\t", 1)[0].strip()
            if barcode:
                barcodes.add(barcode)
    return barcodes


def gather_candidates(conn):
    """barcode -> [(chain_id, chain_label, name), ...], across every chain."""
    chain_names = dict(conn.execute("SELECT chain_id, name FROM chains"))
    by_barcode = collections.defaultdict(list)
    cur = conn.execute(
        "SELECT chain_id, barcode, name FROM chain_products "
        "WHERE name IS NOT NULL AND TRIM(name) != ''"
    )
    for chain_id, barcode, name in cur:
        label = TRUSTED_CHAINS.get(chain_id) or chain_names.get(chain_id, chain_id)
        by_barcode[barcode].append((chain_id, label, name.strip()))
    return by_barcode


def propose(by_barcode, existing):
    """Yield (barcode, proposed_name, source, candidates) for every barcode
    not already in `existing`, where `candidates` is the full
    (chain_id, chain_label, name) list `gather_candidates` saw for it.
    """
    for barcode in sorted(by_barcode):
        if barcode in existing:
            continue
        rows = by_barcode[barcode]
        trusted = [row for row in rows if row[0] in TRUSTED_CHAINS]
        if trusted:
            pool, source = trusted, "trusted"
        else:
            pool, source = rows, "capping-fallback"
        if not pool:
            continue
        counts = collections.Counter(name for _, _, name in pool)
        top_name, _ = counts.most_common(1)[0]
        yield barcode, top_name, source, rows


def write_review(path, proposals):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["barcode", "proposed_name", "source", "candidates"])
        for barcode, name, source, rows in proposals:
            candidates = " | ".join(f"{label}: {cname}" for _, label, cname in rows)
            writer.writerow([barcode, name, source, candidates])


def main():
    parser = argparse.ArgumentParser(
        description="Propose data/product_names.tsv rows for a human to review "
                    "(KAN-29). Writes a review file; never touches "
                    "product_names.tsv and never commits.")
    parser.add_argument("--db", required=True,
                        help="prices.db (or app.db) to read chain_products from, read-only")
    parser.add_argument("--out", required=True, help="review TSV to write")
    parser.add_argument("--names-tsv", default=NAMES_TSV,
                        help="existing product_names.tsv, to skip barcodes already decided "
                             "(default: the checked-in data/product_names.tsv)")
    args = parser.parse_args()

    existing = load_existing_barcodes(args.names_tsv)
    conn = sqlite3.connect(ro_uri(args.db), uri=True)
    by_barcode = gather_candidates(conn)
    conn.close()

    proposals = list(propose(by_barcode, existing))
    write_review(args.out, proposals)

    trusted_n = sum(1 for _, _, source, _ in proposals if source == "trusted")
    fallback_n = len(proposals) - trusted_n
    print(f"[propose_names] {len(proposals)} proposals written to {args.out} "
          f"({trusted_n} from trusted chains, {fallback_n} capping-fallback) - "
          f"{len(existing)} barcodes already decided were skipped")


if __name__ == "__main__":
    main()
