"""KAN-17: GET /categories and GET /categories/{id}/products."""
import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import categories_fixture as cf  # noqa: E402

from api.main import app  # noqa: E402
import api.main as api_main  # noqa: E402


@pytest.fixture(scope="module")
def app_db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("kan17-categories")
    prices_db = str(tmp / "prices.db")
    cf.build_prices_db(prices_db)
    out = str(tmp / "app.db")
    cf.build_app_db_from_fixture(prices_db, out, tmp)
    return out


@pytest.fixture(autouse=True)
def _isolated_rate_limit_bucket(monkeypatch):
    """See tests/api/test_products.py's identical fixture: TestClient always
    presents as the same fake peer, so every test file sharing the app
    singleton needs its own bucket key or runs risk tripping 429s."""
    monkeypatch.setattr(api_main, "client_ip_for", lambda request: str(uuid.uuid4()))


@pytest.fixture()
def client(app_db, monkeypatch):
    monkeypatch.setenv("APP_DB", app_db)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /categories
# ---------------------------------------------------------------------------

def test_categories_tree_shape_and_counts(client):
    resp = client.get("/categories")
    assert resp.status_code == 200
    body = resp.json()

    # TOP_B is hidden: its only child (SUB_B1) has zero products.
    ids = {c["id"] for c in body["categories"]}
    assert ids == {cf.TOP_A}

    top_a = next(c for c in body["categories"] if c["id"] == cf.TOP_A)
    # SUB_A2 is hidden too (no products point at it); SUB_A1 has 7
    # categorised barcodes.
    assert [c["id"] for c in top_a["children"]] == [cf.SUB_A1]
    assert top_a["children"][0]["count"] == 7
    assert top_a["count"] == 7  # sum of (visible) children


def test_categories_ignores_v_param(client):
    resp = client.get("/categories", params={"v": "some-build-stamp"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /categories/{id}/products - validation
# ---------------------------------------------------------------------------

def test_top_level_id_is_400(client):
    resp = client.get(f"/categories/{cf.TOP_A}/products")
    assert resp.status_code == 400


def test_unknown_id_is_404(client):
    resp = client.get("/categories/999999/products")
    assert resp.status_code == 404


def test_empty_sub_category_is_still_a_valid_200(client):
    """SUB_A2 is a real sub-category with zero products - not a 404, just an
    empty page. (It's hidden from the /categories tree, but a direct hit on
    its id is a normal, if boring, request.)"""
    resp = client.get(f"/categories/{cf.SUB_A2}/products")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"count": 0, "items": [], "next_cursor": None}


def test_bad_cursor_is_400(client):
    resp = client.get(f"/categories/{cf.SUB_A1}/products", params={"cursor": "!!!not-base64"})
    assert resp.status_code == 400


def test_garbage_but_valid_base64_cursor_is_400(client):
    import base64
    junk = base64.urlsafe_b64encode(b"not json at all").decode("ascii")
    resp = client.get(f"/categories/{cf.SUB_A1}/products", params={"cursor": junk})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# The full walk: exactly once, in order, count matches, no leaked NULLs.
# ---------------------------------------------------------------------------

def _walk(client, category_id, limit):
    cursor = None
    items = []
    counts_seen = set()
    pages = 0
    while True:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(f"/categories/{category_id}/products", params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        counts_seen.add(body["count"])
        items.extend(body["items"])
        cursor = body["next_cursor"]
        pages += 1
        assert pages < 100, "runaway pagination - next_cursor never went None"
        if cursor is None:
            break
    assert len(counts_seen) == 1
    return items, counts_seen.pop()


def test_full_walk_matches_count_no_duplicates(client):
    items, count = _walk(client, cf.SUB_A1, limit=2)  # force multiple pages
    assert len(items) == count == 7

    idents = [it["barcode"] for it in items]
    assert len(idents) == len(set(idents)), "duplicate row across pages"

    # ZUCCHINI (category_id NULL) must never appear.
    assert cf.ZUCCHINI not in idents

    # TOMATO_RL and TOMATO_SF are two chains' own barcodes - ordinary,
    # separate rows.
    assert cf.TOMATO_RL in idents
    assert cf.TOMATO_SF in idents


def test_walk_is_in_sort_key_order(client):
    items, _count = _walk(client, cf.SUB_A1, limit=1)  # one row per page
    names = [it["name"] for it in items]
    assert names == sorted(names)


def test_walk_is_stable_regardless_of_page_size(client):
    small, _ = _walk(client, cf.SUB_A1, limit=1)
    large, _ = _walk(client, cf.SUB_A1, limit=50)
    key = lambda it: it["barcode"]  # noqa: E731
    assert [key(it) for it in small] == [key(it) for it in large]


# ---------------------------------------------------------------------------
# Item shape
# ---------------------------------------------------------------------------

def test_plain_item_shape(client):
    items, _count = _walk(client, cf.SUB_A1, limit=50)
    apple = next(it for it in items if it["barcode"] == cf.APPLE)
    assert apple["name"] == "Apple"
    assert apple["min_price"] == 4.90
    assert apple["chains"] == 1
    assert apple["weighted"] is False
    assert apple["on_deal"] is False


def test_weighted_item(client):
    items, _count = _walk(client, cf.SUB_A1, limit=50)
    carrot = next(it for it in items if it["barcode"] == cf.CARROT)
    assert carrot["weighted"] is True


def test_on_deal_item(client):
    items, _count = _walk(client, cf.SUB_A1, limit=50)
    banana = next(it for it in items if it["barcode"] == cf.BANANA)
    assert banana["on_deal"] is True
    date = next(it for it in items if it["barcode"] == cf.DATE)
    assert date["on_deal"] is False


def test_two_chains_own_tomato_barcodes_stay_separate(client):
    items, _count = _walk(client, cf.SUB_A1, limit=50)
    tomato_rl = next(it for it in items if it["barcode"] == cf.TOMATO_RL)
    tomato_sf = next(it for it in items if it["barcode"] == cf.TOMATO_SF)
    assert tomato_rl["weighted"] is True
    assert tomato_rl["chains"] == 1
    assert tomato_rl["min_price"] == 6.90
    assert tomato_sf["chains"] == 1
    assert tomato_sf["min_price"] == 7.90


def test_limit_is_respected(client):
    resp = client.get(f"/categories/{cf.SUB_A1}/products", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None


def test_limit_out_of_range_is_422(client):
    resp = client.get(f"/categories/{cf.SUB_A1}/products", params={"limit": 0})
    assert resp.status_code == 422
    resp = client.get(f"/categories/{cf.SUB_A1}/products", params={"limit": 1000})
    assert resp.status_code == 422
