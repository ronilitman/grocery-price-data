"""Geocode every branch's own address against OSM Nominatim (KAN-34).

The previous plan matched our branches to a third-party store list built by
someone else. That is gone: geocoding our own address removes the matching
problem entirely, because a branch gets coordinates from its own address and
there is nothing to match it against.

Classification is exactly three-way, per branch_uid:

  * geocoded          - Nominatim returned a result. lat/lon/city recorded.
  * no result         - Nominatim was asked and came back empty. Recorded as
                         null in the output - never a guess.
  * no address at all - the chain never gave this branch an address. Also
                         null, and never even sent to Nominatim.

Nominatim's usage policy is load-bearing, not a suggestion: a descriptive
User-Agent and at most one request per second. ~2,133 addressed branches at
1/sec is roughly 35-40 minutes, so this is built to be killed and resumed
without losing or re-spending work - see `load_state`/`save_state` and the
per-lookup persistence in `run()`.

Run on demand only:

    python3 scripts/geocode_branches.py --dumps-dir dumps

Never wired into the nightly build or any GitHub Actions workflow.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assign_ids import iter_dump_files, open_xml, _local  # noqa: E402
from build_chain_db import (  # noqa: E402
    SUBCHAIN_SPLITS, load_branch_uids, load_city_codes, resolve_city_name, split_id,
)
from csvutil import digits  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DEFAULT_DUMPS_DIR = os.path.join(ROOT, "dumps")
DEFAULT_STATE_PATH = os.path.join(ROOT, "outputs", "geocode_state.json")
DEFAULT_OUTPUT_PATH = os.path.join(DATA_DIR, "branch_locations.json")
DEFAULT_REVIEW_PATH = os.path.join(ROOT, "review", "geocode_review.md")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "grocery-price-data/1.0 (ronilitman@gmail.com)"
RATE_LIMIT_SECONDS = 1.0
MAX_RETRIES = 5

WHITESPACE = re.compile(r"\s+")
_EMPTY_ADDRESS = {"", "unknown"}


def _clean_address(raw):
    value = WHITESPACE.sub(" ", str(raw or "")).strip()
    return value


def has_address(raw):
    return _clean_address(raw).lower() not in _EMPTY_ADDRESS


def _walk_stores(el, subchain_id, out, chain_id, chain_name):
    """Like assign_ids._walk_stores, but also captures Address/City/name.

    Mirrors the exact traversal assign_ids.py uses (a store record is any
    element with a direct StoreID/StoreId child) so the branch_uid this
    computes always matches the one data/branch_ids.json already assigned.
    """
    tag = _local(el.tag)
    if tag == "SubChain":
        subchain_id = el.findtext("SubChainID") or el.findtext("SubChainId") or subchain_id
    store_id = None
    address = None
    city = None
    for child in el:
        local = _local(child.tag)
        if local in ("StoreID", "StoreId"):
            store_id = (child.text or "").strip()
        elif local == "Address":
            address = child.text
        elif local == "City":
            city = child.text
    if store_id:
        norm_store_id = store_id.lstrip("0") or "0"
        brand = split_id(chain_id, subchain_id)
        brands = SUBCHAIN_SPLITS.get(chain_id, {})
        name = brands.get(str(subchain_id or "").strip().zfill(3)) or chain_name
        out[(brand, norm_store_id)] = (address, city, name)
        return
    for child in el:
        _walk_stores(child, subchain_id, out, chain_id, chain_name)


def extract_branch_records(dumps_dir):
    """branch_uid -> {chain_id, store_id, chain_name, address, city_name}.

    Reads the STORE_FILE dumps directly (same helpers assign_ids.py uses),
    not the built chain_dbs, which this repo does not commit. Dumps are
    processed in sorted (chronological-by-filename) order and a later file
    overwrites an earlier one for the same branch, so a branch that moved
    keeps its most recent address - the same last-write-wins behaviour
    build_chain_db.load_stores() gets from its CSV ordering.

    A branch with no entry in data/branch_ids.json (should not happen; that
    file is meant to cover every branch ever seen) is skipped with a
    warning rather than failing the run.
    """
    branch_uids = load_branch_uids()
    city_codes = load_city_codes()
    raw = {}
    for path in sorted(iter_dump_files(dumps_dir)):
        try:
            root = open_xml(path)
        except (ET.ParseError, OSError) as exc:
            print(f"[geocode_branches] skipping unreadable dump {path}: {exc}", file=sys.stderr)
            continue
        raw_chain_id = digits(root.findtext("ChainID") or root.findtext("ChainId") or "")
        if not raw_chain_id:
            continue
        chain_name = root.findtext("ChainName") or root.findtext("ChainId") or None
        _walk_stores(root, "", raw, raw_chain_id, chain_name)

    records = {}
    missing_uid = 0
    for (chain_id, store_id), (address, city, chain_name) in raw.items():
        branch_uid = branch_uids.get(f"{chain_id}|{store_id}")
        if not branch_uid:
            missing_uid += 1
            continue
        records[branch_uid] = {
            "chain_id": chain_id,
            "store_id": store_id,
            "chain_name": chain_name or chain_id,
            "address": _clean_address(address),
            "city_name": resolve_city_name(city, city_codes),
        }
    if missing_uid:
        print(f"[geocode_branches] {missing_uid} branch(es) in the dumps have no "
              f"branch_uid yet - run scripts/assign_ids.py first", file=sys.stderr)
    return records


def build_query(record):
    """The Nominatim query string for one branch.

    With a city: "<address>, <city>, ישראל". Without one (311 branches the
    chain leaves blank): the address alone, per KAN-34 - a lower hit rate
    there is expected and accepted, not worked around.
    """
    address = record["address"]
    if record["city_name"]:
        return f"{address}, {record['city_name']}, ישראל"
    return address


def geocode(query, timeout=15):
    """One Nominatim lookup. Returns its (possibly empty) result list.

    Raises urllib.error.* on anything short of a valid HTTP response -
    callers back off and retry those, per Nominatim's usage policy.
    """
    params = urllib.parse.urlencode({"q": query, "format": "json", "limit": 1})
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def load_state(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state, path):
    """Write the resume state atomically, so a kill mid-write can't corrupt it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def run(dumps_dir=DEFAULT_DUMPS_DIR, state_path=DEFAULT_STATE_PATH,
        sleep_fn=time.sleep, max_retries=MAX_RETRIES, geocode_fn=geocode):
    """Geocode every branch found in `dumps_dir`, resuming from `state_path`.

    Persists after every single lookup. A branch already in `state` is
    never re-queried - that is the whole resume mechanism. A branch that
    keeps failing after `max_retries` backoffs is left out of `state`
    entirely so a later run retries it fresh, rather than being recorded
    as a false "no result".
    """
    records = extract_branch_records(dumps_dir)
    state = load_state(state_path)

    # No-address branches cost no network call and no rate-limit budget.
    changed = False
    for uid, rec in records.items():
        if uid in state:
            continue
        if not has_address(rec["address"]):
            state[uid] = {"status": "no_address"}
            changed = True
    if changed:
        save_state(state, state_path)

    pending = [uid for uid in records if uid not in state]
    last_request = 0.0
    for i, uid in enumerate(pending):
        rec = records[uid]
        query = build_query(rec)
        retries = 0
        while True:
            elapsed = time.monotonic() - last_request
            if elapsed < RATE_LIMIT_SECONDS:
                sleep_fn(RATE_LIMIT_SECONDS - elapsed)
            try:
                results = geocode_fn(query)
                last_request = time.monotonic()
            except urllib.error.HTTPError as e:
                last_request = time.monotonic()
                if e.code == 429 or 500 <= e.code < 600:
                    retries += 1
                    if retries > max_retries:
                        print(f"[geocode_branches] giving up on {uid} this run "
                              f"after {retries - 1} retries (HTTP {e.code}); "
                              "will retry next run", file=sys.stderr)
                        break
                    wait = min(60, 2 ** retries)
                    print(f"[geocode_branches] HTTP {e.code} for {uid}, "
                          f"backing off {wait}s (attempt {retries})", file=sys.stderr)
                    sleep_fn(wait)
                    continue
                print(f"[geocode_branches] HTTP {e.code} for {uid}, skipping "
                      "this run (will retry next run)", file=sys.stderr)
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_request = time.monotonic()
                retries += 1
                if retries > max_retries:
                    print(f"[geocode_branches] giving up on {uid} this run "
                          f"after {retries - 1} retries ({e}); will retry next run",
                          file=sys.stderr)
                    break
                wait = min(60, 2 ** retries)
                print(f"[geocode_branches] connection error for {uid} ({e}), "
                      f"backing off {wait}s (attempt {retries})", file=sys.stderr)
                sleep_fn(wait)
                continue
            else:
                if results:
                    top = results[0]
                    state[uid] = {
                        "status": "geocoded",
                        "lat": float(top["lat"]),
                        "lon": float(top["lon"]),
                        "city": rec["city_name"],
                        "query": query,
                    }
                else:
                    state[uid] = {"status": "no_result", "query": query}
                save_state(state, state_path)
                if (i + 1) % 100 == 0:
                    print(f"[geocode_branches] {i + 1}/{len(pending)} done")
                break
    return records, state


def write_locations(records, state, output_path=DEFAULT_OUTPUT_PATH):
    locations = {}
    for uid in records:
        entry = state.get(uid)
        if entry and entry["status"] == "geocoded":
            locations[uid] = {
                "lat": entry["lat"],
                "lon": entry["lon"],
                "city": entry.get("city"),
                "method": "nominatim",
            }
        else:
            locations[uid] = None
    payload = {
        "_meta": {
            "description": "branch_uid -> {lat, lon, city, method}, geocoded from "
                            "the branch's own Address/City (KAN-34). A branch that "
                            "did not resolve - no address, or Nominatim found "
                            "nothing - is null. Never a guess.",
            "source": "OSM Nominatim (nominatim.openstreetmap.org/search)",
            "method": "nominatim",
            "date": time.strftime("%Y-%m-%d"),
        },
        "locations": locations,
    }
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, output_path)
    return locations


def write_review(records, state, review_path=DEFAULT_REVIEW_PATH):
    per_chain = defaultdict(lambda: defaultdict(int))
    no_result = []
    for uid, rec in records.items():
        status = state.get(uid, {}).get("status", "pending")
        per_chain[rec["chain_name"]][status] += 1
        if status == "no_result":
            no_result.append((rec["chain_name"], rec["chain_id"], rec["store_id"],
                               rec["address"], rec["city_name"] or ""))

    lines = ["# Geocode review (KAN-34)", "",
             "Nominatim geocoding of every branch's own address. `no result` means "
             "Nominatim was queried and came back empty; `no address` means the "
             "chain never gave this branch one. Neither is a guess - both are "
             "`null` in `data/branch_locations.json`.", "",
             "## Per-chain", "",
             "| Chain | Branches | Geocoded | No result | No address |",
             "|---|---:|---:|---:|---:|"]
    for chain_name in sorted(per_chain):
        counts = per_chain[chain_name]
        total = sum(counts.values())
        lines.append(f"| {chain_name} | {total} | {counts.get('geocoded', 0)} | "
                      f"{counts.get('no_result', 0)} | {counts.get('no_address', 0)} |")

    lines += ["", f"## No result ({len(no_result)})", "",
              "Nominatim was queried for these and returned nothing.", "",
              "| Chain | chain_id | store_id | Address | City |",
              "|---|---|---|---|---|"]
    for chain_name, chain_id, store_id, address, city in sorted(no_result):
        lines.append(f"| {chain_name} | {chain_id} | {store_id} | {address} | {city} |")

    os.makedirs(os.path.dirname(review_path), exist_ok=True)
    with open(review_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Geocode every branch's own address against OSM Nominatim "
                     "(KAN-34). Run on demand only - never wired into the "
                     "nightly build.")
    parser.add_argument("--dumps-dir", default=DEFAULT_DUMPS_DIR)
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--review", default=DEFAULT_REVIEW_PATH)
    args = parser.parse_args()

    records, state = run(dumps_dir=args.dumps_dir, state_path=args.state_path)
    locations = write_locations(records, state, args.output)
    write_review(records, state, args.review)

    resolved = sum(1 for v in locations.values() if v is not None)
    no_result = sum(1 for uid in records if state.get(uid, {}).get("status") == "no_result")
    no_address = sum(1 for uid in records if state.get(uid, {}).get("status") == "no_address")
    print(f"[geocode_branches] {len(records)} branches: {resolved} geocoded, "
          f"{no_result} no result, {no_address} no address")
    return 0


if __name__ == "__main__":
    sys.exit(main())
