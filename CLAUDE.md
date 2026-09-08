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
| `index.json` | `levels`, `shards`, `chains` (id→name), `built_at`, `chain_as_of`, `products`, `promo_products` |
| `stores.json` | `{chain_id: {store_id: [name, city_code, priced_items]}}` |
| `{prefix}.json` | search shards, keyed by barcode prefix |
| `detail/{prefix}.json` | per-store prices, fetched on drill-down |
| `promo/{prefix}.json` | promotions, one entry per distinct offer |

## Things that will bite you

**A parser can fail silently.** The chains change their XML and the upstream
parser reports `errors: False` with zero rows — that is how Super-Pharm shipped
nothing for months, and how Fresh Market's branches vanished. Never trust the
status field. Check row counts and whether a CSV was actually written.

**One chain is scraped by this repo, not by the library.** נתיב החסד moved
off the host the library's `NETIV_HASED` points at - it still answers HTTP 500 -
and the library responded by disabling the chain, so it is absent from
`ScraperFactory.all_scrapers_name()` entirely. It publishes at
`app.netiv-hesed.com`, and `scripts/netiv.py` downloads from there; the
library's *parser* for the chain is still correct and still used. The portal
sits behind Cloudflare, which rejects bot User-Agents (`python-requests`,
`curl`) but not datacenter addresses - an empty UA gets through - so it is not
a `HOME_EGRESS` chain.

**That one ChainID is three separately-priced brands.** נתיב החסד, בר-כל and
שירה מרקט all publish under `7290058160839`. שירה מרקט's branches agree with
each other on 99% of shared barcodes and disagree with a נתיב החסד branch on
about 40% of them, so one baseline for the parent id would be a בר-כל price -
45 branches out-voting שירה מרקט's 9 - shown for a chain where nobody pays it.
`SUBCHAIN_SPLITS` in `build_chain_db.py` gives each brand its own chain id,
`<chain id>-<subchain id>`. Split only a chain that genuinely prices its brands
apart: Shufersal's שלי / דיל / אקספרס share one price list.

**A promotion file will not tell you its sub-chain.** Every one of Netiv
Hesed's PromoFull dumps says `<SubChainID>000</SubChainID>` while the PriceFull
for the same branch says `009`. The filename is no better - see the note in
`dumps.py` about chains whose names lie about that field. Promotions are
attributed through the *branch* instead, which `load_stores` has already
placed. `tests/subchains/` guards it.

**Six chains cannot be scraped from a datacenter.** Super-Pharm (Reblaze, HTTP
247), Hazi Hinam (Cloudflare 403), Victory, Mahsani Ashuk and Het Cohen
(laibcatalog never answers at all), Osher Ad. Their jobs route egress through a
Tailscale exit node named `pricebox` — currently Roni's Mac, eventually a Pi. If
*exactly* those six fail, suspect the exit node being asleep before you suspect
the chains. A headless browser does not help; only the egress does.

Het Cohen was the last one found, on 29 Aug 2026: it is a laibcatalog chain that
had been left on runner egress, so it failed every night on a 30s `getbranches`
timeout while the same URL answered in 56ms from home. A lone chain failing on a
connect timeout is worth checking against `HOME_EGRESS` before treating it as a
chain-side fault.

**A failing chain is carried forward, not dropped.** `backfill_chains.py`
refills it from the newest artifact within the 7-day retention, and
`chain_as_of` records how old that data is so the app can label it. Rebuilding
from scratch used to *delete* a chain that failed for one night.

**`verify.py` is the publish gate**, and `publish` can be red while the site
deployed fine — the job's colour is the last step's, not the deploy's. Read the
step list, not the conclusion.

**A promotion is an offer, not a price.** `u` is what one unit costs under it
and `q` is how many you must buy to get that; `t` is the headline total. Show
`u` without `q` and a two-for-34 Toffifee reads as a 34-shekel Toffifee. `c: 1`
means a loyalty card is required - half of Yellow's promotions are club-only.
`s` lists the branches honouring the offer and is absent when every branch of
that chain does. The unit price is always `DiscountedPrice / MinQty`, computed
by us: `DiscountedPricePerMida` is per *measure* unit, so Stop Market publishes
8.80 for a two-for-22 deal on a 125 g box. That division is also exactly what
CHP shows.

**Promo files come in two dialects and a chain can publish both.** Branches
either nest items under `<Groups>` with the terms per item, or list them flat
under `<PromotionItems>` with the terms on the promotion. Keshet runs 24
branches on one and 2 on the other, emitting the same promotion id with
different `RewardType` and `AllowMultipleDiscounts`. So those fields, and
`IsGiftItem`, describe the *file*, not the offer - never branch on them. Only
`PromotionId`, `PromotionDescription`, `MinQty`, `DiscountedPrice`, `ClubId`
and the dates survive the dialect. Reading one dialect only is how Yellow
shipped 1 of 244 branches and Stop Market shipped none of 11.

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
- **The app does not read `promo/` yet.** The shards are published and match
  CHP's numbers; wiring them into `grocery-list-app` is the next change.
- Firestore rules need the anonymous provider enabled in the Firebase console
  before stricter rules can ship (the one real security item).
- Search shards should split on a byte budget like `split_by_size` already does
  for detail shards; the largest is ~330 KB against a claimed ~150 KB.

Detail on how Super-Pharm was diagnosed:
<https://claude.ai/code/artifact/475c6e1d-8a30-4be6-a171-2afeed270224>

[`grocery-list-app`]: https://github.com/ronilitman/grocery-list-app
