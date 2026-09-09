#!/usr/bin/env python3
"""
Scrape category taxonomies from Israeli supermarket chains.
Shufersal: web shop with category breadcrumbs and category codes.
Tiv Taam: SelfPoint API with category paths but no barcodes (check for usability).
Carrefour: Cloudflare-blocked (browser-only).
"""

import json
import time
import requests
import sys
from collections import defaultdict
from urllib.parse import unquote
from pathlib import Path

def scrape_shufersal():
    """Scrape Shufersal web shop. ~25k products across 251 pages."""
    print("Scraping Shufersal...")

    base_url = "https://www.shufersal.co.il/online/he/search/results"
    params = {
        "q": ":relevance:",
        "limit": 100,
        "page": 1
    }

    products_by_barcode = {}
    category_paths = defaultdict(int)  # path → count
    errors = []

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    # Shufersal paginates from zero. Starting at 1 silently drops the first
    # hundred products - which is how cottage cheese went missing.
    page = 0
    max_pages = None

    while True:
        params["page"] = page
        try:
            resp = requests.get(base_url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            errors.append(f"Page {page}: {e}")
            break

        # Extract pagination info
        if max_pages is None:
            if "pagination" in data:
                max_pages = data["pagination"].get("numberOfPages", 1)
                print(f"  Found {data['pagination'].get('totalNumberOfResults')} products across {max_pages} pages")

        # Extract products
        results = data.get("results", [])
        if not results:
            print(f"  No results on page {page}, stopping")
            break

        for product in results:
            sku = product.get("sku")
            if not sku:
                continue

            # Extract category path from URL breadcrumb:
            #   /קטגוריות/<root>/<dept>/<category>/<sub>/<product-slug>/p/P_<sku>
            #
            # The segment before /p/ is the PRODUCT, not a category. Keeping it
            # turns 24,931 products into 21,647 one-product "categories" and no
            # tree can be built from that; dropping it leaves 262 real
            # department/category pairs. Depth varies - a promotion landing
            # page is two segments deep - so take what is there rather than
            # assuming a fixed shape.
            url = product.get("url", "")
            if url:
                parts = [unquote(p) for p in url.split("/") if p]
                while parts and (parts[-1] == "p" or parts[-1].startswith("P_")):
                    parts.pop()
                if parts and parts[0] == "קטגוריות":
                    parts = parts[1:]
                if parts[:-1]:
                    category_paths[" > ".join(parts[:-1])] += 1

            products_by_barcode[sku] = {
                "brand": product.get("brandName", ""),
                "second_level": product.get("secondLevelCategory", ""),
                "all_codes": product.get("allCategoryCodes", []),
                "url": url
            }

        print(f"  Page {page}/{max_pages}: {len(results)} products, {len(products_by_barcode)} total")

        if page >= max_pages - 1:
            break

        page += 1
        time.sleep(0.5)  # Be respectful

    return {
        "chain": "shufersal",
        "products_count": len(products_by_barcode),
        "distinct_paths": len(category_paths),
        "products": products_by_barcode,
        "category_paths": dict(category_paths),
        "errors": errors
    }


def scrape_tiv_taam():
    """
    Scrape Tiv Taam SelfPoint API.
    Check if productId matches our existing ItemCode before using.
    """
    print("\nScrapy Tiv Taam (exploratory)...")
    print("  Note: Tiv Taam has no barcode in API. Checking if productId matches ItemCode...")

    # This requires a browser context and fingerprinting bypass
    # For now, report that it needs browser-driven approach
    return {
        "chain": "tiv_taam",
        "status": "requires_browser",
        "notes": "SelfPoint API blocked by fingerprinting, needs browser context"
    }


def main():
    data_dir = Path(__file__).parent.parent / "data" / "chain_taxonomies"
    data_dir.mkdir(parents=True, exist_ok=True)

    shufersal = scrape_shufersal()

    # Save raw Shufersal dump
    out_file = data_dir / "shufersal.json"
    with open(out_file, "w") as f:
        json.dump(shufersal, f, indent=2, ensure_ascii=False)
    print(f"\nSaved Shufersal data to {out_file}")

    # Summary
    print(f"\n=== SUMMARY ===")
    print(f"Shufersal:")
    print(f"  Products: {shufersal['products_count']}")
    print(f"  Distinct category paths: {shufersal['distinct_paths']}")
    print(f"  Errors: {len(shufersal['errors'])}")

    if shufersal['errors']:
        print(f"  Sample errors: {shufersal['errors'][:3]}")


if __name__ == "__main__":
    main()
