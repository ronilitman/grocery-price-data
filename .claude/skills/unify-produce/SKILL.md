---
name: unify-produce
description: Rebuild data/produce_units.tsv - the barcode each chain uses for each loose fruit and vegetable. Use when a chain is added to or removed from the build matrix, when tests/produce fails, or when the produce price comparison looks wrong or thin for a chain.
---

# Re-unify loose produce across the chains

Every chain gives a kilo of tomatoes its own internal code, and one chain's code
for tomatoes is another's for frozen chicken breast. `data/produce_units.tsv`
collapses those into one row per product per chain, so the app can answer "what
do tomatoes cost" at all.

Nothing rebuilds this on a nightly run: it is built by hand from a merged
database that is not in the repo. That is why `tests/produce` exists - it fails
when the build's chain matrix has moved on and this file has not.

## When to run it

- A chain was added to (or removed from) the `matrix.chain` list in
  `.github/workflows/build.yml`. The new chain's produce is missing from every
  comparison until this runs.
- `tests/produce/test_produce_units.py` fails.
- A product is thin - priced by only a handful of chains when it should be in
  most of them.

## 1. Get a merged database

The merged `prices.db` is not committed. Build it from the per-chain artifacts
that every successful nightly run uploads; they keep for seven days.

```bash
gh run list --workflow build.yml --limit 10          # find a green run
gh run download <run-id> -D /tmp/chain_dbs           # ~200 MB, 30 artifacts
mkdir -p /tmp/flat && find /tmp/chain_dbs -name '*.db' -exec cp {} /tmp/flat/ \;
python3 scripts/merge_db.py --in-dir /tmp/flat --out /tmp/prices.db
```

Expect roughly 237,000 products and 31 chains. If a chain is missing from the
artifact list its job failed that night - pick an earlier green run rather than
unifying without it, or the tests will flag it as thin.

## 2. Rebuild

```bash
python3 scripts/build_produce_units.py --db /tmp/prices.db
```

It writes `data/produce_units.tsv` and `data/produce_chains.tsv` and prints:

- `[thin]` - products found in fewer than five chains.
- `[missing]` - products no chain sells by weight at all. `chard` and `rocket`
  are genuinely in this list; they are sold in packets, not loose.
- `[note]` - rows dropped for costing more than four times the median across
  chains. These are usually a cheese or a jar wearing the product's name, and
  each one is worth reading.

## 3. Review the picks - this is the part that needs judgement

```bash
python3 scripts/build_produce_units.py --db /tmp/prices.db --report
```

Read the chosen name for each chain. What goes wrong, in order of how often:

- **A variety instead of the plain thing.** Several chains sell only
  `עגבניה מגי`, a premium tomato. That is fine - it is their tomato. But if a
  chain has both, the plain one must win.
- **A different product sharing the word.** `שריר בננה` is a cut of beef,
  `בוט בננה` is a peanut, `גבינת שמנת שום שמיר` is cream cheese. Add the word
  to that entry's `reject` in `scripts/produce_spec.py`.
- **Hebrew final letters.** `טחון` and `טחונה` share no substring, so a reject
  written one way silently misses the other. The builder folds ךםןףץ before
  matching, so write either - but if a reject looks ignored, check this first.
- **A price that is not that product's price.** Every entry has a band in
  `PRICE_BAND`. Herbs run to ₪150/kg because they are sold as 20-gram bunches;
  a vegetable at ₪80 is not a vegetable.

Edit `scripts/produce_spec.py` and re-run until the report reads right. That
file is the only place judgement lives - the builder does no guessing.

## 4. Prove it and commit

```bash
python3 -m pytest tests/produce -q
```

If a chain was added or removed, that suite fails on purpose until you refresh
the fixture, in the same commit as the data:

```bash
python3 - <<'EOF'
import re, io, json
t = io.open('.github/workflows/build.yml', encoding='utf-8').read()
names = sorted({n for n in re.findall(r'^\s+-\s+([A-Z][A-Z0-9_]{2,})\s*$', t, re.M)})
io.open('tests/produce/fixtures/build_matrix_chains.json', 'w',
        encoding='utf-8').write(json.dumps(names, indent=1) + "\n")
print(len(names), 'chains')
EOF
```

Commit `data/produce_units.tsv`, `data/produce_chains.tsv`, any
`scripts/produce_spec.py` edits, and the fixture together. Say in the message
which chain moved and what the coverage did.

## Adding a product rather than a chain

Append an entry to `PRODUCE` in `scripts/produce_spec.py` - slug, Hebrew name,
kind, a `match` regex for the head word, a `reject` regex for everything that
shares it - and a band in `PRICE_BAND`. Then run steps 2 to 4. If it belongs on
a normal shopping list, add its slug to `CORE` in the test as well.
