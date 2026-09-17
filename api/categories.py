"""``GET /categories`` and ``GET /categories/{id}/products`` (KAN-17): the
department -> aisle -> product-grid browse the app's Categories page reads.

Self-contained module + ``APIRouter``, same pattern as KAN-12's
``api/search.py`` and KAN-13's ``api/products.py`` - ``api/main.py`` only
gains a two-line ``include_router``.

``category_counts`` (``scripts/build_app_db.py``, KAN-17) does the heavy
lifting at build time: counts are a straight read, never a COUNT(*) per
request. This module's own job is keyset pagination over ``products``, and
turning each page of rows into priced, dealt items - live reads, because
price/deal state changes without a rebuild.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api import catalog
from api.main import get_connection

router = APIRouter()

DEFAULT_LIMIT = 24
MIN_LIMIT = 1
MAX_LIMIT = 100


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


# ---------------------------------------------------------------------------
# GET /categories - the department -> aisle tree, with counts.
# ---------------------------------------------------------------------------

@router.get("/categories")
def get_categories(v: Optional[str] = None):
    conn = _open_conn()
    try:
        _meta_or_503(conn)
        rows = conn.execute(
            "SELECT id, name_he, parent_id FROM categories ORDER BY id").fetchall()
        counts = dict(conn.execute("SELECT category_id, count FROM category_counts"))

        children_of = defaultdict(list)
        tops = []
        for category_id, name_he, parent_id in rows:
            if parent_id is None:
                tops.append((category_id, name_he))
            else:
                children_of[parent_id].append((category_id, name_he))

        categories = []
        for top_id, top_name in tops:
            children = []
            for child_id, child_name in children_of.get(top_id, []):
                count = counts.get(child_id, 0)
                if count <= 0:
                    continue  # hide empty aisles (KAN-17 scope decision)
                children.append({"id": child_id, "name_he": child_name, "count": count})
            total = sum(c["count"] for c in children)
            if total <= 0:
                continue  # hide empty departments
            categories.append({
                "id": top_id, "name_he": top_name, "count": total,
                "children": children,
            })

        return {"categories": categories}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /categories/{id}/products - keyset-paginated product grid.
# ---------------------------------------------------------------------------

def _encode_cursor(sort_key: str, barcode: str) -> str:
    raw = json.dumps([sort_key, barcode], ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parsed = json.loads(raw)
        sort_key, barcode = parsed
        if not isinstance(sort_key, str) or not isinstance(barcode, str):
            raise ValueError("cursor must decode to [sort_key, barcode] strings")
        return sort_key, barcode
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="bad cursor") from exc


def _barcodes_on_deal(conn, barcodes) -> set:
    """Batched: which of `barcodes` carry a `deals` row with discount_pct > 0."""
    if not barcodes:
        return set()
    placeholders = ",".join("?" * len(barcodes))
    return {
        barcode for (barcode,) in conn.execute(
            f"SELECT DISTINCT barcode FROM deals "
            f"WHERE discount_pct > 0 AND barcode IN ({placeholders})",
            list(barcodes))
    }


def _barcode_items(conn, barcodes):
    """{barcode: item_dict} for a page's rows."""
    basics = catalog.products_basic(conn, barcodes)
    prices = catalog.chain_prices_for(conn, barcodes)
    dealing = _barcodes_on_deal(conn, barcodes)

    out = {}
    for barcode in barcodes:
        name, is_weighted, _unit_qty = basics.get(barcode, (None, False, None))
        chain_prices = prices.get(barcode, {})
        price_values = [row[0] for row in chain_prices.values()]
        out[barcode] = {
            "barcode": barcode,
            "name": name,
            "min_price": min(price_values) if price_values else None,
            "chains": len(chain_prices),
            "on_deal": barcode in dealing,
            "weighted": bool(is_weighted),
        }
    return out


@router.get("/categories/{category_id}/products")
def get_category_products(
    category_id: int,
    limit: int = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    v: Optional[str] = None,
):
    conn = _open_conn()
    try:
        _meta_or_503(conn)

        row = conn.execute(
            "SELECT parent_id FROM categories WHERE id = ?", (category_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown category")
        (parent_id,) = row
        if parent_id is None:
            raise HTTPException(
                status_code=400,
                detail="category_id must be a sub-category, not a top-level department")

        count_row = conn.execute(
            "SELECT count FROM category_counts WHERE category_id = ?",
            (category_id,)).fetchone()
        count = count_row[0] if count_row else 0

        # Keyset on (sort_key, barcode) via idx_products_category_sort, never
        # OFFSET - app.db is replaced wholesale nightly, and an offset would
        # shift under a reader mid-session (see the CLAUDE.md/KAN-17 spec).
        if cursor:
            after_sort_key, after_barcode = _decode_cursor(cursor)
            rows = conn.execute(
                "SELECT barcode, sort_key FROM products "
                "WHERE category_id = ? "
                "AND (sort_key, barcode) > (?, ?) "
                "ORDER BY sort_key, barcode LIMIT ?",
                (category_id, after_sort_key, after_barcode, limit + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT barcode, sort_key FROM products "
                "WHERE category_id = ? "
                "ORDER BY sort_key, barcode LIMIT ?",
                (category_id, limit + 1),
            ).fetchall()

        has_more = len(rows) > limit
        page = rows[:limit]

        barcodes = sorted({b for b, _sk in page})
        barcode_items = _barcode_items(conn, barcodes)

        items = []
        for barcode, _sort_key in page:
            item = barcode_items.get(barcode)
            if item is not None:
                items.append(item)

        next_cursor = None
        if has_more:
            last_barcode, last_sort_key = page[-1]
            next_cursor = _encode_cursor(last_sort_key, last_barcode)

        return {"count": count, "items": items, "next_cursor": next_cursor}
    finally:
        conn.close()
