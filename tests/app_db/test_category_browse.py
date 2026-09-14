"""KAN-17: build_category_browse (collapsing a loose-produce generic's
categorised members into one browse row) and build_category_counts
(precomputed category_counts), tested directly against build_app_db's own
SCHEMA/SCHEMA_PRODUCE rather than through the whole build() pipeline - these
two functions only touch `products`, `generic_barcodes` and `categories`,
so a connection carrying just those is enough and keeps the collision
tie-break case (see build_category_browse's docstring) easy to construct by
hand.
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
    connection.executescript(build_app_db.SCHEMA_PRODUCE)
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


def test_ordinary_categorised_product_stays_visible(conn):
    """A barcode that belongs to no generic at all (the overwhelming
    majority of categorised products) is untouched: browse_visible stays 1,
    browse_generic_key stays NULL."""
    _insert_product(conn, "1", "Milk", category_id=9)
    conn.commit()

    heads, hidden = build_app_db.build_category_browse(conn)

    assert (heads, hidden) == (0, 0)
    row = conn.execute(
        "SELECT browse_visible, browse_generic_key FROM products WHERE barcode='1'"
    ).fetchone()
    assert row == (1, None)


def test_generic_with_no_categorised_member_is_untouched(conn):
    """A generic key exists (generic_barcodes has rows for it) but none of
    its members are individually categorised - nothing to collapse."""
    _insert_product(conn, "1", "Tomato RL", category_id=None)
    conn.execute("INSERT INTO generic_barcodes VALUES ('טמטו', '1', 1)")
    conn.commit()

    heads, hidden = build_app_db.build_category_browse(conn)

    assert (heads, hidden) == (0, 0)


def test_generic_members_collapse_to_one_head_at_earliest_sort_position(conn):
    """Two categorised members of the same generic key collapse to one
    visible row: the head is whichever sorts first by (sort_key, barcode),
    matching the spec's "takes the position of its first member in sort
    order"; the other becomes browse_visible=0."""
    _insert_product(conn, "20", "Zzz Tomato", category_id=9, sort_key="Zzz Tomato")
    _insert_product(conn, "10", "Aaa Tomato", category_id=9, sort_key="Aaa Tomato")
    conn.executemany(
        "INSERT INTO generic_barcodes VALUES (?,?,?)",
        [("טמטו", "20", 1), ("טמטו", "10", 0)])
    conn.commit()

    heads, hidden = build_app_db.build_category_browse(conn)
    assert (heads, hidden) == (1, 1)

    # "Aaa Tomato" (barcode 10) sorts first - it is the head.
    head_row = conn.execute(
        "SELECT browse_visible, browse_generic_key FROM products WHERE barcode='10'"
    ).fetchone()
    assert head_row == (1, "טמטו")
    hidden_row = conn.execute(
        "SELECT browse_visible, browse_generic_key FROM products WHERE barcode='20'"
    ).fetchone()
    assert hidden_row == (0, None)


def test_three_categorised_members_collapse_to_one_head(conn):
    """More than two members of the same key still collapse to exactly one
    visible row - not just pairwise."""
    _insert_product(conn, "3", "C Tomato", category_id=9)
    _insert_product(conn, "1", "A Tomato", category_id=9)
    _insert_product(conn, "2", "B Tomato", category_id=9)
    conn.executemany(
        "INSERT INTO generic_barcodes VALUES (?,?,?)",
        [("טמטו", "3", 0), ("טמטו", "1", 1), ("טמטו", "2", 0)])
    conn.commit()

    build_app_db.build_category_browse(conn)

    visible = conn.execute(
        "SELECT barcode FROM products WHERE category_id=9 AND browse_visible=1"
    ).fetchall()
    assert visible == [("1",)]


def test_barcode_collision_keeps_only_the_larger_category_cluster(conn, capsys):
    """A generic key whose categorised members split across TWO different
    category ids (a real barcode-collision artefact on the live catalogue,
    e.g. one chain's tomato and an unrelated chain's butter sharing a short
    internal code - see the KAN-13 review) collapses only the larger
    cluster; the odd one out is left as an ordinary standalone product in
    its own (different) category, not silently reassigned."""
    _insert_product(conn, "1", "Tomato A", category_id=2)
    _insert_product(conn, "2", "Tomato B", category_id=2)
    _insert_product(conn, "3", "Butter Collision", category_id=14)
    conn.executemany(
        "INSERT INTO generic_barcodes VALUES (?,?,?)",
        [("טמטו", "1", 1), ("טמטו", "2", 0), ("טמטו", "3", 0)])
    conn.commit()

    build_app_db.build_category_browse(conn)

    # The larger (category 2) cluster collapses to one head.
    cat2_visible = conn.execute(
        "SELECT barcode FROM products WHERE category_id=2 AND browse_visible=1"
    ).fetchall()
    assert cat2_visible == [("1",)]
    # The odd-one-out (category 14) is untouched: still its own visible row,
    # not folded into the generic.
    row3 = conn.execute(
        "SELECT browse_visible, browse_generic_key FROM products WHERE barcode='3'"
    ).fetchone()
    assert row3 == (1, None)
    assert "WARNING" in capsys.readouterr().out


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


def test_category_counts_excludes_hidden_browse_rows(conn):
    """A collapsed generic's non-head member must not be double-counted."""
    _insert_category(conn, 100, "Top", None)
    _insert_category(conn, 101, "Sub", 100)
    _insert_product(conn, "1", "A Tomato", category_id=101)
    _insert_product(conn, "2", "B Tomato", category_id=101)
    _insert_product(conn, "3", "Plain", category_id=101)
    conn.executemany(
        "INSERT INTO generic_barcodes VALUES (?,?,?)",
        [("טמטו", "1", 1), ("טמטו", "2", 0)])
    conn.commit()

    build_app_db.build_category_browse(conn)
    build_app_db.build_category_counts(conn)

    counts = dict(conn.execute("SELECT category_id, count FROM category_counts"))
    # 2 categorised browse-visible rows (the collapsed generic + "Plain"),
    # not 3 - the hidden member must not inflate the count.
    assert counts == {100: 2, 101: 2}
