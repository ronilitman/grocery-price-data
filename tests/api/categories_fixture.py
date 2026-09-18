"""A small prices.db + categories.json/product_categories.tsv fixture for
KAN-17's /categories and /categories/{id}/products tests.

Deliberately its own fixture, not tests/api/catalog_fixture.py's: that one's
fixture barcodes are never in the REAL data/product_categories.tsv (which
build_app_db.build() reads by default), so nothing in it is ever
categorised. This module builds its own small categories.json/
product_categories.tsv (passed explicitly to build_app_db.build()) so a
category listing has real rows to walk.

Layout, one barcode/case per DoD line item:

  TOP_A (id 100)
    SUB_A1 (id 101) - APPLE, BANANA (on a live club deal), CARROT (weighed),
                       DATE, EGGPLANT, TOMATO_RL and TOMATO_SF (two chains'
                       own barcodes for the same loose-produce item,
                       individually categorised - 7 categorised barcodes,
                       7 browse rows).
    SUB_A2 (id 102) - no product points here: an empty aisle, hidden by the
                       API's "omit empty tiles" rule.
  TOP_B (id 200)
    SUB_B1 (id 201) - also empty, so the whole TOP_B department is hidden
                       too (its only child has count 0).

  ZUCCHINI - priced, has a name, but NOT in product_categories.tsv at all:
             category_id stays NULL, and it must never appear in any
             /categories response (count or listing).
"""
import os
import sys

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
_SCRIPTS_DIR = os.path.join(_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

TODAY = "2026-09-14"

APPLE = "3000000000001"
BANANA = "3000000000002"       # on a live club deal -> on_deal True
CARROT = "3000000000003"       # weighed
DATE = "3000000000004"
EGGPLANT = "3000000000005"
TOMATO_RL = "3000000000006"    # RAMI_LEVY's own barcode for "עגבניה"
TOMATO_SF = "3000000000007"    # SHUFERSAL's own barcode for "עגבניה"
ZUCCHINI = "3000000000099"     # priced, but never categorised

TOP_A, SUB_A1, SUB_A2 = 100, 101, 102
TOP_B, SUB_B1 = 200, 201


def _module(name):
    import importlib
    return importlib.import_module(name)


def build_prices_db(path):
    merge_db = _module("merge_db")
    import sqlite3
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.executemany("INSERT INTO chains VALUES (?,?,?)", [
        ("RAMI_LEVY", "Rami Levy", None),
        ("SHUFERSAL", "Shufersal", None),
    ])
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", "10", None, "Rami Levy A", "3000", None, "St 1", 100, None),
            ("RAMI_LEVY", "20", None, "Rami Levy B", "3000", None, "St 2", 100, None),
            ("SHUFERSAL", "1", None, "Shufersal A", "5000", None, "St 3", 100, None),
        ],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [
            (APPLE, "Apple", "Acme", "1", 1.0, None, 0),
            (BANANA, "Banana", "Acme", "1", 1.0, None, 0),
            (CARROT, "Carrot", "Acme", "kg", 1.0, "kg", 1),
            (DATE, "Date", "Acme", "1", 1.0, None, 0),
            (EGGPLANT, "Eggplant", "Acme", "1", 1.0, None, 0),
            (TOMATO_RL, "Tomato", "Acme", "1", 1.0, "kg", 1),
            (TOMATO_SF, "Tomato", "Acme", "1", 1.0, "kg", 1),
            (ZUCCHINI, "Zucchini", "Acme", "1", 1.0, None, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO chain_prices VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", APPLE, 4.90, 2),
            ("RAMI_LEVY", BANANA, 6.90, 2),
            ("RAMI_LEVY", CARROT, 5.90, 2),
            ("RAMI_LEVY", DATE, 12.90, 2),
            ("RAMI_LEVY", EGGPLANT, 8.90, 2),
            ("RAMI_LEVY", TOMATO_RL, 6.90, 2),
            ("SHUFERSAL", TOMATO_SF, 7.90, 1),
            ("RAMI_LEVY", ZUCCHINI, 3.90, 2),
        ],
    )
    conn.executemany(
        "INSERT INTO chain_products VALUES (?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", TOMATO_RL, "עגבניה", "1", "kg", 1),
            ("SHUFERSAL", TOMATO_SF, "עגבניה", "1", "kg", 1),
        ],
    )
    # A live, unexpired club offer on BANANA, cheaper than its baseline -
    # the one item in this fixture with discount_pct > 0 in `deals`.
    conn.executemany(
        "INSERT INTO promo_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "RAMI_LEVY", "P1", BANANA, 1, 0, 1, 4.90, 4.90,
             "club price on bananas", "2026-09-01", "2026-12-31"),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_stores VALUES (?,?)",
        [(1, "10"), (1, "20")],
    )
    conn.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [
            ("built_at", f"{TODAY}T02:00:00Z"),
            ("chain_as_of", "{}"),
        ],
    )
    conn.commit()
    conn.close()


def write_category_files(dir_path):
    """This fixture's own small categories.json/product_categories.tsv -
    NOT the real 116-row/38,555-row checked-in files. Returns their paths."""
    import json

    categories_json = os.path.join(dir_path, "categories.json")
    with open(categories_json, "w", encoding="utf-8") as handle:
        json.dump([
            {"id": TOP_A, "slug": "top-a", "name_he": "Top A", "parent_id": None},
            {"id": SUB_A1, "slug": "sub-a1", "name_he": "Sub A1", "parent_id": TOP_A},
            {"id": SUB_A2, "slug": "sub-a2", "name_he": "Sub A2", "parent_id": TOP_A},
            {"id": TOP_B, "slug": "top-b", "name_he": "Top B", "parent_id": None},
            {"id": SUB_B1, "slug": "sub-b1", "name_he": "Sub B1", "parent_id": TOP_B},
        ], handle, ensure_ascii=False)

    tsv_path = os.path.join(dir_path, "product_categories.tsv")
    with open(tsv_path, "w", encoding="utf-8") as handle:
        for barcode in (APPLE, BANANA, CARROT, DATE, EGGPLANT, TOMATO_RL, TOMATO_SF):
            handle.write(f"{barcode}\t{SUB_A1}\tRAMI_LEVY\n")
        # ZUCCHINI deliberately absent - category_id must stay NULL for it.
    return categories_json, tsv_path


def build_app_db_from_fixture(prices_db_path, out_path, tmp_path):
    """Run build_app_db.build() against the fixture, pointed at THIS
    module's own categories files rather than the real checked-in ones
    (which know nothing about this fixture's barcodes)."""
    build_app_db = _module("build_app_db")
    dir_path = str(tmp_path)
    categories_json, categories_tsv = write_category_files(dir_path)

    counts, _size, _elapsed, _merged_total = build_app_db.build(
        prices_db_path, out_path,
        categories_json=categories_json, categories_tsv=categories_tsv)
    return counts
