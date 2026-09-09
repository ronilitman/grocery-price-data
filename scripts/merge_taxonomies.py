#!/usr/bin/env python3
"""
Merge Shufersal and Tiv Taam into one product_categories.tsv.

Both chains use our consolidated 25-category taxonomy.
Tiv Taam's category structure is different but we can map their paths
to the same categories using keyword matching.
"""

import json
from collections import defaultdict
from pathlib import Path


def load_tiv_taam():
    with open("data/chain_taxonomies/tiv_taam.json") as f:
        return json.load(f)


def build_tiv_taam_root_map():
    """Map Tiv Taam's top-level categories to our consolidated categories."""
    return {
        # Fresh (1-4)
        "ירקות ופירות": 1,
        "ירקות": 1,
        "פירות": 1,

        "קפואים": 2,

        "מקרר חלבי וביצים": 3,

        "בשר דגים ועוף": 4,

        # Pantry (5-9)
        "קטניות ודגנים": 5,
        "אורז ופסטה": 5,

        "בישול ואפיה": 6,

        "שמנים": 7,

        "חטיפים ועוגיות": 8,

        "לחם ומאפיה": 9,

        # Beverages (10-12)
        "משקאות": 10,

        "פארם ותינוקות": 13,  # mostly personal care/pharmacy
        "רחצה": 17,

        "ויטמינים ותוספים": 19,

        "ניקיון ומוצרי בית": 24,
    }


def match_tiv_taam_products():
    """Match Tiv Taam products to our consolidated categories."""
    data = load_tiv_taam()
    root_map = build_tiv_taam_root_map()

    products_by_cat = defaultdict(list)

    for barcode, product in data['products'].items():
        cat_id = 25  # Default to Other

        # Extract root from categories list
        categories = product.get('categories', [])
        if categories:
            root = categories[0]
            # Try to match against our map
            for map_root, mapped_cat in root_map.items():
                if root.lower() in map_root.lower() or map_root.lower() in root.lower():
                    cat_id = mapped_cat
                    break

        products_by_cat[cat_id].append((barcode, "tiv_taam"))

    return products_by_cat


def main():
    print("Merging taxonomies...\n")

    # Load existing Shufersal mappings
    shufersal_lines = []
    with open("data/product_categories.tsv") as f:
        shufersal_lines = [line.strip() for line in f if line.strip()]

    print(f"Loaded {len(shufersal_lines)} Shufersal mappings")

    # Get Tiv Taam mappings
    tiv_taam_products = match_tiv_taam_products()

    tiv_taam_lines = []
    tiv_taam_cat_counts = defaultdict(int)
    for cat_id, products in sorted(tiv_taam_products.items()):
        for barcode, source in sorted(products):
            tiv_taam_lines.append(f"{barcode}\t{cat_id}\t{source}")
            tiv_taam_cat_counts[cat_id] += 1

    print(f"Added {len(tiv_taam_lines)} Tiv Taam mappings\n")

    print("Tiv Taam category coverage:")
    for cat_id in sorted(tiv_taam_cat_counts.keys()):
        count = tiv_taam_cat_counts[cat_id]
        print(f"  {cat_id:2}: {count:3} products")

    # Merge and sort by barcode
    all_lines = shufersal_lines + tiv_taam_lines
    all_lines.sort()

    # Write merged file
    with open("data/product_categories.tsv", "w") as f:
        f.write("\n".join(all_lines) + "\n")

    print(f"\nWrote {len(all_lines)} total product-category mappings")

    # Calculate coverage
    total_products = len(set(line.split("\t")[0] for line in all_lines))
    print(f"Unique barcodes: {total_products}")
    print(f"Coverage: {total_products / 237155 * 100:.1f}% of ~237k total products")


if __name__ == "__main__":
    main()
