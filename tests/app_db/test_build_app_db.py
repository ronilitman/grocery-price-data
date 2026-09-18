"""build_app_db.py against a small fixture built from merge_db.SCHEMA.

The fixture stands in for a real prices.db: a handful of rows in each table
merge_db produces, including one products barcode that is in the checked-in
data/product_categories.tsv and one that deliberately is not. The assertions
are the contract KAN-6 promises the next two subtasks (KAN-7's deals table,
KAN-8's FTS indexes): every row that should survive does, nothing that should
be dropped sneaks through, and a crash never leaves a stray .tmp file.
"""

import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402
import merge_db  # noqa: E402

TSV = os.path.join(ROOT, "data", "product_categories.tsv")
BARCODE_NOT_IN_TSV = "0000000000000"


def _a_real_tsv_entry():
    """A (barcode, category_id) pair taken from the real, checked-in TSV."""
    with open(TSV, encoding="utf-8") as handle:
        barcode, category_id, _source = handle.readline().rstrip("\n").split("\t")
    assert barcode != BARCODE_NOT_IN_TSV
    return barcode, int(category_id)


@pytest.fixture
def fixture_db(tmp_path):
    """A miniature prices.db, built with merge_db's own schema."""
    categorised_barcode, categorised_category_id = _a_real_tsv_entry()

    path = str(tmp_path / "prices.db")
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.executemany("INSERT INTO chains VALUES (?,?,?)", [
        ("RAMI_LEVY", "Rami Levy", None),
        ("SHUFERSAL", "Shufersal", None),
    ])
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", "1", None, "Rami Levy Center", "3000", None, "Jaffa Rd", 500, None),
            ("RAMI_LEVY", "2", None, "Rami Levy North", "5000", None, "Ibn Gvirol", 480, None),
            ("SHUFERSAL", "1", None, "Shufersal Deal", "5000", None, "Dizengoff", 600, None),
        ],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [
            (categorised_barcode, "  Tomato   Sauce  ", "Acme", "700", 700.0, "g", 0),
            (BARCODE_NOT_IN_TSV, "Mystery Item", "Acme", "1", 1.0, "unit", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO chain_prices VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", categorised_barcode, 9.90, 2),
            ("RAMI_LEVY", BARCODE_NOT_IN_TSV, 3.50, 2),
            ("SHUFERSAL", categorised_barcode, 10.50, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO price_exceptions VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", "2", categorised_barcode, 9.50),
        ],
    )
    conn.executemany(
        "INSERT INTO chain_products VALUES (?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", categorised_barcode, "Tomato Sauce (RL)", "700", "g", 0),
        ],
    )
    # promo_offers / promo_stores / product_tokens: populated so the test can
    # prove build_app_db drops them, not just that it never wrote any.
    conn.executemany(
        "INSERT INTO promo_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(1, "RAMI_LEVY", "P1", categorised_barcode, 0, 0, 1, 8.90, 8.90,
          "2 for 17", "2026-01-01", "2026-01-31")],
    )
    conn.execute("INSERT INTO promo_stores VALUES (1, '1')")
    conn.execute("INSERT INTO product_tokens VALUES ('tomato', ?)", (categorised_barcode,))

    chain_as_of = {"SHUFERSAL": "2026-09-10T00:00:00+00:00"}
    conn.executemany("INSERT INTO meta VALUES (?,?)", [
        ("built_at", "2026-09-13T02:00:00+00:00"),
        ("chain_as_of", json.dumps(chain_as_of)),
        ("sources", json.dumps(["rami_levy.db", "shufersal.db"])),
    ])
    conn.commit()
    conn.close()

    return path, categorised_barcode, categorised_category_id


def _source_counts(db_path, tables):
    conn = sqlite3.connect(db_path)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    conn.close()
    return counts


COPIED_TABLES = ["chains", "stores", "products", "chain_prices",
                  "price_exceptions", "chain_products", "meta"]


def test_row_counts_match_the_source(fixture_db, tmp_path):
    db_path, _, _ = fixture_db
    out_path = str(tmp_path / "app.db")

    expected = _source_counts(db_path, COPIED_TABLES)
    build_app_db.build(db_path, out_path)

    actual = _source_counts(out_path, COPIED_TABLES)
    assert actual == expected


def test_category_id_is_set_only_for_the_listed_barcode(fixture_db, tmp_path):
    db_path, categorised_barcode, categorised_category_id = fixture_db
    out_path = str(tmp_path / "app.db")
    build_app_db.build(db_path, out_path)

    conn = sqlite3.connect(out_path)
    got = dict(conn.execute("SELECT barcode, category_id FROM products"))
    conn.close()

    assert got[categorised_barcode] == categorised_category_id
    assert got[BARCODE_NOT_IN_TSV] is None


def test_meta_built_at_and_chain_as_of_are_carried(fixture_db, tmp_path):
    db_path, _, _ = fixture_db
    out_path = str(tmp_path / "app.db")
    build_app_db.build(db_path, out_path)

    conn = sqlite3.connect(out_path)
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    conn.close()

    assert meta["built_at"] == "2026-09-13T02:00:00+00:00"
    assert json.loads(meta["chain_as_of"]) == {"SHUFERSAL": "2026-09-10T00:00:00+00:00"}


def test_promo_stores_and_product_tokens_are_absent(fixture_db, tmp_path):
    db_path, _, _ = fixture_db
    out_path = str(tmp_path / "app.db")
    build_app_db.build(db_path, out_path)

    conn = sqlite3.connect(out_path)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()

    assert "promo_stores" not in tables
    assert "product_tokens" not in tables
    # Out of scope for this subtask too (KAN-7): no half-copied deals input.
    assert "promo_offers" not in tables


def test_no_tmp_file_left_behind(fixture_db, tmp_path):
    db_path, _, _ = fixture_db
    out_path = str(tmp_path / "app.db")
    build_app_db.build(db_path, out_path)

    assert os.path.exists(out_path)
    assert not os.path.exists(out_path + ".tmp")


def test_sort_key_is_whitespace_collapsed_and_trimmed(fixture_db, tmp_path):
    db_path, categorised_barcode, _ = fixture_db
    out_path = str(tmp_path / "app.db")
    build_app_db.build(db_path, out_path)

    conn = sqlite3.connect(out_path)
    sort_key = conn.execute(
        "SELECT sort_key FROM products WHERE barcode = ?", (categorised_barcode,)
    ).fetchone()[0]
    conn.close()

    assert sort_key == "Tomato Sauce"


def test_categories_table_is_populated_from_the_checked_in_taxonomy(fixture_db, tmp_path):
    db_path, _, _ = fixture_db
    out_path = str(tmp_path / "app.db")
    build_app_db.build(db_path, out_path)

    with open(os.path.join(ROOT, "data", "categories.json"), encoding="utf-8") as handle:
        expected = json.load(handle)

    conn = sqlite3.connect(out_path)
    got = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    conn.close()

    assert got == len(expected)


def test_source_db_is_untouched(fixture_db, tmp_path):
    """ATTACH ... mode=ro must make the source genuinely unwritable."""
    db_path, _, _ = fixture_db
    before = open(db_path, "rb").read()

    out_path = str(tmp_path / "app.db")
    build_app_db.build(db_path, out_path)

    after = open(db_path, "rb").read()
    assert before == after
