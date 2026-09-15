"""``GET /deals`` - the Discounts page's paginated card list (KAN-15), plus
the chain/branch filters added by KAN-19.

With no ``chain_id``, this reads ``deal_products`` (KAN-15,
``scripts/build_app_db.py``), not ``deals`` directly: ``deal_products``
already holds exactly one row per product with a real discount, chosen by
the owner's rule (lowest unit_price wins, regardless of minimum quantity) -
so this module never re-groups anything in that mode, it only filters,
paginates and shapes what's already there. See ``deal_products``'s own
schema comment for why a per-request GROUP BY over the full ``deals`` table
would be needed without it, and why that would cost a scan of ~426k rows on
every request on a 1 GB VM.

With ``chain_id`` (KAN-19), ``deal_products``'s single global
"cheapest-anywhere" representative is the wrong shape - it can hide a
product that is genuinely on offer at the requested chain behind some other
chain's better deal. So this mode groups ``deals`` itself, narrowed first by
the indexed ``(chain_id, category_id, discount_pct DESC, deal_id)`` prefix
and (with ``store_id``) a ``has_branch()`` bit test that must run after that
narrowing - it cannot use an index (see KAN-19's Jira description for the
measured cost of getting this order backwards: 26ms narrowed by chain
first vs. 261ms for a full scan on 483k rows). No new build-time table: the
owner's KAN-19 spec prefers request-time grouping so a chain/branch filter
never has to wait for a nightly rebuild.

Keyset pagination, never OFFSET (app.db is replaced nightly - see the root
CLAUDE.md and KAN-4): the cursor is base64url JSON of
``[discount_pct, deal_id]`` for the last row of a page; the next page reads
``WHERE (discount_pct < ?) OR (discount_pct = ? AND deal_id > ?)``, matching
the table's own ``(discount_pct DESC, deal_id ASC)`` order exactly so a walk
through every page visits every qualifying product exactly once, in a stable
order, even if rows are being read as app.db swaps underneath. The same
cursor shape works for the chain/branch modes because their representative
rows are ordered the same way: ``discount_pct DESC, deal_id ASC``.

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


def _popcount(blob: bytes) -> int:
    """Number of set bits in a ``scripts.bitmap.pack`` blob. Trailing pad
    bits (n not a multiple of 8) are never set by ``pack``, so a plain
    whole-blob popcount already stays within the chain's real branch count -
    no masking needed."""
    return bin(int.from_bytes(blob, "little")).count("1")


def run_deals(
    conn,
    limit: int = DEFAULT_LIMIT,
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    chain_id: Optional[str] = None,
    category_id: Optional[int] = None,
    store_id: Optional[str] = None,
) -> dict:
    """Pure function: everything /deals does, given an open connection. No
    HTTP, no FastAPI - tests call this directly against a fixture app.db, or
    the route below wraps it and turns a bad cursor into a 400.

    ``store_id`` without ``chain_id`` is a 400 (nothing to look its bit up
    against); an unknown ``chain_id``, or a ``(chain_id, store_id)`` pair
    ``store_bits`` has no bit for, is a 404. Both are raised as
    ``HTTPException`` directly - they're routing-shaped errors, not the
    "malformed cursor" ``ValueError`` the route wraps into a 400.
    """
    if store_id is not None and chain_id is None:
        raise HTTPException(
            status_code=400,
            detail="store_id requires chain_id (a branch only means "
                   "something within a chain)")

    meta = catalog.read_meta(conn)
    built_at = meta.get("built_at")

    if chain_id is None:
        return _run_deals_default(
            conn, built_at, limit=limit, cursor=cursor, q=q,
            category_id=category_id)

    return _run_deals_for_chain(
        conn, built_at, limit=limit, cursor=cursor, q=q, chain_id=chain_id,
        category_id=category_id, store_id=store_id)


def _run_deals_default(conn, built_at, *, limit, cursor, q, category_id):
    """No ``chain_id``: unchanged KAN-15 behaviour, reading the
    already-grouped ``deal_products`` table."""
    where = []
    params: list = []

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


# Columns read from `deals` for the chain/branch-filtered modes - a superset
# of _ITEM_COLUMNS (deal_id for the cursor, branches for branches_on_deal;
# chains_on_deal is never part of these modes, see the module docstring).
_CHAIN_QUERY_COLUMNS = (
    "barcode", "deal_id", "chain_id", "base_price", "name", "unit_price",
    "price", "min_qty", "club", "coupon", "ends", "discount_pct", "branches",
)


def _run_deals_for_chain(
        conn, built_at, *, limit, cursor, q, chain_id, category_id, store_id):
    """``chain_id`` (optionally + ``store_id``): group ``deals`` itself at
    request time - see the module docstring for why ``deal_products`` (a
    single global cheapest-anywhere representative) is the wrong shape here.

    One card per product: the representative is that product's lowest-
    unit_price qualifying offer at the chain (ties broken by the lowest
    deal_id) - narrowed first to offers valid at ``store_id`` when given, so
    "best offer" means "best offer a shopper at that branch can actually
    use", not the chain's best offer anywhere.
    """
    if conn.execute(
            "SELECT 1 FROM chains WHERE chain_id = ?", (chain_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"unknown chain_id: {chain_id!r}")

    bit = None
    branches_total = None
    if store_id is not None:
        bit_row = conn.execute(
            "SELECT bit FROM store_bits WHERE chain_id = ? AND store_id = ?",
            (chain_id, store_id),
        ).fetchone()
        if bit_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown store_id {store_id!r} for chain_id {chain_id!r}")
        bit = bit_row[0]
    else:
        (branches_total,) = conn.execute(
            "SELECT COUNT(*) FROM store_bits WHERE chain_id = ?", (chain_id,)
        ).fetchone()

    # Indexed prefix first (idx_deals_chain_cat_discount), then the
    # unindexed filters - discount_pct and (with store_id) the bit test -
    # exactly the order KAN-19's Jira description measured.
    inner_where = ["chain_id = ?"]
    inner_params: list = [chain_id]
    if category_id is not None:
        inner_where.append("category_id = ?")
        inner_params.append(category_id)
    inner_where.append("discount_pct > 0")
    if bit is not None:
        inner_where.append("has_branch(branches, ?)")
        inner_params.append(bit)
    if q is not None and q.strip():
        match = app_search.build_match(q)
        if match is None:
            return {"built_at": built_at, "items": [], "next_cursor": None}
        inner_where.append(
            "barcode IN (SELECT barcode FROM fts_deals WHERE fts_deals MATCH ?)")
        inner_params.append(match)

    outer_where = ["rn = 1"]
    outer_params: list = []
    if cursor is not None:
        cursor_discount, cursor_deal_id = _decode_cursor(cursor)
        outer_where.append(
            "(discount_pct < ? OR (discount_pct = ? AND deal_id > ?))")
        outer_params.extend([cursor_discount, cursor_discount, cursor_deal_id])

    sql = (
        "WITH ranked AS ("
        f"  SELECT {', '.join(_CHAIN_QUERY_COLUMNS)}, "
        "          ROW_NUMBER() OVER ("
        "              PARTITION BY barcode ORDER BY unit_price ASC, deal_id ASC"
        "          ) AS rn"
        "   FROM deals"
        f"  WHERE {' AND '.join(inner_where)}"
        ") "
        f"SELECT {', '.join(_CHAIN_QUERY_COLUMNS)} FROM ranked "
        f"WHERE {' AND '.join(outer_where)} "
        "ORDER BY discount_pct DESC, deal_id ASC "
        "LIMIT ?"
    )
    params = inner_params + outer_params + [limit + 1]

    rows = conn.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]

    barcodes = [row[0] for row in page]
    generic_of = catalog.generic_keys_for_barcodes(conn, barcodes)

    items = []
    for row in page:
        values = dict(zip(_CHAIN_QUERY_COLUMNS, row))
        del values["deal_id"]
        branches_blob = values.pop("branches")
        if store_id is None:
            values["branches_on_deal"] = _popcount(branches_blob)
            values["branches_total"] = branches_total
        values["generic_key"] = generic_of.get(values["barcode"])
        items.append(values)

    next_cursor = None
    if has_more and page:
        last_discount_pct = page[-1][_CHAIN_QUERY_COLUMNS.index("discount_pct")]
        last_deal_id = page[-1][_CHAIN_QUERY_COLUMNS.index("deal_id")]
        next_cursor = _encode_cursor(last_discount_pct, last_deal_id)

    return {"built_at": built_at, "items": items, "next_cursor": next_cursor}


@router.get("/deals")
def get_deals(
    limit: int = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    chain_id: Optional[str] = None,
    category_id: Optional[int] = None,
    store_id: Optional[str] = None,
    v: Optional[str] = None,
):
    """``GET /deals?limit=24&cursor=<opaque>&q=<text>&chain_id=<id>&category_id=<id>&store_id=<id>&v=<build>``.

    Three modes (KAN-19):

    * No ``chain_id``: unchanged KAN-15 behaviour - one card per product, on
      offer somewhere for real (``discount_pct > 0``), represented by its
      cheapest-per-unit deal anywhere, with ``chains_on_deal``.
    * ``chain_id`` alone: every product with a qualifying offer *at that
      chain*, even when it's cheaper elsewhere - one card per product,
      represented by that chain's cheapest-per-unit qualifying offer. Each
      item carries ``branches_on_deal`` (branches where the representative
      offer applies) and ``branches_total`` (the chain's real branch count);
      ``chains_on_deal`` is omitted - the grouping is already chain-scoped.
    * ``chain_id`` + ``store_id``: only offers valid at that branch (a
      ``has_branch()`` bitmap test) - one card per product, represented by
      the best offer actually usable there. ``chains_on_deal``,
      ``branches_on_deal`` and ``branches_total`` are all omitted - there is
      only ever one branch in play.

    All three modes share the same ``discount_pct DESC, deal_id ASC``
    ordering and opaque keyset ``cursor``/``next_cursor``; ``q`` and
    ``category_id`` combine with any mode. A malformed cursor is a 400, not
    a 500 or a silently wrong page. ``store_id`` without ``chain_id`` is a
    400; an unknown ``chain_id`` or ``(chain_id, store_id)`` pair is a 404.
    ``v`` is the client's build stamp, accepted and ignored like every other
    endpoint.
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
                chain_id=chain_id, category_id=category_id, store_id=store_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
