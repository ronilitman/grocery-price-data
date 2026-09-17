"""``GET /search`` - full-text product search over ``fts_all``/``fts_deals``
(KAN-12). Self-contained module + ``APIRouter`` so KAN-13's ``/product`` work
on the same service doesn't collide with this file; ``api/main.py`` only
gains a two-line ``include_router``.

One thing changed after this task's spec was written (2026-09-14 review of
KAN-8) and this module follows the newer behaviour, not the spec text:
``scripts.app_search.build_match`` now keeps every token, filler included -
dropping filler hurt ranking on real data (``"שוקולד חלב"`` used to search
only ``חלב`` and rank plain milk above chocolate milk). ``None`` only means
the query tokenised to nothing at all, not "all filler".

The FTS ``name`` column holds normalised, tokenised text (e.g. a name with a
bare "3" or a `"` in it comes back without them - see
``scripts.app_search.index_text``), so display always comes from
``products.name``, joined by barcode, never from the FTS row itself.

Ranking (KAN-12 Step 2)
------------------------
1. ``build_match(q)``; ``None`` -> ``results: []`` without querying.
2. Candidates come from ``bm25()`` over the chosen FTS table, ``limit * 4``
   of them.
3. FTS5's ``bm25()`` ranks a better match with a smaller (more negative)
   number, so ascending order is "best first". Final order is that rank
   ascending, ``chains`` descending as the tie-break.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from api.main import db_path, get_connection
from scripts import app_search

router = APIRouter()

MIN_QUERY_LEN = 1
MAX_QUERY_LEN = 100
DEFAULT_LIMIT = 20
MIN_LIMIT = 1
MAX_LIMIT = 50
OVERFETCH_FACTOR = 4


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _barcode_row(conn: sqlite3.Connection, barcode: str) -> Optional[dict]:
    product = conn.execute(
        "SELECT name, category_id FROM products WHERE barcode = ?", (barcode,)
    ).fetchone()
    if product is None:
        return None
    name, category_id = product

    prices = [
        price
        for (price,) in conn.execute(
            "SELECT price FROM chain_prices WHERE barcode = ?", (barcode,)
        )
    ]
    if not prices:
        # No chain currently prices it - nothing to rank or show a price for.
        return None
    chains = conn.execute(
        "SELECT COUNT(DISTINCT chain_id) FROM chain_prices WHERE barcode = ?",
        (barcode,),
    ).fetchone()[0]
    on_deal = bool(
        conn.execute(
            "SELECT EXISTS(SELECT 1 FROM deals WHERE barcode = ? AND discount_pct > 0)",
            (barcode,),
        ).fetchone()[0]
    )

    return {
        "barcode": barcode,
        "name": name,
        "category_id": category_id,
        "chains": chains,
        "min_price": min(prices),
        "on_deal": on_deal,
    }


def run_search(
    conn: sqlite3.Connection,
    q: str,
    limit: int = DEFAULT_LIMIT,
    deals_only: bool = False,
) -> dict:
    """Pure function: everything ``/search`` does, given an open connection.

    No HTTP, no FastAPI - a test opens a sqlite3 connection to a fixture
    app.db and calls this directly, or the route below wraps it.
    """
    built_at = _meta(conn, "built_at")

    match = app_search.build_match(q)
    if match is None:
        return {"built_at": built_at, "results": []}

    table = "fts_deals" if deals_only else "fts_all"
    candidates = conn.execute(
        f"SELECT barcode, bm25({table}) AS rank FROM {table} "
        f"WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
        (match, limit * OVERFETCH_FACTOR),
    ).fetchall()

    results = []
    for barcode, rank in candidates:
        row = _barcode_row(conn, barcode)
        if row is None:
            continue
        row["_rank"] = rank
        results.append(row)

    results.sort(key=lambda r: (r["_rank"], -r["chains"]))
    for row in results:
        del row["_rank"]

    return {"built_at": built_at, "results": results[:limit]}


@router.get("/search")
def search(
    q: str = Query(...),
    limit: int = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    deals_only: int = Query(0, ge=0, le=1),
    v: Optional[str] = None,
):
    """``GET /search?q=<text>&limit=20&deals_only=0&v=<build>``.

    ``q`` must be 1-100 characters after trimming; ``limit`` clamps to
    1-50 (default 20); ``deals_only=1`` searches ``fts_deals`` instead of
    ``fts_all``. ``v`` is the client's build stamp - accepted and ignored,
    same convention as ``/health``.
    """
    trimmed = q.strip()
    if not (MIN_QUERY_LEN <= len(trimmed) <= MAX_QUERY_LEN):
        raise HTTPException(
            status_code=422,
            detail=f"q must be {MIN_QUERY_LEN}-{MAX_QUERY_LEN} characters after trimming",
        )

    path = db_path()
    if not Path(path).exists():
        return JSONResponse(
            status_code=503, content={"error": f"database file missing: {path}"}
        )
    try:
        conn = get_connection()
        try:
            return run_search(conn, trimmed, limit=limit, deals_only=bool(deals_only))
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": f"database unreadable: {path} ({exc})"},
        )
