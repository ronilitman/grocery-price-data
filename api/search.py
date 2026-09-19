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

Ranking (KAN-24, re-decided 2026-09-19)
----------------------------------------
Plain bm25-then-chains ranking (KAN-12's original scheme) sank widely-stocked
products once KAN-29 gave them fuller names - bm25 penalises a longer
document, so a barcode still named plainly ``במבה`` outranked the 32-chain
``חטיף במבה מאנצ' צ'דר`` it used to tie with. The first KAN-24 fix put match
tier (below) ahead of chains, which traded that bug for a worse one: five
one-chain barcodes literally named ``שקדי מרק`` (chicken-soup almonds - a
snack, not the soup mix) outranked the 33-chain ``שקדי מרק רכיבים טבעי`` and
26-chain ``שקדי מרק 400 גר`` that almost every chain actually stocks, because
"the query IS the name" (tier 0) beat "the name starts with the query"
(tier 1) even when tier 0 meant a single store's odd listing. The owner
re-decided: chain count should outrank the tier-0-vs-tier-1 distinction, not
lose to it. The decided order is:

1. **Real match, or not** - collapse the old three-way tier into two groups:
   *matched* (the query IS the name, or the name STARTS with it - the old
   tiers 0 and 1) beats *not really matched* (the query only appears
   somewhere else in the name, via ``bm25()``'s own OR semantics - the old
   tier 2), full stop. ``_match_tier`` still returns 0/1/2 (the pool logic
   below still needs the finer distinction); ``_rank_group`` collapses it to
   0 or 1 for sorting.
2. **chains, most first** - *within* the matched group, this is now the
   primary key, which is the actual fix: ``שקדי מרק רכיבים טבעי`` (33
   chains, tier 1) now outranks the five tier-0 one-chain listings, because
   both are in the matched group and 33 beats 1. A not-really-matched
   product never gets to compete on chains at all, no matter how big the
   number - it is still behind every matched product, which is what keeps
   ``ריבת חלב`` (milk jam, up to 29 chains) and ``חלב מרוכז`` (condensed
   milk, up to 25) below ``חלב תנובה טרי`` (32 chains, tier 1): neither
   starts with ``חלב``, so both are tier 2 regardless of chain count.
   A plain chain-count-first scheme (chains ahead of *all* tier, including
   tier 2) was measured and rejected for the same reason plus a second,
   worse case found while measuring it: for ``שמן זית`` (olive oil), every
   real tier-1 olive oil tops out around 30 chains while unrelated products
   merely containing the word ``שמן`` (oil) - שמן קנולה (canola oil),
   shampoo - reach 32, so chains-before-tier put shampoo above olive oil.
   Bucketing chain counts (the fallback the ticket suggested) has the same
   problem one level down: any fixed bucket boundary that is wide enough to
   tie ``שקדי מרק``'s 33 against 26 is also wide enough to let ``שמן``'s
   32-chain shampoo tie against, and then beat on bm25, a 20-chain olive
   oil. Gating on tier 2 instead of on a chain-count number sidesteps that
   entirely - it needs no tuned constant, and every case above is decided
   without one.
3. **bm25()**, ascending (better match first) - the original ranking,
   unchanged, as the final tie-break, exactly as it already was within a
   tier.

Candidates: unchanged from the first KAN-24 fix, and still correct under the
new sort key. KAN-12's ``limit * 4`` bm25-ordered window undercounts once
anything but bm25 is the primary key - a tier-0/1 candidate can sit
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

This pool split still holds now that chains outrank tier 0-vs-1, and does
not need widening for it: group 0 (tier 0/1, sorted by chains) is exactly
the "every tier-0/1 candidate, however deep" pool already fetched in full,
so its chain-count ordering is exact, not an artifact of the window. Group
1 (tier 2) only ever fills remaining slots below every group-0 result, the
same window-bounded, bm25-first ordering KAN-12 always gave it - so a
tier-2 candidate outside the window can never be the row that should have
won, because a matched (group-0) candidate always wins first when one
exists. Checked directly against the real catalogue for all six ranking
queries below: hydrating the *entire* match set (no window at all) instead
of this pool produces byte-identical top-10s.
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


def _rank_group(tier: int) -> int:
    """0 for a real match (tier 0 or 1 - the query IS or STARTS the name),
    1 for tier 2 (the query only matched elsewhere via bm25's OR). Chains
    decide within group 0; group 1 never outranks group 0, however many
    chains it has - see the module docstring's KAN-24 section for why."""
    return 0 if tier < 2 else 1


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

    results.sort(key=lambda r: (_rank_group(r["_tier"]), -r["chains"], r["_rank"]))
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
