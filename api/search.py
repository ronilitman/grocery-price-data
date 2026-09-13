"""``GET /search`` - full-text product search over ``fts_all``/``fts_deals``
(KAN-12). Self-contained module + ``APIRouter`` so KAN-13's ``/product`` work
on the same service doesn't collide with this file; ``api/main.py`` only
gains a two-line ``include_router``.

Two things changed after this task's spec was written (2026-09-14 review of
KAN-8/KAN-9) and this module follows the newer behaviour, not the spec text:

* ``scripts.app_search.build_match`` now keeps every token, filler included -
  dropping filler hurt ranking on real data (``"שוקולד חלב"`` used to search
  only ``חלב`` and rank plain milk above chocolate milk). ``None`` only means
  the query tokenised to nothing at all, not "all filler".
* The FTS ``name`` column holds normalised, tokenised text (e.g. a name with
  a bare "3" or a `"` in it comes back without them - see
  ``scripts.app_search.index_text``), so display always comes from
  ``products.name`` / ``generics.name``, joined by barcode/key, never from
  the FTS row itself.

Ranking and grouping (KAN-12 Step 2)
-------------------------------------
1. ``build_match(q)``; ``None`` -> ``results: []`` without querying.
2. Candidates come from ``bm25()`` over the chosen FTS table, ``limit * 4``
   of them so grouping still fills the page.
3. Each candidate barcode is resolved to either a generic's key or itself
   (see ``_resolve_group`` for the precedence rule from KAN-9).
4. Groups keep the best (lowest/most negative) ``bm25`` score any of their
   members earned as candidates - FTS5's ``bm25()`` ranks a better match
   with a smaller (more negative) number, so ascending order is "best
   first". Final order is that rank ascending, ``chains`` descending as the
   tie-break.
5. ``chains``/``min_price`` for a *group* are computed over ALL of the
   group's members (every produce_units row for a mapped slug, or every
   generic_members row for an unmapped key) - not just the member that
   happened to match the query text - so "how many chains carry this" stays
   correct regardless of which single barcode's name matched.
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


def _resolve_group(conn: sqlite3.Connection, barcode: str):
    """A candidate barcode's precedence-resolved identity (KAN-9 Step 3).

    Returns ``("generic", key, source)`` when the barcode collapses into a
    generic row (``source`` is ``"produce"`` when produce_units/
    produce_generic_map supplied it, ``"algorithmic"`` when generic_members
    did), or ``("barcode", barcode, None)`` when it stands on its own.

    Order matters: a barcode in ``produce_units`` resolves to its slug's
    *primary* key first; only when that slug has no mapped primary (7 of
    the 61 slugs have no generic key at all - KAN-9's report) does a
    ``generic_members`` hit apply instead, so an unmapped slug's barcode
    keeps working exactly as it did before KAN-9.
    """
    row = conn.execute(
        "SELECT slug FROM produce_units WHERE barcode = ? LIMIT 1", (barcode,)
    ).fetchone()
    if row is not None:
        primary = conn.execute(
            "SELECT generic_key FROM produce_generic_map "
            "WHERE slug = ? AND is_primary = 1",
            (row[0],),
        ).fetchone()
        if primary is not None:
            return "generic", primary[0], "produce"

    row = conn.execute(
        "SELECT key FROM generic_members WHERE barcode = ? LIMIT 1", (barcode,)
    ).fetchone()
    if row is not None:
        return "generic", row[0], "algorithmic"

    return "barcode", barcode, None


def _generic_row(conn: sqlite3.Connection, key: str, source: str) -> Optional[dict]:
    generic = conn.execute(
        "SELECT name, weighted FROM generics WHERE key = ?", (key,)
    ).fetchone()
    if generic is None:
        return None
    name, weighted = generic

    if source == "produce":
        # Members and prices come from produce_units for this slug (KAN-9
        # Step 3); display name/unit/image_id stay on the generics row so
        # the hand-resolved picture is kept.
        slug_row = conn.execute(
            "SELECT slug FROM produce_generic_map "
            "WHERE generic_key = ? AND is_primary = 1",
            (key,),
        ).fetchone()
        slug = slug_row[0]
        chains = {
            chain_id
            for (chain_id,) in conn.execute(
                "SELECT DISTINCT chain_id FROM produce_units WHERE slug = ?",
                (slug,),
            )
        }
        prices = [
            price
            for (price,) in conn.execute(
                "SELECT price FROM produce_units WHERE slug = ?", (slug,)
            )
        ]
    else:
        chains = set()
        prices = []
        for chain_id, barcode in conn.execute(
            "SELECT chain_id, barcode FROM generic_members WHERE key = ?", (key,)
        ):
            price_row = conn.execute(
                "SELECT price FROM chain_prices WHERE chain_id = ? AND barcode = ?",
                (chain_id, barcode),
            ).fetchone()
            if price_row is not None:
                chains.add(chain_id)
                prices.append(price_row[0])

    if not prices:
        # Every member has stopped being priced (produce_units drift, or an
        # unmapped generic whose members all dropped out of chain_prices) -
        # nothing honest to show, so this candidate contributes no result
        # rather than a min_price-less row.
        return None

    return {
        "generic_key": key,
        "name": name,
        "chains": len(chains),
        "min_price": min(prices),
        "weighted": bool(weighted),
    }


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
        # No chain currently prices it - nothing to rank or show a price
        # for; matches the generic path's same "no live price, no row" call.
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

    # Candidates arrive best-rank-first, so the first time a group is seen
    # is already its best member - later duplicates of the same group are
    # simply skipped rather than compared.
    order: list[str] = []
    pending: dict[str, tuple] = {}  # ident -> (kind, source, best_rank)
    for barcode, rank in candidates:
        kind, ident, source = _resolve_group(conn, barcode)
        if ident not in pending:
            pending[ident] = (kind, source, rank)
            order.append(ident)

    results = []
    for ident in order:
        kind, source, rank = pending[ident]
        row = _generic_row(conn, ident, source) if kind == "generic" else _barcode_row(conn, ident)
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
