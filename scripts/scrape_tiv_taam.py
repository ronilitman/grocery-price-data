#!/usr/bin/env python3
"""
Scrape Tiv Taam category taxonomy and extract barcodes from image URLs.

Tiv Taam uses SelfPoint/ZuZ platform. Their web API returns:
- Product family (category hierarchy)
- Image URL with embedded barcode: /gs1-products/1062/.../{{BARCODE}}-{{ID}}/{{BARCODE}}/...

Strategy:
1. Query SelfPoint API for products in each category
2. Extract barcode from image URL
3. Map family.categoriesPaths to consolidated categories
4. Match against government price file ItemCodes to verify coverage
"""

import json
import re
import time
import requests
from collections import defaultdict
from urllib.parse import quote
from pathlib import Path


# Tiv Taam details (from network requests)
RETAILER_ID = "1062"
BRANCH_ID = "924"
APP_ID = "4"

# Base API endpoint
API_BASE = "https://www.tivtaam.co.il/v2/retailers/{}/branches/{}/products".format(
    RETAILER_ID, BRANCH_ID
)

# Headers to mimic browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.tivtaam.co.il/",
}

# Standard filters (from observed requests)
BASE_FILTERS = {
    "must": {
        "exists": ["family.id", "family.categoriesPaths.id", "branch.regularPrice"],
        "term": {
            "branch.isActive": True,
            "branch.isVisible": True,
        }
    },
    "mustNot": {
        "term": {
            "branch.regularPrice": 0,
            "branch.isOutOfStock": False,
        }
    }
}


def extract_barcode_from_image_url(url):
    """
    Extract barcode from image URL.

    Pattern: /gs1-products/1062/{{size}}/{{BARCODE}}-{{ID}}/{{BARCODE}}/...

    Returns barcode (EAN) or None if not found.
    """
    if not url:
        return None

    # Look for pattern: /gs1-products/1062/.../BARCODE-ID/BARCODE/
    # Barcodes are 8-14 digit numbers
    match = re.search(r'/gs1-products/1062/[^/]+/(\d{8,14})-\d+/\1/', url)
    if match:
        return match.group(1)

    # Fallback: look for any 8-14 digit sequence after /1062/
    match = re.search(r'/gs1-products/1062/[^/]+/(\d{8,14})-', url)
    if match:
        return match.group(1)

    return None


def scrape_tiv_taam_categories():
    """
    Scrape all Tiv Taam products and their categories.

    Fetch products by category, extracting:
    - Barcode (from image URL)
    - Category hierarchy (from family.categoriesPaths)
    - Brand, name
    """

    print("Scraping Tiv Taam products...")

    products_by_barcode = {}
    category_paths = defaultdict(int)
    path_examples = defaultdict(list)
    errors = []

    # We'll fetch products in batches - start with a broad query to get all products
    # Page through results
    page = 0
    page_size = 50
    total_fetched = 0

    while True:
        from_offset = page * page_size

        # Query: all active, visible products
        params = {
            "appId": APP_ID,
            "filters": json.dumps(BASE_FILTERS),
            "from": from_offset,
            "size": page_size,
        }

        try:
            resp = requests.get(API_BASE, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            errors.append(f"Page {page} (offset {from_offset}): {e}")
            break

        products = data.get("products", [])
        if not products:
            print(f"  Page {page}: No products returned, stopping")
            break

        total = data.get("total", 0)
        print(f"  Page {page} (offset {from_offset}): {len(products)} products (total in API: {total})")

        for product in products:
            # Extract barcode from image URL
            image = product.get("image", {})
            image_url = image.get("url", "")
            barcode = extract_barcode_from_image_url(image_url)

            if not barcode:
                # Try fallback: check images array
                images = product.get("images", [])
                for img in images:
                    img_url = img.get("url", "")
                    barcode = extract_barcode_from_image_url(img_url)
                    if barcode:
                        break

            if barcode:
                # Extract category path
                family = product.get("family", {})
                category_paths_list = family.get("categoriesPaths", [])

                # Each product has one or more category paths
                # categoriesPaths is an array of arrays (each inner array is a path)
                for path_array in category_paths_list:
                    if path_array:
                        # Build path from category names
                        path_parts = []
                        for cat in path_array:
                            cat_name = cat.get("names", {}).get("1", "")
                            if cat_name:
                                path_parts.append(cat_name)

                        if path_parts:
                            path = " > ".join(path_parts)
                            category_paths[path] += 1

                # Store product info in the shape every dump here shares:
                # a decoded `path` plus the product name. See
                # data/chain_taxonomies/README.md.
                path = ">".join(
                    c for c in (cat.get("names", {}).get("1", "")
                                for cat in family.get("categories", []))
                    if c
                )
                name = product.get("names", {}).get("1", {}).get("short", "")
                row = {"path": path}
                if name:
                    row["name"] = name
                products_by_barcode[barcode] = row
                if path:
                    category_paths[path] += 1
                    if len(path_examples[path]) < 8 and name:
                        path_examples[path].append(name)

            total_fetched += 1

        page += 1
        time.sleep(0.5)  # Rate limiting

        # Stop if we've fetched all products
        if from_offset + len(products) >= total:
            break

    return {
        "chain": "tiv_taam",
        "note": "SelfPoint v2 API, retailer 1062 - needs a browser context; the EAN "
                "comes from the gs1-products CDN image path, the API has no "
                "barcode field.",
        "products_count": len(products_by_barcode),
        "distinct_paths": len(category_paths),
        "products": products_by_barcode,
        "category_paths": dict(category_paths),
        "path_examples": {k: v for k, v in path_examples.items()},
        "errors": errors
    }


def verify_against_price_files(tiv_taam_data):
    """
    Check if Tiv Taam barcodes match our government price file data.

    Returns coverage stats: how many of our Tiv Taam price products have barcodes.
    """
    # Load the chain_db to see what Tiv Taam products we have from price files
    db_path = Path(__file__).parent.parent / "sqlite" / "chain_products.db"

    if not db_path.exists():
        print("  Note: chain_products.db not found, skipping coverage check")
        return None

    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Tiv Taam chain ID
        tiv_taam_chain_id = "7290027600001"  # This is Tiv Taam's GS1 chain ID

        # Count products for this chain
        cursor.execute(
            "SELECT COUNT(DISTINCT barcode) FROM chain_prices WHERE chain_id = ?",
            (tiv_taam_chain_id,)
        )
        price_file_count = cursor.fetchone()[0]

        # Check how many of our barcodes appear in chain_prices
        barcodes_str = ",".join(f"'{b}'" for b in tiv_taam_data['products'].keys())
        cursor.execute(
            f"SELECT COUNT(DISTINCT barcode) FROM chain_prices WHERE chain_id = ? AND barcode IN ({barcodes_str})",
            (tiv_taam_chain_id,)
        )
        matched_count = cursor.fetchone()[0]

        conn.close()

        coverage = (matched_count / price_file_count * 100) if price_file_count > 0 else 0

        return {
            "price_file_products": price_file_count,
            "barcodes_extracted": len(tiv_taam_data['products']),
            "matched": matched_count,
            "coverage_percent": coverage
        }
    except Exception as e:
        print(f"  Error checking coverage: {e}")
        return None



def _sorted(dump):
    """Sort the barcode- and path-keyed maps before writing.

    JSON preserves insertion order, and the API returns products in whatever
    order it likes, so an unsorted dump rewrites most of its own lines on every
    scrape. Sorted, the diff is the products that actually changed.
    """
    out = dict(dump)
    for key in ("products", "category_paths", "path_examples"):
        if key in out:
            out[key] = {k: out[key][k] for k in sorted(out[key])}
    return out


def main():
    print("=== Tiv Taam Scraper ===\n")

    data = scrape_tiv_taam_categories()

    print(f"\n=== RESULTS ===")
    print(f"Products with extracted barcodes: {data['products_count']}")
    print(f"Distinct category paths: {data['distinct_paths']}")
    print(f"Total API items processed: {data['total_fetched']}")
    print(f"Errors: {len(data['errors'])}")

    if data['errors']:
        print(f"\nSample errors:")
        for err in data['errors'][:3]:
            print(f"  {err}")

    # Show top category paths
    print(f"\nTop 10 category paths:")
    paths = sorted(data['category_paths'].items(), key=lambda x: x[1], reverse=True)
    for path, count in paths[:10]:
        print(f"  {count:4} - {path}")

    # Save raw data
    data_dir = Path(__file__).parent.parent / "data" / "chain_taxonomies"
    data_dir.mkdir(parents=True, exist_ok=True)

    out_file = data_dir / "tiv_taam.json"
    with open(out_file, "w") as f:
        json.dump(_sorted(data), f, indent=1, ensure_ascii=False)
    print(f"\nSaved to {out_file}")

    # Try to verify coverage
    print(f"\nVerifying against government price files...")
    coverage = verify_against_price_files(data)
    if coverage:
        print(f"  Price file products for Tiv Taam: {coverage['price_file_products']}")
        print(f"  Barcodes extracted from web: {coverage['barcodes_extracted']}")
        print(f"  Matched: {coverage['matched']}")
        print(f"  Coverage: {coverage['coverage_percent']:.1f}%")


if __name__ == "__main__":
    main()
