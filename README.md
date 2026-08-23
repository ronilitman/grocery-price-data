# grocery-price-data

Builds a single, queryable SQLite database of Israeli supermarket prices every
night and publishes it as a static file. No server, no database host, no bill.

The app queries it directly from the browser over HTTP range requests, so a
lookup pulls a few kilobytes out of the file instead of downloading it.

**This repository must be public.** GitHub Pages does not serve from private
repositories on the Free plan, and Actions minutes are only unlimited on public
ones. Nothing here is secret — it is all published under the price transparency
regulations.

---

## How it works

```
scripts/fetch.py CHAIN        scrape one chain  →  dumps/    →  parse  →  outputs/*.csv
scripts/build_chain_db.py     outputs/*.csv     →  chain_dbs/<chain>.db
scripts/merge_db.py           chain_dbs/*.db    →  dist/prices.db  (+ metadata.json)
scripts/build_catalog.py      dist/prices.db    →  dist/catalog/*.json + index.json
scripts/verify.py             refuses to publish an obviously broken build
```

Scraping and XML parsing are both done by the OpenIsraeliSupermarkets packages
([scrapers](https://github.com/OpenIsraeliSupermarkets/israeli-supermarket-scarpers),
[parsers](https://github.com/OpenIsraeliSupermarkets/israeli-supermarket-parsers)).
Every chain publishes the same legally-mandated data in a different dialect and
the parsers package has a dedicated parser for each one — writing your own XML
reader is how you end up with data from a single chain.

The workflow runs one chain per matrix job. A national scrape does not fit on a
single runner's disk, and `fail-fast: false` means one broken chain does not
sink the other seventeen.

### The collapse

Per-store prices nationally are roughly 80 million rows — a multi-gigabyte file
that no free host will serve. But chains price almost everything chain-wide, so
`build_chain_db.py` stores one baseline price per `(chain, barcode)` plus only
the stores that genuinely differ. Same answers, about fifty times smaller.

The `store_prices` view puts it back together:

```sql
COALESCE(price_exceptions.price, chain_prices.price)
```

---

## Schema

| table | what's in it |
|---|---|
| `chains` | `chain_id`, `name` |
| `stores` | `chain_id`, `store_id`, `subchain_id`, `store_name`, `city`, `address` |
| `products` | `barcode`, `name`, `manufacturer`, `unit_qty`, `quantity`, `unit_of_measure`, `is_weighted` |
| `chain_prices` | `chain_id`, `barcode`, `price`, `store_count` — the baseline |
| `price_exceptions` | `chain_id`, `store_id`, `barcode`, `price` — stores that differ |
| `promos` | `chain_id`, `store_id`, `promo_id`, `barcode`, `description`, `price`, `starts`, `ends` |
| `product_tokens` | `token`, `barcode` — one row per word in a product name |
| `store_prices` | view: effective price per store |
| `meta` | `built_at`, row counts, which chains contributed |

`dist/catalog/` holds the same product data as prefix-sharded JSON, for
scanning a barcode without loading the SQLite engine. Sharding is adaptive —
`729` is Israel's GS1 country prefix, so a flat split would put most of the
catalogue in one file. `catalog/index.json` lists the shard prefixes; take the
longest one your barcode starts with.

Each shard entry is `{n: name, p: {chain_id: [price, branches_at_baseline]}}`,
plus two optional keys. `w: 1` marks a weighed product, whose price is per unit
rather than per item — a cart cannot sum those without a weight from the user —
and `u` names that unit. Both are omitted when they would say nothing, so
absent `w` means "not weighed", never "unknown"; `u` appears only alongside `w`,
because on a packaged product `unit_qty` is a bare "gram" whose count lives in a
column the shards do not carry.

`product_tokens` exists because `LIKE '%milk%'` cannot use an index. Over range
requests an unindexed scan means fetching the entire products table on every
keystroke; a word index turns it back into a seek.

---

## Example queries

Cheapest match for a word, in one city:

```sql
SELECT p.name, c.name AS chain, s.store_name, COALESCE(e.price, cp.price) AS price
FROM product_tokens t
JOIN products p      ON p.barcode  = t.barcode
JOIN chain_prices cp ON cp.barcode = p.barcode
JOIN chains c        ON c.chain_id = cp.chain_id
JOIN stores s        ON s.chain_id = cp.chain_id AND s.city = ?
LEFT JOIN price_exceptions e
       ON e.chain_id = cp.chain_id AND e.store_id = s.store_id AND e.barcode = p.barcode
WHERE t.token = ?
ORDER BY price
LIMIT 50;
```

One barcode across every chain:

```sql
SELECT c.name, cp.price, cp.store_count
FROM chain_prices cp JOIN chains c ON c.chain_id = cp.chain_id
WHERE cp.barcode = ? ORDER BY cp.price;
```

Live promotions at one chain:

```sql
SELECT pr.description, p.name, pr.price
FROM promos pr JOIN products p ON p.barcode = pr.barcode
WHERE pr.chain_id = ? AND pr.ends >= date('now')
ORDER BY pr.price;
```

Start every query from the narrowest table (`product_tokens`, or `chain_prices`
by barcode). Starting from `store_prices` unfiltered makes SQLite scan, which
over HTTP is slow and wasteful. Check with `EXPLAIN QUERY PLAN` — you want
`SEARCH ... USING INDEX` on every line, never `SCAN`.

---

## Running it locally

```bash
pip install -r requirements.txt

# One small chain, 20 files, to prove the plumbing works
python scripts/fetch.py POLIZER --limit 20
python scripts/build_chain_db.py --chain POLIZER
python scripts/merge_db.py
python scripts/build_catalog.py
python scripts/verify.py --min-barcodes 500 --min-chains 1 --min-stores 1 --min-cities 1

# Serve dist/ with range support, the way Pages does
python scripts/serve_local.py dist 8099
```

Then check it the way a browser will:

```bash
curl -sI http://127.0.0.1:8099/prices.db | grep -i accept-ranges     # → bytes
curl -s -r 0-15 http://127.0.0.1:8099/prices.db                      # → SQLite format 3
curl -s http://127.0.0.1:8099/metadata.json
```

`client/index.html` is a working query console — open
`http://127.0.0.1:8099/` after copying it into `dist/` (the workflow does this
automatically, along with vendoring the sql.js-httpvfs worker).

---

## One-time setup

1. Push this to a **public** repository.
2. Settings → Pages → Source: **GitHub Actions**.
3. Actions → *Build price database* → **Run workflow**, with `limit: 20` for a
   first smoke test.
4. Once green, check `https://<user>.github.io/<repo>/metadata.json`.

The nightly schedule is 02:00 UTC, after the chains publish their full files.

## Cost

| | free tier | this build |
|---|---|---|
| Actions (public repo) | unlimited minutes | ~40 min/night |
| Pages | 1 GB site, 100 GB/month | ~200 MB, well under |
| Releases | no bandwidth billing | one `prices.db` per run |

Nothing here needs a billing account.
