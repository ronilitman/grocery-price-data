"""Endpoint-level KAN-13 tests that aren't about shard equivalence: /meta's
shape, the POST /products batching guarantee ("queries must be batched,
never a loop of single lookups"), and a couple of precedence/validation
edges the equivalence suite doesn't otherwise touch.

Shares tests/api/catalog_fixture.py's fixture with test_equivalence.py.
"""
import os
import sqlite3
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog_fixture as cf  # noqa: E402

from api.main import app  # noqa: E402  (import before api.products - see below)
import api.main as api_main  # noqa: E402
import api.products as products_mod  # noqa: E402

# api/products.py does `from api.main import get_connection` at module scope,
# and api/main.py includes api.products's router at module scope too - so
# whichever of the two is imported first must be `api.main` (its
# get_connection is defined before the include_router line runs), or the
# circular import only half-resolves.


@pytest.fixture(scope="module")
def app_db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("kan13-products")
    prices_db = str(tmp / "prices.db")
    cf.build_prices_db(prices_db)
    out = str(tmp / "app.db")
    cf.build_app_db_from_fixture(prices_db, out, tmp)
    return out


@pytest.fixture(autouse=True)
def _isolated_rate_limit_bucket(monkeypatch):
    """RateLimitMiddleware's token buckets live on the shared `app`
    singleton for the whole test session (KAN-10) - starlette's TestClient
    always presents as the same fake peer ("testclient"), so this file's
    requests would otherwise share one bucket with every other api test
    file's, and can trip 429s depending on run order/volume. A fresh random
    key per request gives every test here its own bucket, same as a real
    distinct caller would get - it doesn't touch test_rate_limit.py's own
    (real) rate-limit behaviour at all.
    """
    monkeypatch.setattr(api_main, "client_ip_for", lambda request: str(uuid.uuid4()))


@pytest.fixture()
def client(app_db, monkeypatch):
    monkeypatch.setenv("APP_DB", app_db)
    return TestClient(app)


def test_meta_shape(client):
    resp = client.get("/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "built_at", "chain_as_of", "chains", "stores", "products", "promo_products"}
    assert body["chains"] == {"RAMI_LEVY": "Rami Levy", "SHUFERSAL": "Shufersal"}
    assert body["stores"]["RAMI_LEVY"]["10"] == ["Rami Levy A", "3000", 100]
    assert body["products"] == 7
    # Distinct barcodes with a live offer in `deals`: CLUB_OFFER, COUPON_OFFER,
    # EXPIRED_TRAP (its live survivor) - the expired-only offer is not one.
    assert body["promo_products"] == 3


def test_meta_ignores_v_param(client):
    resp = client.get("/meta", params={"v": "some-build-stamp"})
    assert resp.status_code == 200


def test_health_and_products_coexist(client):
    """KAN-10's /health and KAN-13's /product both read the same app.db
    through the same connection helper - a smoke check that including this
    router didn't disturb /health (api/main.py only gained one import + one
    include_router line)."""
    assert client.get("/health").status_code == 200
    assert client.get(f"/product/{cf.DAIRY}").status_code == 200


def test_post_products_uses_bounded_queries_not_a_loop(app_db, monkeypatch):
    """The spec's own wording: "Queries must be batched (WHERE barcode IN
    (...)), not a loop of single lookups." Repeating the SAME two barcodes
    ten times over must cost exactly as many SQL statements as asking for
    them once - a per-item loop would instead multiply the count by 10.
    """
    monkeypatch.setenv("APP_DB", app_db)
    query_counts = []

    def counting_get_connection():
        conn = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=1")
        counter = {"n": 0}
        conn.set_trace_callback(lambda sql: counter.__setitem__("n", counter["n"] + 1))
        query_counts.append(counter)
        return conn

    monkeypatch.setattr(products_mod, "get_connection", counting_get_connection)
    client = TestClient(app)

    small_body = {"items": [{"barcode": cf.DAIRY}, {"barcode": cf.BRANCH_EXC}]}
    resp = client.post("/products", json=small_body)
    assert resp.status_code == 200
    small_n = query_counts[-1]["n"]

    many_body = {"items": [{"barcode": cf.DAIRY}, {"barcode": cf.BRANCH_EXC}] * 10}
    resp = client.post("/products", json=many_body)
    assert resp.status_code == 200
    many_n = query_counts[-1]["n"]

    assert many_n == small_n, (
        f"20 repeats of the same 2 barcodes issued {many_n} queries against "
        f"{small_n} for 2 - looks like a per-item loop, not a batch")


def test_post_products_item_with_no_barcode_is_uncounted(client):
    body = {"items": [{}]}
    resp = client.post("/products", json=body)
    assert resp.status_code == 200
    # No barcode on the item - it contributes nothing to `results` (no key
    # to report it under). The request as a whole still succeeds.
    assert resp.json()["results"] == {}


def test_post_products_built_at_matches_meta(client):
    meta = client.get("/meta").json()
    resp = client.post("/products", json={"items": []})
    assert resp.json()["built_at"] == meta["built_at"]
