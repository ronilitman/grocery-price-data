"""Barcode-prefix JSON shards, for the scan-a-product path.

The SQLite file can answer this too, but a scan wants one small cached request
with no WASM warm-up.

Sharding is adaptive. A flat 3-digit split does not work here: 729 is Israel's
GS1 country prefix, so that one shard would hold most of the catalogue. Any
oversized bucket is re-split at six digits, and index.json records which
prefixes went deep so the client knows which file to ask for.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict

import generics as generics_mod

LEVELS = (3, 6, 8)      # split deeper only where a bucket is actually crowded
MAX_ITEMS = 2000        # ~150 KB per shard
MAX_FILES = 20000       # the tightest limit among static hosts

# Per-store prices are far bulkier than the search payload, so they get their
# own deeper split and a byte budget rather than an item count.
DETAIL_LEVELS = (3, 6, 8, 10, 13)
DETAIL_MAX_BYTES = 120_000

# Promotions split on a shallower ladder and a looser budget than detail. They
# are fetched once per product view rather than per barcode in a cart, and at
# detail's settings the first build wanted 23,941 files - over the 20,000-file
# host limit on its own.
PROMO_LEVELS = (3, 6, 8, 10)
PROMO_MAX_BYTES = 250_000

# Name search. Shards are keyed by the first characters of a WORD rather than
# of a barcode, so the client fetches one file for whatever is being typed and
# filters it locally. Two characters is the shallowest useful split: a Hebrew
# query is worth searching from two letters, and anything shallower puts a
# quarter of the catalogue in one file.
NAME_LEVELS = (2, 3, 4)
NAME_MAX_BYTES = 200_000
# An autocomplete shows ten rows; a query for עגבניה matches 500 products
# across 31 chains. Keeping the 30 carried by the most chains both shrinks the
# index by two thirds and puts the staple ahead of the oddity - the ranking is
# the point, the size is the side effect.
NAME_MAX_PER_TERM = 30
# A word on 1%+ of everything is packaging, not product. Indexing `גרם` buys
# nobody anything and it is the single largest posting list there is.
NAME_FILLER_AT = 0.01
NAME_WORD = re.compile(r"[\w\"'\u05f4\u05f3]+", re.UNICODE)
# Quotes belong in a Hebrew word (`ק"ג`, `תפו"א`) but never in a file name, and
# the first characters of a word ARE the file name. Bucketing on a stripped
# form keeps `תפו"א` and `תפוא` in the same shard, which is what someone typing
# either of them expects anyway. prices.js strips identically.
NAME_QUOTES = re.compile(r"[\"'\u05f4\u05f3]")

# What the chains publish when they have no unit; carrying it forward would
# only let a client print "unknown" where it could print nothing.
UNKNOWN_UNIT = "\u05dc\u05d0 \u05d9\u05d3\u05d5\u05e2"


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def split(barcodes, depth_index):
    """Bucket by prefix, recursing into any bucket that is still too big."""
    depth = LEVELS[depth_index]
    buckets = defaultdict(list)
    for barcode in barcodes:
        buckets[barcode[:depth].ljust(depth, "0")].append(barcode)

    shards = {}
    for prefix, members in buckets.items():
        if len(members) <= MAX_ITEMS or depth_index + 1 >= len(LEVELS):
            shards[prefix] = members
        else:
            shards.update(split(members, depth_index + 1))
    return shards


def split_by_size(entries, keys, depth_index, levels=DETAIL_LEVELS,
                  budget=DETAIL_MAX_BYTES):
    """Like split(), but bucket until the encoded shard fits the byte budget."""
    depth = levels[depth_index]
    buckets = defaultdict(list)
    for key in keys:
        buckets[key[:depth].ljust(depth, "0")].append(key)

    shards = {}
    for prefix, members in buckets.items():
        payload = {k: entries[k] for k in members}
        if (len(dump(payload).encode()) <= budget
                or depth_index + 1 >= len(levels)):
            shards[prefix] = members
        else:
            shards.update(split_by_size(entries, members, depth_index + 1,
                                        levels, budget))
    return shards


def write_detail(conn, out_dir):
    """Per-store prices: barcode -> chain -> [[store_id, price], ...].

    Only stores whose price differs from the chain baseline appear here - the
    rest are covered by the baseline already in the search shard. Store names
    live once in stores.json and are referenced by id.
    """
    detail_dir = os.path.join(out_dir, "detail")
    os.makedirs(detail_dir, exist_ok=True)

    # [name, city_code, priced_items]. The third element is what lets a client
    # tell "this branch charges the baseline" from "this branch published
    # nothing" - absence from the detail shards means the first, never the
    # second, and without a count the two are indistinguishable.
    stores = defaultdict(dict)
    for chain_id, store_id, name, city, priced in conn.execute(
            "SELECT chain_id, store_id, store_name, city, priced_items FROM stores"):
        stores[chain_id][store_id] = [name or "", city or "", priced]
    with open(os.path.join(out_dir, "stores.json"), "w", encoding="utf-8") as handle:
        handle.write(dump(stores))

    entries = defaultdict(lambda: defaultdict(list))
    for barcode, chain_id, store_id, price in conn.execute(
            "SELECT barcode, chain_id, store_id, price FROM price_exceptions"):
        entries[barcode][chain_id].append([store_id, price])

    shards = split_by_size(entries, list(entries), 0)
    largest = 0
    for prefix, barcodes in shards.items():
        path = os.path.join(detail_dir, f"{prefix}.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(dump({b: entries[b] for b in barcodes}))
        largest = max(largest, os.path.getsize(path))

    # Fetched lazily on the first drill-down, so it stays out of page load.
    with open(os.path.join(detail_dir, "index.json"), "w", encoding="utf-8") as handle:
        handle.write(dump({"levels": list(DETAIL_LEVELS), "shards": sorted(shards)}))

    print(f"[catalog] {len(shards)} detail shards, {len(entries):,} products with "
          f"per-store prices, largest {largest / 1e3:.0f} KB")
    return len(shards)


def _merge_chain(conn, chain_id, today):
    """One chain's offers, merged on their terms rather than their id.

    Chains republish the same deal under a new promotion id per campaign, per
    branch or per week: 309,602 of Super-Pharm's 453,021 offers share their
    terms with another. Merging on (club, coupon, quantity, unit price) and
    unioning the branch lists is what makes the published form fit.

    Coupon is part of that key rather than a detail hanging off the offer. A
    coupon at 12.90 and a shelf discount at 12.90 carry the same number and
    different conditions, and collapsing them would silently promote one to the
    other in whichever direction the merge happened to run.
    """
    branches_of = defaultdict(set)
    for offer_id, store_id in conn.execute(
            "SELECT ps.offer_id, ps.store_id FROM promo_stores ps "
            "JOIN promo_offers o ON o.offer_id = ps.offer_id WHERE o.chain_id = ?",
            (chain_id,)):
        branches_of[offer_id].add(store_id)

    merged = {}
    everywhere = set()
    for (offer_id, barcode, club, coupon, min_qty, price, unit_price,
         description, starts, ends) in conn.execute(
            "SELECT offer_id, barcode, club, coupon, min_qty, price, unit_price, "
            "description, starts, ends FROM promo_offers WHERE chain_id = ?",
            (chain_id,)):
        # A MinQty below 1 is a weight, not a pack size, so price/MinQty is not
        # a unit price - it is that number multiplied by a hundred. Rami Levy's
        # 2.90 tomato deal is filed as MinQty 0.01 and came out at 290.00 a
        # kilo, which no client would show and none should. promos.py now
        # stores these correctly, but the correction lives here as well because
        # a republish reuses chain databases built before that fix.
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


def _emit_chain(chain_id, merged, everywhere, offers, today):
    """Prune what nobody could prefer, and encode the rest."""
    by_product = defaultdict(list)
    for (barcode, club, coupon, min_qty, unit_price), body in merged.items():
        by_product[(barcode, club, coupon)].append((unit_price, min_qty, body))

    kept = 0
    for (barcode, club, coupon), group in by_product.items():
        group.sort(key=lambda row: (row[0], row[1]))
        covered = set()
        for unit_price, min_qty, body in group:
            # Every branch this runs at already has something cheaper.
            if body["where"] <= covered:
                continue
            covered |= body["where"]
            kept += 1
            entry = {"u": unit_price, "d": body["description"], "e": body["ends"]}
            if min_qty and min_qty != 1:
                entry["q"] = min_qty
                entry["t"] = body["price"]      # the headline "2 for 34"
            if club:
                entry["c"] = 1
            if coupon:
                entry["k"] = 1
            if body["starts"] and body["starts"] > today:
                entry["b"] = body["starts"]     # announced, not yet live
            missing = everywhere - body["where"]
            if missing:
                # Whichever list is shorter says the same thing: an offer
                # running at 300 of 305 branches should not carry 300 ids.
                if len(missing) < len(body["where"]):
                    entry["x"] = sorted(missing)
                else:
                    entry["s"] = sorted(body["where"])
            offers[barcode][chain_id].append(entry)
    return kept


def write_promos(conn, out_dir, today):
    """Promotions: barcode -> chain -> [offer, ...], sharded like detail/.

    An offer is the deal as published, not a price: ``u`` is what one unit
    costs under it and ``q`` is how many you must buy to get that. A client
    that shows ``u`` without ``q`` says a Toffifee costs 17.00 when the offer
    is two for 34.00.

    ``s`` names the branches honouring the offer and ``x`` the ones excluded;
    whichever is shorter is written, and neither appears when every branch of
    the chain honours it.

    ``c`` means a loyalty card is required and ``k`` means a coupon claimed in
    the chain's own app is - two different conditions on the same number, and
    both absent when the price is simply the deal.
    """
    offers = defaultdict(lambda: defaultdict(list))
    total_merged = kept = 0
    # One chain at a time: Super-Pharm alone holds 4.5M offer-branch links and
    # the national total is 17.5M, which is gigabytes of runner memory to hold
    # at once for no benefit, since an offer never spans chains.
    for (chain_id,) in conn.execute("SELECT DISTINCT chain_id FROM promo_offers"):
        merged, everywhere = _merge_chain(conn, chain_id, today)
        total_merged += len(merged)
        kept += _emit_chain(chain_id, merged, everywhere, offers, today)

    promo_dir = os.path.join(out_dir, "promo")
    os.makedirs(promo_dir, exist_ok=True)
    shards = (split_by_size(offers, list(offers), 0, PROMO_LEVELS, PROMO_MAX_BYTES)
              if offers else {})
    largest = 0
    for prefix, barcodes in shards.items():
        path = os.path.join(promo_dir, f"{prefix}.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(dump({b: offers[b] for b in barcodes}))
        largest = max(largest, os.path.getsize(path))
    with open(os.path.join(promo_dir, "index.json"), "w", encoding="utf-8") as handle:
        handle.write(dump({"levels": list(PROMO_LEVELS), "shards": sorted(shards)}))

    print(f"[catalog] {len(shards)} promo shards, {len(offers):,} products on offer, "
          f"{kept:,} offers kept of {total_merged:,} merged, "
          f"largest {largest / 1e3:.0f} KB")
    return len(shards), len(offers)


def write_generics(conn, out_dir):
    """Loose produce collapsed across chains: one row, every chain's price.

    A barcode names a row in one chain's price file, not a product - fifteen
    codes carry `עגבניה` and one of them is frozen chicken breast somewhere
    else. This is the layer that lets the app answer "what do tomatoes cost"
    with one line instead of fifteen near-identical ones.

    ``b`` lists the member barcodes so a scan of any of them lands here, and
    ``i`` is the Pricez picture id - loose produce has no package shot, so its
    barcode has no image, and the id resolved by hand is the only way to show
    one at all.
    """
    entries, of_barcode = generics_mod.from_db(conn)
    path = os.path.join(out_dir, "generics.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(dump(entries))
    with_image = sum(1 for e in entries.values() if "i" in e)
    print(f"[catalog] {len(entries)} produce generics over {len(of_barcode):,} "
          f"barcodes, {with_image} with a picture, "
          f"{os.path.getsize(path) / 1e3:.0f} KB")
    return entries, of_barcode


def _name_terms(name, filler):
    """The words worth indexing, folded to lower case and de-duplicated.

    A term has to begin with a letter or digit, because its first characters
    become a filename and a URL path. `ק"ג` is a fine word; a token that starts
    with the quote is a tokeniser artifact that would ask the host for
    `name/"0.json`.
    """
    out = set()
    for word in NAME_WORD.findall(name or ""):
        low = word.lower()
        if len(low) < 2 or low.isdigit() or low in filler:
            continue
        if not low[0].isalnum():
            continue
        out.add(low)
    return out


def write_names(entries, generic_entries, of_barcode, out_dir):
    """Search shards keyed by the first letters of a word, not of a barcode.

    The client takes the longest prefix in the index that its query starts
    with and fetches that one file - the same ladder the barcode shards use,
    pointed at words. Each shard carries whole rows rather than postings, so
    one request answers a multi-word query too: whichever word picks the file,
    the filter then runs over the full name.

    A product that belongs to a generic is NOT indexed. Its generic is, in its
    place, which is what turns thirty near-identical `עגבניה` rows into one.
    """
    rows, terms_of = {}, {}
    freq = Counter()
    for barcode, entry in entries.items():
        if barcode in of_barcode:
            continue                       # its generic stands for it
        freq.update(_name_terms(entry["n"], frozenset()))
    for key, entry in generic_entries.items():
        freq.update(_name_terms(entry["n"], frozenset()))

    indexed = len(entries) - len(of_barcode) + len(generic_entries)
    filler = {w for w, c in freq.items() if c >= max(50, indexed * NAME_FILLER_AT)}

    def add(row_id, name, weighted, chains, aliases=()):
        # Aliases are searched but never displayed. A generic labelled `עגבניה`
        # has to answer a query for `עגבניות`, and only its members carry that
        # spelling - so the words ride along in a fourth field the client
        # matches against and never renders.
        terms = _name_terms(name, filler)
        extra = set()
        for alias in aliases:
            extra |= _name_terms(alias, filler)
        extra -= terms
        if not terms:
            return
        row = [row_id, name, 1 if weighted else 0]
        if extra:
            row.append(" ".join(sorted(extra)))
        rows[row_id] = row
        terms_of[row_id] = (terms | extra, chains)

    for barcode, entry in entries.items():
        if barcode in of_barcode:
            continue
        add(barcode, entry["n"], entry.get("w"), len(entry["p"]))
    # "@" marks a generic. The client needs to tell the two apart before it can
    # decide whether to fetch a barcode's shard or read generics.json, and a
    # prefix costs one byte against a second field on every row.
    for key, entry in generic_entries.items():
        add("@" + key, entry["n"], 1, len(entry["p"]), entry.get("a", ()))

    postings = defaultdict(list)
    for row_id, (terms, chains) in terms_of.items():
        for term in terms:
            postings[term].append((chains, row_id))

    def bucket_key(term, depth):
        plain = NAME_QUOTES.sub("", term)
        return plain[:depth] if len(plain) >= depth else None

    buckets = defaultdict(set)
    for term, members in postings.items():
        members.sort(key=lambda pair: -pair[0])
        prefix = bucket_key(term, NAME_LEVELS[0])
        if not prefix:
            continue
        for _, row_id in members[:NAME_MAX_PER_TERM]:
            buckets[prefix].add(row_id)

    # Only the oversized buckets go deeper, and only along the terms that are
    # long enough to be split. A two-letter query can then still be answered by
    # the two-letter shard.
    def deepen(prefix, members, depth):
        payload = dump([rows[r] for r in members])
        if len(payload) <= NAME_MAX_BYTES or depth + 1 >= len(NAME_LEVELS):
            return {prefix: members}
        cut = NAME_LEVELS[depth + 1]
        deeper = defaultdict(set)
        for term, entries_ in postings.items():
            plain = NAME_QUOTES.sub("", term)
            if not plain.startswith(prefix) or len(plain) <= len(prefix):
                continue
            ids = {r for _, r in sorted(entries_, key=lambda p: -p[0])[:NAME_MAX_PER_TERM]}
            deeper[plain[:cut]] |= ids & members
        out = {}
        for sub, ids in deeper.items():
            if ids:
                out.update(deepen(sub, ids, depth + 1))
        # Terms exactly as long as the prefix have nowhere deeper to go.
        stay = members - {i for ids in deeper.values() for i in ids}
        if stay:
            out[prefix] = stay
        return out

    shards = {}
    for prefix, members in buckets.items():
        shards.update(deepen(prefix, members, 0))

    name_dir = os.path.join(out_dir, "name")
    os.makedirs(name_dir, exist_ok=True)
    largest = 0
    for prefix, members in shards.items():
        path = os.path.join(name_dir, f"{prefix}.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(dump(sorted((rows[r] for r in members),
                                     key=lambda row: len(row[1]))))
        largest = max(largest, os.path.getsize(path))
    with open(os.path.join(name_dir, "index.json"), "w", encoding="utf-8") as handle:
        handle.write(dump({"levels": list(NAME_LEVELS), "shards": sorted(shards)}))

    print(f"[catalog] {len(shards)} name shards over {len(rows):,} searchable rows "
          f"({len(generic_entries)} generics standing in for {len(of_barcode):,} "
          f"products), {len(filler)} filler words, largest {largest / 1e3:.0f} KB")
    return len(shards)


def main():
    parser = argparse.ArgumentParser(description="Write barcode-prefix JSON shards.")
    parser.add_argument("--db", default="dist/prices.db")
    parser.add_argument("--out-dir", default="dist/catalog")
    args = parser.parse_args()

    if os.path.isdir(args.out_dir):
        shutil.rmtree(args.out_dir)
    os.makedirs(args.out_dir, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    entries = {}
    # "w" marks a weighed product, whose price is per unit_qty (a kilo, almost
    # always) rather than per item - a cart cannot sum those without asking the
    # user for a weight - and "u" names that unit.
    #
    # Both are omitted when they say nothing. Only 12% of products are weighed,
    # and with 109k entries a key that is always present is paid for 109k
    # times. "u" rides along only on weighed entries for the same reason: on a
    # packaged product unit_qty is a bare "gram" with the count in a column the
    # shards do not carry, which costs ~9% of shard bytes to say nothing.
    for barcode, name, unit_qty, is_weighted in conn.execute(
            "SELECT barcode, name, unit_qty, is_weighted FROM products"):
        entry = {"n": name, "p": {}}
        if is_weighted:
            entry["w"] = 1
            unit = (unit_qty or "").strip()
            if unit and unit != UNKNOWN_UNIT:
                entry["u"] = unit
        entries[barcode] = entry
    # [price, how many branches sell at that baseline] - the count lets the
    # drill-down say "and 29 other branches" without naming stores we only
    # know by absence from price_exceptions.
    for barcode, chain_id, price, store_count in conn.execute(
            "SELECT barcode, chain_id, price, store_count FROM chain_prices"):
        entry = entries.get(barcode)
        if entry is not None:
            entry["p"][chain_id] = [price, store_count or 0]
    # Shards store chain ids, not names - the names ride along in index.json
    # so the client can label a result without a second lookup.
    chain_names = dict(conn.execute("SELECT chain_id, name FROM chains"))
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    built_at = meta.get("built_at", "")
    # When a chain could not be scraped, its data is carried forward from an
    # earlier run rather than dropped. Publishing when each chain was actually
    # built is what lets the client label those prices as old instead of
    # presenting them as today's.
    chain_as_of = json.loads(meta.get("chain_as_of") or "{}")
    detail_count = write_detail(conn, args.out_dir)
    promo_count, promo_products = write_promos(
        conn, args.out_dir, (built_at or "")[:10] or "0000-00-00")
    generic_entries, of_barcode = write_generics(conn, args.out_dir)
    conn.close()

    # "g" points a member barcode at its generic, so scanning the code printed
    # on one chain's tomato sticker resolves to the row that knows all of them.
    # It rides on the product's existing shard rather than a reverse map of its
    # own: the scan already fetches that file.
    for barcode, key in of_barcode.items():
        entry = entries.get(barcode)
        if entry is not None:
            entry["g"] = key

    name_count = write_names(entries, generic_entries, of_barcode, args.out_dir)

    shards = split(list(entries), 0)

    largest = 0
    for prefix, barcodes in shards.items():
        payload = {barcode: entries[barcode] for barcode in barcodes}
        path = os.path.join(args.out_dir, f"{prefix}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        largest = max(largest, os.path.getsize(path))

    # The client takes the longest prefix in this list that its barcode starts
    # with, and requests that shard.
    with open(os.path.join(args.out_dir, "index.json"), "w", encoding="utf-8") as handle:
        json.dump({"levels": list(LEVELS), "shards": sorted(shards),
                   "chains": chain_names, "built_at": built_at,
                   "chain_as_of": chain_as_of,
                   "products": len(entries),
                   # How many products have a promotion somewhere. A client can
                   # skip fetching promo shards entirely when this is 0.
                   "promo_products": promo_products,
                   # Loose produce collapsed across chains. Zero means the
                   # generics layer is absent and the client should fall back
                   # to per-barcode rows, so an older API stays readable.
                   "generics": len(generic_entries)}, handle, ensure_ascii=False)

    depths = defaultdict(int)
    for prefix in shards:
        depths[len(prefix)] += 1
    stale = [chain_names.get(cid, cid) for cid, at in chain_as_of.items()
             if built_at and at[:10] < built_at[:10]]
    if stale:
        print(f"[catalog] carried forward from an earlier build: {', '.join(sorted(stale))}")
    print(f"[catalog] {len(shards)} shards, {len(entries):,} barcodes, "
          f"largest {largest / 1e3:.0f} KB -> {args.out_dir}")
    print("[catalog] shards by prefix length: "
          + ", ".join(f"{k}->{v}" for k, v in sorted(depths.items())))
    # + index, stores, generics, detail/index, promo/index, name/index
    total_files = len(shards) + detail_count + promo_count + name_count + 6
    print(f"[catalog] {total_files} files total")
    if total_files > MAX_FILES:
        print(f"[catalog] {total_files} files exceeds the {MAX_FILES}-file limit",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
