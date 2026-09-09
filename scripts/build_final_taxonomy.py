#!/usr/bin/env python3
"""
Build final data/categories.json and data/product_categories.tsv using
Shufersal's second_level field (the actual root category field).
"""

import json
from collections import defaultdict

def load_shufersal():
    """Load Shufersal raw data."""
    with open("data/chain_taxonomies/shufersal.json") as f:
        return json.load(f)

def load_tiv_taam():
    """Load Tiv Taam raw data."""
    with open("data/chain_taxonomies/tiv_taam.json") as f:
        return json.load(f)

def get_second_level_to_category_mapping():
    """Map second_level values (with spaces) to category IDs."""
    mapping = {
        # Produce (1)
        "פירות וירקות": 1,
        "ירקות, פירות ואגוזים": 1,
        # Frozen (2)
        "מזון מהמקרר ומהמקפיא": 2,
        "מן המקפיא": 2,
        # Dairy (3)
        "מוצרי חלב וביצים": 3,
        "מוצרי מקרר וביצים": 3,
        # Meat (4)
        "מוצרי בשר, עוף ודגים": 4,
        "סלטים ונקניקים": 4,
        # Cooking (5)
        "בישול אפיה ושימורים": 5,
        "בישול אפיה ממרחים ושימורים": 5,
        "ללא גלוטן": 5,
        "ראש השנה": 5,
        # Bread (6)
        "לחמים ומוצרי מאפה": 6,
        "לחם, קרקרים ופריכיות": 6,
        # Snacks (7)
        "חטיפים מתוקים ודגני בוקר": 7,
        "דגנים חטיפים מתוקים ומשקאות": 7,
        "פורים": 7,
        # Drinks (8)
        "יינות משקאות כהליים ותירוש": 8,
        # Vitamins (9)
        "ויטמינים ותוספי תזונה": 9,
        "מעושר בחלבון": 9,
        # Organic (10)
        "אורגני ובריאות": 10,
        # Hair Care (11)
        "טיפוח הגוף והשיער": 11,
        "פארם וטיפוח": 11,
        "טיפוח שיער מקצועי": 11,
        # Skincare (12)
        "טיפוח פנים": 12,
        "דרמוקוסמטיקה": 12,
        "הגנה מפני יתושים": 12,
        # Fragrance (13)
        "בישום": 13,
        # Makeup (14)
        "איפור": 14,
        "K BEAUTY": 14,
        # Bath (15)
        "רחצה והגיינה": 15,
        # Pharmacy (16)
        "בית מרקחת ואופטיקה": 16,
        "מוצרים בפיקוח": 16,
        # Kids (17)
        "ילדים ותינוקות": 17,
        "תינוקות וילדים": 17,
        "בחזרה לבית ספר ולגן": 17,
        "עולם התינוקות": 17,
        "משחקי שולחן וסל": 17,
        # Pets (18)
        "בעלי חיים": 18,
        # Household (19)
        "ניקיון הבית וחד פעמי": 19,
        "SUPER SALE": 19,
        "מה חדש": 19,
        "שדרת המותגים": 19,
        "רשימות": 19,
        "הכל לבית – במשלוח של הסופר": 19,
        "מוצרים לבית": 19,
        "מתנות לבית": 19,
        "מבצעי כרטיס אשראי": 19,
        "SBOX ומבצעי אשראי": 19,
        "מוצרים לשגרת חירום": 19,
        "מותגים מובילים": 19,
        "שוברים שיאים במבצעים": 19,
        "חורף חם עם שטראוס": 19,
        "מתחדשים עם BAUER": 19,
        "מתחדשים עם PILOT": 19,
        "מתנות ליום האשה": 19,
        "הכל לסוכה": 19,
        "DROP DAYS": 19,
        "עוברים דירה": 19,
        "בלעדי באונליין": 19,
        # Kitchen (20)
        "מטבח, אירוח וחד פעמי": 20,
        "חשמל למטבח": 20,
        " צלחות קורנינג": 20,
        "צלחות קורנינג": 20,
        # Bedroom (21)
        "חדר שינה": 21,
        # Bathroom (22)
        "אמבטיה וחדר כביסה": 22,
        # Living Room (23)
        "סלון ואווירה": 23,
        # Garden (24)
        "גינה וחוץ": 24,
        # Travel (25)
        "נסיעות קמפינג ופנאי": 25,
        # Electronics (26)
        "אלקטרוניקה וסלולר": 26,
        "חשמל לבית": 26,
        "DIY ולמשרד": 26,
        "Apple": 26,
        "GALA HOME": 26,
        # Clothing (27)
        "גברים": 27,
        "הלבשה תחתונה": 27,
        "גרביים וגרביונים": 27,
    }
    return mapping

def build_categories_json():
    """Build categories.json with 27 top-level categories."""

    top_cat_info = {
        1: ("פירות וירקות", "produce"),
        2: ("מוצרים קפואים", "frozen"),
        3: ("מוצרי חלב וביצים", "dairy"),
        4: ("בשר, עוף ודגים", "meat"),
        5: ("חומרי בישול", "cooking"),
        6: ("לחמים", "bread"),
        7: ("חטיפים וממתקים", "snacks"),
        8: ("משקאות", "drinks"),
        9: ("ויטמינים", "vitamins"),
        10: ("אורגני ובריאות", "organic"),
        11: ("שיער וטיפוח גוף", "hair"),
        12: ("טיפוח פנים", "skincare"),
        13: ("בישום", "fragrance"),
        14: ("איפור", "makeup"),
        15: ("רחצה והגיינה", "bath"),
        16: ("רוקח ותרופות", "pharmacy"),
        17: ("ילדים ותינוקות", "kids"),
        18: ("חיות מחמד", "pets"),
        19: ("בית וניקיון", "household"),
        20: ("מטבח", "kitchen"),
        21: ("חדר שינה", "bedroom"),
        22: ("אמבטיה וכביסה", "bathroom"),
        23: ("סלון", "living-room"),
        24: ("גינה וחוץ", "garden"),
        25: ("טיולים וקמפינג", "travel"),
        26: ("אלקטרוניקה", "electronics"),
        27: ("ביגוד", "clothing"),
    }

    categories = {}
    for cat_id, (name, slug) in top_cat_info.items():
        categories[cat_id] = {
            "name": name,
            "slug": slug,
            "sub_categories": {}
        }

    # Save categories.json
    output = {
        "version": "2.0",
        "structure": "2-level hierarchy (consolidated from Shufersal's actual structure)",
        "consolidated_from": "shufersal",
        "categories": categories
    }

    with open("data/categories.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"✓ Created data/categories.json with 27 top-level categories")
    return categories

def map_shufersal_to_categories():
    """Map Shufersal barcodes to category IDs using second_level field."""
    shufersal = load_shufersal()
    mapping = get_second_level_to_category_mapping()

    product_cats = {}
    unmapped_count = 0

    for barcode, product in shufersal['products'].items():
        second_level = product.get('second_level')

        if second_level:
            # Normalize: strip whitespace and look up
            normalized = second_level.strip()
            if normalized in mapping:
                cat_id = mapping[normalized]
                product_cats[barcode] = (cat_id, "shufersal")
            else:
                unmapped_count += 1
        else:
            unmapped_count += 1

    return product_cats, unmapped_count

def map_tiv_taam_to_categories():
    """Map Tiv Taam barcodes to category IDs."""
    tiv_taam = load_tiv_taam()

    tiv_to_cat = {
        "ירקות פירות": 1, "ירקות": 1, "פירות": 1,
        "קפואים": 2,
        "מקרר חלבי וביצים": 3,
        "בשר דגים ועוף": 4,
        "קטניות ודגנים": 5, "אורז ופסטה": 5, "בישול ואפיה": 5, "שמנים": 5,
        "חטיפים ועוגיות": 7,
        "לחם ומאפיה": 6,
        "משקאות": 8, "קפה": 8,
        "פארם ותינוקות": 17, "פארם": 16, "תינוקות": 17,
        "רחצה": 15,
        "ויטמינים ותוספים": 9,
        "ניקיון ומוצרי בית": 19, "ניקיון": 19,
    }

    product_cats = {}
    for barcode, product in tiv_taam['products'].items():
        categories = product.get('categories', [])
        if categories:
            root = categories[0]
            cat_id = tiv_to_cat.get(root)
            if cat_id:
                product_cats[barcode] = (cat_id, "tiv_taam")

    return product_cats

def main():
    print("Building final taxonomy and product mappings...\n")

    categories = build_categories_json()

    print("\nMapping Shufersal products...")
    shufersal_cats, unmapped = map_shufersal_to_categories()
    print(f"✓ Mapped {len(shufersal_cats)} Shufersal products")
    if unmapped > 0:
        print(f"  ({unmapped} products couldn't be mapped)")

    print("\nMapping Tiv Taam products...")
    tiv_taam_cats = map_tiv_taam_to_categories()
    print(f"✓ Mapped {len(tiv_taam_cats)} Tiv Taam products")

    # Merge
    all_products = {}
    all_products.update(shufersal_cats)
    all_products.update(tiv_taam_cats)

    # Save product_categories.tsv
    with open("data/product_categories.tsv", "w") as f:
        for barcode in sorted(all_products.keys()):
            cat_id, source = all_products[barcode]
            f.write(f"{barcode}\t{cat_id}\t{source}\n")

    print(f"\n✓ Created data/product_categories.tsv")
    print(f"\nCoverage summary:")
    print(f"  Shufersal:       {len(shufersal_cats)} products")
    print(f"  Tiv Taam:        {len(tiv_taam_cats)} products")
    print(f"  Total unique:    {len(all_products)} products")
    print(f"\n✓ Done! Taxonomy built from Shufersal's actual structure.")

if __name__ == "__main__":
    main()
