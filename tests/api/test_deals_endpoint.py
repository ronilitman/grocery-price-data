"""GET /deals (KAN-15) against a real app.db built by build_app_db.py from a
fixture prices.db, not a hand-rolled deal_products table - so these tests
catch a drift between api/deals.py's assumptions and what build_app_db.py
(KAN-7/KAN-15) actually produces. Same pattern as tests/api/test_search.py.

Fixture layout (see `_build_prices_db`):

* CHOC_A/CHOC_B/CHOC_C - three שוקולד ("chocolate") products, discounts
  90 / 80 / 80 (a tie) - the backbone of the full-walk and search tests.
* MULTI_CHAIN - on offer at three chains with different unit prices; the
  cheapest-per-unit one (SHUFERSAL, 60% off) must be the representative,
  even though it is neither the biggest discount nor the first chain tried.
* LOW_DISCOUNT - a single, small (10%) discount, to round out the order.
* EXPIRED_ONLY - its only offer's `ends` is before the build date: absent
  from `deals` entirely (offers.merge_chain drops it), so absent from
  deal_products too.
* ZERO_DISCOUNT - a multi-buy deal no cheaper than the shelf price
  (discount_pct == 0): kept in `deals` (per KAN-7's spec) but must be
  excluded from deal_products/`/deals`.
* NO_DEAL - a plain product with no promo_offers row at all.
"""

import base64
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

CHOC_A = "1000000000001"   # 90% off
CHOC_B = "1000000000002"   # 80% off
CHOC_C = "1000000000003"   # 80% off - ties CHOC_B
MULTI_CHAIN = "2000000000001"
LOW_DISCOUNT = "2000000000002"
EXPIRED_ONLY = "2000000000003"
ZERO_DISCOUNT = "2000000000004"
NO_DEAL = "2000000000005"

BUILT_AT = "2026-09-14T02:00:00+00:00"
TODAY = "2026-09-14"


def _build_prices_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.executemany(
        "INSERT INTO chains VALUES (?,?)",
        [
            ("RAMI_LEVY", "Rami Levy"),
            ("SHUFERSAL", "Shufersal"),
            ("OSHER_AD", "Osher Ad"),
        ],
    )
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", "10", None, "Rami Levy A", "3000", "St 1", 100),
            ("SHUFERSAL", "1", None, "Shufersal A", "5000", "St 2", 100),
            ("OSHER_AD", "5", None, "Osher Ad A", "2000", "St 3", 100),
        ],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [
            (CHOC_A, "שוקולד מריר 70 אחוז", "Acme", "100", 100.0, "g", 0),
            (CHOC_B, "שוקולד חלב", "Acme", "100", 100.0, "g", 0),
            (CHOC_C, "עוגיות שוקולד", "Acme", "200", 200.0, "g", 0),
            (MULTI_CHAIN, "גבינה צהובה", "Acme", "200", 200.0, "g", 0),
            (LOW_DISCOUNT, "קפה שחור", "Acme", "200", 200.0, "g", 0),
            (EXPIRED_ONLY, "פסטה", "Acme", "500", 500.0, "g", 0),
            (ZERO_DISCOUNT, "אורז", "Acme", "1", 1.0, "kg", 0),
            (NO_DEAL, "חלב 3 אחוז", "Acme", "1", 1.0, "L", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO chain_prices VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", CHOC_A, 100.00, 5),    # -> 90% off at 10.00
            ("RAMI_LEVY", CHOC_B, 100.00, 5),    # -> 80% off at 20.00
            ("SHUFERSAL", CHOC_C, 100.00, 5),    # -> 80% off at 20.00 (tie)
            ("RAMI_LEVY", MULTI_CHAIN, 100.00, 5),   # -> 50% off at 50.00
            ("SHUFERSAL", MULTI_CHAIN, 100.00, 5),   # -> 60% off at 40.00 (best)
            ("OSHER_AD", MULTI_CHAIN, 100.00, 5),    # -> 40% off at 60.00
            ("RAMI_LEVY", LOW_DISCOUNT, 100.00, 5),  # -> 10% off at 90.00
            ("RAMI_LEVY", ZERO_DISCOUNT, 40.00, 5),  # multi-buy, not cheaper
            ("RAMI_LEVY", NO_DEAL, 6.90, 5),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "RAMI_LEVY", "P1", CHOC_A, 0, 0, 1, 10.00, 10.00,
             "dark choc deal", "2026-09-01", "2026-12-31"),
            (2, "RAMI_LEVY", "P2", CHOC_B, 0, 0, 1, 20.00, 20.00,
             "milk choc deal", "2026-09-01", "2026-12-31"),
            (3, "SHUFERSAL", "P3", CHOC_C, 0, 0, 1, 20.00, 20.00,
             "choc cookies deal", "2026-09-01", "2026-12-31"),
            (4, "RAMI_LEVY", "P4", MULTI_CHAIN, 0, 0, 1, 50.00, 50.00,
             "cheese at rami levy", "2026-09-01", "2026-12-31"),
            (5, "SHUFERSAL", "P5", MULTI_CHAIN, 0, 0, 1, 40.00, 40.00,
             "cheese at shufersal - cheapest unit price", "2026-09-01", "2026-12-31"),
            (6, "OSHER_AD", "P6", MULTI_CHAIN, 0, 0, 1, 60.00, 60.00,
             "cheese at osher ad", "2026-09-01", "2026-12-31"),
            (7, "RAMI_LEVY", "P7", LOW_DISCOUNT, 0, 0, 1, 90.00, 90.00,
             "coffee, small discount", "2026-09-01", "2026-12-31"),
            # Expired well before TODAY - offers.merge_chain drops it, so it
            # never reaches `deals` at all.
            (8, "RAMI_LEVY", "P8", EXPIRED_ONLY, 0, 0, 1, 5.00, 5.00,
             "expired pasta deal", "2026-01-01", "2026-02-01"),
            # Buy-2 deal that is not actually cheaper than the shelf price
            # (unit_price 40.00 == base_price 40.00) -> discount_pct == 0.
            (9, "RAMI_LEVY", "P9", ZERO_DISCOUNT, 0, 0, 2, 80.00, 40.00,
             "rice, not actually cheaper", "2026-09-01", "2026-12-31"),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_stores VALUES (?,?)",
        [
            (1, "10"), (2, "10"), (3, "1"),
            (4, "10"), (5, "1"), (6, "5"),
            (7, "10"), (8, "10"), (9, "10"),
        ],
    )
    conn.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [
            ("built_at", BUILT_AT),
            ("chain_as_of", json.dumps({})),
            ("sources", json.dumps(["rami_levy.db", "shufersal.db", "osher_ad.db"])),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    units_tsv = tmp_path / "produce_units.tsv"
    units_tsv.write_text(
        "slug\tname_he\tkind\tchain_id\tchain_name\tbarcode\t"
        "chain_product_name\tprice\tnote\n", encoding="utf-8")
    map_tsv = tmp_path / "produce_generic_map.tsv"
    map_tsv.write_text("slug\tgeneric_key\tprimary\tnote\n", encoding="utf-8")
    import build_app_db as bad_mod
    monkeypatch.setattr(bad_mod, "PRODUCE_UNITS_TSV", str(units_tsv))
    monkeypatch.setattr(bad_mod, "PRODUCE_GENERIC_MAP_TSV", str(map_tsv))

    prices_path = str(tmp_path / "prices.db")
    _build_prices_db(prices_path)
    out_path = str(tmp_path / "app.db")
    build_app_db.build(prices_path, out_path)
    return out_path


_peer_counter = itertools.count()


def _client_with_fresh_peer():
    """See tests/api/test_search.py's identical helper: a fresh ASGI-visible
    peer per client keeps RateLimitMiddleware's shared token bucket from
    leaking between tests."""
    peer_ip = f"test-deals-peer-{next(_peer_counter)}"

    async def app_with_forced_peer(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(peer_ip, 12345))
        await app(scope, receive, send)

    return TestClient(app_with_forced_peer)


@pytest.fixture
def client(app_db, monkeypatch):
    monkeypatch.setenv("APP_DB", app_db)
    return _client_with_fresh_peer()


def _barcodes(body):
    return [item["barcode"] for item in body["items"]]


# ---------------------------------------------------------------------------
# Full walk: every qualifying product exactly once, in order, no gaps.
# ---------------------------------------------------------------------------

def test_full_walk_matches_deal_products_ground_truth_exactly(app_db, client):
    """Independently read the true order straight from deal_products (the
    ground truth build_app_db.py wrote), then walk /deals a page at a time
    with a small limit and assert the concatenation matches exactly - same
    barcodes, same order, no duplicate, no gap."""
    conn = sqlite3.connect(app_db)
    expected = [row[0] for row in conn.execute(
        "SELECT barcode FROM deal_products ORDER BY discount_pct DESC, deal_id ASC"
    )]
    conn.close()
    assert set(expected) == {CHOC_A, CHOC_B, CHOC_C, MULTI_CHAIN, LOW_DISCOUNT}
    assert len(expected) == 5
    # CHOC_B/CHOC_C tie at 80% - deal_products already picked a stable
    # deal_id order for them; /deals must reproduce that exact order, not
    # just the same set, below.

    got = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/deals", params=params)
        assert resp.status_code == 200
        body = resp.json()
        got.extend(_barcodes(body))
        pages += 1
        assert pages < 20, "walk did not terminate"
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert got == expected
    assert len(got) == len(set(got)), "duplicate barcode across pages"


def test_ties_are_stable_across_repeated_calls(app_db, client):
    first = client.get("/deals", params={"limit": 48}).json()
    second = client.get("/deals", params={"limit": 48}).json()
    assert _barcodes(first) == _barcodes(second)
    # CHOC_B and CHOC_C are a genuine 80.0% tie; both present, adjacent, and
    # in the same order every time.
    order = _barcodes(first)
    assert set(order[1:3]) == {CHOC_B, CHOC_C}


def test_expired_and_non_positive_discount_deals_are_absent(app_db, client):
    body = client.get("/deals", params={"limit": 48}).json()
    barcodes = set(_barcodes(body))
    assert EXPIRED_ONLY not in barcodes
    assert ZERO_DISCOUNT not in barcodes
    assert NO_DEAL not in barcodes
    assert len(barcodes) == 5


# ---------------------------------------------------------------------------
# One card per product, represented by the cheapest-unit-price deal.
# ---------------------------------------------------------------------------

def test_multi_chain_product_shows_the_cheapest_unit_price_and_chain_count(app_db, client):
    body = client.get("/deals", params={"limit": 48}).json()
    item = next(i for i in body["items"] if i["barcode"] == MULTI_CHAIN)

    assert item["chain_id"] == "SHUFERSAL"
    assert item["unit_price"] == 40.00
    assert item["discount_pct"] == 60.0
    assert item["chains_on_deal"] == 3


def test_response_item_shape(app_db, client):
    body = client.get("/deals", params={"limit": 1}).json()
    item = body["items"][0]
    assert set(item) == {
        "barcode", "name", "chain_id", "base_price", "unit_price", "price",
        "min_qty", "club", "coupon", "ends", "discount_pct", "chains_on_deal",
        "generic_key",
    }
    assert body["built_at"] == BUILT_AT


# ---------------------------------------------------------------------------
# q narrows, keeps discount ordering (never switches to relevance).
# ---------------------------------------------------------------------------

def test_q_narrows_to_matching_products_keeping_discount_order(app_db, client):
    body = client.get("/deals", params={"limit": 48, "q": "שוקולד"}).json()
    barcodes = _barcodes(body)
    assert set(barcodes) == {CHOC_A, CHOC_B, CHOC_C}
    # Still discount_pct DESC, deal_id ASC - not bm25 relevance order.
    assert barcodes[0] == CHOC_A  # 90% - the only non-tied one


def test_q_with_no_matches_returns_empty_not_error(app_db, client):
    body = client.get("/deals", params={"q": "שזזזזזלא-קיים"}).json()
    assert body["items"] == []
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# limit and cursor validation
# ---------------------------------------------------------------------------

def test_bad_cursor_is_400(client):
    resp = client.get("/deals", params={"cursor": "not-valid-base64!!!"})
    assert resp.status_code == 400


def test_cursor_from_a_different_shape_is_400(client):
    garbage = base64.urlsafe_b64encode(json.dumps({"not": "a list"}).encode()).decode().rstrip("=")
    resp = client.get("/deals", params={"cursor": garbage})
    assert resp.status_code == 400


def test_limit_out_of_range_is_422(client):
    assert client.get("/deals", params={"limit": 0}).status_code == 422
    assert client.get("/deals", params={"limit": 49}).status_code == 422


def test_default_limit_is_24(app_db, client):
    resp = client.get("/deals")
    assert resp.status_code == 200
    # Only 5 qualifying products exist in the fixture, well under 24, so a
    # single page with no next_cursor proves the default limit didn't
    # truncate anything unexpectedly.
    assert resp.json()["next_cursor"] is None
