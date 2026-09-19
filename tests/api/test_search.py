"""GET /search (KAN-12) against a real app.db built by build_app_db.py from
a fixture prices.db - not a hand-rolled sqlite schema - so these tests catch
a drift between api/search.py's assumptions and what KAN-6/7/8 actually
produce.

Fixture layout (see `_build_prices_db`):

* CHEESE/CREAM/COFFEE - three differently-worded "15" products, to prove
  bm25 ranking (not truncation) decides order.
* DEAL_PRODUCT/PLAIN_PRODUCT - one product with a real promo (discount_pct
  > 0, lands in fts_deals), one without (fts_all only).
"""

import itertools
import json
import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402
import merge_db  # noqa: E402

from api.main import app  # noqa: E402

CHEESE = "1000000000001"
CREAM = "1000000000002"
COFFEE = "1000000000003"
DEAL_PRODUCT = "3000000000001"
PLAIN_PRODUCT = "3000000000002"

# KAN-24 ranking fixtures - each scenario uses its own query word so the
# scenarios can't bleed into each other's results. "Widely-stocked" chains
# (CHAIN_C..CHAIN_F) exist only to give a product a chain count without a
# real store per chain - build_store_bits (see its own docstring) doesn't
# require one.
A_EXACT = "9100000000001"    # "בננה" IS the query "בננה" - tier 0
A_LONG = "9100000000002"     # contains "בננה" mid-name, 6 chains - tier 2
B_LEAD = "9200000000001"     # STARTS WITH "תפוח" - tier 1
B_MID = "9200000000002"      # "תפוח" mid-name, 6 chains - tier 2
C_LOW = "9300000000001"      # STARTS WITH "גזר", 1 chain - tier 1
C_HIGH = "9300000000002"     # STARTS WITH "גזר", 6 chains - tier 1
D_SHORT = "9400000000001"    # "בצל" mid-name, short doc, 2 chains - tier 2
D_LONG = "9400000000002"     # "בצל" mid-name, long doc, same 2 chains - tier 2

BUILT_AT = "2026-09-13T02:00:00+00:00"


def _build_prices_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.executemany(
        "INSERT INTO chains VALUES (?,?,?)",
        [
            ("RAMI_LEVY", "Rami Levy", None),
            ("SHUFERSAL", "Shufersal", None),
            ("CHAIN_C", "Chain C", None),
            ("CHAIN_D", "Chain D", None),
            ("CHAIN_E", "Chain E", None),
            ("CHAIN_F", "Chain F", None),
        ],
    )
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [("RAMI_LEVY", "10", None, "Rami Levy A", "3000", None, "St 1", 100, None, None, None, None)],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [
            (CHEESE, "גבינה צהובה 15% דק", "Acme", "200", 200.0, "g", 0),
            (CREAM, "שמנת 15%", "Acme", "250", 250.0, "ml", 0),
            (COFFEE, "קפסולות קפה 15", "Acme", "1", 1.0, "unit", 0),
            (DEAL_PRODUCT, "מבצע שוקולד", "Acme", "100", 100.0, "g", 0),
            (PLAIN_PRODUCT, "שוקולד רגיל", "Acme", "100", 100.0, "g", 0),
            (A_EXACT, "בננה", "Acme", "1", 1.0, "unit", 0),
            (A_LONG, "עוגת בננה טרופית ומדהימה מאוד", "Acme", "1", 1.0, "unit", 0),
            (B_LEAD, "תפוח עץ ירוק", "Acme", "1", 1.0, "unit", 0),
            (B_MID, "מיץ תפוח טבעי מרוכז", "Acme", "1", 1.0, "unit", 0),
            (C_LOW, "גזר טרי בשקית", "Acme", "1", 1.0, "unit", 0),
            (C_HIGH, "גזר טרי בקילו ארוז", "Acme", "1", 1.0, "unit", 0),
            (D_SHORT, "ירקות בצל", "Acme", "1", 1.0, "unit", 0),
            (D_LONG, "ירקות בצל קלוי ומתובל בתערובת תבלינים מיוחדת",
             "Acme", "1", 1.0, "unit", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO chain_prices VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", CHEESE, 12.90, 5),
            ("RAMI_LEVY", CREAM, 6.90, 5),
            ("RAMI_LEVY", COFFEE, 29.90, 5),
            ("RAMI_LEVY", DEAL_PRODUCT, 10.00, 5),
            ("RAMI_LEVY", PLAIN_PRODUCT, 8.00, 5),
            ("RAMI_LEVY", A_EXACT, 5.00, 5),
            *[(c, A_LONG, 20.00, 5) for c in
              ("RAMI_LEVY", "SHUFERSAL", "CHAIN_C", "CHAIN_D", "CHAIN_E", "CHAIN_F")],
            ("RAMI_LEVY", B_LEAD, 6.00, 5),
            *[(c, B_MID, 15.00, 5) for c in
              ("RAMI_LEVY", "SHUFERSAL", "CHAIN_C", "CHAIN_D", "CHAIN_E", "CHAIN_F")],
            ("RAMI_LEVY", C_LOW, 4.00, 5),
            *[(c, C_HIGH, 4.50, 5) for c in
              ("RAMI_LEVY", "SHUFERSAL", "CHAIN_C", "CHAIN_D", "CHAIN_E", "CHAIN_F")],
            ("RAMI_LEVY", D_SHORT, 3.00, 5),
            ("SHUFERSAL", D_SHORT, 3.20, 5),
            ("RAMI_LEVY", D_LONG, 8.00, 5),
            ("SHUFERSAL", D_LONG, 8.20, 5),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "RAMI_LEVY", "P1", DEAL_PRODUCT, 0, 0, 1, 5.00, 5.00,
             "half off", "2026-09-01", "2026-12-31"),
        ],
    )
    conn.execute("INSERT INTO promo_stores VALUES (1, '10')")
    conn.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [
            ("built_at", BUILT_AT),
            ("chain_as_of", json.dumps({})),
            ("sources", json.dumps(["rami_levy.db", "shufersal.db"])),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    """A real app.db, built by build_app_db.build() end to end (KAN-12 Step
    3's instruction)."""
    prices_path = str(tmp_path / "prices.db")
    _build_prices_db(prices_path)
    out_path = str(tmp_path / "app.db")
    build_app_db.build(prices_path, out_path)
    return out_path


_peer_counter = itertools.count()


def _client_with_fresh_peer():
    """A TestClient whose ASGI-visible TCP peer is unique to this call.

    api.main's RateLimitMiddleware is built once, at import time, into a
    single ASGI app whose per-IP token buckets live for the whole pytest
    process - so a plain ``TestClient(app)`` shares one "testclient" bucket
    across every test in the whole session (this file's and
    tests/api/test_rate_limit.py's, which deliberately drains it). A fresh,
    never-seen-before peer per test client keeps these tests independent of
    rate-limiting and of test execution order, exactly like
    test_rate_limit.py's own ``_client_with_peer`` helper.
    """
    peer_ip = f"test-peer-{next(_peer_counter)}"

    async def app_with_forced_peer(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(peer_ip, 12345))
        await app(scope, receive, send)

    return TestClient(app_with_forced_peer)


@pytest.fixture
def client(app_db, monkeypatch):
    monkeypatch.setenv("APP_DB", app_db)
    return _client_with_fresh_peer()


def _names(body):
    return [r.get("name") for r in body["results"]]


def test_percent_sign_never_500s(client):
    resp = client.get("/search", params={"q": "15%"})
    assert resp.status_code == 200
    assert resp.json()["results"]  # cream/coffee/cheese all carry "15"


def test_cheese_outranks_cream_and_coffee_capsules(client):
    resp = client.get("/search", params={"q": "גבינה 15 אחוז"})
    assert resp.status_code == 200
    names = _names(resp.json())
    assert names, "expected at least one result"
    assert names[0] == "גבינה צהובה 15% דק", names


def test_unmatched_token_is_ignored_not_fatal(client):
    resp = client.get("/search", params={"q": "זזזזז צהובה 15"})
    assert resp.status_code == 200
    names = _names(resp.json())
    assert "גבינה צהובה 15% דק" in names


def test_query_with_no_tokens_at_all_returns_empty(client):
    """KAN-8's review fix (2026-09-14) makes ``build_match`` keep every
    token, filler included - the spec's original "filler-only query returns
    []" case no longer exists (a filler-only query like "500" now still
    searches for "500"). The only way left to get None out of build_match is
    a query that tokenises to nothing, e.g. punctuation alone."""
    resp = client.get("/search", params={"q": "!!!"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_query_with_no_tokens_never_queries_fts(app_db):
    """Same case as above, but proved at the ``run_search`` level: a query
    that tokenises to nothing must return before the FTS candidate query
    runs - not just happen to come back empty. (``built_at`` is still read -
    that's every response's own metadata, not part of the search.)"""
    import sqlite3

    from api.search import run_search

    real_conn = sqlite3.connect(app_db)

    class _GuardedConnection:
        def execute(self, sql, *args, **kwargs):
            assert "fts" not in sql.lower(), f"run_search queried FTS: {sql!r}"
            return real_conn.execute(sql, *args, **kwargs)

    result = run_search(_GuardedConnection(), "!!!")
    assert result["results"] == []


def test_exact_name_match_outranks_longer_name_containing_the_query(client):
    """KAN-24: tier 0 (the query IS the name) beats tier 2 (the query is
    somewhere in a longer name) even though the longer name has far more
    chains - this is the case the owner rejected a chains-only scheme over
    (milk jam over drinking milk)."""
    resp = client.get("/search", params={"q": "בננה"})
    assert resp.status_code == 200
    names = _names(resp.json())
    assert names[0] == "בננה", names
    assert "עוגת בננה טרופית ומדהימה מאוד" in names[1:]


def test_leading_word_match_outranks_mid_name_match(client):
    """KAN-24: tier 1 (the name STARTS with the query) beats tier 2 (the
    query is mid-name), again despite the tier-2 product having more
    chains."""
    resp = client.get("/search", params={"q": "תפוח"})
    assert resp.status_code == 200
    names = _names(resp.json())
    assert names.index("תפוח עץ ירוק") < names.index("מיץ תפוח טבעי מרוכז")


def test_more_chains_wins_within_the_same_tier(client):
    """KAN-24: within one tier, chains is the tie-break - ahead of bm25."""
    resp = client.get("/search", params={"q": "גזר"})
    assert resp.status_code == 200
    names = _names(resp.json())
    assert names.index("גזר טרי בקילו ארוז") < names.index("גזר טרי בשקית")


def test_bm25_breaks_a_remaining_tie(client):
    """KAN-24: same tier, same chain count - bm25 (shorter, more focused
    document) still decides, exactly as it did before this change."""
    resp = client.get("/search", params={"q": "בצל"})
    assert resp.status_code == 200
    names = _names(resp.json())
    assert names.index("ירקות בצל") < names.index(
        "ירקות בצל קלוי ומתובל בתערובת תבלינים מיוחדת"
    )


def test_deals_only_returns_only_products_with_a_real_discount(client):
    resp = client.get("/search", params={"q": "שוקולד", "deals_only": 1})
    assert resp.status_code == 200
    body = resp.json()
    barcodes = {r["barcode"] for r in body["results"]}
    assert DEAL_PRODUCT in barcodes
    assert PLAIN_PRODUCT not in barcodes
    for r in body["results"]:
        assert r["on_deal"] is True


def test_deals_only_result_reports_on_deal_and_correct_price(client):
    resp = client.get("/search", params={"q": "מבצע", "deals_only": 1})
    body = resp.json()
    assert len(body["results"]) == 1
    row = body["results"][0]
    assert row["barcode"] == DEAL_PRODUCT
    assert row["on_deal"] is True


def test_non_deals_only_search_still_flags_on_deal(client):
    resp = client.get("/search", params={"q": "מבצע"})
    body = resp.json()
    row = next(r for r in body["results"] if r["barcode"] == DEAL_PRODUCT)
    assert row["on_deal"] is True


def test_query_over_100_chars_is_422(client):
    resp = client.get("/search", params={"q": "א" * 101})
    assert resp.status_code == 422


def test_query_of_exactly_100_chars_is_accepted(client):
    resp = client.get("/search", params={"q": "א" * 100})
    assert resp.status_code == 200


def test_whitespace_only_query_is_422(client):
    resp = client.get("/search", params={"q": "   "})
    assert resp.status_code == 422


def test_response_has_built_at(client):
    resp = client.get("/search", params={"q": "שוקולד"})
    assert resp.json()["built_at"] == BUILT_AT


def test_limit_is_respected(client):
    resp = client.get("/search", params={"q": "15", "limit": 1})
    assert len(resp.json()["results"]) <= 1


def test_limit_out_of_range_is_422(client):
    assert client.get("/search", params={"q": "חלב", "limit": 0}).status_code == 422
    assert client.get("/search", params={"q": "חלב", "limit": 51}).status_code == 422
