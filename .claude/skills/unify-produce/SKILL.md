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

## 2. There is no builder. Read the rows and decide.

This was first done with match patterns, price bands and a scoring rule. It
filed fennel as garlic, beef as cherry tomato and a steak as lychee, and it
merged plain `עגבניה` with `עגבניה מגי` - two different tomatoes at ₪7.9 and
₪16.9 - which made seven chains look two to four times dearer than they are.
Patterns cannot tell those apart. Reading can. Do not rebuild the matcher.

Work one product at a time. Dump every weighted row for a head word, with its
price, for every chain:

```python
import sqlite3, collections
C = sqlite3.connect('file:/tmp/prices.db?mode=ro', uri=True)
CH = dict(C.execute('select chain_id, name from chains'))
P = {(a, b): p for a, b, p in C.execute('select chain_id,barcode,price from chain_prices')}
rows = collections.defaultdict(list)
for a, b, n in C.execute("select chain_id,barcode,name from chain_products "
                         "where is_weighted=1 and name<>''"):
    rows[a].append((b, n.strip()))
term = 'עגבני'
for cid in sorted(rows, key=lambda c: CH.get(c, '')):
    hits = [(b, n, P.get((cid, b))) for b, n in rows[cid] if term in n and P.get((cid, b))]
    if hits:
        print(CH[cid], ' | '.join(f'{n}=₪{p:g}[{b}]' for b, n, p in sorted(hits, key=lambda x: x[2])))
```

Then read the list and write one line per chain into `data/produce_units.tsv`.

**Derive the type structure before deciding anything.** Count what qualifier
follows the head word, and how many chains use each. Tomato comes out as five
products, not one - מגי in 18 chains at ₪16.9, שרי in 15 at ₪19.4, plain in 12
at ₪8.9, תמר in 7, אשכולות in 3. Each gets its own line, and a chain appears in
whichever lines it stocks.

What separates a product from a variant, in practice:

- **A price tier is a product.** If two candidates differ by more than roughly
  two times, they are different things. Kosher certification is a tier of its
  own: `כרוב לבן` is ₪1.9-4.9, `כרוב לבן מהדרין/חסלט/גלאט` is ₪9.9-17.9.
- **A colour is a product** for peppers, onions and cabbage - shoppers ask for
  the colour and the prices differ.
- **Packaging is not.** `בחוץ`, `תפזורת`, `ארוז`, `ברשת`, `שקיל`, `במשקל`,
  `בקרטון` all describe how it is sold.
- **Watch for words that merely contain the head word.** `שומר` is fennel and
  `שומן` is fat, neither is `שום`. `שריר` is a cut of beef, not `שרי`.
  `סטוליצני` is a steak, not `ליצ'י`.

Where a chain sells nothing that is the product, it gets no row. That is a real
answer - 11 chains sell no plain tomato at all, only מגי or שרי.

## 3. Check yourself

Re-read every row against the database before committing: the barcode exists at
that chain, is still flagged weighted, and the price is what you wrote.

```bash
python3 scripts/check_produce_units.py --db /tmp/prices.db
```

It catches the mistakes that reading makes - a barcode typed from the wrong
line, the same product recorded twice for one chain, a price copied from the
row above. It caught a duplicated lemon block on the run that produced this
file.

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
the fixture, and any test changes together. Say in the message
which chain moved and what the coverage did.

## Adding a product rather than a chain

Dump its head word across every chain as in step 2, read the rows, and append
one line per chain to `data/produce_units.tsv`. Columns are slug, Hebrew name,
kind (`veg`/`fruit`/`herb`), chain id, chain name, barcode, the chain's own
name for it, price, and a note where the choice was not obvious. If it belongs
on a normal shopping list, add its slug to `CORE` in the test as well.

## Still missing

Herbs (`פטרוזיליה`, `כוסברה`, `שמיר`, `נענע`, `בזיליקום`), and the stone and
soft fruit: `שזיף`, `אפרסק`, `נקטרינה`, `משמש`, `דובדבן`, `תמר`, `תאנה`,
`תות`, `קיווי`, `אננס`, `פפאיה`, `ליצ'י`, `גויאבה`, `פסיפלורה`. Also
`ארטישוק`, `במיה`, `שעועית ירוקה`, `אפונה`, `תרד`, `סלרי`, `כרישה`,
`ג'ינג'ר`, `חזרת`. Same procedure for each.
