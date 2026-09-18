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

Ranking (KAN-24)
-----------------
Plain bm25-then-chains ranking (KAN-12's original scheme, still the tie-break
below) sank widely-stocked products once KAN-29 gave them fuller names -
bm25 penalises a longer document, so a barcode still named plainly ``במבה``
outranked the 32-chain ``חטיף במבה מאנצ' צ'דר`` it used to tie with. The
owner measured a plain chain-count-first scheme too and rejected it: it put
``ריבת חלב`` (milk jam) and ``חלב מרוכז`` (condensed milk) above every
drinking milk, because a jam or a tin outnumbers fresh milk's chain count.
The decided order is:

1. **Match tier** - 0 if the query IS the name, 1 if the name STARTS with
   the query, 2 otherwise (``_match_tier``). Compared normalised-to-
   normalised: the FTS ``name`` column is already ``app_search.tokens(q)``
   run at index time, so the query is tokenised the same way and the two
   token lists are compared directly - a user typing ``במבה`` matches a
   stored ``במבה`` regardless of punctuation, and "the query IS the name"
   means the *whole* name, not just its first word.
2. **chains**, most first - the tie-break the owner asked for, and the one
   that puts drinking milk over milk jam once both are tier 2.
3. **bm25()**, ascending (better match first) - the original ranking,
   unchanged, as the final tie-break.

Candidates: KAN-12's ``limit * 4`` bm25-ordered window is no longer enough
on its own. It was safe when bm25 was the primary key, because truncating
early only ever dropped candidates that were already going to rank low.
Now tier and chains outrank bm25, and a tier-0/1 candidate can sit
arbitrarily deep in bm25 order (measured on the real catalogue: the
32-chain ``חלב תנובה טרי`` needed the *entire* 2,324-row ``חלב`` candidate
set before it surfaced - no fixed multiple of ``limit`` caught it within
2,324, let alone within a small one). So every match is fetched from the
FTS index (cheap - no join, ``bm25()`` sorts ~14k rows in ~30ms on the real
catalogue) and split into two pools: the bm25 window (``limit * 4``, as
before) for tier-2 candidates, plus *every* tier-0/1 candidate found
anywhere in the full match set, however deep. Only that combined pool is
hydrated (chains/price/deal, batched over all its barcodes in a handful of
``IN (...)`` queries rather than one query per candidate - the thing that
made fetching the whole match set for hydration too slow to try instead:
~370ms for ~14k candidates one row at a time on this repo's machine,
against ~10ms batched). A query whose only matches are all tier-2 (no
exact/leading match anywhere) costs exactly what KAN-12 did.
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


HYDRATE_CHUNK = 500


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _match_tier(indexed_name: str, q_tokens: list) -> int:
    """0 if ``indexed_name`` (the FTS ``name`` column - already
    ``app_search.tokens``) is exactly the query's tokens, 1 if the query's
    tokens are a leading prefix of it, 2 for anything else (bm25's own OR
    match still pulled it in, but only some of the words, or out of order,
    or in the middle of the name)."""
    name_tokens = indexed_name.split(" ") if indexed_name else []
    if name_tokens == q_tokens:
        return 0
    if name_tokens[: len(q_tokens)] == q_tokens:
        return 1
    return 2


def _hydrate(conn: sqlite3.Connection, barcodes: list):
    """products/chain_prices/deals for many barcodes at once, batched into
    ``IN (...)`` queries in chunks rather than one round trip per barcode -
    see the module docstring for why that's what makes considering every
    tier-0/1 candidate affordable."""
    chains_of: dict = {}
    prices_of: dict = {}
    on_deal: set = set()
    product_of: dict = {}

    for i in range(0, len(barcodes), HYDRATE_CHUNK):
        chunk = barcodes[i : i + HYDRATE_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        chains_of.update(
            conn.execute(
                f"SELECT barcode, COUNT(DISTINCT chain_id) FROM chain_prices "
                f"WHERE barcode IN ({placeholders}) GROUP BY barcode",
                chunk,
            )
        )
        for barcode, price in conn.execute(
            f"SELECT barcode, price FROM chain_prices WHERE barcode IN ({placeholders})",
            chunk,
        ):
            prices_of.setdefault(barcode, []).append(price)
        on_deal.update(
            barcode
            for (barcode,) in conn.execute(
                f"SELECT DISTINCT barcode FROM deals "
                f"WHERE barcode IN ({placeholders}) AND discount_pct > 0",
                chunk,
            )
        )
        product_of.update(
            (barcode, (name, category_id))
            for barcode, name, category_id in conn.execute(
                f"SELECT barcode, name, category_id FROM products "
                f"WHERE barcode IN ({placeholders})",
                chunk,
            )
        )

    return chains_of, prices_of, on_deal, product_of


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
    q_tokens = app_search.tokens(q)

    table = "fts_deals" if deals_only else "fts_all"
    # Every match, cheapest (best bm25) first - see the module docstring for
    # why this can no longer stop at a fixed window.
    all_candidates = conn.execute(
        f"SELECT barcode, name, bm25({table}) AS rank FROM {table} "
        f"WHERE {table} MATCH ? ORDER BY rank",
        (match,),
    ).fetchall()

    window = all_candidates[: limit * OVERFETCH_FACTOR]
    beyond_window = all_candidates[limit * OVERFETCH_FACTOR :]
    extra_tier01 = [
        row for row in beyond_window if _match_tier(row[1], q_tokens) < 2
    ]
    to_hydrate = window + extra_tier01

    chains_of, prices_of, on_deal, product_of = _hydrate(
        conn, [barcode for barcode, _name, _rank in to_hydrate]
    )

    results = []
    for barcode, indexed_name, rank in to_hydrate:
        if barcode not in product_of or barcode not in prices_of:
            # No chain currently prices it, or somehow not in products -
            # nothing to rank or show a price for.
            continue
        name, category_id = product_of[barcode]
        results.append(
            {
                "barcode": barcode,
                "name": name,
                "category_id": category_id,
                "chains": chains_of.get(barcode, 0),
                "min_price": min(prices_of[barcode]),
                "on_deal": barcode in on_deal,
                "_tier": _match_tier(indexed_name, q_tokens),
                "_rank": rank,
            }
        )

    results.sort(key=lambda r: (r["_tier"], -r["chains"], r["_rank"]))
    for row in results:
        del row["_tier"]
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
