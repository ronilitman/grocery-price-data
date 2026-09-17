"""Catalogue-reading helpers shared by the /meta, /product and /products
endpoints (KAN-13).

Every function here takes an already-open sqlite3.Connection (api.main's
get_connection()) and does exactly one batched query per call - never one
query per barcode/key - so a 50-item POST /products body costs a handful of
queries, not fifty.

``scripts`` is imported as a plain namespace package (same as KAN-12's
api/search.py: ``from scripts import app_search``), relying on the repo
root already being on sys.path - true wherever ``api.main`` itself is
importable (tests, and the VM's ``uvicorn api.main:app`` run from
``/srv/grocery/app``, which ``api/deploy/deploy.sh`` deploys ``scripts/``
alongside as a sibling directory).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from scripts import bitmap
from scripts import offers as offers_mod

_DIGITS = re.compile(r"\D")

# Same placeholder build_catalog.py's UNKNOWN_UNIT uses for "no real unit on
# file" - duplicated, not imported: build_catalog.py's own bare sibling
# imports only resolve when scripts/ itself is on sys.path, which this
# module deliberately doesn't add (KAN-12's `from scripts import X`
# convention, see the module docstring).
UNKNOWN_UNIT = "לא ידוע"  # "unknown" - what a chain publishes when it has no unit


def display_unit(is_weighted, unit_qty):
    """The `u` field's value: None unless the product is weighed AND has a
    real unit on file - matches build_catalog.py's own rule for "u" exactly
    (see UNKNOWN_UNIT above), so a non-weighed product's unit_qty (a raw
    package size like "750", meaningless as a unit) never leaks into `u`.
    """
    if not is_weighted:
        return None
    unit = (unit_qty or "").strip()
    if not unit or unit == UNKNOWN_UNIT:
        return None
    return unit


# ---------------------------------------------------------------------------
# Barcode candidates - server-side mirror of grocery-list-app's
# src/prices.js barcodeCandidates(), so a scan resolves the same way whether
# the client or the API does the matching.
# ---------------------------------------------------------------------------

def barcode_candidates(raw):
    """Digits only; if it starts with 729000 (Israel's weighed-goods internal
    prefix) also the remainder and the remainder without leading zeros; if 13
    digits starting with 0, also without it (UPC-A read as EAN-13). Order
    matters - the first candidate present in the catalogue is the match,
    same as the client.
    """
    barcode = _DIGITS.sub("", raw or "")
    if not barcode:
        return []
    tries = [barcode]
    if barcode.startswith("729000") and len(barcode) > 6:
        stripped = barcode[6:]
        tries.append(stripped)
        unpadded = stripped.lstrip("0")
        if unpadded and unpadded != stripped:
            tries.append(unpadded)
    if len(barcode) == 13 and barcode.startswith("0"):
        tries.append(barcode[1:])
    seen = set()
    out = []
    for candidate in tries:
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def resolve_barcodes(conn, raw_barcodes):
    """{raw: resolved_barcode_or_None} for a batch of raw scan strings - one
    query against `products` covering every candidate of every item."""
    candidates_of = {raw: barcode_candidates(raw) for raw in raw_barcodes}
    all_candidates = sorted({c for cs in candidates_of.values() for c in cs})
    existing = set()
    if all_candidates:
        placeholders = ",".join("?" * len(all_candidates))
        existing = {b for (b,) in conn.execute(
            f"SELECT barcode FROM products WHERE barcode IN ({placeholders})",
            all_candidates)}
    return {raw: next((c for c in cands if c in existing), None)
            for raw, cands in candidates_of.items()}


# ---------------------------------------------------------------------------
# stores= filter - "chain_id:store_id,chain_id:store_id" either as a GET
# query string or a POST JSON list of the same strings.
# ---------------------------------------------------------------------------

def parse_stores_param(value):
    if not value:
        return None
    return _parse_store_pairs(value.split(","))


def parse_stores_list(values):
    if not values:
        return None
    return _parse_store_pairs(values)


def _parse_store_pairs(parts):
    pairs = set()
    for part in parts:
        chain_id, _, store_id = str(part).strip().partition(":")
        if chain_id and store_id:
            pairs.add((chain_id, store_id))
    return pairs or None


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

def read_meta(conn):
    return dict(conn.execute("SELECT key, value FROM meta"))


def chain_as_of(meta):
    return json.loads(meta.get("chain_as_of") or "{}")


def today_from_meta(meta):
    return (meta.get("built_at") or "")[:10] or "0000-00-00"


# ---------------------------------------------------------------------------
# products - name/unit/weighed, chain prices, per-branch exceptions
# ---------------------------------------------------------------------------

def products_basic(conn, barcodes):
    """{barcode: (name, is_weighted, unit_qty)}."""
    out = {}
    if not barcodes:
        return out
    placeholders = ",".join("?" * len(barcodes))
    for barcode, name, unit_qty, is_weighted in conn.execute(
            f"SELECT barcode, name, unit_qty, is_weighted FROM products "
            f"WHERE barcode IN ({placeholders})", list(barcodes)):
        out[barcode] = (name, bool(is_weighted), unit_qty)
    return out


def chain_prices_for(conn, barcodes):
    """{barcode: {chain_id: [price, store_count]}}."""
    out = defaultdict(dict)
    if not barcodes:
        return out
    placeholders = ",".join("?" * len(barcodes))
    for barcode, chain_id, price, store_count in conn.execute(
            f"SELECT barcode, chain_id, price, store_count FROM chain_prices "
            f"WHERE barcode IN ({placeholders})", list(barcodes)):
        out[barcode][chain_id] = [price, store_count or 0]
    return out


def price_exceptions_for(conn, barcodes, stores_filter=None):
    """{barcode: {chain_id: [[store_id, price], ...]}}, optionally narrowed
    to the (chain_id, store_id) pairs in `stores_filter`."""
    out = defaultdict(lambda: defaultdict(list))
    if not barcodes:
        return out
    placeholders = ",".join("?" * len(barcodes))
    for barcode, chain_id, store_id, price in conn.execute(
            f"SELECT barcode, chain_id, store_id, price FROM price_exceptions "
            f"WHERE barcode IN ({placeholders})", list(barcodes)):
        if stores_filter is not None and (chain_id, store_id) not in stores_filter:
            continue
        out[barcode][chain_id].append([store_id, price])
    return out


# ---------------------------------------------------------------------------
# promotions - deals + store_bits, assembled through scripts/offers.py's
# emit_chain (the same function build_catalog.py uses for promo/*.json).
# ---------------------------------------------------------------------------

def _store_bits_for_chains(conn, chain_ids):
    """{chain_id: {store_id: bit}} - decodes both `deals.branches` and
    `promo_everywhere.branches`, which build_app_db.py packs from the same
    per-chain bit assignment."""
    bit_of = defaultdict(dict)
    if not chain_ids:
        return bit_of
    placeholders = ",".join("?" * len(chain_ids))
    for chain_id, store_id, bit in conn.execute(
            f"SELECT chain_id, store_id, bit FROM store_bits "
            f"WHERE chain_id IN ({placeholders})", list(chain_ids)):
        bit_of[chain_id][store_id] = bit
    return bit_of


def _promo_everywhere_for_chains(conn, chain_ids, bit_of):
    """{chain_id: {store_id, ...}} - every branch that ever published ANY
    promotion for the chain, expired or not (`promo_everywhere`, KAN-13).

    This is NOT reconstructable from `deals` alone: `deals` holds only kept,
    unexpired offers, so a branch whose only offer expired would be invisible
    to a union-of-deals-branches approximation - silently turning a live
    offer running at every still-active branch into a wrong `x` (or `s`)
    instead of neither. build_app_db.py now persists the exact set
    `merge_chain` computes at build time, so the API can match
    build_catalog.py's `s`/`x`/neither choice exactly instead of
    approximating it.
    """
    out = defaultdict(set)
    if not chain_ids:
        return out
    placeholders = ",".join("?" * len(chain_ids))
    for chain_id, branches in conn.execute(
            f"SELECT chain_id, branches FROM promo_everywhere "
            f"WHERE chain_id IN ({placeholders})", list(chain_ids)):
        chain_bits = bit_of.get(chain_id, {})
        out[chain_id] = {store_id for store_id, bit in chain_bits.items()
                          if bitmap.has_branch(branches, bit)}
    return out


def promos_for(conn, barcodes, today):
    """{barcode: {chain_id: [offer, ...]}}, decoded from `deals` and emitted
    through offers.emit_chain - the exact function build_catalog.py uses for
    promo/*.json, against the exact `everywhere` set it always used
    (`promo_everywhere`, KAN-13) - so the two can never disagree about the
    shape of an offer or about when it gets `s`/`x`/neither.
    """
    offers_out = defaultdict(lambda: defaultdict(list))
    if not barcodes:
        return offers_out
    placeholders = ",".join("?" * len(barcodes))
    rows = conn.execute(
        f"SELECT chain_id, barcode, club, coupon, min_qty, unit_price, price, "
        f"description, starts, ends, branches FROM deals "
        f"WHERE barcode IN ({placeholders}) ORDER BY deal_id",
        list(barcodes)).fetchall()
    if not rows:
        return offers_out
    chain_ids = sorted({row[0] for row in rows})
    bit_of = _store_bits_for_chains(conn, chain_ids)
    everywhere = _promo_everywhere_for_chains(conn, chain_ids, bit_of)

    kept_by_chain = defaultdict(dict)
    for (chain_id, barcode, club, coupon, min_qty, unit_price, price,
         description, starts, ends, branches) in rows:
        chain_bits = bit_of.get(chain_id, {})
        where = {store_id for store_id, bit in chain_bits.items()
                 if bitmap.has_branch(branches, bit)}
        key = (barcode, club, coupon, min_qty, unit_price)
        kept_by_chain[chain_id][key] = {
            "price": price, "description": description or "",
            "starts": starts or "", "ends": ends or "", "where": where,
        }
    for chain_id, kept in kept_by_chain.items():
        offers_mod.emit_chain(chain_id, kept, everywhere.get(chain_id, set()),
                               offers_out, today)
    return offers_out

