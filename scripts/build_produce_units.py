"""One loose fruit or vegetable, and the barcode each chain sells it under.

Every chain gives a kilo of tomatoes its own internal code - and one of those
codes means frozen chicken breast at a different chain - so a barcode names a
row in a chain's price file, not a product. Anyone asking "what do tomatoes
cost" needs the codes collapsed into one thing.

Scope is deliberately narrow: fruit and vegetables sold by weight. That is
where the evidence is - short names, a real per-kilo price, a small closed
vocabulary - and it is most of what goes on a list.

The judgement lives in produce_spec.py: which products exist, and which names
are that product rather than a jar of it. Here we only pick, and the rule is
boring on purpose - among the rows a spec entry admits, prefer the name that
is exactly the product once the selling words are stripped, then the fewest
extra words, then the lowest price. Anything left ambiguous is reported, not
guessed at.

    python3 scripts/build_produce_units.py --db path/to/prices.db

The merged database is not in the repo; build it from the per-chain artifacts
of a nightly run (see the skill: .claude/skills/unify-produce/SKILL.md).
"""

import argparse
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from produce_spec import FILLER, PRICE_BAND, PRODUCE  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
OUT = os.path.join(DATA, "produce_units.tsv")
CHAINS = os.path.join(DATA, "produce_chains.tsv")

WORD = re.compile(r"[\w\"'״׳%]+", re.UNICODE)
# Shufersal writes `עגבנייה` where everyone else writes `עגבניה`; the doubled
# yod/vav is spelling, not a different tomato.
KTIV = ((r"יי", "י"), (r"וו", "ו"))
# Hebrew writes five letters differently at the end of a word, so `טחון` and
# `טחונה` share no substring and a reject list silently misses half of what it
# names. Fold both sides before matching.
FINALS = str.maketrans("ךםןףץ", "כמנפצ")

# A kilo of one thing does not cost four times a kilo of the same thing at the
# next chain. Anything further out is a different product wearing the same
# name - the 60-shekel "tomato" that was really frozen chicken breast.
OUTLIER_FACTOR = 4


def normalise(name):
    """Drop the words that say how a thing is sold, and settle the spelling."""
    text = name.strip()
    for old, new in KTIV:
        text = re.sub(old, new, text)
    words = [w for w in WORD.findall(text) if w not in FILLER]
    return " ".join(words)


def plural_fold(text):
    """`עגבניות` reads as `עגבניה` so an exact match survives the plural."""
    for long, short in (("יות", "ה"), ("ות", "ה"), ("ים", ""), ("ות", "")):
        if text.endswith(long):
            return text[: -len(long)] + short
    return text


def load(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    chains = dict(conn.execute("SELECT chain_id, name FROM chains"))
    price = {(c, b): p for c, b, p in
             conn.execute("SELECT chain_id, barcode, price FROM chain_prices")}
    rows = defaultdict(list)
    for chain_id, barcode, name in conn.execute(
            "SELECT chain_id, barcode, name FROM chain_products "
            "WHERE is_weighted = 1 AND name <> ''"):
        rows[chain_id].append((barcode, name.strip()))
    return chains, price, rows


def candidates(entry, chain_id, rows, price):
    slug, hebrew, _, match, reject = entry
    low, high = PRICE_BAND.get(slug, (0, 10 ** 6))
    keep = re.compile(match.translate(FINALS))
    drop = re.compile(reject.translate(FINALS)) if reject else None
    out = []
    for barcode, name in rows[chain_id]:
        folded = name.translate(FINALS)
        if not keep.search(folded) or (drop and drop.search(folded)):
            continue
        cost = price.get((chain_id, barcode))
        if cost is None or not low <= cost <= high:
            continue
        clean = normalise(name)
        out.append({
            "barcode": barcode, "name": name, "clean": clean, "price": cost,
            "exact": plural_fold(clean) == plural_fold(normalise(hebrew)),
            "extra": len(clean.split()) - len(normalise(hebrew).split()),
        })
    return out


def choose(found):
    """Plainest name wins, then fewest extra words, then cheapest."""
    return sorted(found, key=lambda r: (not r["exact"], max(r["extra"], 0),
                                        r["price"]))[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--report", action="store_true",
                        help="print what was picked and what was dropped")
    args = parser.parse_args()

    chains, price, rows = load(args.db)
    picked, notes = [], []
    for entry in PRODUCE:
        slug, hebrew, kind = entry[0], entry[1], entry[2]
        per_chain = {}
        for chain_id in rows:
            found = candidates(entry, chain_id, rows, price)
            if found:
                per_chain[chain_id] = choose(found)
        if not per_chain:
            notes.append(f"{slug}: no chain sells it by weight")
            continue
        # Price sanity, across chains rather than within one: a chain whose
        # "tomato" costs four times the median is not selling tomatoes.
        middle = statistics.median(r["price"] for r in per_chain.values())
        for chain_id, row in sorted(per_chain.items()):
            if row["price"] > middle * OUTLIER_FACTOR:
                notes.append(f"{slug}: dropped {chains.get(chain_id, chain_id)} "
                             f"- {row['name']} at {row['price']} vs median {middle}")
                continue
            picked.append((slug, hebrew, kind, chain_id,
                           chains.get(chain_id, chain_id), row["barcode"],
                           row["name"], f"{row['price']:g}"))

    picked.sort(key=lambda r: (r[0], r[3]))
    with open(OUT, "w", encoding="utf-8") as handle:
        handle.write("slug\tname_he\tkind\tchain_id\tchain_name\tbarcode\t"
                     "chain_product_name\tprice\n")
        for row in picked:
            handle.write("\t".join(row) + "\n")

    # Which chains this run actually saw. Committed so a test can notice a
    # chain that joined the build and never got unified.
    per_chain = Counter((r[3], r[4]) for r in picked)
    with open(CHAINS, "w", encoding="utf-8") as handle:
        handle.write("chain_id\tchain_name\tproducts\n")
        for (chain_id, name), count in sorted(per_chain.items(),
                                              key=lambda x: -x[1]):
            handle.write(f"{chain_id}\t{name}\t{count}\n")

    per_item = Counter(r[0] for r in picked)
    print(f"[produce] {len(per_item)} products across {len(rows)} chains, "
          f"{len(picked):,} barcodes -> {os.path.relpath(OUT)}")
    thin = [f"{s} ({n})" for s, n in per_item.items() if n < 5]
    if thin:
        print(f"[thin]    {len(thin)} products found in fewer than 5 chains: "
              + ", ".join(sorted(thin)))
    missing = [e[0] for e in PRODUCE if e[0] not in per_item]
    if missing:
        print(f"[missing] {', '.join(missing)}")
    for note in notes:
        print(f"[note]    {note}")

    if args.report:
        print()
        for slug, _ in per_item.most_common():
            these = [r for r in picked if r[0] == slug]
            print(f"### {slug}  {these[0][1]}  ({len(these)} chains)")
            for r in these:
                print(f"    {r[4][:20]:22} {r[5]:>15}  {r[7]:>6}  {r[6]}")


if __name__ == "__main__":
    main()
