"""data/product_names.json and its loader (KAN-29).

A name decided once by a human is meant to win over whichever chain's file
build_app_db would otherwise have picked, and to fall back to today's
behaviour untouched when a barcode has not been reviewed yet - the whole
point being that partial coverage of the table can never break a product
that is not in it. These are unit tests: build_app_db.copy_products and
load_product_names are exercised directly against tiny fixtures, not through
the full build() pipeline that tests/app_db/test_build_app_db.py already
covers.

The table is plain JSON (json.load in one call, not JSONL) precisely so a
name containing a `"` or an embedded newline round-trips correctly without
hand-rolled quoting rules - the encoder and decoder agree about quoting on
their own. See tests below for both cases.
"""

import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402

REAL_NAMES_JSON = build_app_db.PRODUCT_NAMES_JSON


def _write_names_json(path, products, note="test fixture", source_build="0"):
    path.write_text(
        json.dumps(
            {"note": note, "source_build": source_build, "products": products},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _src_db(tmp_path, rows):
    """A standalone prices.db-shaped `products` table, to ATTACH as `src`."""
    path = str(tmp_path / "src.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE products(barcode TEXT PRIMARY KEY, name TEXT, "
        "manufacturer TEXT, unit_qty TEXT, quantity REAL, "
        "unit_of_measure TEXT, is_weighted INTEGER)"
    )
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def _out_conn(src_path):
    """An in-memory `products` table (build_app_db's own SCHEMA columns) with
    the fixture db attached as `src`, same as build() does for the real one.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE products(barcode TEXT PRIMARY KEY, name TEXT, "
        "manufacturer TEXT, unit_qty TEXT, quantity REAL, "
        "unit_of_measure TEXT, is_weighted INTEGER, category_id INTEGER, "
        "has_image INTEGER, sort_key TEXT)"
    )
    conn.execute("ATTACH DATABASE ? AS src", (build_app_db.ro_uri(src_path),))
    return conn


def test_table_row_wins_over_source_name(tmp_path):
    src_path = _src_db(tmp_path, [
        ("1111111111111", "*מבצע* שם קטוע", "Acme", "500", 500.0, "g", 0),
    ])
    conn = _out_conn(src_path)

    build_app_db.copy_products(conn, category_map={}, name_map={
        "1111111111111": "השם המלא שהוחלט עליו",
    })

    name, sort_key = conn.execute(
        "SELECT name, sort_key FROM products WHERE barcode = ?",
        ("1111111111111",),
    ).fetchone()
    assert name == "השם המלא שהוחלט עליו"
    assert sort_key == "השם המלא שהוחלט עליו"


def test_missing_barcode_falls_back_to_todays_name(tmp_path):
    src_path = _src_db(tmp_path, [
        ("2222222222222", "Untouched Name", "Acme", "1", 1.0, "unit", 0),
    ])
    conn = _out_conn(src_path)

    build_app_db.copy_products(conn, category_map={}, name_map={
        "9999999999999": "A different barcode entirely",
    })

    name = conn.execute(
        "SELECT name FROM products WHERE barcode = ?", ("2222222222222",)
    ).fetchone()[0]
    assert name == "Untouched Name"


def test_no_names_json_at_all_falls_back_for_everything(tmp_path):
    src_path = _src_db(tmp_path, [
        ("3333333333333", "Whatever Was There", "Acme", "1", 1.0, "unit", 0),
    ])
    conn = _out_conn(src_path)

    build_app_db.copy_products(conn, category_map={}, name_map=None)

    name = conn.execute(
        "SELECT name FROM products WHERE barcode = ?", ("3333333333333",)
    ).fetchone()[0]
    assert name == "Whatever Was There"


def test_malformed_or_blank_entry_is_skipped_not_crashed(tmp_path):
    json_path = tmp_path / "product_names.json"
    _write_names_json(json_path, [
        {"barcode": "1111111111111", "name": "Good Row", "source": "manual",
         "decided_at": "2026-09-17"},
        {"name": "Missing barcode entirely", "source": "manual"},
        {"barcode": "", "name": "", "source": "manual"},  # blank barcode/name
        {"barcode": "   ", "name": "   ", "source": "manual"},  # whitespace-only
        {"barcode": "4444444444444"},  # missing name key
        "not even an object",
        {"barcode": "3333333333333", "name": "Another Good Row",
         "source": "manual", "decided_at": "2026-09-17"},
    ])

    names = build_app_db.load_product_names(str(json_path))

    assert names == {
        "1111111111111": "Good Row",
        "3333333333333": "Another Good Row",
    }


def test_unknown_extra_key_is_ignored(tmp_path):
    # A later field (brand, size, unit, image URL, category) must be
    # addable to new entries without this loader - or any entry written
    # before it existed - breaking.
    json_path = tmp_path / "product_names.json"
    _write_names_json(json_path, [
        {"barcode": "5555555555555", "name": "Has An Extra Field",
         "source": "manual", "decided_at": "2026-09-17", "brand": "Acme",
         "size": "500g"},
    ])

    names = build_app_db.load_product_names(str(json_path))

    assert names == {"5555555555555": "Has An Extra Field"}


def test_malformed_top_level_shape_raises_loudly(tmp_path):
    json_path = tmp_path / "product_names.json"
    json_path.write_text(json.dumps(["not", "the", "right", "shape"]),
                          encoding="utf-8")

    with pytest.raises(ValueError):
        build_app_db.load_product_names(str(json_path))


def test_gershayim_name_round_trips_write_then_load(tmp_path):
    # The gershayim (מ"ל, ס"מ, בד"צ, ...) is extremely common in Hebrew
    # product names. A hand-rolled delimited format can mangle it (wrap the
    # whole field in quotes and double this character); plain JSON's own
    # encoder/decoder agree about quoting on their own, so it must not.
    name = 'פנטן שמפו קלאסי 600 מ"ל'
    json_path = tmp_path / "product_names.json"
    _write_names_json(json_path, [
        {"barcode": "1234567890123", "name": name, "source": "manual",
         "decided_at": "2026-09-18"},
    ])

    got = build_app_db.load_product_names(str(json_path))["1234567890123"]

    assert [ord(c) for c in got] == [ord(c) for c in name]
    assert got.encode("utf-8") == name.encode("utf-8")
    assert '""' not in got


def test_embedded_newline_name_round_trips_write_then_load(tmp_path):
    # A raw newline inside a name would split one logical row across two
    # physical lines in a delimited-text format; JSON has no such problem -
    # json.dump escapes it and json.load hands back the exact same string.
    name = "שקית אצבעות שוקולד\n252 גרם בד\"צ"
    json_path = tmp_path / "product_names.json"
    _write_names_json(json_path, [
        {"barcode": "9876543210987", "name": name, "source": "manual",
         "decided_at": "2026-09-18"},
    ])

    names = build_app_db.load_product_names(str(json_path))
    got = names["9876543210987"]

    assert [ord(c) for c in got] == [ord(c) for c in name]
    assert len(names) == 1


@pytest.mark.parametrize("barcode,expected", [
    ("7290011723200", 'טחינה גולמית הר ברכה תעשיות 500 גרם'),
    ("8714789733296", 'פלמוליב נטורלס תחליב רחצה זיתים וחלב 750 מ"ל'),
])
def test_real_json_has_the_two_required_barcodes_settled(barcode, expected):
    names = build_app_db.load_product_names(REAL_NAMES_JSON)
    got = names[barcode]
    assert [ord(c) for c in got] == [ord(c) for c in expected]
