# Category data

Every product we publish should carry a category, so the app can group a
shopping list by aisle. The government price files carry name, manufacturer,
unit and a weighted flag, and **no category at all** - but the chains' own web
shops file each barcode under a tree of their own. This directory holds those
trees; the rest of the pipeline turns them into one category per barcode.

Nothing here is inferred from a product's name. A barcode gets the category its
own chain filed it under, or it gets nothing and waits for the LLM pass.

## The files, in the order they are used

### 1. Raw scrapes - `data/chain_taxonomies/*.json` (this directory)

One file per chain, committed so the whole thing rebuilds without re-scraping.
That is what made the first rebuild an hour rather than a day.

| file | size | products | distinct paths |
|---|---|---|---|
| `shufersal.json` | 6.2 MB | 25,108 | 935 |
| `hazi_hinam.json` | 1.1 MB | 10,489 | 171 |
| `rami_levy.json` | 1.1 MB | 7,554 | 386 |
| `carrefour.json` | 0.9 MB | 7,399 | 227 |
| `yenot_bitan.json` | 0.9 MB | 6,721 | 219 |
| `keshet_teamim.json` | 0.7 MB | 5,528 | 235 |
| `tiv_taam.json` | 0.2 MB | 711 | 152 |

All seven share a shape:

```json
{
  "chain": "shufersal",
  "note": "how it was reached, and where the barcode comes from",
  "products_count": 24931,
  "distinct_paths": 935,
  "products":       { "<barcode>": {"path": "dept>category>sub", "name": "…"} },
  "category_paths": { "dept>category>sub": 121 },
  "path_examples":  { "dept>category>sub": ["a few product names"] },
  "errors":         []
}
```

`products`, `category_paths` and `path_examples` are **sorted by key**. The APIs
return products in whatever order they like, so an unsorted dump rewrote most of
its own lines on every scrape - 58,800 changed lines for 775 changed products.
Sorted, the diff is what actually changed.

`path` is **already decoded, and the product's own segment is removed**. Both
matter:

- Shufersal's URL is `…/<dept>/<category>/<sub>/<product-slug>/p/P_<sku>`, and
  the segment before `/p/` is the product. Keeping it turned 24,931 products
  into 21,647 one-product "categories" and no tree could be built from that.
  Dropping it leaves 262 real department/category pairs.
- The dump used to store the raw percent-encoded URL. Hebrew is two bytes a
  letter and percent-encoding makes that six characters, so one 98-character
  path was stored as 438 - which was 10 MB of this directory saying nothing
  extra.

Shufersal rows also keep `second_level`, the chain's own department field, used
as a fallback for products whose URL gives no usable path.

### 2. The decisions - `scripts/chain_maps.py` and the tables in `build_categories.py`

About 550 hand-written lines mapping each chain's own category names onto our
slugs. This is where all the judgement lives, and it is the only place to edit
when a placement looks wrong.

- `build_categories.py` holds Shufersal's tables - `MAP` (department+category,
  118 lines), `DEPT` (whole departments, 25), `LEAF` (17 leaves that contradict
  their own pair, e.g. vegetable seeds shelved with the vegetables) - and
  `TAXONOMY`, which *is* the source of `categories.json`.
- `chain_maps.py` holds the five later chains. Carrefour, Keshet Teamim and
  Yenot Bitan all run on the SelfPoint platform and share most of a tree, so
  they share one table.

There is no chain-to-chain translation anywhere: every table points straight at
our slugs. Shufersal only went first, and is not privileged by the design - just
by the precedence order in the merge.

### 3. The builder - `scripts/build_categories.py`

Reads the dumps, applies the tables, writes both outputs. Deterministic: the
same dumps always produce byte-identical files. Before writing anything it
audits both directions - a table key naming a slug that does not exist, and a
key that no longer appears in any dump.

Where two chains place the same barcode differently, precedence decides and the
disagreement is not recorded: Shufersal, then Carrefour, Hazi Hinam, Keshet
Teamim, Rami Levy, Yenot Bitan, and Tiv Taam last - the narrowest catalogue,
shelving a specialty range.

```bash
python3 scripts/build_categories.py
```

### 4. The outputs - `data/categories.json` and `data/product_categories.tsv`

| file | what it is |
|---|---|
| `categories.json` | The tree. 116 rows of `{id, slug, name_he, parent_id}` - 15 top-level, 101 sub-categories. The ids are the stable keys everything downstream points at, so **do not renumber them**. |
| `product_categories.tsv` | The answer. `barcode <TAB> category_id <TAB> source chain`, 38,555 rows, sorted by barcode so a nightly run's new rows land as a small localised diff instead of rewriting a megabyte. |

### 5. The scrapers - `scripts/scrape_categories.py`, `scripts/scrape_tiv_taam.py`

Both write exactly the shape above - decoded `path`, `name`, `indent=1`, a
`note` saying how the chain was reached - so re-scraping produces a file that
diffs against the committed one rather than rewriting all of it. Check that
before adding a third: a scraper whose output shape drifts from the dumps is
how the repo stops being able to rebuild itself.

Only Shufersal and Tiv Taam have a committed scraper. **The other five do not.**
Carrefour, Keshet Teamim and Yenot Bitan sit behind Cloudflare and Hazi Hinam's
item endpoints refuse curl, so those four were pulled through a browser by hand;
Rami Levy answers plain curl and simply has not been scripted yet. Their dumps
are reproducible from the data in this directory, but re-scraping them means
redoing the browser work.

### 6. The test - `tests/categories/test_build_categories.py`

The one that matters asserts the committed outputs are what the committed
builder produces, byte for byte. The first version of this data was generated by
a script that never made it into the repo and drifted badly - every fruit and
vegetable filed under facial skincare - while still looking healthy: sorted,
deduplicated, one source per row, plausible counts. "Does it look reasonable"
was never going to catch that.

## How to reach each chain

| chain | endpoint | barcode comes from |
|---|---|---|
| Shufersal | `GET /online/he/search/results?q=:relevance:&limit=100&page=N` - plain curl, paginates from **0** | `sku`, a real EAN |
| Rami Levy | `POST /api/catalog {"store":331,"from":N,"size":200}` - plain curl | `barcode`, a real EAN |
| Carrefour (1540) | SelfPoint `GET /v2/retailers/<id>/branches/<id>/products?appId=4&filters=…` - **needs a browser**, Cloudflare blocks curl | the EAN embedded in the `gs1-products` CDN image path; the API itself has no barcode field |
| Keshet Teamim (1219) | as above | as above |
| Yenot Bitan (1131) | as above | as above |
| Tiv Taam (1062) | as above | as above |
| Hazi Hinam | `GET /proxy/api/item/getItemsBySubCategory?Id=<sub>` - **needs a browser**, curl gets 401 | `BarKod`, a real EAN |

The SelfPoint `filters` parameter is required; without it the API returns
`{"error":"Forbidden"}`. The working value is:

```json
{"must":{"exists":["family.id","family.categoriesPaths.id","branch.regularPrice"],
         "term":{"branch.isActive":true,"branch.isVisible":true}},
 "mustNot":{"term":{"branch.regularPrice":0}}}
```

Recovery rates vary a lot between the SelfPoint chains - Carrefour 89%, Yenot
Bitan 83%, Keshet Teamim 55% - because they depend entirely on whether a given
product was photographed by GS1.

## Re-scraping

```bash
python3 scripts/scrape_categories.py    # Shufersal, ~4 minutes, plain curl
python3 scripts/scrape_tiv_taam.py      # Tiv Taam, needs a browser context
python3 scripts/build_categories.py     # rebuild both outputs from every dump
```

Each scraper overwrites its own dump and touches nothing else, so one chain can
be refreshed without disturbing the other six. `build_categories.py` then reads
whatever is on disk. Expect the product counts to move a little between runs -
chains add and drop lines - and expect `git diff` on the dump to be small; if it
is the whole file, the scraper's output shape has drifted from the committed
one and that is the thing to fix, not the diff.

The other five chains have no scraper, so refreshing them means redoing the
browser extraction by hand.

## What does not exist yet

Nothing downstream reads any of this. `merge_db.py`, `build_catalog.py` and the
app's `prices.js` contain no reference to categories, so today this is a
self-contained dataset: not in the database, not published, not visible in the
app.
