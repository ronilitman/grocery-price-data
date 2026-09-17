"""KAN-17: build_category_counts (precomputed category_counts), tested
directly against build_app_db's own SCHEMA rather than through the whole
build() pipeline - it only touches `products` and `categories`, so a
connection carrying just those is enough.
"""
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(build_app_db.SCHEMA)
    yield connection
    connection.close()


def _insert_product(conn, barcode, name, category_id, sort_key=None):
    conn.execute(
        "INSERT INTO products "
        "(barcode, name, manufacturer, unit_qty, quantity, unit_of_measure, "
        " is_weighted, category_id, has_image, sort_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (barcode, name, None, None, None, None, 0, category_id, None,
         sort_key if sort_key is not None else name))


def _insert_category(conn, id_, name_he, parent_id):
    conn.execute(
        "INSERT INTO categories (id, slug, name_he, parent_id) VALUES (?,?,?,?)",
        (id_, f"slug-{id_}", name_he, parent_id))


def test_category_counts_sub_and_top_level(conn):
    _insert_category(conn, 100, "Top", None)
    _insert_category(conn, 101, "Sub 1", 100)
    _insert_category(conn, 102, "Sub 2 (empty)", 100)
    _insert_product(conn, "1", "A", category_id=101)
    _insert_product(conn, "2", "B", category_id=101)
    _insert_product(conn, "3", "C", category_id=101)
    conn.commit()

    rows = build_app_db.build_category_counts(conn)
    assert rows == 3  # one row per category, including the empty one

    counts = dict(conn.execute("SELECT category_id, count FROM category_counts"))
    assert counts == {100: 3, 101: 3, 102: 0}
