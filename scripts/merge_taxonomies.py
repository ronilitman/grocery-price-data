#!/usr/bin/env python3
"""
Merge Shufersal and Tiv Taam into one product_categories.tsv.

Conflict resolution strategy:
- For barcodes in both sources: prefer Shufersal (authoritative, 35x more data)
- For barcodes only in Tiv Taam: add them to final output
- Result: All unique barcodes from both sources, with smart conflict resolution
"""

import json
from collections import defaultdict


def load_shufersal_categories():
    """Load Shufersal's category assignments."""
    categories = {}
    # Rebuild from scratch using consolidate_simple.py mapping
    with open("data/chain_taxonomies/shufersal.json") as f:
        data = json.load(f)

    # Simple mapping: root category -> consolidated category (from consolidate_simple.py)
    root_map = {
        "פירות-וירקות": 1,
        "אורגני-ובריאות": 1,
        "מזון-מהמקרר-ומהמקפיא": 2,
        "מוצרי-חלב-וביצים": 3,
        "מוצרי-מקרר-וביצים": 3,
        "מוצרי-בשר,-עוף-ודגים-": 4,
        "סלטים-ונקניקים": 4,
        "בישול-אפיה-ושימורים": 6,
        "בישול-אפיה-ממרחים-ושימורים": 6,
        "חטיפים-מתוקים-ודגני-בוקר": 8,
        "דגנים-חטיפים-מתוקים-ומשקאות": 8,
        "לחמים-ומוצרי-מאפה": 9,
        "לחם,-קרקרים-ופריכיות": 9,
        "יינות-משקאות-כהליים-ותירוש": 10,
        "פארם-וטיפוח": 13,
        "טיפוח-הגוף-והשיער": 13,
        "טיפוח-שיער-מקצועי": 13,
        "טיפוח-פנים": 14,
        "בישום": 15,
        "איפור-": 16,
        "רחצה-והגיינה": 17,
        "ויטמינים-ותוספי-תזונה": 19,
        "ויטמינים-ותוספי-תזונה-": 19,
        "בית-מרקחת-ואופטיקה": 20,
        "ילדים-ותינוקות": 21,
        "תינוקות-וילדים": 21,
        "בחזרה-לבית-ספר-ולגן": 21,
        "עולם-התינוקות": 21,
        "בעלי-חיים": 22,
        "ניקיון-הבית-וחד-פעמי": 24,
        "חשמל-לבית": 24,
    }

    # Map each product to category by extracting root from URL
    from urllib.parse import unquote
    for barcode, product in data['products'].items():
        url = product['url']
        parts = [p for p in url.split("/") if p]

        cat_id = 25  # default
        if len(parts) >= 3:
            root_encoded = parts[2]
            # Try to match against root_map
            for root_name, mapped_cat in root_map.items():
                import urllib.parse
                encoded = urllib.parse.quote(root_name.encode('utf-8'), safe='')
                if encoded == root_encoded or root_name.replace("-", "%2D") in url:
                    cat_id = mapped_cat
                    break

        categories[barcode] = (cat_id, "shufersal")

    return categories


def load_tiv_taam_categories():
    """Load Tiv Taam's category assignments."""
    with open("data/chain_taxonomies/tiv_taam.json") as f:
        data = json.load(f)

    root_map = {
        "ירקות ופירות": 1,
        "ירקות": 1,
        "פירות": 1,
        "קפואים": 2,
        "מקרר חלבי וביצים": 3,
        "בשר דגים ועוף": 4,
        "קטניות ודגנים": 5,
        "אורז ופסטה": 5,
        "בישול ואפיה": 6,
        "שמנים": 7,
        "חטיפים ועוגיות": 8,
        "לחם ומאפיה": 9,
        "משקאות": 10,
        "פארם ותינוקות": 13,
        "רחצה": 17,
        "ויטמינים ותוספים": 19,
        "ניקיון ומוצרי בית": 24,
    }

    categories = {}
    for barcode, product in data['products'].items():
        cat_id = 25  # default
        product_categories = product.get('categories', [])
        if product_categories:
            root = product_categories[0]
            for map_root, mapped_cat in root_map.items():
                if root.lower() in map_root.lower() or map_root.lower() in root.lower():
                    cat_id = mapped_cat
                    break

        categories[barcode] = (cat_id, "tiv_taam")

    return categories


def main():
    print("Merging taxonomies with conflict resolution...\n")

    # Load categories from both sources
    shufersal = load_shufersal_categories()
    tiv_taam = load_tiv_taam_categories()

    print(f"Shufersal: {len(shufersal)} categories")
    print(f"Tiv Taam: {len(tiv_taam)} categories")

    # Merge with conflict resolution
    merged = {}
    conflicts = 0

    # Add all Shufersal
    merged.update(shufersal)

    # Add Tiv Taam, handling conflicts
    for barcode, (cat_id, source) in tiv_taam.items():
        if barcode in merged:
            existing_cat, existing_source = merged[barcode]
            if existing_cat != cat_id:
                conflicts += 1
                # Keep Shufersal (authoritative)
        else:
            # Add new Tiv Taam-only product
            merged[barcode] = (cat_id, source)

    print(f"\nConflicts (same barcode, different categories): {conflicts}")
    print(f"Resolution: Kept Shufersal assignments\n")

    # Build output lines
    lines = []
    source_counts = defaultdict(int)
    for barcode in sorted(merged.keys()):
        cat_id, source = merged[barcode]
        lines.append(f"{barcode}\t{cat_id}\t{source}")
        source_counts[source] += 1

    # Write merged file
    with open("data/product_categories.tsv", "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Output statistics:")
    print(f"  Total mappings: {len(lines)}")
    print(f"  Unique barcodes: {len(merged)}")
    for source in sorted(source_counts.keys()):
        print(f"  From {source}: {source_counts[source]}")

    # Calculate coverage
    coverage = len(merged) / 237155 * 100
    print(f"\nCoverage: {len(merged)} products ({coverage:.1f}% of ~237k)")


if __name__ == "__main__":
    main()
