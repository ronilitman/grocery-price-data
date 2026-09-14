"""Catalogue-reading helpers shared by the /meta, /product, /generic and
/products endpoints (KAN-13).

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

This module deliberately imports only ``scripts.bitmap`` and
``scripts.offers`` - never ``scripts.generics`` (KAN-13 post-merge fix).
``generics.from_db()`` needs ``data/produce_words.txt`` and
``data/pricez_images.json``, which live under ``data/`` - a directory
``api/deploy/deploy.sh`` never ships to the VM (only ``api/`` and
``scripts/`` are tarred up), and CI's full checkout never notices the gap.
On the real box this was a 500 on every one of these endpoints
(``FileNotFoundError`` reaching for a file that was never deployed), and
even with the file present, ``from_db()`` took 3.8s on the first request
after a restart and drove free memory on the 969 MB box down to 72 MB -
both an OOM risk and a slow first request every night after the nightly
swap. Everything ``generics.from_db()`` used to supply at request time is
now persisted at build time instead (``scripts/build_app_db.py``'s
``generic_barcodes`` table) and read here like any other table.
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
# imports (`import generics as generics_mod`) only resolve when scripts/
# itself is on sys.path, which this module deliberately doesn't add (KAN-12's
# `from scripts import X` convention, see the module docstring).
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


# ---------------------------------------------------------------------------
# generics - KAN-9 precedence: a key in produce_generic_map takes its
# members/prices from produce_units; an unmapped key resolves exactly as
# build_catalog.py's generics.json always has.
# ---------------------------------------------------------------------------

def resolved_generic_keys_for_barcodes(conn, barcodes):
    """{barcode: generic_key} - the one key generics_mod.from_db's
    `of_barcode` map picks for each barcode (the group with the most chains
    behind it - see generics.py's `build()`), persisted at build time as
    `generic_barcodes.is_resolved` (KAN-13 post-merge fix). A barcode can
    legitimately appear under more than one key (different chains name the
    same colliding barcode differently); at most one row per barcode has
    `is_resolved = 1`.
    """
    out = {}
    if not barcodes:
        return out
    placeholders = ",".join("?" * len(barcodes))
    for barcode, key in conn.execute(
            f"SELECT barcode, key FROM generic_barcodes "
            f"WHERE barcode IN ({placeholders}) AND is_resolved = 1",
            list(barcodes)):
        out[barcode] = key
    return out


def generic_barcodes_for_keys(conn, keys):
    """{key: [barcode, ...]} - the full `b` list for each key, persisted at
    build time in `generic_barcodes` (KAN-13 post-merge fix) rather than
    recomputed from generics_mod.from_db() at request time."""
    out = defaultdict(list)
    if not keys:
        return out
    placeholders = ",".join("?" * len(keys))
    for key, barcode in conn.execute(
            f"SELECT key, barcode FROM generic_barcodes "
            f"WHERE key IN ({placeholders})", list(keys)):
        out[key].append(barcode)
    return out


def generic_members_for_keys(conn, keys):
    """{key: {chain_id: [price, store_count, barcode]}} for UNMAPPED generic
    keys - one representative barcode per chain (`generic_members`, KAN-9),
    priced fresh from `chain_prices` rather than a build-time snapshot -
    the same live-price pattern produce_members_for_slugs uses for mapped
    keys.
    """
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    chain_barcode_of_key = defaultdict(dict)   # key -> {chain_id: barcode}
    for key, chain_id, barcode in conn.execute(
            f"SELECT key, chain_id, barcode FROM generic_members "
            f"WHERE key IN ({placeholders})", list(keys)):
        chain_barcode_of_key[key][chain_id] = barcode

    pair_to_key = {}
    all_barcodes = set()
    for key, by_chain in chain_barcode_of_key.items():
        for chain_id, barcode in by_chain.items():
            pair_to_key[(chain_id, barcode)] = key
            all_barcodes.add(barcode)

    p_of_key = defaultdict(dict)
    if all_barcodes:
        ph = ",".join("?" * len(all_barcodes))
        for chain_id, barcode, price, store_count in conn.execute(
                f"SELECT chain_id, barcode, price, store_count FROM chain_prices "
                f"WHERE barcode IN ({ph})", sorted(all_barcodes)):
            key = pair_to_key.get((chain_id, barcode))
            if key is not None:
                p_of_key[key][chain_id] = [price, store_count or 0, barcode]

    return {key: p_of_key.get(key, {}) for key in keys}


def produce_slugs_for_barcodes(conn, barcodes):
    """{barcode: slug} for every barcode produce_units.tsv knows about."""
    out = {}
    if not barcodes:
        return out
    placeholders = ",".join("?" * len(barcodes))
    for barcode, slug in conn.execute(
            f"SELECT barcode, slug FROM produce_units "
            f"WHERE barcode IN ({placeholders})", list(barcodes)):
        out.setdefault(barcode, slug)
    return out


def primary_keys_for_slugs(conn, slugs):
    """{slug: its primary generic_key}."""
    out = {}
    if not slugs:
        return out
    placeholders = ",".join("?" * len(slugs))
    for slug, key in conn.execute(
            f"SELECT slug, generic_key FROM produce_generic_map "
            f"WHERE slug IN ({placeholders}) AND is_primary = 1", list(slugs)):
        out[slug] = key
    return out


def generic_keys_for_barcodes(conn, barcodes):
    """{barcode: generic_key_or_None} - the `g` field for a batch of
    resolved product barcodes. A barcode produce_units knows about resolves
    to its slug's primary key (KAN-9 step 3), overriding whatever the
    algorithmic generics grouping would say for the same barcode; anything
    else falls back to the persisted `generic_barcodes.is_resolved` map,
    exactly as published today.
    """
    slug_of = produce_slugs_for_barcodes(conn, barcodes)
    primary_of_slug = primary_keys_for_slugs(conn, set(slug_of.values()))
    resolved = resolved_generic_keys_for_barcodes(conn, barcodes)
    out = {}
    for barcode in barcodes:
        slug = slug_of.get(barcode)
        out[barcode] = (primary_of_slug.get(slug) if slug is not None
                         else resolved.get(barcode))
    return out


def mapped_slugs_for_keys(conn, keys):
    """{generic_key: slug} for every key in `keys` that produce_generic_map
    covers (a key maps to at most one slug - verified at KAN-9 review)."""
    out = {}
    if not keys:
        return out
    placeholders = ",".join("?" * len(keys))
    for slug, key in conn.execute(
            f"SELECT slug, generic_key FROM produce_generic_map "
            f"WHERE generic_key IN ({placeholders})", list(keys)):
        out[key] = slug
    return out


def generic_rows(conn, keys):
    """{key: (name, weighted, unit, image_id, aliases_json)} for every key
    in `keys` that exists in the `generics` table."""
    out = {}
    if not keys:
        return out
    placeholders = ",".join("?" * len(keys))
    for key, name, weighted, unit, image_id, aliases_json in conn.execute(
            f"SELECT key, name, weighted, unit, image_id, aliases_json "
            f"FROM generics WHERE key IN ({placeholders})", list(keys)):
        out[key] = (name, weighted, unit, image_id, aliases_json)
    return out


def produce_members_for_slugs(conn, slugs):
    """{slug: ({chain_id: [price, store_count, barcode]}, [barcode, ...])}
    for every slug in `slugs` - members come from produce_units, but the
    price and store_count are read fresh from `chain_prices`, never the
    price baked into the hand-curated TSV (check_produce_units.py already
    tracks how that drifts - see the KAN-9 report). A chain no longer
    pricing that barcode is dropped rather than shown stale or at 0.
    """
    if not slugs:
        return {}
    placeholders = ",".join("?" * len(slugs))
    chain_barcode_of_slug = defaultdict(dict)   # slug -> {chain_id: barcode}
    all_barcodes = defaultdict(set)             # slug -> {barcode, ...} (every row)
    for slug, chain_id, barcode in conn.execute(
            f"SELECT slug, chain_id, barcode FROM produce_units "
            f"WHERE slug IN ({placeholders})", list(slugs)):
        all_barcodes[slug].add(barcode)
        chain_barcode_of_slug[slug].setdefault(chain_id, barcode)

    every_barcode = sorted({b for bs in all_barcodes.values() for b in bs})
    pair_to_slug_chain = {}
    for slug, by_chain in chain_barcode_of_slug.items():
        for chain_id, barcode in by_chain.items():
            pair_to_slug_chain[(chain_id, barcode)] = slug

    p_of_slug = defaultdict(dict)
    if every_barcode:
        ph = ",".join("?" * len(every_barcode))
        for chain_id, barcode, price, store_count in conn.execute(
                f"SELECT chain_id, barcode, price, store_count FROM chain_prices "
                f"WHERE barcode IN ({ph})", every_barcode):
            slug = pair_to_slug_chain.get((chain_id, barcode))
            if slug is not None:
                p_of_slug[slug][chain_id] = [price, store_count or 0, barcode]

    return {slug: (p_of_slug.get(slug, {}), sorted(all_barcodes.get(slug, ())))
            for slug in slugs}


def resolve_generics(conn, keys):
    """{key: {"n","w","u","i","a","p","b"} or None} for a batch of generic
    keys, applying KAN-9's precedence. None means the key never existed in
    this build's generics output at all (404 territory); everything else
    always resolves, per the KAN-9/KAN-13 spec ("every key present in
    today's generics.json must resolve").
    """
    rows = generic_rows(conn, keys)
    known_keys = [k for k in keys if k in rows]
    slug_of_key = mapped_slugs_for_keys(conn, known_keys)
    produce_by_slug = produce_members_for_slugs(
        conn, sorted(set(slug_of_key.values())))
    unmapped_keys = [k for k in known_keys if k not in slug_of_key]
    members_by_key = generic_members_for_keys(conn, unmapped_keys)
    barcodes_by_key = generic_barcodes_for_keys(conn, known_keys)

    out = {}
    for key in keys:
        row = rows.get(key)
        if row is None:
            out[key] = None
            continue
        name, weighted, unit, image_id, aliases_json = row
        base = {
            "n": name, "w": weighted or 0, "u": unit,
            "i": image_id,
            "a": json.loads(aliases_json) if aliases_json else [],
        }
        slug = slug_of_key.get(key)
        if slug is not None:
            p, b = produce_by_slug.get(slug, ({}, []))
        else:
            p = members_by_key.get(key, {})
            b = barcodes_by_key.get(key, [])
        out[key] = {**base, "p": p, "b": b}
    return out
