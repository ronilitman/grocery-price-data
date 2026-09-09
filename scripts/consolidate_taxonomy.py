#!/usr/bin/env python3
"""
Consolidate Shufersal's 67 root categories into 12-20 top-level categories.

Strategy:
1. Load all 67 roots from Shufersal's actual hierarchy
2. Group them into 12-20 logical categories (using Shufersal's structure as guide)
3. Map departments to sub-categories (preserve ~300 from the original)
4. Build a 2-level hierarchy: top_category > sub_category
5. Generate data/categories.json with the consolidated structure
"""

import json
from collections import defaultdict
from pathlib import Path

def load_shufersal_structure():
    """Load Shufersal's full hierarchy."""
    with open("data/chain_taxonomies/shufersal.json") as f:
        data = json.load(f)
    return data

def build_hierarchy():
    """Build root -> departments -> categories mapping."""
    data = load_shufersal_structure()

    root_data = {}  # root -> {departments, product_count}
    root_products = defaultdict(int)
    dept_to_root = {}

    for path, count in data['category_paths'].items():
        parts = path.split(" > ")
        if len(parts) >= 2:
            root = parts[0]
            dept = parts[1]

            if root not in root_data:
                root_data[root] = {"departments": set(), "product_count": 0}

            root_data[root]["departments"].add(dept)
            root_data[root]["product_count"] += count
            dept_to_root[dept] = root
            root_products[root] += count

    return root_data, root_products, dept_to_root

def create_consolidation_mapping():
    """
    Define the mapping from 67 Shufersal roots to 12-20 consolidated categories.

    Returns: {root_name: (top_cat_id, top_cat_name, top_cat_slug)}
    """

    # The consolidation groups roots by their natural grocery/household categories
    # We aim for 16-18 top-level categories with 60-120 sub-categories

    mapping = {
        # GROCERY: Produce (1)
        "פירות-וירקות": (1, "פירות וירקות", "produce"),

        # GROCERY: Refrigerated & Frozen (2-3)
        "מזון-מהמקרר-ומהמקפיא": (2, "מוצרים קפואים", "frozen"),
        "קפואים": (2, "מוצרים קפואים", "frozen"),

        # GROCERY: Dairy & Eggs (3)
        "מוצרי-חלב-וביצים": (3, "מוצרי חלב וביצים", "dairy"),
        "מוצרי-מקרר-וביצים": (3, "מוצרי חלב וביצים", "dairy"),

        # GROCERY: Meat, Poultry & Fish (4)
        "מוצרי-בשר,-עוף-ודגים-": (4, "בשר, עוף ודגים", "meat"),
        "סלטים-ונקניקים": (4, "בשר, עוף ודגים", "meat"),

        # GROCERY: Cooking & Baking (5)
        "בישול-אפיה-ושימורים": (5, "חומרי בישול", "cooking"),
        "בישול-אפיה-ממרחים-ושימורים": (5, "חומרי בישול", "cooking"),
        "שימורים": (5, "חומרי בישול", "cooking"),
        "פסטה-אורז-קוסקוס-וקטניות": (5, "חומרי בישול", "cooking"),

        # GROCERY: Bread & Bakery (6)
        "לחמים-ומוצרי-מאפה": (6, "לחמים", "bread"),
        "לחם,-קרקרים-ופריכיות": (6, "לחמים", "bread"),

        # GROCERY: Snacks & Sweets (7)
        "חטיפים-מתוקים-ודגני-בוקר": (7, "חטיפים וממתקים", "snacks"),
        "דגנים-חטיפים-מתוקים-ומשקאות": (7, "חטיפים וממתקים", "snacks"),

        # GROCERY: Beverages (8)
        "יינות-משקאות-כהליים-ותירוש": (8, "משקאות", "drinks"),
        "משקאות": (8, "משקאות", "drinks"),
        "קפה-ותה": (8, "משקאות", "drinks"),

        # HEALTH: Vitamins & Supplements (9)
        "ויטמינים-ותוספי-תזונה": (9, "ויטמינים", "vitamins"),
        "ויטמינים-ותוספי-תזונה-": (9, "ויטמינים", "vitamins"),

        # HEALTH: Organic & Wellness (10)
        "אורגני-ובריאות": (10, "אורגני ובריאות", "organic"),

        # HEALTH & PERSONAL CARE: Hair Care (11)
        "פארם-וטיפוח": (11, "שיער וטיפוח גוף", "hair"),
        "טיפוח-הגוף-והשיער": (11, "שיער וטיפוח גוף", "hair"),
        "טיפוח-שיער-מקצועי": (11, "שיער וטיפוח גוף", "hair"),

        # PERSONAL CARE: Face Care (12)
        "טיפוח-פנים": (12, "טיפוח פנים", "skincare"),

        # PERSONAL CARE: Fragrance (13)
        "בישום": (13, "בישום", "fragrance"),

        # PERSONAL CARE: Makeup (14)
        "איפור-": (14, "איפור", "makeup"),

        # PERSONAL CARE: Bath & Hygiene (15)
        "רחצה-והגיינה": (15, "רחצה והגיינה", "bath"),

        # HEALTH: Pharmacy & Optometry (16)
        "בית-מרקחת-ואופטיקה": (16, "רוקח ותרופות", "pharmacy"),

        # FAMILY: Kids & Babies (17)
        "ילדים-ותינוקות": (17, "ילדים ותינוקות", "kids"),
        "תינוקות-וילדים": (17, "ילדים ותינוקות", "kids"),
        "בחזרה-לבית-ספר-ולגן": (17, "ילדים ותינוקות", "kids"),
        "עולם-התינוקות": (17, "ילדים ותינוקות", "kids"),

        # FAMILY: Pets (18)
        "בעלי-חיים": (18, "חיות מחמד", "pets"),

        # HOME: Cleaning & Household (19)
        "ניקיון-הבית-וחד-פעמי": (19, "בית וניקיון", "household"),
        "חד-פעמי-ונרות": (19, "בית וניקיון", "household"),
        "חומרי-ניקוי-לבית": (19, "בית וניקיון", "household"),

        # HOME: Kitchen (20)
        "מטבח,-אירוח-וחד-פעמי": (20, "מטבח", "kitchen"),
        "כלי-בישול-ואפיה": (20, "מטבח", "kitchen"),
        "ארגון-וטקסטיל-למטבח": (20, "מטבח", "kitchen"),
        "חשמל-למטבח": (20, "מטבח", "kitchen"),

        # HOME: Bedroom (21)
        "חדר-שינה": (21, "חדר שינה", "bedroom"),
        "ארונות-ואחסון": (21, "חדר שינה", "bedroom"),
        "מצעים": (21, "חדר שינה", "bedroom"),
        "כריות-וכיסויים": (21, "חדר שינה", "bedroom"),

        # HOME: Bathroom (22)
        "אמבטיה-וחדר-כביסה": (22, "אמבטיה וכביסה", "bathroom"),
        "ריהוט-ואביזרים-לאמבטיה": (22, "אמבטיה וכביסה", "bathroom"),
        "מגבות-וחלוקים": (22, "אמבטיה וכביסה", "bathroom"),

        # HOME: Living Room (23)
        "סלון-ואווירה": (23, "סלון", "living-room"),
        "כורסאות,-הדומים-ופופים": (23, "סלון", "living-room"),
        "נוי-ועיצוב-הבית": (23, "סלון", "living-room"),

        # HOME: Garden & Outdoor (24)
        "גינה-וחוץ": (24, "גינה וחוץ", "garden"),
        "ריהוט-גן": (24, "גינה וחוץ", "garden"),
        "גינון-והדברה": (24, "גינה וחוץ", "garden"),
        "ציוד-לגן-ולבית-ספר": (24, "גינה וחוץ", "garden"),

        # LEISURE: Travel & Camping (25)
        "נסיעות-קמפינג-ופנאי": (25, "טיולים וקמפינג", "travel"),
        "לקמפינג-ולים": (25, "טיולים וקמפינג", "travel"),

        # TECH & DIY (26)
        "אלקטרוניקה-וסלולר": (26, "אלקטרוניקה", "electronics"),
        "טלוויזיות,-אוזניות-ואודיו": (26, "אלקטרוניקה", "electronics"),
        "סלולר,-שעונים-,טאבלטים-ומצלמות": (26, "אלקטרוניקה", "electronics"),
        "חשמל-לבית": (26, "אלקטרוניקה", "electronics"),
        "מיזוג,-מוצרי-אקלים": (26, "אלקטרוניקה", "electronics"),
        "כביסה-,ייבוש-וגיהוץ": (26, "אלקטרוניקה", "electronics"),
        "DIY-ולמשרד-": (26, "אלקטרוניקה", "electronics"),
        "כלי-עבודה": (26, "אלקטרוניקה", "electronics"),
        "שולחנות-כתיבה-וספריות": (26, "אלקטרוניקה", "electronics"),
        "Apple-": (26, "אלקטרוניקה", "electronics"),
        "SAMSUNG": (26, "אלקטרוניקה", "electronics"),
        "GALA-HOME": (26, "אלקטרוניקה", "electronics"),

        # CLOTHING (27)
        "גברים": (27, "ביגוד", "clothing"),
        "הלבשה-תחתונה": (27, "ביגוד", "clothing"),
        "גרביים-וגרביונים": (27, "ביגוד", "clothing"),

        # DIET & SPECIALTY FOODS (map to existing categories)
        "ללא-גלוטן": (5, "חומרי בישול", "cooking"),  # Cooking ingredients
        "ללא-תוספת-סוכר": (7, "חטיפים וממתקים", "snacks"),  # Snacks
        "מעושר-בחלבון": (9, "ויטמינים", "vitamins"),  # Health supplements
        "ירקות,-פירות-ואגוזים": (1, "פירות וירקות", "produce"),  # Produce

        # HOLIDAY & SEASONAL (map to Grocery/Household)
        "ראש-השנה": (5, "חומרי בישול", "cooking"),  # Holiday foods/cooking
        "פורים": (7, "חטיפים וממתקים", "snacks"),  # Holiday treats
        "פותחים-את-הקיץ": (25, "טיולים וקמפינג", "travel"),  # Summer stuff

        # PROMOTIONAL & MISCELLANEOUS
        "SUPER-SALE": (19, "בית וניקיון", "household"),  # General store section
        "מה-חדש": (19, "בית וניקיון", "household"),  # New products
        "כוכבי-השבוע-": (19, "בית וניקיון", "household"),  # Weekly specials
        "שדרת-המותגים": (19, "בית וניקיון", "household"),  # Brand showcase
        "בלעדי-באונליין": (19, "בית וניקיון", "household"),  # Online exclusive
        "רשימות": (19, "בית וניקיון", "household"),  # Shopping lists/wishlist

        # SPECIAL CATEGORIES
        "הכל-לבית-–-במשלוח-של-הסופר-": (19, "בית וניקיון", "household"),  # Home delivery
        "מוצרים-לבית": (19, "בית וניקיון", "household"),  # Home products
        "מתנות-לבית": (19, "בית וניקיון", "household"),  # Gift items
        "-צלחות-קורנינג-": (20, "מטבח", "kitchen"),  # Kitchen brand
        "מעשנות-וטאבונים": (24, "גינה וחוץ", "garden"),  # Outdoor cooking
        "משחקי-שולחן-וסל": (17, "ילדים ותינוקות", "kids"),  # Children's games
        "אביזרי-בריאות-ובטיחות-לתינוקות": (17, "ילדים ותינוקות", "kids"),  # Baby health items
        "מן-המקפיא": (2, "מוצרים קפואים", "frozen"),  # Frozen (variant)
    }

    return mapping

def main():
    print("Consolidating Shufersal's 67 roots into top-level categories...\n")

    root_data, root_products, dept_to_root = build_hierarchy()
    mapping = create_consolidation_mapping()

    # Verify coverage
    print(f"Shufersal structure:")
    print(f"  Total roots: {len(root_data)}")
    print(f"  Total departments: {len(dept_to_root)}")
    print(f"  Total products: {sum(root_products.values())}")
    print()

    # Check coverage of mapping
    unmapped_roots = set(root_data.keys()) - set(mapping.keys())
    print(f"Consolidation coverage:")
    print(f"  Mapped roots: {len([r for r in root_data.keys() if r in mapping])}")
    print(f"  Unmapped roots: {len(unmapped_roots)}")
    if unmapped_roots:
        print(f"  Unmapped: {sorted(unmapped_roots)[:10]}")
    print()

    # Count consolidated categories
    consolidated_cats = set((cat_id, cat_name) for _, (cat_id, cat_name, _) in mapping.items())
    print(f"Consolidated categories: {len(consolidated_cats)}")
    for cat_id, cat_name in sorted(consolidated_cats):
        roots_in_cat = [r for r, (c_id, _, _) in mapping.items() if c_id == cat_id]
        print(f"  {cat_id:2}. {cat_name:30} ({len(roots_in_cat)} roots)")
    print()

    # Now build the hierarchy structure for data/categories.json
    categories = {}  # {cat_id: {name, slug, sub_categories}}
    sub_cat_id = 1000  # Start sub-category IDs at 1000

    for root_name, root_info in sorted(root_data.items(), key=lambda x: root_products[x[0]], reverse=True):
        if root_name not in mapping:
            print(f"WARNING: Unmapped root: {root_name}")
            continue

        top_cat_id, top_cat_name, top_cat_slug = mapping[root_name]

        # Ensure top category exists
        if top_cat_id not in categories:
            categories[top_cat_id] = {
                "name": top_cat_name,
                "slug": top_cat_slug,
                "sub_categories": {}
            }

        # Add departments as sub-categories
        for dept in sorted(root_info["departments"]):
            dept_slug = dept.replace(" ", "-").lower()
            sub_cat_key = f"{top_cat_id}.{sub_cat_id}"

            categories[top_cat_id]["sub_categories"][sub_cat_key] = {
                "name": dept,
                "slug": dept_slug,
                "root": root_name
            }
            sub_cat_id += 1

    # Save to data/categories.json
    output = {
        "version": "2.0",
        "consolidated_from": "shufersal",
        "structure": "2-level hierarchy (top_category > sub_category)",
        "categories": categories
    }

    with open("data/categories.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSaved consolidated taxonomy to data/categories.json")

    # Statistics
    total_subs = sum(len(cat["sub_categories"]) for cat in categories.values())
    print(f"Final structure:")
    print(f"  Top-level categories: {len(categories)}")
    print(f"  Sub-categories: {total_subs}")
    print(f"  Products mapped: {sum(root_products.values())}")

if __name__ == "__main__":
    main()
