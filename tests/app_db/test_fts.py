"""FTS5 search indexes build_app_db.build_fts writes (KAN-8).

Two layers:

* Direct FTS5 behaviour (ranking, an unmatched token, an all-filler query,
  gershayim/geresh spelling equivalence) against a small in-memory index
  built straight from ``scripts/app_search`` - the same functions
  ``build_fts`` and, later, the API's ``/search`` (KAN-12) call.
* Wiring through ``build_app_db.build()`` against a fixture prices.db: that
  ``fts_filler``/``fts_all``/``fts_deals`` end up populated, that every
  token - filler included - actually lands in the indexed name, and that
  ``fts_deals`` is exactly the barcodes with a ``discount_pct > 0`` deal -
  not every barcode on offer, and not every barcode in the catalogue.

2026-09-14 reviewer override: the original spec stripped filler from indexed
text as well as from queries. On the real catalogue that deleted genuine
product words - שוקולד (chocolate), עוף (chicken), יין (wine), עוגיות
(cookies) and סוכריות (sweets) all clear the 1% filler threshold - so
searching any of them found nothing. Indexing now keeps every token, and
``build_match`` only trims a *query* down to its filler words when at least
one more distinctive word survives alongside them; an all-filler query still
searches every word in it. See ``test_app_search.py`` for the unit-level
cases and the two tests below for the "chocolate now findable" regression.
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

def _make_index(rows):
    """An in-memory fts_all-shaped table from (barcode, raw_name) pairs.

    Every token is indexed - index_text no longer takes a filler set; only
    build_match (query time) does.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE fts_all USING fts5(name, barcode UNINDEXED, "
        "tokenize='unicode61 remove_diacritics 2')")
    for barcode, name in rows:
        text = app_search.index_text(name)
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


def test_all_filler_query_still_searches_its_own_words():
    # 2026-09-14 reviewer override: this used to return None (a first cut of
    # the spec dropped an all-filler query entirely), which - combined with
    # index-time stripping - meant "שוקולד" found nothing on the real
    # catalogue even though products named exactly that exist. Now it
    # searches every word it has, filler or not.
    filler = {"שוקולד"}
    assert app_search.build_match("שוקולד", filler) == '"שוקולד"'

    conn = _make_index([
        (CHEESE, "גבינה צהובה 15% דק"),
        ("choc-bar", "שוקולד חלב"),
    ])
    results = _search(conn, "שוקולד", filler=filler)
    assert "choc-bar" in results


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


def test_fts_all_has_one_row_per_named_product_every_token_indexed(fixture_db, tmp_path):
    out_path = str(tmp_path / "app.db")
    build_app_db.build(fixture_db, out_path)

    conn = sqlite3.connect(out_path)
    rows = dict(conn.execute("SELECT barcode, name FROM fts_all"))
    total_named_products = conn.execute(
        "SELECT COUNT(*) FROM products WHERE name <> ''").fetchone()[0]
    conn.close()

    assert len(rows) == total_named_products == FILLER_PRODUCT_COUNT + 2
    assert NO_NAME_BARCODE not in rows
    # גרם is real filler on this fixture (62/62 names) but it is NOT
    # stripped from the indexed text (and queries keep it too).
    assert rows[CHEESE_BARCODE] == "גבינה צהובה גרם"
    assert rows[CREAM_BARCODE] == "שמנת גרם"


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
    assert fts_deals_rows[CHEESE_BARCODE] == "גבינה צהובה גרם"


def test_a_match_against_the_built_fts_all_finds_the_product(fixture_db, tmp_path):
    out_path = str(tmp_path / "app.db")
    build_app_db.build(fixture_db, out_path)

    conn = sqlite3.connect(out_path)
    filler = {row[0] for row in conn.execute("SELECT token FROM fts_filler")}
    match = app_search.build_match("גבינה 500 גרם", filler)
    results = [row[0] for row in conn.execute(
        "SELECT barcode FROM fts_all WHERE fts_all MATCH ? ORDER BY bm25(fts_all)",
        (match,))]
    conn.close()

    # Every word is searched, filler included, so "גרם" (on every fixture
    # product) matches the whole catalogue - but bm25 weights it low and
    # "גבינה" is distinctive, so the cheese still ranks first.
    assert results[0] == CHEESE_BARCODE
