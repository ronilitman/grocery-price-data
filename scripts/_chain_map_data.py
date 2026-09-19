# -*- coding: utf-8 -*-
CHAIN_MAP = {
    "סופר-פארם": "7290172900007",
    "יילו": "7290644700005",
    # Confirmed by reading our own data: chain 7290027600007 (Shufersal) has
    # branches literally named "BE <place>" (e.g. "BE מידטאון", "BE דיזנגוף
    # סנטר", "BE חשמונאים") at the same addresses cheapersal lists under the
    # "בי" banner - Shufersal's own express/kiosk brand, written בי/BE.
    "בי": "7290027600007",
    "שופרסל אקספרס": "7290027600007",
    "שופרסל דיל": "7290027600007",
    "AM:PM": "7290492000005",
    "שופרסל שלי": "7290027600007",
    "מיני סופר אלונית": None,
    "ויקטורי": "7290696200003",
    "גוד פארם": "7290058197699",
    "רמי לוי": "7290058140886",
    "קרפור סיטי": "7290055700007",
    "בר כל טוב": "7290058160839-002",
    "יוחננוף": "7290803800003",
    "פרש מרקט": "7290876100000",
    "מעיין 2000": "7290058159628",
    "נתיב החסד": "7290058160839-006",
    "זול ובגדול": "7290058173198",
    "טיב טעם": "7290873255550",
    # Carrefour kept "Mega Ba'ir" branding on 5 of its own branches after the
    # Mega/Yeinot Bitan acquisition - confirmed by reading our own StoreName
    # field: chain 7290055700007 (Carrefour) has stores literally named
    # "בעיר חפץ חיים", "בעיר שדרות B", "בעיר פארק הירקון", "בעיר בת גלים",
    # "בעיר נווה עמל". cheapersal's much larger "מגה בעיר" banner (29 stores)
    # is the old, larger Mega Ba'ir network; only the ones at a matching
    # address are actually still Carrefour - the rest are simply stores we
    # do not carry (Mega Ba'ir locations Carrefour did not keep).
    "מגה בעיר": "7290055700007",
    "מחסני השוק בשבילך": "7290661400001",
    "קינג סטור": "7290058108879",
    "קרפור מרקט": "7290055700007",
    "קשת טעמים": "7290785400000",
    "טיב טעם בסיטי": "7290873255550",
    "יש חסד": "7290027600007",  # confirmed: our own Shufersal branches named "יש חסד <place>"
    "אושר עד": "7290103152017",
    "מחסני השוק בסיטי": "7290661400001",
    "שפע ברכת השם קרוב לבית": "7290058134977",
    "אלונית בקיבוץ ובמושב": None,
    "שוק העיר": "7290058148776",
    "סופר ספיר בשכונה": "7290058156016",
    "מחסני השוק": "7290661400001",
    "סופר ספיר": "7290058156016",
    "סופר ברקת": "7290875100001",
    "יוניברס": "7290027600007",  # confirmed: our own Shufersal branches named "יוניברס <place>"
    "יש בשכונה": None,
    "סטופ מרקט": "7290639000004",
    "שפע ברכת השם": "7290058134977",
    "שירה מרקט": "7290058160839-009",
    "ויקטורי סיטי": "7290696200003",
    "גוד מרקט": "7290027600007",  # confirmed: our own Shufersal branches named "גוד מרקט <place>"
    "פוליצר": "7291059100008",
    "קרפור היפר": "7290055700007",
    "סופר אלונית": None,
    "סאלח דבאח": "7290526500006",
    "סופר בית הפרי": None,
    "סופר דוש": "7290876100000",
    "סופר חביב": "7290876100000",  # confirmed: our own FreshMarket/SuperDosh branches named "חביב <place>"
    "נטו חיסכון": "7290058156016",  # confirmed: our own Super Sapir branches named "נטו חיסכון <place>"
    "ביתן מרקט": "7290055700007",  # Yeinot Bitan is this chain's own other brand (folder name YaynotBitanAndCarrefour); confirmed via "יינות ביתן טבעון" in our own data
    "ביתן מרקט בסיטי": "7290055700007",  # same Yeinot Bitan brand as above
    "שוק מהדרין": "7290055700007",  # confirmed: our own Carrefour/Yeinot Bitan data has a "שוק מהדרין הרצל חיפה" branch
    "שופרסל דיל EXTRA": "7290027600007",
    "היפר כהן": "7290455000004",
    "טיב טעם דלי": "7290873255550",
    "שערי רווחה": "7290027600007",  # confirmed: our own Shufersal branches named "שערי רווחה"
    "מחסני השוק מהדרין": "7290661400001",
    "שופרסל": "7290027600007",
    "סופר יודה": "7290058177776",
}
