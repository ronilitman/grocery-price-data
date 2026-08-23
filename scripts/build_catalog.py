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
    for barcode, name in conn.execute("SELECT barcode, name FROM products"):
        entries[barcode] = {"n": name, "p": {}}
    for barcode, chain_id, price in conn.execute(
            "SELECT barcode, chain_id, price FROM chain_prices"):
        entry = entries.get(barcode)
        if entry is not None:
            entry["p"][chain_id] = price
    # Shards store chain ids, not names - the names ride along in index.json
    # so the client can label a result without a second lookup.
    chain_names = dict(conn.execute("SELECT chain_id, name FROM chains"))
    built_at = dict(conn.execute("SELECT key, value FROM meta")).get("built_at", "")
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
    if len(shards) + 1 > MAX_FILES:
        print(f"[catalog] {len(shards)} shards exceeds the {MAX_FILES}-file limit",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
