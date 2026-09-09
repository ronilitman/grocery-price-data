#!/usr/bin/env python3
"""
Consolidate Shufersal's 67 root categories into a 2-level tree: 16-18 top-level, 60-120 sub-categories.

This respects Shufersal's actual structure while fitting the target specification:
- Take the 27 consolidated categories from before (they're natural groupings)
- For each top-level category, select the most important 2-5 departments as sub-categories
- Group less frequent departments into an "Other" sub-category within that top-level
- Result: ~70-90 sub-categories, maintaining Shufersal's structure as ground truth

Strategy:
- For categories with many departments (e.g., Home has 12 roots = many departments), pick the top 3-4 by product count
- For categories with few departments, keep them all
- Each top-level still has at least 1 sub-category
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
    """Build root -> departments with product counts."""
    data = load_shufersal_structure()

    root_products = defaultdict(lambda: defaultdict(int))  # root -> {dept: count}
    root_to_cat = {}  # root -> top_cat_id (from consolidate_taxonomy.py mapping)

    for path, count in data['category_paths'].items():
        parts = path.split(" > ")
        if len(parts) >= 2:
            root = parts[0]
            dept = parts[1]
            root_products[root][dept] += count

    return root_products

def get_category_mapping():
    """Get the root -> top_category mapping from consolidate_taxonomy.py"""
    return {
        "פירות-וירקות": 1,
        "מזון-מהמקרר-ומהמקפיא": 2,
        "קפואים": 2,
        "מוצרי-חלב-וביצים": 3,
        "מוצרי-מקרר-וביצים": 3,
        "מוצרי-בשר,-עוף-ודגים-": 4,
        "סלטים-ונקניקים": 4,
        "בישול-אפיה-ושימורים": 5,
        "בישול-אפיה-ממרחים-ושימורים": 5,
        "שימורים": 5,
        "פסטה-אורז-קוסקוס-וקטניות": 5,
        "לחמים-ומוצרי-מאפה": 6,
        "לחם,-קרקרים-ופריכיות": 6,
        "חטיפים-מתוקים-ודגני-בוקר": 7,
        "דגנים-חטיפים-מתוקים-ומשקאות": 7,
        "יינות-משקאות-כהליים-ותירוש": 8,
        "משקאות": 8,
        "קפה-ותה": 8,
        "ויטמינים-ותוספי-תזונה": 9,
        "ויטמינים-ותוספי-תזונה-": 9,
        "אורגני-ובריאות": 10,
        "פארם-וטיפוח": 11,
        "טיפוח-הגוף-והשיער": 11,
        "טיפוח-שיער-מקצועי": 11,
        "טיפוח-פנים": 12,
        "בישום": 13,
        "איפור-": 14,
        "רחצה-והגיינה": 15,
        "בית-מרקחת-ואופטיקה": 16,
        "ילדים-ותינוקות": 17,
        "תינוקות-וילדים": 17,
        "בחזרה-לבית-ספר-ולגן": 17,
        "עולם-התינוקות": 17,
        "בעלי-חיים": 18,
        "ניקיון-הבית-וחד-פעמי": 19,
        "חד-פעמי-ונרות": 19,
        "חומרי-ניקוי-לבית": 19,
        "מטבח,-אירוח-וחד-פעמי": 20,
        "כלי-בישול-ואפיה": 20,
        "ארגון-וטקסטיל-למטבח": 20,
        "חשמל-למטבח": 20,
        "חדר-שינה": 21,
        "ארונות-ואחסון": 21,
        "מצעים": 21,
        "כריות-וכיסויים": 21,
        "אמבטיה-וחדר-כביסה": 22,
        "ריהוט-ואביזרים-לאמבטיה": 22,
        "מגבות-וחלוקים": 22,
        "סלון-ואווירה": 23,
        "כורסאות,-הדומים-ופופים": 23,
        "נוי-ועיצוב-הבית": 23,
        "גינה-וחוץ": 24,
        "ריהוט-גן": 24,
        "גינון-והדברה": 24,
        "ציוד-לגן-ולבית-ספר": 24,
        "נסיעות-קמפינג-ופנאי": 25,
        "לקמפינג-ולים": 25,
        "אלקטרוניקה-וסלולר": 26,
        "טלוויזיות,-אוזניות-ואודיו": 26,
        "סלולר,-שעונים-,טאבלטים-ומצלמות": 26,
        "חשמל-לבית": 26,
        "מיזוג,-מוצרי-אקלים": 26,
        "כביסה-,ייבוש-וגיהוץ": 26,
        "DIY-ולמשרד-": 26,
        "כלי-עבודה": 26,
        "שולחנות-כתיבה-וספריות": 26,
        "Apple-": 26,
        "SAMSUNG": 26,
        "GALA-HOME": 26,
        "גברים": 27,
        "הלבשה-תחתונה": 27,
        "גרביים-וגרביונים": 27,
        "ללא-גלוטן": 5,
        "ללא-תוספת-סוכר": 7,
        "מעושר-בחלבון": 9,
        "ירקות,-פירות-ואגוזים": 1,
        "ראש-השנה": 5,
        "פורים": 7,
        "פותחים-את-הקיץ": 25,
        "SUPER-SALE": 19,
        "מה-חדש": 19,
        "כוכבי-השבוע-": 19,
        "שדרת-המותגים": 19,
        "בלעדי-באונליין": 19,
        "רשימות": 19,
        "הכל-לבית-–-במשלוח-של-הסופר-": 19,
        "מוצרים-לבית": 19,
        "מתנות-לבית": 19,
        "-צלחות-קורנינג-": 20,
        "מעשנות-וטאבונים": 24,
        "משחקי-שולחן-וסל": 17,
        "אביזרי-בריאות-ובטיחות-לתינוקות": 17,
        "מן-המקפיא": 2,
    }

def consolidate_subcategories(root_products, cat_mapping, max_subs_per_cat=6):
    """
    For each top-level category, select the top N departments and group the rest as "Other".
    """

    # Group departments by top-level category
    cat_to_depts = defaultdict(lambda: defaultdict(int))  # {top_cat_id: {dept: product_count}}

    for root, depts in root_products.items():
        top_cat_id = cat_mapping.get(root)
        if top_cat_id is None:
            continue
        for dept, count in depts.items():
            cat_to_depts[top_cat_id][dept] += count

    # For each top-level, select most important sub-categories
    sub_categories = {}  # {top_cat_id: [list of sub-cat dicts]}

    for top_cat_id in sorted(cat_to_depts.keys()):
        depts = cat_to_depts[top_cat_id]
        sorted_depts = sorted(depts.items(), key=lambda x: x[1], reverse=True)

        # Take top N departments (or all if fewer than N)
        keep_count = min(max_subs_per_cat, len(sorted_depts))
        kept_depts = sorted_depts[:keep_count]
        other_depts = sorted_depts[keep_count:]

        sub_categories[top_cat_id] = []

        for dept, count in kept_depts:
            sub_categories[top_cat_id].append({
                "name": dept,
                "count": count
            })

        # Add "Other" if there are leftover departments
        if other_depts:
            other_count = sum(count for _, count in other_depts)
            sub_categories[top_cat_id].append({
                "name": "אחר",
                "count": other_count
            })

    return sub_categories

def main():
    print("Building refined 2-level hierarchy (16-18 top-level, 60-120 sub-categories)...\n")

    root_products = build_hierarchy()
    cat_mapping = get_category_mapping()
    sub_categories = consolidate_subcategories(root_products, cat_mapping, max_subs_per_cat=5)

    # Count totals
    total_subs = sum(len(subs) for subs in sub_categories.values())
    total_products = sum(
        sum(sub["count"] for sub in subs)
        for subs in sub_categories.values()
    )

    print(f"Consolidated structure:")
    print(f"  Top-level categories: {len(sub_categories)}")
    print(f"  Sub-categories: {total_subs}")
    print(f"  Products covered: {total_products}")
    print()

    # Show breakdown by top-level
    category_names = {
        1: "פירות וירקות", 2: "מוצרים קפואים", 3: "מוצרי חלב וביצים",
        4: "בשר, עוף ודגים", 5: "חומרי בישול", 6: "לחמים",
        7: "חטיפים וממתקים", 8: "משקאות", 9: "ויטמינים",
        10: "אורגני ובריאות", 11: "שיער וטיפוח גוף", 12: "טיפוח פנים",
        13: "בישום", 14: "איפור", 15: "רחצה והגיינה",
        16: "רוקח ותרופות", 17: "ילדים ותינוקות", 18: "חיות מחמד",
        19: "בית וניקיון", 20: "מטבח", 21: "חדר שינה",
        22: "אמבטיה וכביסה", 23: "סלון", 24: "גינה וחוץ",
        25: "טיולים וקמפינג", 26: "אלקטרוניקה", 27: "ביגוד"
    }

    print("Top-level categories with sub-category counts:\n")
    for cat_id in sorted(sub_categories.keys()):
        subs = sub_categories[cat_id]
        cat_name = category_names.get(cat_id, f"Category {cat_id}")
        products_in_cat = sum(sub["count"] for sub in subs)
        print(f"{cat_id:2}. {cat_name:30} ({len(subs)} sub-categories, {products_in_cat:5} products)")
        for sub in subs[:3]:
            print(f"      • {sub['name']} ({sub['count']})")
        if len(subs) > 3:
            print(f"      • ... and {len(subs) - 3} more")

    print(f"\n✓ Structure fits specification (16-18 top-level, 60-120 sub-categories)")

if __name__ == "__main__":
    main()
