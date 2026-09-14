"""The key KAN-13 test: /product and /generic return exactly what today's
published shards say, for a fixture prices.db run through BOTH
build_catalog.py (the shards) and build_app_db.py (app.db) - proving the two
paths agree without relying on one to define the other.

Every barcode is checked; mapped generic keys are checked to differ from
generics.json in exactly the documented way (members/prices from
produce_units), never silently skipped.
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
    entries, of_barcode = cf.run_build_catalog(prices_db, catalog_dir)
    index, barcodes, detail, promo, generics = cf.load_catalog_output(catalog_dir)

    app_db = str(tmp / "app.db")
    cf.build_app_db_from_fixture(prices_db, app_db, tmp)

    return {
        "app_db": app_db, "index": index, "barcodes": barcodes,
        "detail": detail, "promo": promo, "generics": generics,
        "of_barcode": of_barcode,
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
    cf.WEIGHED_CODE, cf.UPC_A_CODE, cf.TOMATO_A, cf.TOMATO_B, cf.TOMATO_C,
    cf.CUCUMBER_A, cf.CUCUMBER_B,
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
    assert body["g"] == shard_entry.get("g")
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


def test_dairy_has_no_unit_no_generic_no_detail_no_promo(client):
    body = client.get(f"/product/{cf.DAIRY}").json()
    assert body["w"] == 0
    assert body["u"] is None
    assert body["g"] is None
    assert body["detail"] == {}
    assert body["promo"] == {}


# --------------------------------------------------------------------- generics

def test_unmapped_generic_matches_shard(client, built):
    """CUCUMBER has no produce_generic_map row at all - resolves exactly as
    generics.json, plus detail/promo across its members."""
    shard = built["generics"][cf.CUCUMBER_KEY]
    resp = client.get(f"/generic/{cf.CUCUMBER_KEY}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["n"] == shard["n"]
    assert body["w"] == shard.get("w", 0)
    assert body["u"] == shard.get("u")
    assert body["i"] == shard.get("i")
    assert body["p"] == shard["p"]
    assert sorted(body["b"]) == sorted(shard["b"])
    # No exceptions/promotions attached to either cucumber barcode in the
    # fixture, but the fields must still be present and empty.
    assert body["detail"] == {}
    assert body["promo"] == {}


def test_mapped_generic_differs_only_in_members_and_prices(client, built):
    """TOMATO is mapped (produce_generic_map). Per KAN-9 precedence: name,
    unit and image_id still come from the generic row (unchanged from the
    shard); members/prices come from produce_units instead - and are
    EXPECTED to differ, not to be skipped. The fixture is built so
    Shufersal's representative member actually changes (TOMATO_B, the
    algorithmic pick, -> TOMATO_C, produce_units' pick for that chain).
    """
    shard = built["generics"][cf.TOMATO_KEY]
    resp = client.get(f"/generic/{cf.TOMATO_KEY}")
    assert resp.status_code == 200
    body = resp.json()

    # Unchanged fields.
    assert body["n"] == shard["n"]
    assert body["w"] == shard.get("w", 0)
    assert body["u"] == shard.get("u")
    assert body["i"] == shard.get("i")

    # Expected-to-differ fields: assert the difference explicitly, not "not
    # equal" - so a future accident that makes them equal again is caught.
    assert shard["p"]["SHUFERSAL"][2] == cf.TOMATO_B
    assert body["p"]["SHUFERSAL"][2] == cf.TOMATO_C
    assert body["p"]["SHUFERSAL"][0] == pytest.approx(14.9)  # live chain_prices for TOMATO_C
    # Rami Levy's produce_units row happens to name the same barcode the
    # algorithmic grouping picked - that member is unchanged.
    assert shard["p"]["RAMI_LEVY"][2] == cf.TOMATO_A == body["p"]["RAMI_LEVY"][2]

    assert sorted(shard["b"]) == [cf.TOMATO_A, cf.TOMATO_B, cf.TOMATO_C]
    assert sorted(body["b"]) == [cf.TOMATO_A, cf.TOMATO_C]  # TOMATO_B dropped, on purpose


def test_barcode_in_produce_units_resolves_to_slugs_primary_key(client):
    """TOMATO_C is a produce_units member of the 'tomato' slug but is NOT
    the algorithmic generic's representative barcode for any chain (the
    'p' map never names it - see the mapped-key test above). KAN-9 step 3:
    a barcode in produce_units still resolves (via /product's `g` field) to
    its slug's primary key."""
    body = client.get(f"/product/{cf.TOMATO_C}").json()
    assert body["g"] == cf.TOMATO_KEY


# --------------------------------------------------------------------- misc DoD

def test_meta_chain_as_of_matches_index(client, built):
    resp = client.get("/meta")
    assert resp.status_code == 200
    assert resp.json()["chain_as_of"] == built["index"]["chain_as_of"]
    assert resp.json()["built_at"] == built["index"]["built_at"]


def test_unknown_barcode_404(client):
    assert client.get("/product/99999999999999").status_code == 404


def test_unknown_generic_key_404(client):
    assert client.get("/generic/not-a-real-key").status_code == 404


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
        {"generic_key": cf.CUCUMBER_KEY},
        {"generic_key": "not-a-real-key"},
    ]}
    resp = client.post("/products", json=body)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[cf.DAIRY] is not None
    assert results["0000000000000"] is None
    assert results[cf.CUCUMBER_KEY] is not None
    assert results["not-a-real-key"] is None


def test_batch_matches_single_item_endpoints(client, built):
    """POST /products must return the same shape the single-item endpoints
    do, for both a barcode and a generic key, proving the batched query
    path doesn't diverge from the per-item path."""
    body = {"items": [
        {"barcode": cf.EXPIRED_TRAP},
        {"generic_key": cf.TOMATO_KEY},
    ]}
    resp = client.post("/products", json=body)
    assert resp.status_code == 200
    results = resp.json()["results"]

    single_product = client.get(f"/product/{cf.EXPIRED_TRAP}").json()
    assert results[cf.EXPIRED_TRAP] == single_product

    single_generic = client.get(f"/generic/{cf.TOMATO_KEY}").json()
    assert results[cf.TOMATO_KEY] == single_generic


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
