"""Barcode -> category, taken from the chains' own trees rather than inferred.

Shufersal files every product it sells under a four-level path, and the last
level is the product itself:

    סופרמרקט > פירות-וירקות > פירות-וירקות > ירקות-טריים > מנגו-טרי-ארוז
    root       department     category       sub-category   THE PRODUCT

Keeping that last segment is what wrecked the first attempt: it turns 24,931
products into 21,647 "categories", one per product, and no tree can be built
from that. Dropping it leaves 262 real department/category pairs, which is a
list a person can read in one sitting - and that is the whole design here. The
judgement lives in MAP below, decided once; assigning a barcode is then a
dictionary lookup, so the same raw dump always produces the same TSV.

Anything the map does not cover is left out rather than guessed at. Seasonal
collections (`ראש-השנה`, `SUPER-SALE`, `רשימות`) are shelf placements, not
categories - a product only ever seen under one of those has told us nothing,
and belongs to the LLM pass with the rest of the catalogue.
"""

import json
import os
from collections import Counter
from urllib.parse import unquote

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

# (top slug, Hebrew name, [(sub slug, Hebrew name), ...])
#
# Grocery first and in aisle order, because that is what the app groups a
# shopping list by. Everything Shufersal sells that is not food or household
# lands in one `non-grocery` bucket: they run an electronics and homeware arm,
# it is 20% of their catalogue, and splitting it into nine furniture categories
# would bury the four categories a shopper actually scans.
TAXONOMY = [
    ("produce", "פירות וירקות", [
        ("fresh-veg", "ירקות טריים"),
        ("fresh-fruit", "פירות טריים"),
        ("herbs", "עשבי תיבול"),
        ("chilled-produce", "ירקות ופירות מצוננים"),
        ("nuts-dried", "פיצוחים ופירות יבשים"),
        ("organic-produce", "ירקות ופירות אורגניים"),
    ]),
    ("dairy-eggs", "מוצרי חלב וביצים", [
        ("milk-eggs", "חלב וביצים"),
        ("cheese-deli", "גבינות מעדנייה"),
        ("cheese-packaged", "גבינות ארוזות"),
        ("yogurt", "יוגורט ומשקאות יוגורט"),
        ("dairy-desserts", "מעדנים וקינוחים"),
        ("cream-butter", "חמאה ושמנת"),
    ]),
    ("meat-fish", "בשר, עוף ודגים", [
        ("poultry", "עוף והודו"),
        ("beef-lamb", "בשר בקר וכבש"),
        ("fish", "דגים"),
        ("grill-meat", "בשר על האש"),
        ("ready-meat", "בשר להכנה מהירה"),
    ]),
    ("deli", "מעדנייה וסלטים", [
        ("cured-smoked", "נקניקים ומעושנים"),
        ("salads-hummus", "סלטים וחומוס"),
        ("fish-deli", "דגים במעדנייה"),
    ]),
    ("bread-bakery", "לחם ומאפים", [
        ("bread", "לחם ולחמניות"),
        ("bakery-fresh", "מאפיית הסניף"),
        ("pastries", "מאפים ועוגות"),
    ]),
    ("frozen", "קפואים", [
        ("ice-cream", "גלידות"),
        ("frozen-meat-fish", "בשר ודגים קפואים"),
        ("frozen-produce", "ירקות ופירות קפואים"),
        ("frozen-dough", "בצקים ומאפים קפואים"),
        ("frozen-ready", "מזון מוכן קפוא"),
        ("frozen-desserts", "קינוחים קפואים"),
    ]),
    ("pantry", "שימורים ובישול", [
        ("canned", "שימורים"),
        ("pasta-rice-legumes", "פסטה, אורז וקטניות"),
        ("spices-basics", "מוצרי יסוד ותבלינים"),
        ("sauces", "רטבים ותוספות"),
        ("baking", "מוצרי אפייה"),
        ("spreads", "דבש, ריבות וממרחים"),
        ("soups", "מרקים ותבשילים"),
        ("oil-vinegar", "שמן וחומץ"),
        ("pantry-other", "מזווה - שונות"),
    ]),
    ("snacks-sweets", "חטיפים וממתקים", [
        ("candy", "ממתקים"),
        ("cakes-cookies", "עוגות ועוגיות ארוזות"),
        ("salty-snacks", "חטיפים מלוחים"),
        ("cereal", "דגני בוקר וחטיפי דגנים"),
        ("crackers", "פריכיות וקרקרים"),
    ]),
    ("beverages", "משקאות", [
        ("soft-drinks", "משקאות קלים"),
        ("water", "מים וסודה"),
        ("coffee-tea", "קפה ותה"),
        ("beer-energy", "בירה ומשקאות אנרגיה"),
        ("wine", "יין ואלכוהול"),
        ("spirits", "משקאות חריפים"),
    ]),
    ("health-diet", "בריאות ותזונה", [
        ("gluten-free", "ללא גלוטן"),
        ("diet-sugar-free", "דיאט וללא סוכר"),
        ("dairy-alt", "תחליפי חלב"),
        ("meat-alt", "תחליפי בשר"),
        ("health-pantry", "בישול ואפייה - בריאות"),
        ("health-snacks", "חטיפים ומתוקים - בריאות"),
        ("health-bread", "לחם וקרקרים - בריאות"),
        ("health-chilled", "בריאות במקרר"),
        ("health-frozen", "בריאות מהמקפיא"),
        ("vitamins", "ויטמינים"),
        ("supplements", "תוספי תזונה"),
        ("sports-nutrition", "תזונת ספורט"),
    ]),
    ("baby-kids", "תינוקות וילדים", [
        ("diapers-wipes", "חיתולים ומגבונים"),
        ("baby-food", "מזון לתינוקות"),
        ("baby-care", "טיפוח לתינוקות"),
        ("baby-accessories", "אביזרים לתינוקות"),
        ("kids-lunch", "ארוחת עשר"),
    ]),
    ("household", "ניקיון וחד פעמי", [
        ("cleaners", "חומרי ניקוי"),
        ("laundry", "כביסה וגיהוץ"),
        ("dish", "ניקוי כלים"),
        ("paper", "מוצרי נייר"),
        ("disposables", "חד פעמי ונרות"),
        ("cleaning-tools", "אביזרי ניקיון"),
        ("bags", "שקיות"),
        ("pest", "קוטלי חרקים"),
        ("eco", "מוצרים אקולוגיים"),
    ]),
    ("personal-care", "טיפוח ופארם", [
        ("hair", "טיפוח ועיצוב שיער"),
        ("bath-soap", "סבון, שמפו ומרכך"),
        ("oral", "היגיינת הפה"),
        ("deodorant", "דאודורנט"),
        ("shaving", "גילוח והסרת שיער"),
        ("hygiene", "מוצרי היגיינה"),
        ("body-face", "טיפוח גוף"),
        ("skincare", "טיפוח פנים"),
        ("sun", "הגנה מהשמש"),
        ("makeup", "איפור"),
        ("fragrance", "בישום"),
        ("pharmacy", "בית מרקחת"),
        ("medical", "מכשור רפואי"),
        ("optics", "אופטיקה"),
    ]),
    ("pets", "חיות מחמד", [
        ("pet-cat", "מוצרים לחתול"),
        ("pet-dog", "מוצרים לכלב"),
        ("pet-other", "חיות אחרות"),
    ]),
    ("non-grocery", "לא מזון", [
        ("ng-kitchen", "כלי מטבח ואירוח"),
        ("ng-electronics", "חשמל ואלקטרוניקה"),
        ("ng-home", "טקסטיל וריהוט"),
        ("ng-garden", "גינה וחוץ"),
        ("ng-outdoor", "נסיעות וקמפינג"),
        ("ng-toys", "צעצועים ומשחקים"),
        ("ng-clothing", "ביגוד והנעלה"),
        ("ng-stationery", "משרד ובית ספר"),
        ("ng-tobacco", "טבק וגפרורים"),
    ]),
]

# (department, category) -> our sub-category slug.
#
# Keyed on the pair rather than the leaf because that is the level at which
# Shufersal's names already mean something; the handful of leaves that cut
# across their own pair live in LEAF below.
MAP = {
    # ---- פירות וירקות
    ("פירות-וירקות", "פירות-וירקות"): "fresh-veg",
    ("פירות-וירקות", "פיצוחים-ופירות-יבשים"): "nuts-dried",
    ("פירות-וירקות", "ירקות-ופירות-מצוננים"): "chilled-produce",
    ("פירות-וירקות", "פירות-וירקות-אורגניים"): "organic-produce",
    # ---- חלב וביצים
    ("מוצרי-חלב-וביצים", "גבינות-מעדנייה"): "cheese-deli",
    ("מוצרי-חלב-וביצים", "מדף-הגבינות"): "cheese-packaged",
    ("מוצרי-חלב-וביצים", "יוגורט-ומשקאות-יוגורט"): "yogurt",
    ("מוצרי-חלב-וביצים", "מוצרי-חלב-וביצים"): "milk-eggs",
    ("מוצרי-חלב-וביצים", "מעדנים-וקינוחים"): "dairy-desserts",
    ("מוצרי-חלב-וביצים", "מוצרי-אפיה-ובישול"): "cream-butter",
    ("מוצרי-חלב-וביצים", "תחליפי-חלב-וטופו"): "dairy-alt",
    ("מוצרי-מקרר-וביצים", "תחליפי-גבינה-פסטרמה-שמנת-וסלטים"): "dairy-alt",
    ("מוצרי-מקרר-וביצים", "משקאות-ומעדנים-תחליפיים-טופו-ומיצים"): "dairy-alt",
    # ---- מהמקרר ומהמקפיא
    ("מזון-מהמקרר-ומהמקפיא", "גלידות"): "ice-cream",
    ("מזון-מהמקרר-ומהמקפיא", "מוצרי-בשר-עוף-ודגים-קפואים"): "frozen-meat-fish",
    ("מזון-מהמקרר-ומהמקפיא", "ירקות-ופירות-קפואים"): "frozen-produce",
    ("מזון-מהמקרר-ומהמקפיא", "בצקים-ומאפים-קפואים"): "frozen-dough",
    ("מזון-מהמקרר-ומהמקפיא", "אוכל-מוכן-להכנה-מהירה"): "frozen-ready",
    ("מזון-מהמקרר-ומהמקפיא", "קינוחים-ומנות-אחרונות"): "frozen-desserts",
    ("מזון-מהמקרר-ומהמקפיא", "תחליפי-בשר-קפוא"): "meat-alt",
    ("מזון-מהמקרר-ומהמקפיא", "קפואים-לנמנעים-מגלוטן"): "gluten-free",
    ("מזון-מהמקרר-ומהמקפיא", "-דגים-במעדניה-"): "fish-deli",
    ("מן-המקפיא", "תחליפי-בשר"): "meat-alt",
    # ---- בשר, עוף ודגים
    ("מוצרי-בשר,-עוף-ודגים-", "בשר-בקר-וכבש"): "beef-lamb",
    ("מוצרי-בשר,-עוף-ודגים-", "דגים"): "fish",
    ("מוצרי-בשר,-עוף-ודגים-", "מוצרי-עוף-והודו"): "poultry",
    ('מוצרי-בשר,-עוף-ודגים-', 'מוצרי-בשר-"על-האש"'): "grill-meat",
    ("מוצרי-בשר,-עוף-ודגים-", "מוצרים-להכנה-מהירה"): "ready-meat",
    # A barbecue is not a cut of meat; Shufersal shelves them together.
    ("מוצרי-בשר,-עוף-ודגים-", "מנגלים-ומוצרים-נילווים"): "ng-garden",
    # ---- לחם ומאפים
    ("לחמים-ומוצרי-מאפה", "לחם,-לחמניות-ופיתות"): "bread",
    ("לחמים-ומוצרי-מאפה", "לחמים,-לחמניות-ופיתות-מהמאפיה"): "bakery-fresh",
    ("לחמים-ומוצרי-מאפה", "מאפים-ועוגות-מהמאפיה"): "bakery-fresh",
    ("לחמים-ומוצרי-מאפה", "מאפים-מלוחים-ומתוקים"): "pastries",
    ("לחמים-ומוצרי-מאפה", "עוגות-ארוזות"): "cakes-cookies",
    ("לחמים-ומוצרי-מאפה", "לחמניות,-פיתות-ובגטים"): "bread",
    ("לחם,-קרקרים-ופריכיות", "אובלטים-ומצות"): "bread",
    ("לחם,-קרקרים-ופריכיות", "קרקרים"): "crackers",
    # ---- מעדנייה
    ("סלטים-ונקניקים", "נקניקים-ודגים-מעושנים"): "cured-smoked",
    ("סלטים-ונקניקים", "חומוס-וסלטים"): "salads-hummus",
    # ---- מזווה
    ("בישול-אפיה-ושימורים", "שימורים"): "canned",
    ("בישול-אפיה-ושימורים", "פסטה-אורז-קוסקוס-וקטניות"): "pasta-rice-legumes",
    ("בישול-אפיה-ושימורים", "מוצרי-יסוד-ותבלינים"): "spices-basics",
    ("בישול-אפיה-ושימורים", "רטבים-ותוספות"): "sauces",
    ("בישול-אפיה-ושימורים", "מוצרים-לאפיה-ובישול"): "baking",
    ("בישול-אפיה-ושימורים", "דבש-ריבות-וממרחים"): "spreads",
    ("בישול-אפיה-ושימורים", "מרקים-קרוטונים-ותבשילים"): "soups",
    ("בישול-אפיה-ושימורים", "שמן-חומץ-ומיץ-לימון"): "oil-vinegar",
    ("בישול-אפיה-ושימורים", "מיוחדים"): "pantry-other",
    ("בישול-אפיה-ממרחים-ושימורים", "מחיות-פרי"): "spreads",
    ("בישול-אפיה-ממרחים-ושימורים", "שמן-וחומץ"): "oil-vinegar",
    ("ללא-גלוטן", "פסטות-מרקים-ורטבים"): "gluten-free",
    ("ללא-גלוטן", "קפואים-ללא-גלוטן"): "gluten-free",
    ("ללא-גלוטן", "חטיפים-מתוקים-וממתקים"): "gluten-free",
    # ---- חטיפים וממתקים
    ("חטיפים-מתוקים-ודגני-בוקר", "ממתקים"): "candy",
    ("חטיפים-מתוקים-ודגני-בוקר", "עוגות-עוגיות-וופלים---ארוז"): "cakes-cookies",
    ("חטיפים-מתוקים-ודגני-בוקר", "חטיפים-מלוחים"): "salty-snacks",
    ("חטיפים-מתוקים-ודגני-בוקר", "דגנים-וחטיפי-דגנים"): "cereal",
    ("חטיפים-מתוקים-ודגני-בוקר", "פריכיות-וקרקרים"): "crackers",
    ("חטיפים-מתוקים-ודגני-בוקר", "חנות-הממתקים-של-עלית"): "candy",
    ("דגנים-חטיפים-מתוקים-ומשקאות", "מתוקים"): "health-snacks",
    ("דגנים-חטיפים-מתוקים-ומשקאות", "חטיפים"): "health-snacks",
    # ---- משקאות
    ("יינות-משקאות-כהליים-ותירוש", "משקאות-קלים"): "soft-drinks",
    ("יינות-משקאות-כהליים-ותירוש", "מים-וסודה"): "water",
    ("יינות-משקאות-כהליים-ותירוש", "קפה-ותה"): "coffee-tea",
    ("יינות-משקאות-כהליים-ותירוש", "קפסולות-ופולים-לאספרסו"): "coffee-tea",
    ("יינות-משקאות-כהליים-ותירוש", "בירה-ומשקאות-אנרגיה"): "beer-energy",
    ("יינות-משקאות-כהליים-ותירוש", "יינות-משקאות-כהליים-ותירוש"): "wine",
    ("יינות-משקאות-כהליים-ותירוש", "משקאות-חריפים"): "spirits",
    ("יינות-משקאות-כהליים-ותירוש", "סיגריות-וגפרורים"): "ng-tobacco",
    ("ראש-השנה", "יין-ואלכוהול"): "wine",
    ("ראש-השנה", "משקאות-"): "soft-drinks",
    ("ראש-השנה", "מאפים-ומתוקים"): "cakes-cookies",
    ("ראש-השנה", "חד-פעמי"): "disposables",
    # ---- בריאות ותזונה
    ("אורגני-ובריאות", "מוצרים-ללא-גלוטן"): "gluten-free",
    ("אורגני-ובריאות", "דיאט-וללא-סוכר"): "diet-sugar-free",
    ("אורגני-ובריאות", "מוצרים-לבישול-ואפיה"): "health-pantry",
    ("אורגני-ובריאות", "דגנים,-משקאות-ומתוקים"): "health-snacks",
    ("אורגני-ובריאות", "לחם-קרקרים-ופריכיות"): "health-bread",
    ("אורגני-ובריאות", "בריאות-במקרר"): "health-chilled",
    ("אורגני-ובריאות", "מן-המקפיא"): "health-frozen",
    ("אורגני-ובריאות", "פירות-וירקות-אורגני"): "organic-produce",
    ("ויטמינים-ותוספי-תזונה", "תוספי-תזונה"): "supplements",
    ("ויטמינים-ותוספי-תזונה", "ויטמינים"): "vitamins",
    ("ויטמינים-ותוספי-תזונה", "לספורטאים"): "sports-nutrition",
    ("ויטמינים-ותוספי-תזונה-", "תוספי-מזון-"): "supplements",
    # ---- ניקיון
    ("ניקיון-הבית-וחד-פעמי", "חומרי-ניקוי-לבית"): "cleaners",
    ("ניקיון-הבית-וחד-פעמי", "מיוחדים-בניקיון"): "cleaners",
    ("ניקיון-הבית-וחד-פעמי", "מוצרי-כביסה-וגיהוץ"): "laundry",
    ("ניקיון-הבית-וחד-פעמי", "ניקוי-כלים"): "dish",
    ("ניקיון-הבית-וחד-פעמי", "מוצרי-נייר"): "paper",
    ("ניקיון-הבית-וחד-פעמי", "חד-פעמי-ונרות"): "disposables",
    ("ניקיון-הבית-וחד-פעמי", "חד-פעמי-לבישול-ואפיה"): "disposables",
    ("ניקיון-הבית-וחד-פעמי", "אביזרי-ניקיון"): "cleaning-tools",
    ("ניקיון-הבית-וחד-פעמי", "שקיות"): "bags",
    ("ניקיון-הבית-וחד-פעמי", "קוטלי-חרקים"): "pest",
    ("ניקיון-הבית-וחד-פעמי", "מוצרים-אקולוגיים"): "eco",
    # ---- פארם, as the supermarket shelves it
    ("פארם-וטיפוח", "טיפוח-ועיצוב-שיער"): "hair",
    ("פארם-וטיפוח", "סבון-שמפו-ומרכך"): "bath-soap",
    ("פארם-וטיפוח", "הגיינת-הפה"): "oral",
    ("פארם-וטיפוח", "דאודורנט"): "deodorant",
    ("פארם-וטיפוח", "גילוח-והסרת-שיער"): "shaving",
    ("פארם-וטיפוח", "מוצרי-הגיינה"): "hygiene",
    ("פארם-וטיפוח", "טיפוח-גוף-ופנים"): "body-face",
    ("פארם-וטיפוח", "מוצרי-הגנה-מהשמש-והגוף"): "sun",
    ("פארם-וטיפוח", "איפור-ובישום"): "makeup",
    ("פארם-וטיפוח", "מוצרי-קורונה"): "pharmacy",
    ("פארם-וטיפוח", "לתינוק--טיפוח,-הלבשה-ואביזרים"): "baby-care",
    ("פארם-וטיפוח", "לתינוק-מזון-חיתולים-ומגבונים"): "baby-food",
    # ---- תינוקות וילדים
    ("תינוקות-וילדים", "אביזרים"): "baby-accessories",
    ("תינוקות-וילדים", "טיפוח-התינוק-והילד"): "baby-care",
    ("תינוקות-וילדים", "חיתולים-ומגבונים"): "diapers-wipes",
    ("תינוקות-וילדים", "מזון-תינוקות"): "baby-food",
    ("עולם-התינוקות", "הלבשה-ואביזרים-לתינוקות"): "ng-clothing",
    ("בחזרה-לבית-ספר-ולגן", "ארוחת-עשר"): "kids-lunch",
    ("בחזרה-לבית-ספר-ולגן", "ציוד-לבית-ספר"): "ng-stationery",
    # ---- חיות מחמד
    ("בעלי-חיים", "מוצרים-לחתול"): "pet-cat",
    ("בעלי-חיים", "מוצרים-לכלב"): "pet-dog",
    ("בעלי-חיים", "דגי-נוי,-ציפורים-ומכרסמים"): "pet-other",
}

# Departments whose every category maps the same way. Applied when the
# (department, category) pair is not in MAP.
DEPT = {
    # פארם-וקוסמטיקה - a whole root of its own
    "טיפוח-פנים": "skincare",
    "בישום": "fragrance",
    "איפור-": "makeup",
    "טיפוח-שיער-מקצועי": "hair",
    "גברים": "body-face",
    "טיפוח-הגוף-והשיער": "body-face",
    "רחצה-והגיינה": "bath-soap",
    "בית-מרקחת-ואופטיקה": "pharmacy",
    # הקניון - the homeware and electronics arm
    "מטבח,-אירוח-וחד-פעמי": "ng-kitchen",
    "חשמל-למטבח": "ng-electronics",
    "חשמל-לבית": "ng-electronics",
    "אלקטרוניקה-וסלולר": "ng-electronics",
    "חדר-שינה": "ng-home",
    "סלון-ואווירה": "ng-home",
    "אמבטיה-וחדר-כביסה": "ng-home",
    "מוצרים-לבית": "ng-home",
    "מתנות-לבית": "ng-home",
    "GALA-HOME": "ng-home",
    "הכל-לבית-–-במשלוח-של-הסופר-": "ng-home",
    "גינה-וחוץ": "ng-garden",
    "נסיעות-קמפינג-ופנאי": "ng-outdoor",
    "גרילים": "ng-garden",
    "ילדים-ותינוקות": "ng-toys",
    "DIY-ולמשרד-": "ng-stationery",
    "הלבשה": "ng-clothing",
}

# Leaves that contradict their own pair. Keyed (department, category, leaf).
LEAF = {
    # Seeds are shelved with the vegetables they grow into.
    ("פירות-וירקות", "פירות-וירקות", "זרעי-ירקות-"): "ng-garden",
    ("פירות-וירקות", "פירות-וירקות", "פירות-טריים"): "fresh-fruit",
    ("פירות-וירקות", "ירקות-ופירות-מצוננים", "עשבי-תיבול-טריים"): "herbs",
    ("פירות-וירקות", "ירקות-ופירות-מצוננים", "פטריות-ונבטים"): "fresh-veg",
    ("פירות-וירקות", "ירקות-ופירות-מצוננים", "רטבים-לסלט"): "sauces",
    ("פירות-וירקות", "פירות-וירקות-אורגניים", "פירות-אורגניים"): "organic-produce",
    ("פארם-וטיפוח", "לתינוק-מזון-חיתולים-ומגבונים", "חיתולים"): "diapers-wipes",
    ("פארם-וטיפוח", "לתינוק-מזון-חיתולים-ומגבונים", "מגבונים"): "diapers-wipes",
    ("פארם-וטיפוח", "לתינוק--טיפוח,-הלבשה-ואביזרים", "אביזרי-תינוקות-והנקה"): "baby-accessories",
    ("פארם-וטיפוח", "לתינוק--טיפוח,-הלבשה-ואביזרים", "מכשירים-וצעצועי-התפתחות"): "baby-accessories",
    ("פארם-וטיפוח", "לתינוק--טיפוח,-הלבשה-ואביזרים", "הגנה-מהשמש-לתינוק"): "sun",
    ("רחצה-והגיינה", "היגיינת-פה", ""): "oral",
    ("בית-מרקחת-ואופטיקה", "אופטיקה", ""): "optics",
    ("בית-מרקחת-ואופטיקה", "מכשור-רפואי-ואביזרים", ""): "medical",
    ("בית-מרקחת-ואופטיקה", "אורטופדיה", ""): "medical",
    ("טיפוח-הגוף-והשיער", "טיפוח-ועיצוב-שיער", ""): "hair",
    ("טיפוח-הגוף-והשיער", "אביזרים-לשיער", ""): "hair",
}

# Tiv Taam publishes its own three-level tree under different names, so it gets
# its own table. Their web shop is a narrow specialty range - about sixty pairs
# in all - and the barcodes come out of the image URL, which is the only reason
# these rows exist at all: their API carries no barcode field.
TIV = {
    "מקרר חלבי וביצים": {
        "מוצרי חלב": "milk-eggs", "ביצים": "milk-eggs",
        "מוצרים לאפיה ובישול": "cream-butter",
        "עוגות וקינוחים בקירור": "dairy-desserts",
    },
    "מוצרי מכולת": {
        "שימורים": "canned", "רטבים וחומצים": "sauces",
        "פסטות פתיתים ואטריות": "pasta-rice-legumes",
        "אורז קטניות ודגנים": "pasta-rice-legumes",
        "מרקים ותבשילים": "soups", "אפיה": "baking",
        "מוצרי יסוד": "spices-basics", "ממרחים": "spreads",
        "דגני בוקר": "cereal", "גרנולות ומוזלי": "cereal",
        "בישול מיוחד": "pantry-other",
    },
    "חטיפים ועוגיות": {
        "שוקולד וממתקים": "candy", "חטיפים": "salty-snacks",
        "עוגות ועוגיות": "cakes-cookies", "חטיפי בריאות": "health-snacks",
    },
    "משקאות": {
        "מוגזים": "soft-drinks", "מיצים ונקטרים": "soft-drinks",
        "תרכיזים": "soft-drinks", "תה קר": "soft-drinks", "מים": "water",
        "משקאות חמים": "coffee-tea", "תה וחליטות": "coffee-tea",
    },
    "קפואים": {
        "ירקות קפואים": "frozen-produce", "פירות קפואים": "frozen-produce",
        "מזון קפוא": "frozen-ready", "גלידות": "ice-cream",
        "בצקים": "frozen-dough", "תרכיזים קפואים": "frozen-desserts",
    },
    "פארם ותינוקות": {
        "רחצה": "bath-soap", "היגיינת הפה": "oral",
        "טיפוח גוף ופנים": "body-face", "לתינוק": "baby-care",
        "היגיינה נשית": "hygiene", "ציוד רפואי": "medical", "הגנה": "sun",
    },
    "ניקיון ומוצרי בית": {
        "חומרי ניקוי": "cleaners", "חד פעמי": "disposables",
        "מוצרי נייר": "paper",
    },
    "ויטמינים ותוספים": {
        "פארם טבעי": "supplements", "מזון אורגני": "health-pantry",
        "מזון ללא גלוטן": "gluten-free", "מוצרי קיטו": "diet-sugar-free",
        "מזון ללא תוספת סוכר": "diet-sugar-free",
        "משקאות ומעדנים צמחיים": "dairy-alt",
        "מוצרי סויה, טופו, סייטן ותחליפי גבינה": "dairy-alt",
    },
    "מעדנייה מובחרת": {
        "סלטים ארוזים": "salads-hummus", "ממרחים ומתבלים": "salads-hummus",
        "נקניקים ופסטרמות": "cured-smoked", "מזון מוכן": "salads-hummus",
    },
    "הבולונז'רי": {
        "פרכיות/קרקרים/צנימים": "crackers", "מצות": "bread",
        "לחמים וחלות": "bread", "פירורי לחם וקרוטונים": "spices-basics",
    },
    "מעדני הים": {
        "דגים": "fish", "דגים ומעדני ים": "fish-deli", "פירות ים": "fish",
    },
    "בוטיק יין ואלכוהול": {"בירות": "beer-energy", "אלכוהול": "spirits"},
    "האטליז": {"בשר קפוא": "frozen-meat-fish"},
    "הפיצוציה": {
        "פיצוחים, שקדים ואגוזים": "nuts-dried", "פירות יבשים": "nuts-dried",
        "סיגריות וגפרורים": "ng-tobacco", "חומרים למנגל": "ng-garden",
    },
    "כלי בית ופנאי": {
        "אביזרי חשמל ותאורה": "ng-electronics", "כלי בית ופנאי": "ng-home",
    },
    "קולינריה אסייתית": {
        "רטבים וממרחים": "sauces", "אורז ואטריות": "pasta-rice-legumes",
        "חטיפים אסיאתים": "salty-snacks", "משקאות": "soft-drinks",
        "אוכל מוכן": "frozen-ready",
    },
}

# Departments coarse enough to answer on their own, used when a product's URL
# gives no usable path. Non-grocery only, deliberately: a bare grocery
# department cannot pick between nine sub-categories, and those products are
# better served by the LLM pass, which at least sees the product's name.
FALLBACK = {
    "חשמל לבית": "ng-electronics", "חשמל למטבח": "ng-electronics",
    "אלקטרוניקה וסלולר": "ng-electronics", "Apple": "ng-electronics",
    "ילדים ותינוקות": "ng-toys", "צעצועים ומשחקים": "ng-toys",
    "גינה וחוץ": "ng-garden", "נסיעות קמפינג ופנאי": "ng-outdoor",
    "מטבח, אירוח וחד פעמי": "ng-kitchen", "חדר שינה": "ng-home",
    "סלון ואווירה": "ng-home", "אמבטיה וחדר כביסה": "ng-home",
}

# The container each path starts with. They are storefronts, not departments -
# `סופרמרקט`, the pharm arm, the homeware arm - so the department is the
# segment after them, except where a root has no departments of its own.
ROOTS = {
    "סופרמרקט": None, "פארם-וקוסמטיקה": None, "הקניון-הכל-לבית": None,
    "green-בריאות-וטבע": None, "מתחמים-מיוחדים-": None,
    "צעצועים-ומשחקים": "ng-toys",
    "ביגוד-ספורט-והלבשה": "ng-clothing",
    "טקסטיל,-עיצוב-וארגון-הבית": "ng-home",
    "קמפינג,-פנאי,-קורקינטים-ואופניים": "ng-outdoor",
    "בישול,-אפיה-וחד-פעמי": "ng-kitchen",
    "ילדים-וקטנטנים": "ng-toys",
    "פעילויות-מיוחדות": None,
    "חגיגת-מונדיאל": None,
}

# Category-shaped in the URL, but a shelf promotion rather than a category. A
# product seen only here has told us nothing, so it goes to the LLM pass -
# checked last, so a promotion that does name a real aisle still counts.
COLLECTIONS = {
    "ראש-השנה", "פורים", "SUPER-SALE", "מה-חדש", "רשימות", "פותחים-את-הקיץ",
    "בלעדי-באונליין", "שדרת-המותגים", "חגיגת-מונדיאל", "פעילויות-מיוחדות",
    "מתחמים-מיוחדים-", "כוכבי-השבוע-", "c",
}


def path_of(url):
    """The category path of a product, with the product's own segment removed.

    Shufersal's URLs are `/קטגוריות/<root>/<dept>/<cat>/<sub>/<product>/p/P_<sku>`,
    but not every product carries every level - a promotion landing page can be
    two segments deep. Returning what is there and letting the caller decide is
    safer than assuming a fixed depth, which is the mistake this replaces.
    """
    parts = [unquote(p) for p in url.split("/") if p]
    while parts and (parts[-1] == "p" or parts[-1].startswith("P_")):
        parts.pop()
    if parts and parts[0] == "קטגוריות":
        parts = parts[1:]
    return parts[:-1]


def resolve(parts):
    """A category path -> our sub-category slug, or None to defer it.

    Most specific first: a leaf that contradicts its pair, then the pair, then
    the department, then the root for the storefronts that have no departments
    of their own. Only when all of those are silent does a promotion collection
    settle it, so `ראש-השנה > יין-ואלכוהול` is still wine.
    """
    parts = [p for p in parts if p]
    if not parts:
        return None
    root = parts[0] if parts[0] in ROOTS else ""
    body = parts[1:] if root else parts
    dept = body[0] if len(body) > 0 else ""
    cat = body[1] if len(body) > 1 else ""
    leaf = body[2] if len(body) > 2 else ""
    return (LEAF.get((dept, cat, leaf))
            or LEAF.get((dept, cat, ""))
            or MAP.get((dept, cat))
            or DEPT.get(dept)
            or DEPT.get(cat)
            or (ROOTS.get(root) if root else None))


def build_taxonomy():
    """Flat [{id, slug, name_he, parent_id}], ids assigned once and stable."""
    rows, sub_id = [], {}
    next_id = 1
    for top_slug, top_name, subs in TAXONOMY:
        top_id = next_id
        next_id += 1
        rows.append({"id": top_id, "slug": top_slug, "name_he": top_name,
                     "parent_id": None})
        for slug, name in subs:
            rows.append({"id": next_id, "slug": slug, "name_he": name,
                         "parent_id": top_id})
            sub_id[slug] = next_id
            next_id += 1
    return rows, sub_id


def shufersal(sub_id):
    with open(os.path.join(DATA, "chain_taxonomies", "shufersal.json"),
              encoding="utf-8") as handle:
        raw = json.load(handle)["products"]
    out, misses = {}, Counter()
    for barcode, product in raw.items():
        parts = path_of(product.get("url", ""))
        slug = resolve(parts)
        if slug is None:
            slug = FALLBACK.get((product.get("second_level") or "").strip())
        if slug is None:
            misses[" > ".join(parts[:3])] += 1
            continue
        out[barcode] = sub_id[slug]
    return out, misses, len(raw)


def tiv_taam(sub_id):
    """Tiv Taam's own three-level path, mapped through the same table.

    Their web shop carries a narrow specialty range, so this adds hundreds of
    rows rather than thousands - but the barcodes come from the image URL, so
    they are real EANs and cost nothing to include.
    """
    path = os.path.join(DATA, "chain_taxonomies", "tiv_taam.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)["products"]
    out = {}
    for barcode, product in raw.items():
        cats = product.get("categories") or []
        if len(cats) < 2:
            continue
        slug = TIV.get(cats[0], {}).get(cats[1])
        if slug:
            out[barcode] = sub_id[slug]
    return out



def other_chains(sub_id):
    """The five chains added after Shufersal, each through its own table.

    Every one publishes a `department > category > ...` tree of its own, so
    there is no chain-to-chain translation anywhere: each table points
    straight at our slugs. A key with no destination is left for the LLM pass
    rather than guessed at.
    """
    import chain_maps
    out, misses = {}, Counter()
    for chain, table in chain_maps.BY_CHAIN.items():
        path = os.path.join(DATA, "chain_taxonomies", f"{chain}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)["products"]
        for barcode, product in raw.items():
            parts = [x.strip() for x in (product.get("path") or "").split(">")]
            key = ">".join(parts[:2])
            slug = table.get(key)
            if slug is None:
                misses[(chain, key)] += 1
                continue
            out.setdefault(barcode, (sub_id[slug], chain))
    return out, misses


def audit(sub_id):
    """Every table key must name a real slug and still appear in some dump.

    A table shared by several chains - Carrefour, Keshet Teamim and Yenot
    Bitan all run the same platform - is checked against the union of their
    dumps, because each chain carries only part of the shared tree.
    """
    import chain_maps
    live = {}
    for chain, table in chain_maps.BY_CHAIN.items():
        path = os.path.join(DATA, "chain_taxonomies", f"{chain}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)["products"]
        seen = live.setdefault(id(table), set())
        seen.update(">".join(x.strip() for x in (p.get("path") or "").split(">")[:2])
                    for p in raw.values())
    problems = []
    for table in {id(t): t for t in chain_maps.BY_CHAIN.values()}.values():
        seen = live.get(id(table), set())
        for key, slug in table.items():
            if slug not in sub_id:
                problems.append(f"{key!r} -> unknown slug {slug!r}")
            elif seen and key not in seen:
                problems.append(f"{key!r} no longer in any dump")
    return problems


def main():
    rows, sub_id = build_taxonomy()
    with open(os.path.join(DATA, "categories.json"), "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=1)
        handle.write("\n")

    for problem in audit(sub_id):
        print("[audit]", problem)

    shuf, misses, scraped = shufersal(sub_id)
    tiv = tiv_taam(sub_id)
    others, other_misses = other_chains(sub_id)
    # Shufersal wins, then the other chains in the order they were added, and
    # Tiv Taam last - it is the narrowest catalogue and shelves a specialty
    # range, so its placements are the least representative.
    merged = {b: (c, "tiv_taam") for b, c in tiv.items()}
    for barcode, (category_id, chain) in others.items():
        merged.setdefault(barcode, (category_id, chain))
    merged.update({b: (c, "shufersal") for b, c in shuf.items()})

    out = os.path.join(DATA, "product_categories.tsv")
    with open(out, "w", encoding="utf-8") as handle:
        for barcode in sorted(merged):
            category_id, source = merged[barcode]
            handle.write(f"{barcode}\t{category_id}\t{source}\n")

    by_top = Counter()
    top_of = {r["id"]: r for r in rows}
    for category_id, _ in merged.values():
        by_top[top_of[top_of[category_id]["parent_id"]]["name_he"]] += 1
    print(f"[categories] {len(rows)} rows "
          f"({sum(1 for r in rows if r['parent_id'] is None)} top-level)")
    print(f"[products]   {len(merged):,} barcodes placed across "
          f"{len(set(s for _, s in merged.values()))} chains")
    for name, count in by_top.most_common():
        print(f"    {count:6,}  {name}")
    by_source = Counter(src for _, src in merged.values())
    print("[sources]")
    for source, count in by_source.most_common():
        print(f"    {count:6,}  {source}")
    unmapped = sum(misses.values()) + sum(other_misses.values())
    print(f"[unmapped]   {unmapped:,} products - left for the LLM pass")
    for path, count in misses.most_common(5):
        print(f"    {count:6,}  shufersal  {path or '(no path)'}")
    for (chain, key), count in other_misses.most_common(8):
        print(f"    {count:6,}  {chain}  {key or '(no path)'}")


if __name__ == "__main__":
    main()
