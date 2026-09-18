"""Assign generated ids (chain_uid / branch_uid) to every chain and branch.

The nightly build derives no id of its own for a chain or a branch - the only
identity is the raw ``chain_id`` / ``store_id`` a chain publishes, and those
are not ours: a chain can renumber a branch, and ``chain_id`` is only stable
because ``build_chain_db.split_id()`` already normalises it. KAN-34 gives the
app database an id we control, so a branch keeps the same identity even if
its raw fields ever change upstream.

Rules, and they are load-bearing:

  * ids are ``str(uuid.uuid4())`` - random, so there is nothing to derive them
    from. The committed JSON files ARE the assignment; nothing downstream may
    recompute or guess one.
  * assigned once, appended to ``data/chain_ids.json`` /
    ``data/branch_ids.json``, and never touched again. An id is never
    reused, even after its chain or branch disappears from the input -
    the file only grows.
  * running this script twice with the same input changes nothing. Running
    it after a branch vanishes from the input changes nothing either: a
    missing branch is not an id to reclaim, just one this run did not see.

Reads the raw ``Stores*`` / ``StoresFull*`` dumps (the STORE_FILE XML;
plain or gzip'd, whatever encoding the chain sends) instead of the built
chain_dbs, because the dbs are a build artifact this repo does not commit -
the dumps are the thing that is actually there to read, nightly and here.

Usage:
    python3 scripts/assign_ids.py --dumps-dir dumps
"""

import argparse
import glob
import gzip
import json
import os
import sys
import uuid
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CHAIN_IDS_PATH = os.path.join(DATA_DIR, "chain_ids.json")
BRANCH_IDS_PATH = os.path.join(DATA_DIR, "branch_ids.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_chain_db import split_id  # noqa: E402  (single source of truth for chain-id splitting)
from csvutil import digits  # noqa: E402

CHAIN_HEADER = {
    "description": (
        "Raw chain_id -> chain_uid. chain_id is the GS1 chain id, or "
        "'<chain_id>-<subchain_id>' for a chain split by SUBCHAIN_SPLITS in "
        "build_chain_db.py - the same id build_chain_db writes to the "
        "chains table."
    ),
    "note": (
        "APPEND-ONLY. Ids are assigned once by scripts/assign_ids.py and must "
        "never be renumbered, regenerated, or reused after a chain is "
        "removed. Do not hand-edit an existing entry."
    ),
}

BRANCH_HEADER = {
    "description": (
        "Raw '<chain_id>|<store_id>' -> branch_uid. chain_id and store_id are "
        "normalised exactly as build_chain_db.load_stores() writes them to "
        "the stores table (chain_id post-split, store_id left-stripped of "
        "zeros, '0' kept as '0')."
    ),
    "note": (
        "APPEND-ONLY. Ids are assigned once by scripts/assign_ids.py and must "
        "never be renumbered, regenerated, or reused after a branch is "
        "removed. Do not hand-edit an existing entry."
    ),
}


def load_ids(path, header):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return doc.get("ids", {})


def save_ids(path, header, ids):
    doc = {"_meta": header, "ids": {k: ids[k] for k in sorted(ids)}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def open_xml(path):
    """Return the parsed root of a STORE_FILE dump, whatever its wrapping.

    Some chains gzip the dump and leave it that way; others serve it plain
    in UTF-8, UTF-8-BOM or UTF-16. ``ET.fromstring`` on the raw bytes
    handles every encoding as long as the BOM/declaration is intact, so the
    only thing this has to decide is whether to gunzip first.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw)


def is_store_file(name):
    base = os.path.basename(name).lower()
    return base.startswith("stores")


def iter_dump_files(dumps_dir):
    for path in sorted(glob.glob(os.path.join(dumps_dir, "**", "*"), recursive=True)):
        if not os.path.isfile(path):
            continue
        if os.sep + "status" + os.sep in path:
            continue
        if is_store_file(path):
            yield path


def _local(tag):
    return tag.rsplit("}", 1)[-1]  # drop an XML namespace, if any


def _walk_stores(el, subchain_id):
    """Yield (subchain_id, raw_store_id) under ``el``, at any nesting depth.

    Every chain wraps its store records in ``SubChain`` -> ``Stores`` ->
    (one element per store), but the store element's own tag and how many
    generic wrapper layers sit above it vary - most chains use ``Store``
    directly under ``Stores``, City Market's ``.NET`` serialisation adds an
    extra ``SubChainsXMLObject``/``SubChainStoresXMLObject`` layer and names
    the actual record ``SubChainStoreXMLObject``. Rather than special-case
    each shape, this walks down and treats any element with a direct
    ``StoreID``/``StoreId`` child as a store record - wrapper elements never
    carry that field directly, only their descendants do, so this cannot
    double-count a wrapper as a store.
    """
    tag = _local(el.tag)
    if tag == "SubChain":
        subchain_id = el.findtext("SubChainID") or el.findtext("SubChainId") or subchain_id
    store_id = None
    for child in el:
        if _local(child.tag) in ("StoreID", "StoreId"):
            store_id = (child.text or "").strip()
            break
    if store_id:
        yield subchain_id, store_id
        return  # a store record has no further stores nested inside it
    for child in el:
        yield from _walk_stores(child, subchain_id)


def iter_branches(dumps_dir):
    """Yield (chain_id, store_id) for every branch found in the dumps.

    chain_id has split_id() already applied, and store_id is normalised the
    same way build_chain_db.load_stores() does - so both match exactly what
    ends up in the stores table's primary key.
    """
    seen_files = 0
    for path in iter_dump_files(dumps_dir):
        try:
            root = open_xml(path)
        except (ET.ParseError, OSError, gzip.BadGzipFile) as exc:
            print(f"[assign_ids] skipping unreadable dump {path}: {exc}", file=sys.stderr)
            continue
        seen_files += 1
        raw_chain_id = digits(root.findtext("ChainID") or root.findtext("ChainId") or "")
        if not raw_chain_id:
            continue
        for subchain_id, raw_store_id in _walk_stores(root, ""):
            if not raw_store_id:
                continue
            chain_id = split_id(raw_chain_id, subchain_id)
            store_id = raw_store_id.lstrip("0") or "0"
            yield chain_id, store_id
    if seen_files == 0:
        print(f"[assign_ids] no store dumps found under {dumps_dir}", file=sys.stderr)


def assign_with_paths(dumps_dir, chain_ids_path, branch_ids_path):
    """Same as ``assign()``, against explicit id-file paths (tests use this)."""
    chain_ids = load_ids(chain_ids_path, CHAIN_HEADER)
    branch_ids = load_ids(branch_ids_path, BRANCH_HEADER)

    new_chains = 0
    new_branches = 0
    for chain_id, store_id in iter_branches(dumps_dir):
        if chain_id not in chain_ids:
            chain_ids[chain_id] = str(uuid.uuid4())
            new_chains += 1
        branch_key = f"{chain_id}|{store_id}"
        if branch_key not in branch_ids:
            branch_ids[branch_key] = str(uuid.uuid4())
            new_branches += 1

    save_ids(chain_ids_path, CHAIN_HEADER, chain_ids)
    save_ids(branch_ids_path, BRANCH_HEADER, branch_ids)
    return new_chains, len(chain_ids), new_branches, len(branch_ids)


def assign(dumps_dir):
    return assign_with_paths(dumps_dir, CHAIN_IDS_PATH, BRANCH_IDS_PATH)


def main():
    parser = argparse.ArgumentParser(
        description="Assign chain_uid/branch_uid ids for every chain and branch "
                     "seen in the STORE_FILE dumps. Idempotent: existing ids "
                     "are never touched.")
    parser.add_argument("--dumps-dir", default=os.path.join(ROOT, "dumps"),
                         help="Directory tree of Stores*/StoresFull* dumps "
                              "(default: dumps)")
    args = parser.parse_args()

    new_chains, total_chains, new_branches, total_branches = assign(args.dumps_dir)
    print(f"[assign_ids] chains: +{new_chains} new, {total_chains} total")
    print(f"[assign_ids] branches: +{new_branches} new, {total_branches} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
