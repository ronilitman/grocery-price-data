# Netiv Hesed listing fixtures

Three real pages from `app.netiv-hesed.com`, carved down to a handful of
download rows with the surrounding markup left exactly as served — the same
rule as `tests/promotions/fixtures`. **Nothing here is hand-written.** The bug
these guard against is "a page did not mean what we assumed", and an invented
page would encode the assumption instead.

| file | what it is | why it is here |
|---|---|---|
| `listing_with_snapshot.html` | a day with its full snapshot published — one `PriceFull`, one `PromoFull`, the `Stores` file, and a delta for company | the ordinary case, and proof the deltas beside them are never taken |
| `listing_deltas_only.html` | the same page with its full-snapshot rows gone: hourly `Price`/`Promo` only | what the 07:26 build was served on 9 Sep 2026, when the chain published at 16:59 instead of 05:25 |
| `listing_empty.html` | the portal's answer for a date it has nothing for — no download links at all | a day the chain skipped looks identical to a Cloudflare challenge page, so neither may be read as the other |

All three were fetched on 9 September 2026.

## Refreshing one

The portal serves history by date, so these can be re-cut:

```python
html = netiv._session().get(netiv.INDEX, params={"Date": "2026-09-09"}).text
```

Keep the page's own markup and drop rows; do not retype it. `listing_empty`
is whatever the portal returns for a date with nothing on it — tomorrow's will
do.
