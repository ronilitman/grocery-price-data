"""``GET /deals`` - the Discounts page's paginated card list (KAN-15).

Reads ``deal_products`` (KAN-15, ``scripts/build_app_db.py``), not ``deals``
directly: ``deal_products`` already holds exactly one row per product with a
real discount, chosen by the owner's rule (lowest unit_price wins,
regardless of minimum quantity) - so this module never re-groups anything,
it only filters, paginates and shapes what's already there. See
``deal_products``'s own schema comment for why a per-request GROUP BY over
the full ``deals`` table would be needed without it, and why that would cost
a scan of ~426k rows on every request on a 1 GB VM.

Keyset pagination, never OFFSET (app.db is replaced nightly - see the root
CLAUDE.md and KAN-4): the cursor is base64url JSON of
``[discount_pct, deal_id]`` for the last row of a page; the next page reads
``WHERE (discount_pct < ?) OR (discount_pct = ? AND deal_id > ?)``, matching
the table's own ``(discount_pct DESC, deal_id ASC)`` order exactly so a walk
through every page visits every qualifying product exactly once, in a stable
order, even if rows are being read as app.db swaps underneath.

KAN-20: rows this endpoint returns are not filtered for the pre-existing
~90%+-off data bugs the owner tracked in KAN-20 (Dor Alon, City Market). They
sort to the top on purpose - the page is meant to surface them, not hide
them behind an algorithm's judgement call about what looks "too good to be
true". The Discounts page's spec and the KAN-15 report ask them to be listed,
not filtered.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from api import catalog
from api.main import db_path, get_connection
from pathlib import Path
from scripts import app_search

router = APIRouter()

MIN_LIMIT = 1
MAX_LIMIT = 48
DEFAULT_LIMIT = 24

# The exact response item shape the spec lists (barcode..chains_on_deal);
# generic_key is added separately below, and deal_id/category_id are query
# plumbing only (the cursor and the chain_id/category_id filters), never
# part of a response item.
_ITEM_COLUMNS = (
    "barcode", "name", "chain_id", "base_price", "unit_price", "price",
    "min_qty", "club", "coupon", "ends", "discount_pct", "chains_on_deal",
)
_QUERY_COLUMNS = _ITEM_COLUMNS + ("deal_id",)


def _encode_cursor(discount_pct: float, deal_id: int) -> str:
    raw = json.dumps([discount_pct, deal_id]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str):
    """[discount_pct, deal_id], or raises ValueError on anything malformed -
    the route turns that into a 400, never a 500."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"malformed cursor: {exc}") from exc
    if (not isinstance(data, list) or len(data) != 2
            or not isinstance(data[0], (int, float))
            or isinstance(data[0], bool)
            or not isinstance(data[1], int) or isinstance(data[1], bool)):
        raise ValueError("cursor must decode to [discount_pct, deal_id]")
    return float(data[0]), int(data[1])


def run_deals(
    conn,
    limit: int = DEFAULT_LIMIT,
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    chain_id: Optional[str] = None,
    category_id: Optional[int] = None,
) -> dict:
    """Pure function: everything /deals does, given an open connection. No
    HTTP, no FastAPI - tests call this directly against a fixture app.db, or
    the route below wraps it and turns a bad cursor into a 400.
    """
    meta = catalog.read_meta(conn)
    built_at = meta.get("built_at")

    where = []
    params: list = []

    if chain_id is not None:
        where.append("chain_id = ?")
        params.append(chain_id)
    if category_id is not None:
        where.append("category_id = ?")
        params.append(category_id)

    if q is not None and q.strip():
        match = app_search.build_match(q)
        if match is None:
            return {"built_at": built_at, "items": [], "next_cursor": None}
        where.append(
            "barcode IN (SELECT barcode FROM fts_deals WHERE fts_deals MATCH ?)")
        params.append(match)

    if cursor is not None:
        cursor_discount, cursor_deal_id = _decode_cursor(cursor)
        where.append("(discount_pct < ? OR (discount_pct = ? AND deal_id > ?))")
        params.extend([cursor_discount, cursor_discount, cursor_deal_id])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT {', '.join(_QUERY_COLUMNS)} FROM deal_products "
        f"{where_sql} "
        f"ORDER BY discount_pct DESC, deal_id ASC "
        f"LIMIT ?"
    )
    params.append(limit + 1)

    rows = conn.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]

    # generic_key ("g"): the same KAN-9-precedence lookup /product uses, so a
    # deal card that belongs to a generic (loose produce) opens the same
    # product the modal would via productPath - a generic wins when present.
    barcodes = [row[0] for row in page]
    generic_of = catalog.generic_keys_for_barcodes(conn, barcodes)

    items = []
    for row in page:
        values = dict(zip(_QUERY_COLUMNS, row))
        del values["deal_id"]
        values["generic_key"] = generic_of.get(values["barcode"])
        items.append(values)

    next_cursor = None
    if has_more and page:
        last_discount_pct = page[-1][_QUERY_COLUMNS.index("discount_pct")]
        last_deal_id = page[-1][_QUERY_COLUMNS.index("deal_id")]
        next_cursor = _encode_cursor(last_discount_pct, last_deal_id)

    return {"built_at": built_at, "items": items, "next_cursor": next_cursor}


@router.get("/deals")
def get_deals(
    limit: int = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    chain_id: Optional[str] = None,
    category_id: Optional[int] = None,
    v: Optional[str] = None,
):
    """``GET /deals?limit=24&cursor=<opaque>&q=<text>&chain_id=<id>&category_id=<id>&v=<build>``.

    One card per product, on offer somewhere for real (``discount_pct > 0``),
    represented by its cheapest-per-unit deal - see ``deal_products``'s own
    schema comment and ``run_deals`` above. Ordered by that representative's
    ``discount_pct`` descending, ``deal_id`` ascending as the tie-break.
    ``cursor`` is opaque and must come from a previous page's
    ``next_cursor``; a malformed one is a 400, not a 500 or a silently wrong
    page. ``chain_id`` narrows to products whose *representative* deal is at
    that chain (a product's better deal elsewhere still wins the card even
    when a lesser deal also runs at the filtered chain - full branch/chain
    filtering is KAN-19). ``v`` is the client's build stamp, accepted and
    ignored like every other endpoint.
    """
    path = db_path()
    if not Path(path).exists():
        return JSONResponse(
            status_code=503, content={"error": f"database file missing: {path}"}
        )
    try:
        conn = get_connection()
    except Exception as exc:  # sqlite3.OperationalError - missing/unreadable file
        return JSONResponse(
            status_code=503, content={"error": f"database unreadable: {exc}"}
        )
    try:
        try:
            return run_deals(
                conn, limit=limit, cursor=cursor, q=q,
                chain_id=chain_id, category_id=category_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
