"""Per-chain promo dedupe and domination-prune.

build_catalog.py's ``write_promos`` (the published ``promo/`` JSON) and
build_app_db.py's ``build_deals`` (the ``deals`` table, KAN-7) both need to
turn a chain's raw ``promo_offers``/``promo_stores`` rows into "the offers
worth showing, each with its final branch set". This module is that logic,
factored out once so the two outputs can never drift apart - build_catalog.py
proves it stays byte-identical after this split (see the KAN-7 report).
"""

from collections import defaultdict


def merge_chain(conn, chain_id, today, table_prefix=""):
    """One chain's offers, merged on their terms rather than their id.

    Chains republish the same deal under a new promotion id per campaign, per
    branch or per week: 309,602 of Super-Pharm's 453,021 offers share their
    terms with another. Merging on (club, coupon, quantity, unit price) and
    unioning the branch lists is what makes the published form fit.

    Coupon is part of that key rather than a detail hanging off the offer. A
    coupon at 12.90 and a shelf discount at 12.90 carry the same number and
    different conditions, and collapsing them would silently promote one to
    the other in whichever direction the merge happened to run.

    ``table_prefix`` lets a caller that attached the source database under
    another schema name (build_app_db.py attaches prices.db as ``src``) point
    the queries at ``"src."`` instead of the bare table names build_catalog.py
    uses when it holds the connection directly.

    Returns ``(merged, everywhere)``: ``merged`` maps
    ``(barcode, club, coupon, min_qty, unit_price)`` to a dict with
    ``price``, ``description``, ``starts``, ``ends`` and ``where`` (the set of
    store ids honouring it); ``everywhere`` is every branch that publishes any
    promotion at all for the chain, expired or not - the denominator a caller
    needs for "runs everywhere but these branches".
    """
    stores_tbl = f"{table_prefix}promo_stores"
    offers_tbl = f"{table_prefix}promo_offers"
    branches_of = defaultdict(set)
    for offer_id, store_id in conn.execute(
            f"SELECT ps.offer_id, ps.store_id FROM {stores_tbl} ps "
            f"JOIN {offers_tbl} o ON o.offer_id = ps.offer_id WHERE o.chain_id = ?",
            (chain_id,)):
        branches_of[offer_id].add(store_id)

    merged = {}
    everywhere = set()
    for (offer_id, barcode, club, coupon, min_qty, price, unit_price,
         description, starts, ends) in conn.execute(
            f"SELECT offer_id, barcode, club, coupon, min_qty, price, unit_price, "
            f"description, starts, ends FROM {offers_tbl} WHERE chain_id = ?",
            (chain_id,)):
        # A MinQty below 1 is a weight, not a pack size, so price/MinQty is not
        # a unit price - it is that number multiplied by a hundred. Rami Levy's
        # 2.90 tomato deal is filed as MinQty 0.01 and came out at 290.00 a
        # kilo, which no client would show and none should. promos.py now
        # stores these correctly, but the correction lives here as well
        # because a republish reuses chain databases built before that fix.
        if min_qty and min_qty < 1:
            min_qty, unit_price = 1.0, price
        where = branches_of.get(offer_id)
        if not where:
            continue                      # no branch honours it; nothing to show
        # Every branch that publishes promotions at all, whether or not this
        # particular offer has ended - it is the denominator for "everywhere".
        everywhere |= where
        if ends and ends < today:
            continue                      # finished; nobody can still get it

        key = (barcode, club, coupon, min_qty, unit_price)
        found = merged.get(key)
        if found is None:
            merged[key] = {
                "price": price, "description": description or "",
                "starts": starts or "", "ends": ends or "", "where": set(where),
            }
            continue
        found["where"] |= where
        # Latest end, earliest start: the deal runs as long as any copy says.
        if (ends or "") > found["ends"]:
            found["ends"] = ends or ""
        if starts and (not found["starts"] or starts < found["starts"]):
            found["starts"] = starts
        if description and (not found["description"]
                            or len(description) < len(found["description"])):
            found["description"] = description
    return merged, everywhere


def prune_dominated(merged):
    """Drop every offer dominated, at every branch it runs at, by something
    cheaper on the same (barcode, club, coupon).

    Returns a dict keyed exactly like ``merged``'s input, holding only the
    survivors. Iteration order is grouped by (barcode, club, coupon) and then
    ascending (unit_price, min_qty) within each group - both callers rely on
    it: build_catalog.py for the order offers are written in, build_app_db.py
    for a stable ``deal_id`` assignment.
    """
    by_product = defaultdict(list)
    for key, body in merged.items():
        barcode, club, coupon, _min_qty, _unit_price = key
        by_product[(barcode, club, coupon)].append(key + (body,))

    kept = {}
    for group in by_product.values():
        group.sort(key=lambda row: (row[4], row[3]))   # unit_price, min_qty
        covered = set()
        for row in group:
            key, body = row[:5], row[5]
            # Every branch this runs at already has something cheaper.
            if body["where"] <= covered:
                continue
            covered |= body["where"]
            kept[key] = body
    return kept
