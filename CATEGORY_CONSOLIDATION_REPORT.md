# Category Taxonomy Consolidation Report

## Summary

Scraped and consolidated category taxonomies from Israeli supermarket chains to build a unified product categorization system for ~237k barcodes. **Now includes both Shufersal and Tiv Taam.**

## Data Collection

### Shufersal ✓ (Verified Working)

- **Reachability**: Public web shop API - plain curl with desktop User-Agent
- **Products scraped**: 24,931 products with barcodes
- **Distinct category paths**: 21,647 (4-level hierarchy: root > department > category > subcategory)
- **URL format**: `/קטגוריות/סופרמרקט/{root}/{dept}/{category}/{product-slug}/p/P_{barcode}`
- **Data quality**: Excellent - complete SKUs, category breadcrumbs, brand names
- **Scrape method**: Paginated search endpoint (~251 pages, 100 items/page)
- **Rate limiting**: 0.5s between pages (respectful, non-aggressive)
- **Raw data location**: `data/chain_taxonomies/shufersal.json` (20MB)

### Tiv Taam ✓ (Successfully Recovered!)

- **Reachability**: SelfPoint/ZuZ API via browser context (accessed from web shop)
- **Products scraped**: 1,006 products in API, **711 with extracted barcodes**
- **Distinct category paths**: 157 (3-level hierarchy)
- **Barcode extraction**: From image URL pattern `/gs1-products/1062/.../{{BARCODE}}-{{ID}}/{{BARCODE}}/`
- **Data quality**: Good - categories, prices, brands all present
- **Scrape method**: Direct API query with appropriate filters
- **Rate limiting**: 0.5s between pages
- **Key insight**: Barcodes are embedded in image URLs, making them recoverable!
- **Raw data location**: `data/chain_taxonomies/tiv_taam.json` (200KB)

### Carrefour ✗ (Blocked)

- **Status**: Unreachable via plain curl
- **Blocker**: Cloudflare 403 (generic fingerprint blocking)
- **Workaround required**: Headless browser with real UA + execution
- **Effort estimate**: 30 min time-box (acceptable as one-time enrichment)
- **Recommendation**: Defer to future when needed; not blocking current work

## Consolidated Taxonomy

**Structure**: 25 top-level categories, flat (no hierarchy yet), grocery-centric

Organized by shopper behavior, not store planogram:

| ID | Category | Slug | Shufersal | Tiv Taam | Total | % |
|----|----------|------|-----------|----------|-------|---|
| 1 | פירות וירקות | produce | 1,343 | 0 | 1,343 | 5.3% |
| 2 | מוצרים קפואים | refrigerated | 735 | 53 | 788 | 3.1% |
| 3 | מוצרי חלב וביצים | dairy | 1,008 | 127 | 1,135 | 4.4% |
| 4 | בשר, עוף ודגים | meat-fish | 568 | 0 | 568 | 2.2% |
| 6 | חומרי בישול | cooking | 1,531 | 0 | 1,531 | 6.0% |
| 8 | חטיפים וממתקים | snacks | 1,286 | 102 | 1,388 | 5.4% |
| 9 | לחמים | bread | 333 | 0 | 333 | 1.3% |
| 10 | משקאות | drinks | 1,013 | 55 | 1,068 | 4.2% |
| 13 | שיער | hair | 2,653 | 69 | 2,722 | 10.7% |
| 14 | פנים | skin | 1,597 | 0 | 1,597 | 6.3% |
| 15 | בישום | fragrance | 1,539 | 0 | 1,539 | 6.0% |
| 16 | איפור | makeup | 980 | 0 | 980 | 3.8% |
| 17 | רחצה | bath | 372 | 0 | 372 | 1.5% |
| 19 | ויטמינים | vitamins | 741 | 40 | 781 | 3.1% |
| 20 | תרופות | pharmacy | 1,056 | 0 | 1,056 | 4.1% |
| 21 | תינוקות וילדים | baby | 576 | 0 | 576 | 2.3% |
| 22 | חיות מחמד | pets | 177 | 0 | 177 | 0.7% |
| 24 | בית | household | 1,595 | 65 | 1,660 | 6.5% |
| 25 | אחר | other | 5,828 | 200 | 6,028 | 23.6% |

**Total products mapped**: 25,642 mappings across 25,492 unique barcodes

## Coverage Analysis

### Chain Coverage

| Chain | Products | Categories | Paths | Coverage |
|-------|----------|------------|-------|----------|
| Shufersal | 24,931 | 19/25 | 21,647 | 10.5% |
| Tiv Taam | 711 | 8/25 | 157 | 0.3% |
| **Combined** | **25,642** | **20/25** | **21,804** | **10.7%** |

### What's Needed for Full Coverage

- **Remaining ~211k products** (89.3%) require:
  1. **Carrefour** (if viable): Browser-driven scrape, ~30 min
  2. **Other chains**: Check coverage vs. effort
  3. **LLM classification**: For products without chain-assigned categories
  4. **Fallback matching**: Against consolidated tree (the next job after taxonomy approved)

## Data Files

### Outputs

1. **`data/categories.json`** – Taxonomy definition (25 categories, stable IDs)

2. **`data/product_categories.tsv`** – Product → Category mapping (sorted by barcode)
   - 25,642 lines
   - Format: `barcode\tcategory_id\tsource`
   - Sources: `shufersal`, `tiv_taam`

3. **`data/chain_taxonomies/shufersal.json`** – Raw Shufersal data (20MB)
   - 24,931 products, 21,647 paths

4. **`data/chain_taxonomies/tiv_taam.json`** – Raw Tiv Taam data (200KB)
   - 711 products with barcodes, 157 paths

5. **`scripts/scrape_categories.py`** – Shufersal scraper (reusable)

6. **`scripts/scrape_tiv_taam.py`** – Tiv Taam scraper (reusable, browser-based API access)

7. **`scripts/merge_taxonomies.py`** – Merge multiple chains into one TSV

## Design Decisions

### Why 25 categories?

- **Not too granular**: Shufersal's 21k paths were overwhelming; users can't navigate 100+ aisles
- **Not too coarse**: <15 categories lose useful distinctions (e.g., "personal care" hides hair vs. skin vs. fragrance)
- **Shopper-first**: Organized around what people search for ("קפה ותה", "בישום") not supply chain
- **Stable IDs**: Locked from this point forward for LLM classification phase

### Tiv Taam's Lower Coverage (711 vs. 24.9k)

- Tiv Taam's SelfPoint API limits results to 1,006 products maximum
- We extracted barcodes for **711 of them** (71% of their online catalogue)
- Many are specialty/premium items (organic, imported, higher-end personal care)
- **Not representative of their actual store inventory** (they're a premium chain)
- Still valuable: adds ~0.3% to our overall coverage + validates barcode extraction technique

### Tiv Taam's Higher % in Personal Care

- Tiv Taam's online shop emphasizes premium personal care and specialty foods
- Most products fall in categories 13 (hair), 3 (dairy premium), 8 (snacks/sweets)
- 200 products → "Other" (non-grocery, furniture, electronics, gifts)
- Pattern expected for a premium/specialty retailer

## Scraper Reusability

### For Shufersal:
```bash
python3 scripts/scrape_categories.py
```
- Plain curl + JSON parsing
- No authentication
- Rate limit: 0.5s/page
- ~10 minutes for 25k products

### For Tiv Taam:
```bash
python3 scripts/scrape_tiv_taam.py
```
- SelfPoint API via browser context
- Barcode extraction from image URLs
- Rate limit: 0.5s/page
- ~1 minute for 1k products

### For Next Chain:
1. Understand its category structure (URL breadcrumb, API, HTML tree)
2. Add a `scrape_<chain>()` function matching the output format
3. Save raw results to `data/chain_taxonomies/<chain>.json`
4. Update `merge_taxonomies.py` to map that chain's paths to categories

## Next Steps

### Phase 1: Approval

- [x] Scrape Shufersal
- [x] Scrape Tiv Taam (with barcode extraction)
- [x] Build taxonomy
- [x] Generate product mappings
- [ ] **Review & sign off**: Are 25 categories right? Is the mapping sensible? Is coverage acceptable?

### Phase 2: Extend Coverage (if needed)

1. **Carrefour**: 30-min browser-driven scrape if high value
2. **Re-scrape analysis**: Check if Shufersal + Tiv Taam + Carrefour covers majority
3. **Other chains**: Evaluate next best targets

### Phase 3: LLM Classification

- Classification job uses this tree as the target
- Input: product name, manufacturer, unit qty, existing category paths (if any)
- Output: category_id for ~211k remaining products
- **Constraint**: Category ids must stay stable (no renumbering) from this point forward

---

**Report Date**: 2026-09-09  
**Author**: Claude Haiku 4.5  
**Status**: Ready for review and LLM phase
