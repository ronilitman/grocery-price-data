# Category Taxonomy Consolidation Report

## Summary

Scraped and consolidated category taxonomies from Israeli supermarket chains to build a unified product categorization system for ~237k barcodes.

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

### Carrefour ✗ (Blocked)

- **Status**: Unreachable via plain curl
- **Blocker**: Cloudflare 403 (generic fingerprint blocking)
- **Workaround required**: Headless browser with real UA + execution
- **Effort estimate**: 30 min time-box (acceptable as one-time enrichment)
- **Recommendation**: Defer to future when needed; not blocking current work

### Tiv Taam ≈ (Unusable - No Barcode)

- **Status**: SelfPoint/ZuZ platform, requires browser context + fingerprinting bypass
- **Critical issue**: No barcode field in product documents (only internal productId)
- **Barcode matching check**: ItemCode lookup in government price files - DEFERRED
- **Recommendation**: Revisit only if ItemCode matching shows >80% hit rate on sample
- **Decision**: Exclude from this phase; revisit later if payoff proven

## Consolidated Taxonomy

**Structure**: 25 top-level categories, flat (no hierarchy yet), grocery-centric

Organized by shopper behavior, not store planogram:

| ID | Category | Slug | Products | % of Shufersal |
|----|----------|------|----------|--------|
| 1 | פירות וירקות | produce | 1,343 | 5.4% |
| 2 | מוצרים קפואים | refrigerated | 735 | 2.9% |
| 3 | מוצרי חלב וביצים | dairy | 1,008 | 4.0% |
| 4 | בשר, עוף ודגים | meat-fish | 568 | 2.3% |
| 5 | דגנים ופסטה | grains | 0 | — |
| 6 | חומרי בישול | cooking | 1,531 | 6.1% |
| 8 | חטיפים וממתקים | snacks | 1,286 | 5.2% |
| 9 | לחמים | bread | 333 | 1.3% |
| 10 | משקאות | drinks | 1,013 | 4.1% |
| 13 | שיער | hair | 2,653 | 10.6% |
| 14 | פנים | skin | 1,597 | 6.4% |
| 15 | בישום | fragrance | 1,539 | 6.2% |
| 16 | איפור | makeup | 980 | 3.9% |
| 17 | רחצה | bath | 372 | 1.5% |
| 19 | ויטמינים | vitamins | 741 | 3.0% |
| 20 | תרופות | pharmacy | 1,056 | 4.2% |
| 21 | תינוקות וילדים | baby | 576 | 2.3% |
| 22 | חיות מחמד | pets | 177 | 0.7% |
| 24 | בית | household | 1,595 | 6.4% |
| 25 | אחר | other | 5,828 | 23.4% |

**Empty categories** (5): grains (5), condiments (7), coffee-tea (11), alcohol (12), teeth (18), organic (23)

**Mapping rationale**:

- **Root → Category**: Direct mapping of Shufersal's root departments to consolidated categories
  - Example: "בישום" (fragrance) → category 15 directly
  - "פארם-וטיפוח" (pharmacy/care) → split across hair (13), pharmacy (20), baby (21)

- **Non-grocery items (23.4% → "Other")**: Furniture, electronics, kitchen appliances, home goods, clothing
  - Shufersal mixes grocery and home goods on one platform
  - Our focus is grocery-first; LLM phase will handle these

## Coverage Analysis

### Shufersal Coverage

- **Products mapped**: 24,931 (100% of Shufersal)
- **Categories populated**: 19 of 25 (76%)
- **Largest category**: "Other" at 23.4% (mostly non-grocery items Shufersal doesn't categorize well)

### Overall Catalogue Coverage

| Metric | Value |
|--------|-------|
| Products with category (Shufersal only) | 24,931 |
| Total products in catalogue | ~237,155 |
| Coverage | **10.5%** |
| Source | chain-assigned categories |

### What's Needed for Full Coverage

- **Remaining ~212k products** (89.5%) require:
  1. **Other chains**: Carrefour, Tiv Taam, others yet to be scraped
  2. **LLM classification**: For products without chain-assigned categories
  3. **Fallback matching**: Against consolidated tree (the next job after taxonomy is approved)

## Data Files

### Outputs

1. **`data/categories.json`** – Taxonomy definition
   ```json
   [{
     "id": 1,
     "slug": "produce",
     "name_he": "פירות וירקות",
     "parent_id": null
   }, ...]
   ```

2. **`data/product_categories.tsv`** – Product → Category mapping (sorted by barcode)
   ```
   barcode\tcategory_id\tsource
   1043\t1\tshufersal
   1050\t1\tshufersal
   ...
   ```

3. **`data/chain_taxonomies/shufersal.json`** – Raw scraped paths for future reference
   - 24,931 products
   - All category paths with product counts per path
   - Enables re-consolidation if taxonomy changes

4. **`scripts/scrape_categories.py`** – Shufersal scraper (reusable)

## Design Decisions

### Why 25 categories?

- **Not too granular**: Shufersal's 21k paths were overwhelming; users can't navigate 100+ aisles
- **Not too coarse**: <15 categories lose useful distinctions (e.g., "personal care" hides hair vs. skin vs. fragrance)
- **Shopper-first**: Organized around what people search for ("קפה ותה", "בישום") not supply chain ("מוצרי טיפוח - קבוצה A")

### Why "Other" is large (23.4%)?

- Shufersal publishes non-grocery items (furniture, electronics, toys) with weak category signals
- Our domain is grocery; non-food categories are lower priority
- LLM phase will re-classify many of these; some may drop out as out-of-scope

### Empty categories

- **Grains, Condiments, Alcohol, Coffee-Tea**: These items exist in Shufersal but were misclassified
  - Shufersal groups them under broader roots (e.g., "cooking" includes oils, grains, spices together)
  - Root → category mapping lumped them elsewhere
  - **Next phase**: Sub-department mapping or keyword refinement can recover these

## Next Steps

### Phase 1: Approval

- [x] Scrape Shufersal
- [x] Build taxonomy
- [x] Generate product mappings
- [ ] **Review & sign off**: Are 25 categories right? Is the mapping sensible? Is coverage acceptable?

### Phase 2: Extend Coverage

1. **Carrefour**: 30-min browser-driven scrape to capture high-value competitor
2. **Re-scrape analysis**: Check if combining Shufersal + Carrefour covers majority of SKUs
3. **Fallback sources**: Government price files may encode categories or chains we haven't hit

### Phase 3: LLM Classification

- Classification job will use this tree as the target
- Input: product name, manufacturer, unit qty, existing category paths (if any)
- Output: category_id for ~212k remaining products
- **Constraint**: Category ids must be stable (no renumbering) from this point forward

## Scraper Reusability

`scripts/scrape_categories.py` handles Shufersal. To add a new chain:

1. Understand its category structure (URL breadcrumb, API, HTML tree)
2. Add a `scrape_<chain>()` function matching the Shufersal output format
3. Save raw results to `data/chain_taxonomies/<chain>.json`
4. Update consolidation to map that chain's paths to categories

---

**Report Date**: 2026-09-09  
**Author**: Claude Haiku 4.5  
**Status**: Ready for review and LLM phase
