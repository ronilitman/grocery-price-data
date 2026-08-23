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
import shutil
import sqlite3
import sys
from collections import defaultdict

LEVELS = (3, 6, 8)      # split deeper only where a bucket is actually crowded
MAX_ITEMS = 2000        # ~150 KB per shard
MAX_FILES = 20000       # the tightest limit among static hosts

# Per-store prices are far bulkier than the search payload, so they get their
# own deeper split and a byte budget rather than an item count.
DETAIL_LEVELS = (3, 6, 8, 10, 13)
DETAIL_MAX_BYTES = 120_000


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


def split_by_size(entries, keys, depth_index):
    """Like split(), but bucket until the encoded shard fits the byte budget."""
    depth = DETAIL_LEVELS[depth_index]
    buckets = defaultdict(list)
    for key in keys:
        buckets[key[:depth].ljust(depth, "0")].append(key)

    shards = {}
    for prefix, members in buckets.items():
        payload = {k: entries[k] for k in members}
        if (len(dump(payload).encode()) <= DETAIL_MAX_BYTES
                or depth_index + 1 >= len(DETAIL_LEVELS)):
            shards[prefix] = members
        else:
            shards.update(split_by_size(entries, members, depth_index + 1))
    return shards


def write_detail(conn, out_dir):
    """Per-store prices: barcode -> chain -> [[store_id, price], ...].

    Only stores whose price differs from the chain baseline appear here - the
    rest are covered by the baseline already in the search shard. Store names
    live once in stores.json and are referenced by id.
    """
    detail_dir = os.path.join(out_dir, "detail")
    os.makedirs(detail_dir, exist_ok=True)

    stores = defaultdict(dict)
    for chain_id, store_id, name, city in conn.execute(
            "SELECT chain_id, store_id, store_name, city FROM stores"):
        stores[chain_id][store_id] = [name or "", city or ""]
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
    # user for a weight. "u" carries that unit for display. Both are omitted
    # when empty: 88% of products are not weighed, and with 109k entries every
    # key that is always present is paid for 109k times.
    for barcode, name, unit_qty, is_weighted in conn.execute(
            "SELECT barcode, name, unit_qty, is_weighted FROM products"):
        entry = {"n": name, "p": {}}
        if is_weighted:
            entry["w"] = 1
        unit = (unit_qty or "").strip()
        if unit:
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
    built_at = dict(conn.execute("SELECT key, value FROM meta")).get("built_at", "")
    detail_count = write_detail(conn, args.out_dir)
    conn.close()

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
                   "products": len(entries)}, handle, ensure_ascii=False)

    depths = defaultdict(int)
    for prefix in shards:
        depths[len(prefix)] += 1
    print(f"[catalog] {len(shards)} shards, {len(entries):,} barcodes, "
          f"largest {largest / 1e3:.0f} KB -> {args.out_dir}")
    print("[catalog] shards by prefix length: "
          + ", ".join(f"{k}->{v}" for k, v in sorted(depths.items())))
    total_files = len(shards) + detail_count + 3   # + index, stores, detail/index
    print(f"[catalog] {total_files} files total")
    if total_files > MAX_FILES:
        print(f"[catalog] {total_files} files exceeds the {MAX_FILES}-file limit",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
