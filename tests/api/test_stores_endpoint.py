"""GET /stores/nearby (KAN-34) against a real app.db built by
build_app_db.py from a fixture prices.db - not a hand-rolled schema - so
these tests catch a drift between api/stores.py's assumptions and what
merge_db.py/build_app_db.py actually carry through for lat/lon/precision.

Fixture layout - all branches sit around one origin point
(``ORIGIN_LAT``/``ORIGIN_LON``, a stand-in for central Tel Aviv):

* NEAR / MID / FAR_IN_RADIUS - precision='address', 0.15km / 1.46km / 4.37km
  from the origin respectively - all inside the default 5km radius, in that
  distance order, to prove nearest-first sorting.
* OUTSIDE - precision='address', 17.48km away - outside even the 15km cap,
  to prove a real distance filter is applied (not just the bounding box).
* CITY_CLOSE - precision='city', only 0.07km away (closer than everything
  above) - must NEVER be returned: a 'city' point is the town centre, not
  the branch.
* NULL_CLOSE - precision=NULL (never geocoded), 0.15km away - must also
  never be returned.

SPARSE_LAT/SPARSE_LON is a point nowhere near any fixture branch (a stand-in
for the Arava), for the "no branches nearby" case.
"""

import itertools
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

TODAY = "2026-09-19"

ORIGIN_LAT, ORIGIN_LON = 32.0800, 34.7800
SPARSE_LAT, SPARSE_LON = 29.5000, 34.9000  # nowhere near any fixture branch

# (chain_id, store_id, store_name, lat, lon, precision) - distance from
# origin noted above each. chain_id doubles as a human-readable label.
STORE_ROWS = [
    ("RAMI_LEVY", "1", "Rami Levy Near", 32.0810, 34.7810, "address"),       # 0.146km
    ("RAMI_LEVY", "2", "Rami Levy Mid", 32.0900, 34.7900, "address"),        # 1.457km
    ("SHUFERSAL", "1", "Shufersal Far", 32.1100, 34.8100, "address"),        # 4.372km
    ("SHUFERSAL", "2", "Shufersal Outside", 32.2000, 34.9000, "address"),    # 17.484km
    ("OSHER_AD", "1", "Osher Ad City-Only", 32.0805, 34.7805, "city"),       # 0.073km, city precision
    ("OSHER_AD", "2", "Osher Ad Never Geocoded", 32.0810, 34.7810, None),    # 0.146km, no precision
]

NEAR, MID, FAR_IN_RADIUS, OUTSIDE, CITY_CLOSE, NULL_CLOSE = STORE_ROWS


def _build_prices_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.executemany("INSERT INTO chains VALUES (?,?,?)", [
        ("RAMI_LEVY", "Rami Levy", None),
        ("SHUFERSAL", "Shufersal", None),
        ("OSHER_AD", "Osher Ad", None),
    ])
    conn.executemany(
        "INSERT INTO stores "
        "(chain_id, store_id, subchain_id, store_name, city, city_name, "
        "address, priced_items, branch_uid, lat, lon, precision) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (chain_id, store_id, None, store_name, "5000", "תל אביב",
             f"Address for {store_name}", 0, None, lat, lon, precision)
            for chain_id, store_id, store_name, lat, lon, precision in STORE_ROWS
        ],
    )
    conn.executemany("INSERT INTO meta VALUES (?,?)", [
        ("built_at", f"{TODAY}T02:00:00+00:00"),
        ("chain_as_of", "{}"),
    ])
    conn.commit()
    conn.close()


@pytest.fixture
def app_db(tmp_path):
    prices_path = str(tmp_path / "prices.db")
    _build_prices_db(prices_path)
    out_path = str(tmp_path / "app.db")
    build_app_db.build(prices_path, out_path)
    return out_path


_peer_counter = itertools.count()


def _client_with_fresh_peer():
    """A fresh ASGI-visible peer per client keeps RateLimitMiddleware's
    shared token bucket from leaking between tests - see
    tests/api/test_search.py's identical helper."""
    peer_ip = f"test-stores-peer-{next(_peer_counter)}"

    async def app_with_forced_peer(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(peer_ip, 12345))
        await app(scope, receive, send)

    return TestClient(app_with_forced_peer)


@pytest.fixture
def client(app_db, monkeypatch):
    monkeypatch.setenv("APP_DB", app_db)
    return _client_with_fresh_peer()


def _names(stores):
    return [s["store_name"] for s in stores]


def test_default_radius_returns_nearby_address_branches_sorted_nearest_first(client):
    resp = client.get("/stores/nearby", params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON})
    assert resp.status_code == 200
    stores = resp.json()["stores"]
    assert _names(stores) == [NEAR[2], MID[2], FAR_IN_RADIUS[2]]


def test_precision_city_never_returned(client):
    # CITY_CLOSE is closer than everything else in the fixture (0.073km) -
    # if it ever leaked through it would be item 0, not merely present.
    resp = client.get(
        "/stores/nearby",
        params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "radius_km": 15},
    )
    assert resp.status_code == 200
    names = _names(resp.json()["stores"])
    assert CITY_CLOSE[2] not in names


def test_precision_null_never_returned(client):
    resp = client.get(
        "/stores/nearby",
        params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "radius_km": 15},
    )
    assert resp.status_code == 200
    names = _names(resp.json()["stores"])
    assert NULL_CLOSE[2] not in names


def test_wider_radius_includes_more_but_not_outside_15km(client):
    # OUTSIDE is 17.48km away - even the maximum allowed radius (15km)
    # must not reach it.
    resp = client.get(
        "/stores/nearby",
        params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "radius_km": 15},
    )
    assert resp.status_code == 200
    names = _names(resp.json()["stores"])
    assert names == [NEAR[2], MID[2], FAR_IN_RADIUS[2]]
    assert OUTSIDE[2] not in names


def test_radius_above_15km_is_rejected(client):
    resp = client.get(
        "/stores/nearby",
        params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "radius_km": 15.0001},
    )
    assert resp.status_code in (400, 422)

    resp2 = client.get(
        "/stores/nearby",
        params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "radius_km": 50},
    )
    assert resp2.status_code in (400, 422)


def test_radius_at_exactly_15km_is_accepted(client):
    resp = client.get(
        "/stores/nearby",
        params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "radius_km": 15},
    )
    assert resp.status_code == 200


def test_sparse_point_has_no_nearby_branches(client):
    resp = client.get(
        "/stores/nearby",
        params={"lat": SPARSE_LAT, "lon": SPARSE_LON, "radius_km": 15},
    )
    assert resp.status_code == 200
    assert resp.json()["stores"] == []


@pytest.mark.parametrize("lat,lon", [
    (95, 34.78),      # lat out of range
    (-95, 34.78),
    (32.08, 200),     # lon out of range
    (32.08, -200),
])
def test_invalid_coordinates_rejected(client, lat, lon):
    resp = client.get("/stores/nearby", params={"lat": lat, "lon": lon})
    assert resp.status_code in (400, 422)


def test_response_shape_has_only_the_documented_fields(client):
    resp = client.get(
        "/stores/nearby",
        params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "radius_km": 15},
    )
    assert resp.status_code == 200
    stores = resp.json()["stores"]
    assert stores, "fixture should have produced at least one result"
    expected_keys = {"chain_id", "store_id", "chain_name", "store_name", "city",
                      "raw_address"}
    for store in stores:
        assert set(store.keys()) == expected_keys


def test_no_lat_lon_or_distance_ever_leaks_into_the_response(client):
    """Hard requirement from the owner: withholding lat/lon (and never
    returning a distance either, so a caller can't trilaterate a branch's
    position over three queries) must survive a future change that adds a
    field back "helpfully". Checked both structurally (key names) and by
    scanning the raw body for banned substrings, so a differently-named
    leak (e.g. "latitude", "dist_km") is also caught.
    """
    resp = client.get(
        "/stores/nearby",
        params={"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "radius_km": 15},
    )
    assert resp.status_code == 200
    body = resp.json()
    stores = body["stores"]
    assert stores

    banned_key_fragments = ("lat", "lon", "dist")
    for store in stores:
        for key in store.keys():
            lowered = key.lower()
            for fragment in banned_key_fragments:
                assert fragment not in lowered, f"leaked field {key!r} in response"

    raw_text = resp.text.lower()
    for fragment in ("latitude", "longitude", "\"lat\"", "\"lon\"", "distance", "dist_km"):
        assert fragment not in raw_text
