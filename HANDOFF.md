# Handoff: grocery price API → grocery list app

Written for an agent picking this up cold. Two repos are involved. Both are now
live, and most of the plan below is **built** — the sections are kept as the
record of why each thing works the way it does. What is still open is collected
in Part 6.

| repo | role | status |
|---|---|---|
| [`grocery-price-data`](https://github.com/ronilitman/grocery-price-data) | scrapes Israeli supermarket prices, publishes them as a static JSON API | **live**, updates nightly |
| [`grocery-list-app`](https://github.com/ronilitman/grocery-list-app) | React + Firebase shared shopping list | **live** on Firebase Hosting |

Goal: teach the app to hold barcodes, remember favourite stores, and price a
shopping cart across them.

---

## Part 1 — What exists today

### 1.1 The price API (done, don't rebuild)

Static JSON on GitHub Pages. No server, no key, no rate limit, `$0`.
CORS is `access-control-allow-origin: *`, so any origin can read it.

```
BASE = https://ronilitman.github.io/grocery-price-data/catalog/
```

| endpoint | returns |
|---|---|
| `index.json` | `{levels, shards[], chains{id→name}, built_at, products}` — fetch once, cache |
| `{prefix}.json` | `{barcode: {n: name, p: {chain_id: [price, branches_at_baseline]}, w?: 1, u?: unit}}` — `w`/`u` only on weighed goods |
| `detail/index.json` | `{levels, shards[]}` for the per-store files |
| `detail/{prefix}.json` | `{barcode: {chain_id: [[store_id, price], …]}}` — **only branches that differ** |
| `stores.json` | `{chain_id: {store_id: [name, city_code]}}` (~55 KB) |

**Two rules govern everything:**

1. **Shard resolution.** Filenames are barcode prefixes, split deeper where
   crowded (`729` is Israel's GS1 country prefix, so most products live under
   it). Take the **longest** prefix in `shards` that your barcode starts with,
   padding short barcodes with `0`:

   ```js
   function shardFor(barcode, { levels, shards }) {
     let best = null;
     for (const d of levels) {
       const p = barcode.slice(0, d).padEnd(d, "0");
       if (shards.includes(p)) best = p;
     }
     return best;
   }
   ```
   `4412784` → `441.json`; `7290004127329` → `72900041.json`.

2. **Price at a specific store** = its entry in `detail/`, else the chain
   baseline from the search shard. Absence from `detail/` means "pays the
   baseline", **not** "no price".

Worked example — tomatoes at Tiv Taam:

```bash
curl -s .../catalog/441.json        | jq '."4412784"'
# {"n":"עגבניות","p":{"7290873255550":[6.9,29]},"w":1,"u":"קילוגרם"}   ← ₪6.90/kg at 29 branches
curl -s .../catalog/detail/441.json | jq '."4412784"'
# {"7290873255550":[["63",10.9],["21",8.9], …]}       ← 24 branches differ
curl -s .../catalog/stores.json     | jq '."7290873255550"."21"'
# ["יהוד","9400"]
```

Scale: 109,456 products, 1,225 stores, 10 chains, 4,516 files, 50 MB total.
Verified against competitor chp.co.il — identical prices and tiers.

### 1.2 The app

React 19 + Vite + Firebase Firestore. One file does everything:
`src/App.jsx`, ~480 lines.

- **Live at** <https://gen-lang-client-0902689301.web.app> — Firebase Hosting,
  same GCloud project as Firestore. Served from the **domain root**, so
  `vite.config.js` must keep its default `base` (`/`); setting a subpath base
  would 404 every asset. Deploys are manual: `npm run build && firebase deploy`.
  There is no CI for it — worth adding.
- **Storage:** Firestore collection `groceries_{room}`; `room` comes from the
  `?room=` query param, default `our-groceries`. No auth, no user accounts —
  the URL *is* the credential.
- **Item shape:** `{name, quantity, notes, category, status: 'pending'|'bought',
  createdAt, boughtAt}`. **No barcode field yet.**
- **Features:** add/edit/delete, pending vs bought tabs, keyword→category
  guessing (`AUTO_CATEGORY_MAP`), duplicate-name guard, confetti on completion.

---

## Part 2 — Facts that will bite you

These cost real time to discover. Trust them.

1. **Weighed goods use short internal barcodes.** Tomatoes are `4412784`, not
   `7290004412784`. Competitor sites display a padded 13-digit form
   (`729000` + the code). If a scan returns nothing, try stripping a `729000`
   prefix. **12,853 of 109,456 products are weighed.**

2. **Weighed prices are per kilogram.** ₪6.90 for tomatoes means ₪6.90/kg. Cart
   totals **cannot** just sum prices for these — you need a weight from the
   user. The shard entry now carries `w: 1` for weighed goods and `u` for the
   unit. Both are **omitted when empty**, so a missing `w` means "not weighed",
   never "unknown".

3. **`city` is unusable.** All 1,225 stores store a *number* — the Israeli CBS
   locality code (`3000` Jerusalem, `5000` Tel Aviv, `2600` Eilat), because
   that is how the chains publish it. Not a parsing bug. Identify branches by
   `store_name`, which usually contains the location. Filtering by city needs a
   code→name table that does not exist yet.

4. **A chain missing from `p` does not sell the product.** Treat as "unavailable",
   never as price 0 — otherwise the cheapest store is whichever stocks least.

5. **Only ~10 of 17 chains have data.** Victory, Mahsani Ashuk (upstream host
   times out), Hazi Hinam (HTTP 403), Super-Pharm (Reblaze JS bot-challenge,
   unfixable without a headless browser), King Store, Zol VeBegadol (publishes
   thousands of dead file links). Missing chains are an upstream reality, not a
   bug to fix.

6. **Chain ids are numeric strings** (GS1 company prefixes) like
   `"7290873255550"`. Map to names via `index.json`'s `chains`.

7. **Prices are daily snapshots**, rebuilt by cron at 02:00 UTC. Not real-time.

8. **Redeploying the site does not require re-scraping.** The workflow accepts
   `reuse_run_id`, which skips the scrape matrix and reuses a prior run's
   artifacts (~1 min instead of ~30):
   ```bash
   gh workflow run build.yml -f reuse_run_id=<previous_run_id>
   ```
   Caveat: artifacts have `retention-days: 1`. Bump to 7 if iterating.

---

## Part 3 — Blockers

**3.1 Firestore is wide open — STILL OPEN, deliberately deferred.**
`firestore.rules` is:

```
match /{document=**} { allow read, write: if true; }
```

The app is **publicly live**, so this is not theoretical: anyone who views source
gets the project id and can read, modify, or wipe every list — now including the
`settings_*` docs holding favourite stores. The fix is to scope the rules to
`groceries_*` and `settings_*` and add anonymous auth or App Check, but the
anonymous provider has to be enabled in the Firebase console **first**, or
stricter rules break the live app the moment they ship. The Firebase web
`apiKey` in `src/firebase.js` is *not* a secret and is fine to commit; the open
rules are the problem.

`sa-key.json` was once committed (removed from tracking in `12c4f15`, still
reachable in history). Confirm that credential was rotated.

**3.2 Duplicate price pipeline — removed.** `scripts/process_prices.py` and
`.github/workflows/daily_prices.yml` were an earlier, weaker attempt at the same
job: 3 shard files against the 4,516 now live. Both are gone, along with the
now-unused `requirements.txt`. The vestigial `gh-pages` branch still exists and
should go too — GitHub Pages is not enabled on that repo and the URL 404s, so it
serves nothing:

```bash
git push origin --delete gh-pages     # branch tip 077b09e, if it needs restoring
```

**3.3 Deploy automation — added.** `.github/workflows/deploy.yml` runs lint and
build on every push and PR, and deploys to Firebase Hosting on `master`. The
deploy step is gated on a `FIREBASE_SERVICE_ACCOUNT` secret that does not exist
yet; until someone adds it (`firebase init hosting:github` generates one) the
workflow is a build gate and deploys stay manual.

---

## Part 4 — The plan

### 4.1 Prerequisite: expose weight info in the API — **done**

`scripts/build_catalog.py` now reads `unit_qty` and `is_weighted` alongside the
name and writes them as `w` / `u`, omitting either when it would be empty:

```python
entry = {"n": name, "p": {}}
if is_weighted:
    entry["w"] = 1
    if unit_qty and unit_qty.strip() != "לא ידוע":
        entry["u"] = unit_qty.strip()
```

Omission is deliberate: only 12% of products are weighed, and with 109k entries
an always-present key is paid for 109k times. `u` rides only on weighed entries
for the same reason — on a packaged product `unit_qty` is a bare "gram" whose
count is in a column the shards do not carry, so it cost ~9% of shard bytes to
say nothing. Published with `reuse_run_id`, no scrape.
`.github/workflows/build.yml` artifact retention went 1 day → 7, so
`reuse_run_id` still has artifacts to reuse a few days later.

### 4.2 Feature 1 — Add a product by barcode — **built**

**Data.** Add to each Firestore item:
```js
{ barcode: "4412784" | null,
  isWeighted: false,
  unit: "קילוגרם" | null,
  weightKg: 1.5 | null }   // only for weighed items
```
Keep barcode optional — plenty of list entries ("birthday card") have none. The
existing free-text flow must keep working.

**Lookup module.** New `src/prices.js`: implements `shardFor`, caches
`index.json` and fetched shards in memory (plus `localStorage`, since the data
only changes daily). Exposes:
```js
lookup(barcode)        → {name, isWeighted, unit, prices: {chainId: [price, n]}}
storePrice(barcode, chainId, storeId) → number | null
```

**UI.** Barcode field in the add form → on blur/enter, `lookup()` → prefill the
name and lock in `isWeighted`. Show "not found" without blocking the add. If the
raw scan misses, retry after stripping a leading `729000` (§2.1).

**Scanning (optional, do last).** `BarcodeDetector` is native on Chrome/Android
but **absent on iOS Safari** — ship `@zxing/browser` as the fallback, or start
with manual entry only.

### 4.3 Feature 2 — Favourite stores — **built**

**Data.** A settings doc per room, e.g. `settings_{room}/prefs`:
```js
{ favouriteStores: [{ chainId: "7290873255550", storeId: "21" }] }
```
Cap at ~5; comparison output gets unreadable beyond that.

**UI.** Load `stores.json` (55 KB, once). Group by chain, filter with a text box
over `store_name` — **not** city, which is a numeric code (§2.3). Show as
"chain — branch".

### 4.4 Feature 3 — Compare the cart across stores — **built**

**Input:** pending items that have a barcode. **Output:** one row per favourite
store.

Algorithm:
```
for each pending item with a barcode:
    entry  = search shard   (name, per-chain baselines, is_weighted)
    detail = detail shard   (per-store overrides)
for each favourite store (chainId, storeId):
    if chainId not in entry.p        → unavailable, add to `missing`
    else price = detail override ?? baseline
    line = price * (isWeighted ? weightKg : quantity)
    total += line
```

**Presentation.** Sort by total, cheapest first, and — critically — **show
coverage next to it**: `₪184.20 · 12/14 items`. A store missing two items looks
artificially cheap; never rank on total alone. Let the user expand a store to
see per-item prices and which items were missing.

**Must handle:**
- items with no barcode → excluded, shown as a footnote count
- weighed items with no `weightKg` → prompt, or exclude and flag
- no favourite stores yet → prompt to pick some
- a chain absent from `p` → "not sold here", not ₪0

**Network cost:** ~2 fetches per distinct shard, heavily shared across a cart,
all cached. A 15-item cart is a handful of requests totalling well under 100 KB.

### 4.5 Where each step landed

| # | step | status |
|---|---|---|
| 1 | §3.1 Firestore rules | **open** — deferred by choice, see Part 6 |
| 2 | §4.1 API weight fields | done, live |
| 3 | §4.2 barcode on items, manual entry | done |
| 4 | §4.3 favourite stores | done |
| 5 | §4.4 comparison view | done |
| 6 | §4.2 camera scanning | done |
| 7 | §3.2 delete the duplicate pipeline | pipeline done, `gh-pages` still to delete |
| 8 | §3.3 deploy automation | workflow added, needs a secret |

---

## Part 5 — How it actually got built

`src/prices.js` is the whole API client. Beyond `shardFor` and `lookup` it holds
the cart pricing, because the two rules in §1.1 and the traps in Part 2 are
easier to keep honest in one file than spread across components.

Caching is keyed by the build's `built_at` stamp — `s:{built_at}:{prefix}` for
search shards, `d:` for detail. A nightly rebuild changes the stamp, which
orphans every stale key at once with no expiry logic. `localStorage` writes that
hit quota drop the whole cache and retry once rather than maintaining an LRU;
the cost of being wrong is one cold fetch.

`lookup()` tries a short list of barcode forms, not just the scanned one: the
raw digits, the same minus a leading `729000` (§2.1), that minus leading zeros,
and a 13-digit form minus a leading zero (UPC-A read as EAN-13).

`compareCart()` returns `covered` and `considered` per store and sorts on
coverage **before** total, so a branch that stocks more of your basket outranks
one that is cheap by absence. Weighed items with no `weightKg` produce a line
with a `null` total, counted in `unpriceable` and shown as "needs a weight" —
they are never silently charged as one kilo.

Scanning prefers `BarcodeDetector` and falls back to `@zxing/browser` behind a
dynamic `import()`, keeping its 477 KB out of the main bundle until someone taps
scan. On iOS Safari the fallback is not an edge case, it is the only path.

**Verified end to end** against the live API: `7290004412784` resolves to
`4412784` via the strip, prefills עגבניות, exposes the per-kilo weight field,
and a two-item cart prices correctly across five branches — ₪31.90 × 2 + ₪6.90 ×
1.5 = ₪74.15 at Tiv Taam Netanya (2/2 items), ranked above two ₪51.80 stores
showing 1/2.

---

## Part 6 — What is still open

1. **Firestore rules (§3.1).** The one real risk left. Needs the anonymous
   provider enabled in the Firebase console before stricter rules can ship.
2. **Delete the `gh-pages` branch** in `grocery-list-app` (§3.2). Serves nothing;
   one command.
3. **Add the `FIREBASE_SERVICE_ACCOUNT` secret** (§3.3) to turn CI into a real
   deploy.
4. **Confirm `sa-key.json` was rotated** (§3.1).
5. **Shard size.** The largest search shard is ~330 KB, well over the ~150 KB the
   `MAX_ITEMS = 2000` comment in `build_catalog.py` claims. This predates the
   `w`/`u` fields. The detail shards already split on a byte budget
   (`split_by_size`); the search shards should too.
6. **Category detection skips catalogue hits.** `AUTO_CATEGORY_MAP` is keyed on
   English words and only runs on typing, so a barcode-filled Hebrew name always
   lands in "Other".

---

## Appendix — verify the API is alive

```bash
curl -s https://ronilitman.github.io/grocery-price-data/catalog/index.json | head -c 200
curl -s https://ronilitman.github.io/grocery-price-data/catalog/441.json | jq '."4412784"'
```

Live demo of a barcode lookup with per-store drill-down:
<https://ronilitman.github.io/grocery-price-data/?bc=4412784> (tap the chain row).
