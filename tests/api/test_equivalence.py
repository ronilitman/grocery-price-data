"""The key KAN-13 test: /product returns exactly what today's published
shards say, for a fixture prices.db run through BOTH build_catalog.py (the
shards) and build_app_db.py (app.db) - proving the two paths agree without
relying on one to define the other.

Every barcode is checked.
"""
import json
import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog_fixture as cf  # noqa: E402

import api.main as api_main  # noqa: E402
from api.main import app  # noqa: E402


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build the fixture prices.db once, then both build_catalog.py's
    shards and build_app_db.py's app.db from it, and hand back everything a
    test needs to compare."""
    tmp = tmp_path_factory.mktemp("kan13")
    prices_db = str(tmp / "prices.db")
    cf.build_prices_db(prices_db)

    catalog_dir = str(tmp / "catalog")
    cf.run_build_catalog(prices_db, catalog_dir)
    index, barcodes, detail, promo = cf.load_catalog_output(catalog_dir)

    app_db = str(tmp / "app.db")
    cf.build_app_db_from_fixture(prices_db, app_db, tmp)

    return {
        "app_db": app_db, "index": index, "barcodes": barcodes,
        "detail": detail, "promo": promo,
    }


@pytest.fixture()
def client(built, monkeypatch):
    monkeypatch.setenv("APP_DB", built["app_db"])
    # RateLimitMiddleware's token buckets live on the shared `app` singleton
    # for the whole test session (KAN-10) - starlette's TestClient always
    # presents as the same fake peer ("testclient"), so without this, this
    # file's ~30 requests would share one bucket with every other api test
    # file's requests and can trip 429s depending on run order. A fresh
    # random key per client gives this file its own bucket, same as a real
    # distinct caller would get - it doesn't touch test_rate_limit.py's own
    # (real) rate-limit behaviour at all.
    monkeypatch.setattr(api_main, "client_ip_for", lambda request: str(uuid.uuid4()))
    return TestClient(app)


ALL_BARCODES = [
    cf.DAIRY, cf.BRANCH_EXC, cf.CLUB_OFFER, cf.COUPON_OFFER, cf.EXPIRED_TRAP,
    cf.WEIGHED_CODE, cf.UPC_A_CODE,
]


def _sorted_offer(offer):
    """An offer dict with its only list fields (s/x) sorted, for comparison
    order-independence - the spec explicitly asks to "compare after sorting
    lists"."""
    out = dict(offer)
    for field in ("s", "x"):
        if field in out:
            out[field] = sorted(out[field])
    return out


def _sorted_promo(promo_for_barcode):
    return {chain_id: sorted((_sorted_offer(o) for o in offers),
                              key=lambda o: json.dumps(o, sort_keys=True))
            for chain_id, offers in (promo_for_barcode or {}).items()}


def _sorted_detail(detail_for_barcode):
    return {chain_id: sorted(rows) for chain_id, rows in (detail_for_barcode or {}).items()}


@pytest.mark.parametrize("barcode", ALL_BARCODES)
def test_product_matches_shards(client, built, barcode):
    """/product/{barcode} == the merge of the search-shard entry, the detail
    shard entry and the promo shard entry for that barcode."""
    shard_entry = built["barcodes"][barcode]
    resp = client.get(f"/product/{barcode}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["barcode"] == barcode
    assert body["n"] == shard_entry["n"]
    assert body["w"] == shard_entry.get("w", 0)
    assert body["u"] == shard_entry.get("u")
    assert body["p"] == shard_entry.get("p", {})
    assert _sorted_detail(body["detail"]) == _sorted_detail(built["detail"].get(barcode))
    assert _sorted_promo(body["promo"]) == _sorted_promo(built["promo"].get(barcode))


def test_expired_offer_still_counts_toward_everywhere(client):
    """The trap: RAMI_LEVY's EXPIRED_TRAP live offer runs at branches 10,20;
    an offer that ONLY ran at branch 30 expired before `built_at`. Branch 30
    never appears in `deals` at all, but it must still count toward
    "everywhere" (merge_chain unions branches before filtering expired
    offers) - so the live offer gets `x: ["30"]`, not neither. Branch "40"
    (a real RAMI_LEVY store that never ran ANY promotion) must NOT appear -
    proving the denominator is promo_everywhere, not store_bits' full
    per-chain branch count (which would also include "40" and wrongly flip
    this to `s: ["10", "20"]` - see api/catalog.py's promos_for docstring).
    """
    resp = client.get(f"/product/{cf.EXPIRED_TRAP}")
    assert resp.status_code == 200
    offers = resp.json()["promo"]["RAMI_LEVY"]
    assert len(offers) == 1
    assert offers[0].get("x") == ["30"]
    assert "s" not in offers[0]


def test_club_and_coupon_flags(client):
    club = client.get(f"/product/{cf.CLUB_OFFER}").json()["promo"]["RAMI_LEVY"][0]
    assert club.get("c") == 1
    assert "k" not in club

    coupon = client.get(f"/product/{cf.COUPON_OFFER}").json()["promo"]["RAMI_LEVY"][0]
    assert coupon.get("k") == 1
    assert "c" not in coupon


def test_branch_exception_detail(client):
    body = client.get(f"/product/{cf.BRANCH_EXC}").json()
    assert body["detail"] == {"RAMI_LEVY": [["20", 7.5]]}


def test_dairy_has_no_unit_no_detail_no_promo(client):
    body = client.get(f"/product/{cf.DAIRY}").json()
    assert body["w"] == 0
    assert body["u"] is None
    assert body["detail"] == {}
    assert body["promo"] == {}


# --------------------------------------------------------------------- misc DoD

def test_meta_chain_as_of_matches_index(client, built):
    resp = client.get("/meta")
    assert resp.status_code == 200
    assert resp.json()["chain_as_of"] == built["index"]["chain_as_of"]
    assert resp.json()["built_at"] == built["index"]["built_at"]


def test_unknown_barcode_404(client):
    assert client.get("/product/99999999999999").status_code == 404


def test_candidate_resolution_729000_prefix(client):
    """The spec's own example: scanning 7290000123456 finds a product
    stored as 123456 - here, scanning 729000000123 finds WEIGHED_CODE
    ('000123')."""
    resp = client.get("/product/729000000123")
    assert resp.status_code == 200
    assert resp.json()["barcode"] == cf.WEIGHED_CODE


def test_candidate_resolution_upc_a_leading_zero(client):
    resp = client.get("/product/0123456789012")
    assert resp.status_code == 200
    assert resp.json()["barcode"] == cf.UPC_A_CODE


def test_batch_of_51_is_422(client):
    body = {"items": [{"barcode": str(i)} for i in range(51)]}
    resp = client.post("/products", json=body)
    assert resp.status_code == 422


def test_batch_results_include_null_for_unknown_items(client):
    body = {"items": [
        {"barcode": cf.DAIRY},
        {"barcode": "0000000000000"},
    ]}
    resp = client.post("/products", json=body)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[cf.DAIRY] is not None
    assert results["0000000000000"] is None


def test_batch_matches_single_item_endpoints(client, built):
    """POST /products must return the same shape the single-item /product
    endpoint does, proving the batched query path doesn't diverge from the
    per-item path."""
    body = {"items": [{"barcode": cf.EXPIRED_TRAP}]}
    resp = client.post("/products", json=body)
    assert resp.status_code == 200
    results = resp.json()["results"]

    single_product = client.get(f"/product/{cf.EXPIRED_TRAP}").json()
    assert results[cf.EXPIRED_TRAP] == single_product


def test_stores_filter_narrows_detail(client):
    """?stores= on /product narrows `detail` to the given branches - here,
    a branch NOT in price_exceptions, so the filtered result drops the one
    real exception."""
    full = client.get(f"/product/{cf.BRANCH_EXC}").json()
    assert full["detail"] == {"RAMI_LEVY": [["20", 7.5]]}

    narrowed = client.get(
        f"/product/{cf.BRANCH_EXC}", params={"stores": "RAMI_LEVY:10"}).json()
    assert narrowed["detail"] == {}

    kept = client.get(
        f"/product/{cf.BRANCH_EXC}", params={"stores": "RAMI_LEVY:20"}).json()
    assert kept["detail"] == {"RAMI_LEVY": [["20", 7.5]]}
