"""``GET /meta``, ``GET /product/{barcode}``, ``GET /generic/{key}`` and
``POST /products`` (KAN-13) - everything grocery-list-app's src/prices.js
reads from the published JSON shards except name search (KAN-12's
``/search``): product prices, per-branch prices, promotions, loose-produce
groups, chains and stores.

Self-contained module + ``APIRouter`` so this doesn't collide with KAN-12's
``api/search.py`` on the same service - ``api/main.py`` only gains a
two-line ``include_router``, matching the pattern KAN-12 already established.

The response shapes deliberately mirror today's published JSON (index.json,
stores.json, the barcode/detail/promo/generics shards - see the root
CLAUDE.md's "published contract") so KAN-14's client cut-over can rebuild
the same objects ``prices.js`` returns today. ``/product`` and ``/generic``
differ from the shards in one way on purpose: ``w``/``u``/``g`` (and
``n``/``i``/``a`` on a generic) are always present, even when null/0/empty,
rather than omitted when falsy - the shards omit them to save bytes on a
static file fetched by everyone; a single JSON response doesn't pay that
cost, and always-present fields are simpler for a client to read.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api import catalog
from api.main import get_connection

router = APIRouter()

MAX_BATCH_ITEMS = 50


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
# GET /meta
# ---------------------------------------------------------------------------

@router.get("/meta")
def get_meta(v: Optional[str] = None):
    conn = _open_conn()
    try:
        meta = _meta_or_503(conn)
        chains = dict(conn.execute("SELECT chain_id, name FROM chains"))
        stores = {}
        for chain_id, store_id, name, city, priced in conn.execute(
                "SELECT chain_id, store_id, store_name, city, priced_items FROM stores"):
            stores.setdefault(chain_id, {})[store_id] = [name or "", city or "", priced]
        (products,) = conn.execute("SELECT COUNT(*) FROM products").fetchone()
        (promo_products,) = conn.execute(
            "SELECT COUNT(DISTINCT barcode) FROM deals").fetchone()
        return {
            "built_at": meta.get("built_at"),
            "chain_as_of": catalog.chain_as_of(meta),
            "chains": chains,
            "stores": stores,
            "products": products,
            "promo_products": promo_products,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /product/{barcode}
# ---------------------------------------------------------------------------

def _product_response(conn, resolved_barcode, meta, stores_filter):
    """Assemble one /product-shaped object for an already-resolved barcode.
    Every lookup here is batched with a single-element list - the endpoint
    functions that call this for many barcodes at once (POST /products)
    build the same pieces in bulk instead of calling this per item."""
    today = catalog.today_from_meta(meta)
    basic = catalog.products_basic(conn, [resolved_barcode]).get(resolved_barcode)
    if basic is None:
        return None
    name, is_weighted, unit_qty = basic
    prices = catalog.chain_prices_for(conn, [resolved_barcode]).get(resolved_barcode, {})
    detail = catalog.price_exceptions_for(
        conn, [resolved_barcode], stores_filter).get(resolved_barcode, {})
    promo = catalog.promos_for(conn, [resolved_barcode], today).get(resolved_barcode, {})
    g = catalog.generic_keys_for_barcodes(conn, [resolved_barcode], meta).get(resolved_barcode)
    return {
        "barcode": resolved_barcode,
        "n": name,
        "w": 1 if is_weighted else 0,
        "u": unit_qty,
        "g": g,
        "p": dict(prices),
        "detail": {c: v for c, v in detail.items()},
        "promo": {c: v for c, v in promo.items()},
    }


@router.get("/product/{barcode}")
def get_product(barcode: str, stores: Optional[str] = None, v: Optional[str] = None):
    conn = _open_conn()
    try:
        meta = _meta_or_503(conn)
        resolved = catalog.resolve_barcodes(conn, [barcode]).get(barcode)
        if resolved is None:
            raise HTTPException(status_code=404, detail="unknown barcode")
        stores_filter = catalog.parse_stores_param(stores)
        return _product_response(conn, resolved, meta, stores_filter)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /generic/{key}
# ---------------------------------------------------------------------------

def _generic_response(conn, key, base, meta):
    """base is one entry of catalog.resolve_generics()'s output - the
    n/w/u/i/a/p/b fields already resolved through KAN-9's precedence."""
    today = catalog.today_from_meta(meta)
    member_barcodes = sorted({v[2] for v in base["p"].values()})
    detail_by_barcode = catalog.price_exceptions_for(conn, member_barcodes)
    promo_by_barcode = catalog.promos_for(conn, member_barcodes, today)
    detail = {}
    for by_chain in detail_by_barcode.values():
        for chain_id, rows in by_chain.items():
            detail.setdefault(chain_id, []).extend(rows)
    promo = {}
    for by_chain in promo_by_barcode.values():
        for chain_id, offer_list in by_chain.items():
            promo.setdefault(chain_id, []).extend(offer_list)
    return {**base, "detail": detail, "promo": promo}


@router.get("/generic/{key}")
def get_generic(key: str, v: Optional[str] = None):
    conn = _open_conn()
    try:
        meta = _meta_or_503(conn)
        resolved = catalog.resolve_generics(conn, [key], meta).get(key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="unknown generic key")
        return _generic_response(conn, key, resolved, meta)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /products (batch)
# ---------------------------------------------------------------------------

class BatchItem(BaseModel):
    barcode: Optional[str] = None
    generic_key: Optional[str] = None


class BatchRequest(BaseModel):
    items: list[BatchItem] = Field(default_factory=list)
    stores: Optional[list[str]] = None


@router.post("/products")
def post_products(body: BatchRequest):
    if len(body.items) > MAX_BATCH_ITEMS:
        raise HTTPException(
            status_code=422,
            detail=f"at most {MAX_BATCH_ITEMS} items per request, got {len(body.items)}")

    conn = _open_conn()
    try:
        meta = _meta_or_503(conn)
        stores_filter = catalog.parse_stores_list(body.stores)
        today = catalog.today_from_meta(meta)

        barcode_items = [item.barcode for item in body.items if item.barcode is not None]
        generic_items = [item.generic_key for item in body.items
                          if item.barcode is None and item.generic_key is not None]

        # --- barcodes: resolve, then one batched fetch per data need -----
        resolved_of = catalog.resolve_barcodes(conn, barcode_items)
        resolved_barcodes = sorted({b for b in resolved_of.values() if b is not None})
        basics = catalog.products_basic(conn, resolved_barcodes)
        prices = catalog.chain_prices_for(conn, resolved_barcodes)
        details = catalog.price_exceptions_for(conn, resolved_barcodes, stores_filter)
        promos = catalog.promos_for(conn, resolved_barcodes, today)
        generic_keys = catalog.generic_keys_for_barcodes(conn, resolved_barcodes, meta)

        # --- generics: resolve KAN-9 precedence, then batch their members -
        generics_resolved = catalog.resolve_generics(conn, generic_items, meta)
        all_member_barcodes = sorted({
            v[2] for base in generics_resolved.values() if base is not None
            for v in base["p"].values()
        })
        member_details = catalog.price_exceptions_for(conn, all_member_barcodes)
        member_promos = catalog.promos_for(conn, all_member_barcodes, today)

        results = {}
        for raw in barcode_items:
            resolved = resolved_of.get(raw)
            if resolved is None or resolved not in basics:
                results[raw] = None
                continue
            name, is_weighted, unit_qty = basics[resolved]
            results[raw] = {
                "barcode": resolved,
                "n": name,
                "w": 1 if is_weighted else 0,
                "u": unit_qty,
                "g": generic_keys.get(resolved),
                "p": dict(prices.get(resolved, {})),
                "detail": {c: v for c, v in details.get(resolved, {}).items()},
                "promo": {c: v for c, v in promos.get(resolved, {}).items()},
            }
        for key in generic_items:
            base = generics_resolved.get(key)
            if base is None:
                results[key] = None
                continue
            member_barcodes = sorted({v[2] for v in base["p"].values()})
            detail = {}
            for barcode in member_barcodes:
                for chain_id, rows in member_details.get(barcode, {}).items():
                    detail.setdefault(chain_id, []).extend(rows)
            promo = {}
            for barcode in member_barcodes:
                for chain_id, offer_list in member_promos.get(barcode, {}).items():
                    promo.setdefault(chain_id, []).extend(offer_list)
            results[key] = {**base, "detail": detail, "promo": promo}

        return {"built_at": meta.get("built_at"), "results": results}
    finally:
        conn.close()
