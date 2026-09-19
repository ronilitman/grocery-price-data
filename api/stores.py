"""``GET /stores/nearby`` (KAN-34): which of our geocoded branches are close
to a given point.

Self-contained module + ``APIRouter``, same pattern as KAN-12's
``api/search.py``, KAN-13's ``api/products.py`` and KAN-17's
``api/categories.py`` - ``api/main.py`` only gains a two-line
``include_router``.

Coordinates come from ``stores.lat``/``lon``/``precision``, populated at
build time (``scripts/build_chain_db.py``) from the committed
``data/branch_locations.json`` and carried through ``scripts/merge_db.py``
and ``scripts/build_app_db.py`` exactly like ``branch_uid`` already is. This
module never geocodes anything - it only ever reads what the build wrote.

Two things the owner requires are easy to accidentally undo in a later
change, so both are re-stated at the point someone would touch them below:
who is eligible (``precision`` filtering) and what a response may contain
(no ``lat``/``lon``, no distance).
"""
from __future__ import annotations

import math
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api import catalog
from api.main import get_connection

router = APIRouter()

# Sensible for "which branch is near me" without a caller having to think
# about it; the hard ceiling below is the one that actually matters.
DEFAULT_RADIUS_KM = 5.0

# Hard cap (KAN-34 spec): the VM is a slow e2-micro behind a Tailscale
# Funnel that already adds ~0.8s per request, so a wide-radius request that
# forces a big haversine pass is expensive for everyone else's requests too.
# A request above this is REJECTED (422, via Query's own le= validation),
# not silently clamped - a clamped answer that looks like it covers 50km
# when it only covers 15 is worse than an error saying so.
MAX_RADIUS_KM = 15.0

# Degrees-per-km used only to build a generous SQL bounding box before exact
# haversine distance is computed on the (small) surviving set - see
# _bounding_box(). Both constants are deliberately on the small side, which
# makes the box on the large side: better to haversine a few extra rows than
# to clip the true circle and silently drop a real match.
_KM_PER_DEGREE_LAT = 110.574  # shortest anywhere on Earth (at the equator)
_KM_PER_DEGREE_LON_AT_EQUATOR = 111.320  # longest anywhere (shrinks by cos(lat))
_BOX_SLACK = 1.2  # +20%, so rounding/curvature never clips the circle


def _bounding_box(lat: float, radius_km: float) -> tuple[float, float]:
    """(delta_lat, delta_lon) degrees for a generous box around (lat, *).

    Generous on purpose - this is a pre-filter, not the answer. Exact
    distance is still checked on every row the box returns.
    """
    delta_lat = (radius_km / _KM_PER_DEGREE_LAT) * _BOX_SLACK
    # A degree of longitude is worth fewer km the further from the equator;
    # clamp the cosine so a query near a pole (never happens for Israel, but
    # this is generic code) can't blow the box up to the whole globe.
    lon_km_per_degree = _KM_PER_DEGREE_LON_AT_EQUATOR * max(math.cos(math.radians(lat)), 0.01)
    delta_lon = (radius_km / lon_km_per_degree) * _BOX_SLACK
    return delta_lat, delta_lon


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def _meta_or_503(conn):
    meta = catalog.read_meta(conn)
    if not meta.get("built_at"):
        raise HTTPException(status_code=503, detail="database not ready: meta is empty")
    return meta


def _open_conn():
    try:
        return get_connection()
    except Exception as exc:  # sqlite3.OperationalError - missing/unreadable file
        raise HTTPException(
            status_code=503, detail=f"database unreadable: {exc}") from exc


@router.get("/stores/nearby")
def get_stores_nearby(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(DEFAULT_RADIUS_KM, gt=0, le=MAX_RADIUS_KM),
    v: Optional[str] = None,
):
    """Branches within `radius_km` of (lat, lon), nearest first.

    Eligibility - ``precision = 'address'`` only. A ``'city'`` row's point is
    the town centre the geocoder could place, not the branch itself: an
    address-precision branch 3km away and a city-precision branch actually
    500m away would have the city one reported as farther, or as the
    "nearest" when the geocoder never even placed it - so 'city' rows (and
    NULL, meaning no coordinate at all) are excluded outright rather than
    included with a caveat.

    Response - never includes ``lat``/``lon``, and never includes a
    distance. Withholding the coordinate but handing back the distance from
    a caller-chosen origin would let the origin be walked around the branch
    and the exact position recovered by trilateration in three queries -
    that defeats the whole point of leaving lat/lon out. The result is
    still sorted nearest-first; the ranking is useful on its own without
    exposing the number that makes triangulation possible.
    """
    conn = _open_conn()
    try:
        _meta_or_503(conn)

        delta_lat, delta_lon = _bounding_box(lat, radius_km)
        rows = conn.execute(
            """
            SELECT s.chain_id, s.store_id, s.store_name, s.city_name,
                   s.address, s.lat, s.lon, c.name
            FROM stores s
            LEFT JOIN chains c ON c.chain_id = s.chain_id
            WHERE s.precision = 'address'
              AND s.lat BETWEEN ? AND ?
              AND s.lon BETWEEN ? AND ?
            """,
            (lat - delta_lat, lat + delta_lat, lon - delta_lon, lon + delta_lon),
        ).fetchall()

        candidates = []
        for chain_id, store_id, store_name, city_name, address, s_lat, s_lon, chain_name in rows:
            distance_km = _haversine_km(lat, lon, s_lat, s_lon)
            if distance_km <= radius_km:
                candidates.append((
                    distance_km,
                    {
                        "chain_id": chain_id,
                        "store_id": store_id,
                        "chain_name": chain_name,
                        "store_name": store_name,
                        "city": city_name,
                        "raw_address": address,
                    },
                ))
        candidates.sort(key=lambda pair: pair[0])

        return {"stores": [store for _distance, store in candidates]}
    finally:
        conn.close()
