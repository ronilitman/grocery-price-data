"""data/product_names.tsv and its loader (KAN-29).

A name decided once by a human is meant to win over whichever chain's file
build_app_db would otherwise have picked, and to fall back to today's
behaviour untouched when a barcode has not been reviewed yet - the whole
point being that partial coverage of the table can never break a product
that is not in it. These are unit tests: build_app_db.copy_products and
load_product_names are exercised directly against tiny fixtures, not through
the full build() pipeline that tests/app_db/test_build_app_db.py already
covers.
"""

import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402

REAL_NAMES_TSV = build_app_db.PRODUCT_NAMES_TSV


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


def test_no_names_tsv_at_all_falls_back_for_everything(tmp_path):
    src_path = _src_db(tmp_path, [
        ("3333333333333", "Whatever Was There", "Acme", "1", 1.0, "unit", 0),
    ])
    conn = _out_conn(src_path)

    build_app_db.copy_products(conn, category_map={}, name_map=None)

    name = conn.execute(
        "SELECT name FROM products WHERE barcode = ?", ("3333333333333",)
    ).fetchone()[0]
    assert name == "Whatever Was There"


def test_malformed_or_blank_row_is_skipped_not_crashed(tmp_path):
    tsv_path = tmp_path / "product_names.tsv"
    tsv_path.write_text(
        "# a comment line, same as the checked-in file's header\n"
        "1111111111111\tGood Row\tmanual\t2026-09-17\n"
        "\n"
        "2222222222222\tToo few columns for this loader\n"
        "\t\tsource\t2026-09-17\n"  # blank barcode and name
        "   \t   \tsource\t2026-09-17\n"  # whitespace-only barcode and name
        "3333333333333\tAnother Good Row\tmanual\t2026-09-17\n",
        encoding="utf-8",
    )

    names = build_app_db.load_product_names(str(tsv_path))

    assert names == {
        "1111111111111": "Good Row",
        "3333333333333": "Another Good Row",
    }


def test_hebrew_name_round_trips_byte_for_byte(tmp_path):
    hebrew_name = 'טחינה גולמית הר ברכה תעשיות 500 גרם'
    tsv_path = tmp_path / "product_names.tsv"
    tsv_path.write_bytes(
        f"7290011723200\t{hebrew_name}\tpricez\t2026-09-17\n".encode("utf-8")
    )

    names = build_app_db.load_product_names(str(tsv_path))
    got = names["7290011723200"]

    # Never eyeball rendered Hebrew - compare codepoints and raw UTF-8 bytes.
    assert [ord(c) for c in got] == [ord(c) for c in hebrew_name]
    assert got.encode("utf-8") == hebrew_name.encode("utf-8")


@pytest.mark.parametrize("barcode,expected", [
    ("7290011723200", 'טחינה גולמית הר ברכה תעשיות 500 גרם'),
    ("8714789733296", 'פלמוליב נטורלס תחליב רחצה זיתים וחלב 750 מ"ל'),
])
def test_real_tsv_has_the_two_required_barcodes_settled(barcode, expected):
    names = build_app_db.load_product_names(REAL_NAMES_TSV)
    got = names[barcode]
    assert [ord(c) for c in got] == [ord(c) for c in expected]
