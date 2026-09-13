"""scripts/app_search.py: the normaliser and query builder shared by index
time (build_app_db.build_fts) and query time (the API's /search, KAN-12).

These exercise the module directly, without building a database, plus one
end-to-end check that a string build_match produces is actually accepted by
a real FTS5 MATCH (the ``15%`` syntax-error case the KAN-8 spec calls out).

Filler handling changed on 2026-09-14 (reviewer override): it is neither
stripped from indexed text nor dropped from queries - see
test_build_match_keeps_filler_in_a_multi_word_query below for why.
"""

import os
import sqlite3
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import app_search  # noqa: E402


def _fts5_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE t USING fts5(name, tokenize='unicode61 "
        "remove_diacritics 2')")
    return conn


def test_normalise_strips_all_four_quote_characters():
    assert app_search.normalise('ק"ג') == "קג"
    assert app_search.normalise("ק'ג") == "קג"
    assert app_search.normalise("ק״ג") == "קג"  # gershayim
    assert app_search.normalise("ק׳ג") == "קג"  # geresh


def test_normalise_lowercases_and_collapses_whitespace():
    assert app_search.normalise("  Milk   2%  ") == "milk 2%"


def test_tokens_drops_short_tokens():
    assert app_search.tokens("15% דק") == ["15", "דק"]
    assert "a" not in app_search.tokens("a bb")


def test_tokens_matches_merge_db_token_split_on_hebrew_and_word_chars():
    # Same split as merge_db.TOKEN_SPLIT: a run of anything that is neither a
    # word character nor in the Hebrew block separates tokens.
    assert app_search.tokens("גבינה-צהובה, 15%") == ["גבינה", "צהובה", "15"]


def test_compute_filler_needs_the_floor_of_50_even_at_high_frequency():
    # 10 identical names is 100% of the corpus but far short of the 50-count
    # floor build_catalog.NAME_FILLER_AT also applies - a tiny corpus must
    # not manufacture filler words that would never survive contact with the
    # real ~243k-product catalogue.
    names = ["milk"] * 10
    assert app_search.compute_filler(names) == set()


def test_compute_filler_over_the_floor():
    names = ["milk bottle"] * 60 + ["cheese"] * 5
    filler = app_search.compute_filler(names)
    assert "milk" in filler
    assert "bottle" in filler
    assert "cheese" not in filler


def test_build_match_quotes_each_token_and_ors_them():
    assert app_search.build_match("milk cheese", filler=set()) == \
        '"milk" OR "cheese"'


def test_build_match_on_percent_sign_is_accepted_by_real_fts5():
    # Passing "15%" straight to MATCH raises `fts5: syntax error near "%"`.
    match = app_search.build_match("15%", filler=set())
    assert match is not None
    conn = _fts5_conn()
    conn.execute("INSERT INTO t VALUES ('discount 15 percent')")
    # No exception is the assertion - an unquoted "15%" would raise here.
    conn.execute("SELECT * FROM t WHERE t MATCH ?", (match,)).fetchall()


def test_build_match_keeps_filler_in_a_multi_word_query():
    # Dropping filler here searched "שוקולד חלב" (chocolate milk) as just
    # "חלב" and ranked plain milk first on the real catalogue. Keeping both
    # words lets bm25 put chocolate milk first.
    assert app_search.build_match("שוקולד חלב", filler={"שוקולד"}) == \
        '"שוקולד" OR "חלב"'
    assert app_search.build_match("במבה 80 גרם", filler={"גרם"}) == \
        '"במבה" OR "80" OR "גרם"'


def test_build_match_keeps_filler_tokens_when_nothing_else_is_left():
    # 2026-09-14 reviewer override: the original rule dropped an all-filler
    # query down to None, which on real data meant searching שוקולד
    # (chocolate), עוף (chicken) or גרם found nothing at all - both words
    # cleared the 1% filler threshold on the real catalogue. Now an
    # all-filler query still searches every one of its own words.
    assert app_search.build_match("שוקולד", filler={"שוקולד"}) == '"שוקולד"'
    assert app_search.build_match("גרם", filler={"גרם"}) == '"גרם"'
    assert app_search.build_match("גרם 500", filler={"גרם", "500"}) == \
        '"גרם" OR "500"'


def test_build_match_returns_none_only_when_there_are_no_tokens_at_all():
    # Not "every token is filler" (that case now still searches, see above)
    # - only a query that tokenises to nothing, filler or otherwise.
    assert app_search.build_match("!!!", filler=set()) is None
    assert app_search.build_match("", filler=set()) is None
    assert app_search.build_match("a", filler=set()) is None  # too short


def test_build_match_escapes_embedded_double_quotes():
    match = app_search.build_match('ק"ג', filler=set())
    # normalise() strips the quote before tokenising, so there is nothing
    # left to escape here - the quoted token is just קג.
    assert match == '"קג"'


def test_index_text_indexes_every_token_filler_included():
    # 2026-09-14 reviewer override: index_text no longer takes or drops
    # filler - see the module docstring for why (real words like שוקולד and
    # עוף were being deleted from the index, not just packaging noise).
    assert app_search.index_text("גבינה צהובה 15% גרם") == \
        "גבינה צהובה 15 גרם"
