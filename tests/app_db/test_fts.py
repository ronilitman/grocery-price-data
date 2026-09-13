"""FTS5 search indexes build_app_db.build_fts writes (KAN-8).

Two layers:

* Direct FTS5 behaviour (ranking, an unmatched token, a filler-only query,
  gershayim/geresh spelling equivalence) against a small in-memory index
  built straight from ``scripts/app_search`` - the same functions
  ``build_fts`` and, later, the API's ``/search`` (KAN-12) call.
* Wiring through ``build_app_db.build()`` against a fixture prices.db: that
  ``fts_filler``/``fts_all``/``fts_deals`` end up populated, that filler
  computed over the whole corpus is what actually gets stripped from an
  indexed name, and that ``fts_deals`` is exactly the barcodes with a
  ``discount_pct > 0`` deal - not every barcode on offer, and not every
  barcode in the catalogue.
"""

import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import app_search  # noqa: E402
import build_app_db  # noqa: E402
import merge_db  # noqa: E402


# --------------------------------------------------------------------------
# Direct FTS5 behaviour
# --------------------------------------------------------------------------

def _make_index(rows, filler=frozenset()):
    """An in-memory fts_all-shaped table from (barcode, raw_name) pairs."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE fts_all USING fts5(name, barcode UNINDEXED, "
        "tokenize='unicode61 remove_diacritics 2')")
    for barcode, name in rows:
        text = app_search.index_text(name, filler)
        conn.execute(
            "INSERT INTO fts_all (name, barcode) VALUES (?,?)", (text, barcode))
    return conn


def _search(conn, query, filler=frozenset()):
    match = app_search.build_match(query, filler)
    if match is None:
        return []
    return [row[0] for row in conn.execute(
        "SELECT barcode FROM fts_all WHERE fts_all MATCH ? "
        "ORDER BY bm25(fts_all)", (match,))]


CHEESE = "cheese-barcode"
CREAM = "cream-barcode"
CAPSULES = "capsules-barcode"


def test_query_ranks_the_best_match_first():
    conn = _make_index([
        (CHEESE, "גבינה צהובה 15% דק"),
        (CREAM, "שמנת 15%"),
        (CAPSULES, "קפסולות קפה 15"),
    ])
    results = _search(conn, "גבינה 15 אחוז")
    assert results, "expected at least one match"
    assert results[0] == CHEESE


def test_unmatched_token_does_not_eliminate_the_query():
    conn = _make_index([(CHEESE, "גבינה צהובה 15% דק")])
    results = _search(conn, "זזזזז צהובה 15")
    assert CHEESE in results


def test_filler_only_query_returns_none_before_any_query_runs():
    assert app_search.build_match("500 גרם", filler={"500", "גרם"}) is None


def test_gershayim_and_geresh_spellings_both_match_the_unquoted_product():
    conn = _make_index([("tapua", "תפוא")])
    assert "tapua" in _search(conn, 'תפו"א')   # ASCII double quote
    assert "tapua" in _search(conn, "תפו״א")  # gershayim
    assert "tapua" in _search(conn, "תפו׳א")  # geresh


# --------------------------------------------------------------------------
# Wiring through build_app_db.build()
# --------------------------------------------------------------------------

CHEESE_BARCODE = "1111111111111"
CREAM_BARCODE = "2222222222222"
NO_NAME_BARCODE = "3333333333333"
FILLER_PRODUCT_COUNT = 60


@pytest.fixture
def fixture_db(tmp_path):
    """A prices.db with a real, count-based filler word (גרם/מוצר at 60+
    occurrences, well past the 50-row floor) and one product on a real
    discount, so fts_filler/fts_all/fts_deals all have something to prove.
    """
    path = str(tmp_path / "prices.db")
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.execute("INSERT INTO chains VALUES ('RAMI_LEVY', 'Rami Levy')")
    conn.execute(
        "INSERT INTO stores VALUES ('RAMI_LEVY','10',NULL,'Rami Levy A',"
        "'3000','St 1',100)")

    products = [
        (CHEESE_BARCODE, "גבינה צהובה גרם", "Acme", "1", 1.0, "unit", 0),
        (CREAM_BARCODE, "שמנת גרם", "Acme", "1", 1.0, "unit", 0),
        (NO_NAME_BARCODE, "", "Acme", "1", 1.0, "unit", 0),
    ]
    for i in range(FILLER_PRODUCT_COUNT):
        barcode = f"9{i:012d}"
        products.append((barcode, f"מוצר {i} גרם", "Acme", "1", 1.0, "unit", 0))
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?)", products)

    # Baseline price for CHEESE only, so it is the one product a real
    # discount can be computed against.
    conn.execute(
        "INSERT INTO chain_prices VALUES ('RAMI_LEVY', ?, 20.00, 1)",
        (CHEESE_BARCODE,))

    conn.execute(
        "INSERT INTO promo_offers VALUES "
        "(1, 'RAMI_LEVY', 'P1', ?, 0, 0, 1, 15.00, 15.00, "
        "'cheese deal', '2026-09-01', '2026-12-31')",
        (CHEESE_BARCODE,))
    conn.execute("INSERT INTO promo_stores VALUES (1, '10')")

    conn.executemany("INSERT INTO meta VALUES (?,?)", [
        ("built_at", "2026-09-13T02:00:00+00:00"),
        ("chain_as_of", json.dumps({})),
        ("sources", json.dumps(["rami_levy.db"])),
    ])
    conn.commit()
    conn.close()
    return path


def test_filler_is_computed_over_the_whole_corpus_and_stored(fixture_db, tmp_path):
    out_path = str(tmp_path / "app.db")
    build_app_db.build(fixture_db, out_path)

    conn = sqlite3.connect(out_path)
    filler = {row[0] for row in conn.execute("SELECT token FROM fts_filler")}
    conn.close()

    # "גרם" rides on every product (62/62); "מוצר" rides on the 60 filler
    # rows alone - both clear the 50-count floor. Genuine product words
    # (גבינה, צהובה, שמנת) never repeat enough to qualify.
    assert filler == {"גרם", "מוצר"}


def test_fts_all_has_one_row_per_named_product_with_filler_stripped(fixture_db, tmp_path):
    out_path = str(tmp_path / "app.db")
    build_app_db.build(fixture_db, out_path)

    conn = sqlite3.connect(out_path)
    rows = dict(conn.execute("SELECT barcode, name FROM fts_all"))
    total_named_products = conn.execute(
        "SELECT COUNT(*) FROM products WHERE name <> ''").fetchone()[0]
    conn.close()

    assert len(rows) == total_named_products == FILLER_PRODUCT_COUNT + 2
    assert NO_NAME_BARCODE not in rows
    assert rows[CHEESE_BARCODE] == "גבינה צהובה"
    assert rows[CREAM_BARCODE] == "שמנת"


def test_fts_deals_is_exactly_the_barcodes_on_a_real_discount(fixture_db, tmp_path):
    out_path = str(tmp_path / "app.db")
    build_app_db.build(fixture_db, out_path)

    conn = sqlite3.connect(out_path)
    deal_barcodes = {row[0] for row in conn.execute(
        "SELECT barcode FROM deals WHERE discount_pct > 0")}
    fts_deals_barcodes = {row[0] for row in conn.execute(
        "SELECT barcode FROM fts_deals")}
    fts_deals_rows = dict(conn.execute("SELECT barcode, name FROM fts_deals"))
    conn.close()

    assert deal_barcodes == {CHEESE_BARCODE}
    assert fts_deals_barcodes == deal_barcodes
    assert CREAM_BARCODE not in fts_deals_barcodes
    assert fts_deals_rows[CHEESE_BARCODE] == "גבינה צהובה"


def test_a_match_against_the_built_fts_all_finds_the_product(fixture_db, tmp_path):
    out_path = str(tmp_path / "app.db")
    build_app_db.build(fixture_db, out_path)

    conn = sqlite3.connect(out_path)
    filler = {row[0] for row in conn.execute("SELECT token FROM fts_filler")}
    match = app_search.build_match("גבינה 500 גרם", filler)
    results = [row[0] for row in conn.execute(
        "SELECT barcode FROM fts_all WHERE fts_all MATCH ?", (match,))]
    conn.close()

    # "500" never appeared in any product name and "גרם" is filler - only
    # "גבינה" survives to actually match, and it still finds the cheese.
    assert results == [CHEESE_BARCODE]
