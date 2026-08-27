# grocery-price-data

Scrapes Israeli supermarket price files nightly and publishes them as a static
JSON API on GitHub Pages. It is not the product — [`grocery-list-app`] is.

```
chains → GitHub Actions (02:00 UTC) → GitHub Pages /catalog/*.json
                                           ↓
              ~/Documents/grocery-list-app → Firebase Hosting
```

`grocery-list-app` lives at `~/Documents/grocery-list-app` and fetches
`https://ronilitman.github.io/grocery-price-data/catalog/`. **A change to the
published shape is a change to two repos.** Check the consumer before editing
`build_catalog.py`.

## The published contract

| file | holds |
|---|---|
| `index.json` | `levels`, `shards`, `chains` (id→name), `built_at`, `chain_as_of`, `products` |
| `stores.json` | `{chain_id: {store_id: [name, city_code, priced_items]}}` |
| `{prefix}.json` | search shards, keyed by barcode prefix |
| `detail/{prefix}.json` | per-store prices, fetched on drill-down |

## Things that will bite you

**A parser can fail silently.** The chains change their XML and the upstream
parser reports `errors: False` with zero rows — that is how Super-Pharm shipped
nothing for months, and how Fresh Market's branches vanished. Never trust the
status field. Check row counts and whether a CSV was actually written.

**Five chains cannot be scraped from a datacenter.** Super-Pharm (Reblaze, HTTP
247), Hazi Hinam (Cloudflare 403), Victory and Mahsani Ashuk (laibcatalog never
answers at all), Osher Ad. Their jobs route egress through a Tailscale exit
node named `pricebox` — currently Roni's Mac, eventually a Pi. If *exactly*
those five fail, suspect the exit node being asleep before you suspect the
chains. A headless browser does not help; only the egress does.

**A failing chain is carried forward, not dropped.** `backfill_chains.py`
refills it from the newest artifact within the 7-day retention, and
`chain_as_of` records how old that data is so the app can label it. Rebuilding
from scratch used to *delete* a chain that failed for one night.

**`verify.py` is the publish gate**, and `publish` can be red while the site
deployed fine — the job's colour is the last step's, not the deploy's. Read the
step list, not the conclusion.

**Weighed goods are priced per kilogram.** ~12k of the products. The shard
carries `w: 1` and a unit in `u`; both are omitted when empty, so a missing `w`
means "not weighed", never "unknown". A cart cannot just sum these.

**`city` is a number, not a name.** All stores carry the Israeli CBS locality
code (`3000` Jerusalem, `5000` Tel Aviv). Not a parsing bug — it is how the
chains publish it. Identify branches by `store_name`.

**A chain missing from a product's `p` map does not sell it.** Treat as
unavailable, never as price 0, or the cheapest store is whichever stocks least.

**Barcodes are matched with and without a `729000` prefix.** If a scan finds
nothing, try stripping it.

## Running it

```bash
gh workflow run build.yml                      # full run, ~45 min
gh workflow run build.yml -f limit=2           # smoke test, ~3 min, will fail
                                               # verify (no store files) by design
gh workflow run build.yml -f reuse_run_id=<id> # republish without scraping
```

A full run needs `pricebox` awake for its whole duration.

## Still open

- **King Store** produces zero files and reproduces locally.
- **Alerting** when a chain fails — deliberately deferred.
- Firestore rules need the anonymous provider enabled in the Firebase console
  before stricter rules can ship (the one real security item).
- Search shards should split on a byte budget like `split_by_size` already does
  for detail shards; the largest is ~330 KB against a claimed ~150 KB.

Detail on how Super-Pharm was diagnosed:
<https://claude.ai/code/artifact/475c6e1d-8a30-4be6-a171-2afeed270224>

[`grocery-list-app`]: https://github.com/ronilitman/grocery-list-app
