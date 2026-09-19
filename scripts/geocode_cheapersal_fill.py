"""KAN-34 step 3: fill still-unresolved branches from the cheapersal store
list on exact address agreement, after meaning-preserving normalisation only.

Precedence (highest first): first-pass geocode, cleanup-pass geocode,
cheapersal exact-address fill, otherwise null. This script only touches
branches that are still `null` in data/branch_locations.json after both
geocoding passes - it never overwrites a geocoded entry.

Normalisation (nothing else - no edit distance, no token overlap, no
similarity score, no threshold):
  * strip cheapersal's trailing ", <city>" suffix
  * strip a leading Hebrew street-word prefix (רח' / רח" / רחוב / שד' / שדרות)
  * normalise Hebrew quote marks and apostrophes
  * normalise the gap between street and house number
  * collapse whitespace
  * drop a trailing period or comma

Matching is address-only (not scoped by chain): a cheapersal store sharing
our exact normalised address is the same physical location regardless of
which chain's dump the address came from. A normalised address that maps to
more than one store on either side is ambiguous and is filled for neither.

Also runs the sanity check: every geocoded coordinate (all three sources)
more than 20km from every known cheapersal store in its own city, or outside
Israel's bounding box, is nulled and reported - a bad geocode must not ship
silently.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geocode_recover as gr  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUR_BRANCHES_PATH = os.path.join(DATA_DIR, "branch_addresses.json")
CHEAPERSAL_PATH = os.path.join(DATA_DIR, "cheapersal_stores.json")
LOCATIONS_PATH = os.path.join(DATA_DIR, "branch_locations.json")
CLEANUP_STATE_PATH = os.path.join(ROOT, "outputs", "geocode_cleanup_state.json")
GEOCODE_STATE_PATH = os.path.join(ROOT, "outputs", "geocode_state.json")
BRANCH_IDS_PATH = os.path.join(DATA_DIR, "branch_ids.json")
REVIEW_PATH = os.path.join(ROOT, "review", "geocode_review.md")
FILL_RESULT_PATH = os.path.join(ROOT, "scripts_scratch", "cheapersal_fill_result.json")

# The five chain_ids with zero no_result branches in the first pass, so they
# never appear in the original "No result" table _parse_no_result_table reads
# - resolved by matching each chain_id's branch count against the single
# per-chain row it can only be (checked by hand against data/branch_addresses.json).
CHAIN_NAME_OVERRIDES = {
    "7290000000003": "סיטי מרקט",
    "7290058160839-010": "אקספרס מהדרין",
    "7290058289400": 'קי טי יבוא ושווק בע"מ',
    "7290526500006": "Dabach",
    "7290455000004": "7290455000004",
}

# Israel's bounding box, generously - anything outside this is not a
# plausible branch location no matter what the geocoder said.
ISRAEL_BBOX = {"lat_min": 29.3, "lat_max": 33.5, "lon_min": 34.1, "lon_max": 35.9}
SANITY_RADIUS_KM = 20.0

STREET_PREFIXES = ["רח'", 'רח"', "רחוב", "שד'", "שדרות"]
QUOTES = re.compile(r"[\"'׳״`]")
WS = re.compile(r"\s+")
HEB = r"א-ת"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def strip_city_suffix(addr, city):
    """Cheapersal addresses often end with ', <city>' (or the city leads,
    comma-separated, in a minority of rows) - strip either form. It carries
    no street information and our side never has it."""
    a = (addr or "").strip()
    c = (city or "").strip()
    if not c:
        return a
    variants = {c, c.replace(" - ", "-"), c.replace("-", " - "), c.replace("-", " ")}
    for cv in variants:
        cv = cv.strip()
        if not cv:
            continue
        for sep in (", ", " ,", ","):
            suffix = f"{sep}{cv}"
            if a.endswith(suffix):
                a = a[: -len(suffix)].strip()
                break
        prefix = f"{cv},"
        if a.startswith(prefix):
            a = a[len(prefix):].strip()
    return a


def strip_street_prefix(addr):
    a = addr.strip()
    for p in STREET_PREFIXES:
        if a.startswith(p):
            return a[len(p):].strip()
    return a


def normalize_address(addr, city=None):
    s = (addr or "").strip()
    if city:
        s = strip_city_suffix(s, city)
    s = strip_street_prefix(s)
    s = QUOTES.sub("", s)
    s = s.rstrip(".,").strip()
    # normalise the street/number gap: insert a space at a letter<->digit
    # boundary so "אבןגבירול157" and "אבן גבירול 157" normalise the same.
    s = re.sub(rf"(?<=[{HEB}])(?=\d)", " ", s)
    s = re.sub(rf"(?<=\d)(?=[{HEB}])", " ", s)
    s = WS.sub(" ", s).strip()
    return s


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_our_branches():
    return load_json(OUR_BRANCHES_PATH)["branches"]


def load_cheapersal_stores():
    return load_json(CHEAPERSAL_PATH)["stores"]


def build_cheapersal_index(stores):
    """normalised address -> list of store dicts (with lat/lon/city added)."""
    by_norm = defaultdict(list)
    for sid, s in stores.items():
        addr = s.get("address")
        city = s.get("city")
        norm = normalize_address(addr, city)
        if not norm:
            continue
        loc = s.get("location") or [None, None]
        lon, lat = loc[0], loc[1]
        by_norm[norm].append({
            "id": sid, "lat": lat, "lon": lon, "city": (city or "").strip(),
            "raw_address": addr,
        })
    return by_norm


def compute_fill(locations, our_by_uid, cheapersal_index):
    """Returns (accepted, review_candidates, ambiguous) for branches still
    null. accepted: uid -> fill dict (same city). review_candidates: uid ->
    fill dict (blank/differing city, not applied). ambiguous: list of
    (norm_address, our_uids, their_ids).
    """
    # First pass: find, for each normalised our-address, the branch uids
    # sharing it, and the cheapersal stores sharing it.
    our_norm_map = defaultdict(list)  # norm -> [uid]
    for uid, v in locations.items():
        if v is not None:
            continue
        b = our_by_uid.get(uid)
        if not b:
            continue
        raw = (b.get("address") or "").strip()
        if not raw or raw.lower() == "unknown":
            continue
        norm = normalize_address(raw)
        if not norm:
            continue
        our_norm_map[norm].append(uid)

    accepted = {}
    review_candidates = {}
    ambiguous = []

    for norm, uids in our_norm_map.items():
        their = cheapersal_index.get(norm)
        if not their:
            continue
        if len(uids) > 1 or len(their) > 1:
            ambiguous.append({"address": norm, "our_uids": uids,
                               "their_ids": [t["id"] for t in their]})
            continue
        uid = uids[0]
        store = their[0]
        our_city = (our_by_uid[uid].get("city") or "").strip()
        fill = {
            "lat": store["lat"], "lon": store["lon"], "city": store["city"],
            "method": "cheapersal", "matched_address": norm,
            "cheapersal_id": store["id"],
        }
        if our_city and store["city"] and our_city == store["city"]:
            accepted[uid] = fill
        else:
            review_candidates[uid] = fill

    return accepted, review_candidates, ambiguous


def sanity_check(locations, cheapersal_stores):
    """Flag any filled coordinate (any source) that is outside Israel's
    bounding box, or more than SANITY_RADIUS_KM from every known cheapersal
    store in its own (normalised) city. Pure arithmetic against
    data/cheapersal_stores.json - no model, no network call."""
    city_points = defaultdict(list)
    for s in cheapersal_stores.values():
        city = (s.get("city") or "").strip()
        loc = s.get("location")
        if not city or not loc:
            continue
        lon, lat = loc[0], loc[1]
        city_points[city].append((lat, lon))

    flagged = []
    for uid, v in locations.items():
        if v is None:
            continue
        lat, lon, city = v.get("lat"), v.get("lon"), (v.get("city") or "").strip()
        if lat is None or lon is None:
            continue
        if not (ISRAEL_BBOX["lat_min"] <= lat <= ISRAEL_BBOX["lat_max"]
                and ISRAEL_BBOX["lon_min"] <= lon <= ISRAEL_BBOX["lon_max"]):
            flagged.append({"uid": uid, "reason": "outside_israel_bbox",
                             "lat": lat, "lon": lon, "city": city, "method": v.get("method")})
            continue
        pts = city_points.get(city)
        if not pts:
            continue  # no reference points for this city - nothing to check against
        min_dist = min(haversine_km(lat, lon, plat, plon) for plat, plon in pts)
        if min_dist > SANITY_RADIUS_KM:
            flagged.append({"uid": uid, "reason": "far_from_city", "distance_km": round(min_dist, 1),
                             "lat": lat, "lon": lon, "city": city, "method": v.get("method")})
    return flagged


def build_chain_name_map(our_branches):
    """chain_id -> chain_name for every chain_id in data/branch_addresses.json.

    Most come straight from the first pass's own "No result" table (any
    chain_id with at least one no_result branch). The handful with zero
    no_result branches never appear there, so they come from
    CHAIN_NAME_OVERRIDES instead (each verified by matching branch count -
    see the comment there).
    """
    text = gr._original_review_text()
    mapping = {}
    for chain_name, chain_id, _store_id, _address, _city in gr._parse_no_result_table(text):
        mapping[chain_id] = chain_name
    mapping.update(CHAIN_NAME_OVERRIDES)
    missing = {b["chain_id"] for b in our_branches} - mapping.keys()
    if missing:
        raise SystemExit(f"no chain_name for chain_ids: {sorted(missing)}")
    return mapping


def build_per_chain_table(locations, our_branches, chain_name_map):
    """chain_name -> {total, resolved, unresolved, no_address}, resolved
    meaning any non-null location.locations entry regardless of which of
    the three sources filled it.
    """
    counts = defaultdict(lambda: {"total": 0, "resolved": 0, "unresolved": 0, "no_address": 0})
    for b in our_branches:
        uid = b["branch_uid"]
        name = chain_name_map[b["chain_id"]]
        c = counts[name]
        c["total"] += 1
        raw = (b.get("address") or "").strip()
        if not raw or raw.lower() == "unknown":
            c["no_address"] += 1
        elif locations.get(uid) is not None:
            c["resolved"] += 1
        else:
            c["unresolved"] += 1
    return counts


def rule_and_stage_breakdown(cleanup_state, cleanup_recovered_final_uids):
    """Rule/stage counts over the recoveries that actually survived the
    sanity check (not the raw 305) - the review should describe what
    shipped, not what was briefly true before a bad one got nulled."""
    rule_counts = Counter()
    stage_counts = Counter()
    for uid in cleanup_recovered_final_uids:
        hit = cleanup_state[uid]
        stage_counts[hit["stage"]] += 1
        for rule in hit["rules"]:
            rule_counts[rule] += 1
    return rule_counts, stage_counts


def write_review(review_path=REVIEW_PATH):
    payload = load_json(LOCATIONS_PATH)
    locations = payload["locations"]
    our_branches = load_our_branches()
    our_by_uid = {b["branch_uid"]: b for b in our_branches if b.get("branch_uid")}
    chain_name_map = build_chain_name_map(our_branches)

    geocode_state = load_json(GEOCODE_STATE_PATH)
    cleanup_state = load_json(CLEANUP_STATE_PATH)
    fill_result = load_json(FILL_RESULT_PATH)

    pass1_uids = {uid for uid, v in geocode_state.items() if v.get("status") == "geocoded"}
    pass2_uids = {uid for uid, v in cleanup_state.items() if v.get("status") == "geocoded"}
    cheapersal_uids = set(fill_result["accepted"].keys())
    no_address_uids = {uid for uid, v in geocode_state.items() if v.get("status") == "no_address"}
    sanity_nulled_uids = {s["uid"] for s in fill_result["sanity_nulled"]}

    final_pass1 = pass1_uids - sanity_nulled_uids
    final_pass2 = pass2_uids - sanity_nulled_uids
    final_cheapersal = cheapersal_uids - sanity_nulled_uids  # sanity found none here; kept general
    final_null = {uid for uid, v in locations.items() if v is None} - no_address_uids

    totals = {
        "geocoded_pass1": len(final_pass1),
        "recovered_pass2": len(final_pass2),
        "filled_cheapersal": len(final_cheapersal),
        "still_null": len(final_null),
        "no_address": len(no_address_uids),
    }
    total_sum = sum(totals.values())

    per_chain = build_per_chain_table(locations, our_branches, chain_name_map)
    rule_counts, stage_counts = rule_and_stage_breakdown(cleanup_state, final_pass2)

    lines = []
    lines.append("# Geocode review (KAN-34)")
    lines.append("")
    lines.append(
        "Every branch's own coordinate, from three sources in precedence order: "
        "Nominatim on the branch's own address (first pass), Nominatim again on "
        "a cleaned/narrowed version of addresses the first pass missed (second "
        "pass), and, only for what both passes left unresolved, an exact-address "
        "match (after meaning-preserving normalisation only - no similarity "
        "score) against the third-party cheapersal store list (third pass). "
        "`null` means unresolved and awaiting review - a branch either has no "
        "address at all (the chain's own field is literally \"unknown\" or "
        "empty), or every source above was tried and none produced a "
        "coordinate. Neither is ever a guess."
    )
    lines.append("")
    lines.append(f"## Totals ({total_sum})")
    lines.append("")
    lines.append("| Source | Branches |")
    lines.append("|---|---:|")
    lines.append(f"| Geocoded, first pass (own address) | {totals['geocoded_pass1']} |")
    lines.append(f"| Recovered, cleanup pass (own address, cleaned) | {totals['recovered_pass2']} |")
    lines.append(f"| Filled from cheapersal (exact address match) | {totals['filled_cheapersal']} |")
    lines.append(f"| Still null (unresolved) | {totals['still_null']} |")
    lines.append(f"| No address at all | {totals['no_address']} |")
    lines.append(f"| **Total** | **{total_sum}** |")
    lines.append("")

    lines.append("## Per-chain")
    lines.append("")
    lines.append("Resolved counts a branch regardless of which of the three sources "
                  "filled it; unresolved excludes branches with no address at all.")
    lines.append("")
    lines.append("| Chain | Branches | Resolved | Unresolved | No address |")
    lines.append("|---|---:|---:|---:|---:|")
    for name in sorted(per_chain):
        c = per_chain[name]
        lines.append(f"| {name} | {c['total']} | {c['resolved']} | {c['unresolved']} | {c['no_address']} |")
    lines.append("")

    lines.append("## Cleanup pass (second pass)")
    lines.append("")
    lines.append(
        f"Of the 956 branches left `no result` by the first pass, {len(pass2_uids)} were "
        "recovered by cleaning the address and/or narrowing it to street+house-number, then "
        "re-querying Nominatim. Counts below are for the recoveries that survived the sanity "
        f"check ({len(final_pass2)}) - see below for the ones that did not."
    )
    lines.append("")
    lines.append("By stage (which attempt hit):")
    lines.append("")
    lines.append("| Stage | Recovered |")
    lines.append("|---|---:|")
    for stage in sorted(stage_counts):
        lines.append(f"| {stage} | {stage_counts[stage]} |")
    lines.append("")
    lines.append("By rule (how many recoveries had this rule fire - a recovery can count for "
                  "more than one rule):")
    lines.append("")
    lines.append("| Rule | Recoveries |")
    lines.append("|---|---:|")
    for rule in sorted(rule_counts):
        lines.append(f"| {rule} | {rule_counts[rule]} |")
    lines.append("")

    accepted = fill_result["accepted"]
    review_candidates = fill_result["review_candidates"]
    ambiguous = fill_result["ambiguous"]
    sanity_nulled = fill_result["sanity_nulled"]
    both_passes_unresolved = 651  # 956 no_result - 305 cleanup-pass recoveries (fixed input size)

    lines.append("## Cheapersal fill (third pass)")
    lines.append("")
    lines.append(
        f"Of the {both_passes_unresolved} branches still unresolved after both geocoding "
        f"passes (and with a usable address - not the literal \"unknown\"), "
        f"{len(accepted)} matched a cheapersal store on exact address after normalisation "
        f"(strip a trailing \", <city>\", strip a leading רח'/רח\"/רחוב/שד'/שדרות, normalise "
        "quote marks, normalise the street/number gap, collapse whitespace, drop a trailing "
        f"period or comma - nothing else) **and** agreed on city, and were filled. "
        f"A further {len(review_candidates)} matched on address alone, with the city blank or "
        f"different - left `null`, listed below for the owner to decide. "
        f"{len(ambiguous)} normalised addresses matched more than one store on one side or the "
        "other and were filled for neither (the one-to-one guard)."
    )
    lines.append("")
    lines.append(f"### Cheapersal candidates needing a decision ({len(review_candidates)})")
    lines.append("")
    lines.append("Exact address match, city blank or different - not auto-filled.")
    lines.append("")
    lines.append("| chain | our address | our city | cheapersal city | branch_uid |")
    lines.append("|---|---|---|---|---|")
    rc_rows = []
    for uid, v in review_candidates.items():
        b = our_by_uid.get(uid, {})
        chain_name = chain_name_map.get(b.get("chain_id"), b.get("chain_id"))
        rc_rows.append((chain_name, b.get("address") or "", v.get("our_city") or "",
                         v.get("city") or "", uid))
    for chain_name, addr, our_city, their_city, uid in sorted(rc_rows):
        lines.append(f"| {chain_name} | {addr} | {our_city} | {their_city} | {uid} |")
    lines.append("")

    if ambiguous:
        lines.append(f"### Ambiguous (one-to-one guard) ({len(ambiguous)})")
        lines.append("")
        lines.append("Normalised address shared by more than one branch or more than one "
                      "cheapersal store - filled for neither.")
        lines.append("")
        lines.append("| normalised address | our branch_uids | cheapersal store ids |")
        lines.append("|---|---|---|")
        for a in sorted(ambiguous, key=lambda x: x["address"]):
            lines.append(f"| {a['address']} | {', '.join(a['our_uids'])} | "
                          f"{', '.join(a['their_ids'])} |")
        lines.append("")

    from_pass2 = [s for s in sanity_nulled if s["uid"] in pass2_uids]
    from_pass1 = [s for s in sanity_nulled if s["uid"] in pass1_uids]
    from_cheapersal = [s for s in sanity_nulled if s["uid"] in cheapersal_uids]

    lines.append("## Sanity check: bad coordinates removed")
    lines.append("")
    lines.append(
        "Every filled coordinate (all three sources) checked against "
        "data/cheapersal_stores.json: flagged if outside Israel's bounding box, or more than "
        f"{SANITY_RADIUS_KM:.0f}km from every known cheapersal store in its own city. Pure "
        "arithmetic, no model and no network call. A flagged branch is written `null`, not "
        f"shipped. {len(sanity_nulled)} flagged in total: {len(from_pass2)} from the 305 "
        f"cleanup-pass recoveries, {len(from_pass1)} from the 1,177 first-pass geocodes, and "
        f"{len(from_cheapersal)} from the {len(accepted)} cheapersal fills."
    )
    lines.append("")
    lines.append("| branch_uid | pass | reason | distance (km) | city | address |")
    lines.append("|---|---|---|---:|---|---|")
    for s in sorted(sanity_nulled, key=lambda x: -(x.get("distance_km") or 0)):
        b = our_by_uid.get(s["uid"], {})
        dist = s.get("distance_km", "")
        pass_label = ("cleanup pass" if s["uid"] in pass2_uids
                      else "first pass" if s["uid"] in pass1_uids
                      else "cheapersal")
        lines.append(f"| {s['uid']} | {pass_label} | {s['reason']} | {dist} | "
                      f"{s.get('city') or ''} | {b.get('address') or ''} |")
    lines.append("")

    lines.append(f"## Still null - unresolved, awaiting review ({len(final_null)})")
    lines.append("")
    lines.append("No source above produced a coordinate (or the sanity check removed a bad "
                  "one and it has not been re-filled). `null` in data/branch_locations.json.")
    lines.append("")
    lines.append("| chain | chain_id | store_id | address | city |")
    lines.append("|---|---|---|---|---|")
    null_rows = []
    for uid in final_null:
        b = our_by_uid.get(uid, {})
        chain_name = chain_name_map.get(b.get("chain_id"), b.get("chain_id"))
        null_rows.append((chain_name, b.get("chain_id") or "", b.get("store_id") or "",
                           b.get("address") or "", b.get("city") or ""))
    for row in sorted(null_rows):
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    with open(review_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return totals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-only", action="store_true",
                         help="Skip the fill/sanity-check step and only rewrite "
                              "review/geocode_review.md from the current state "
                              "(data/branch_locations.json and "
                              "scripts_scratch/cheapersal_fill_result.json).")
    args = parser.parse_args()

    if args.review_only:
        totals = write_review()
        print(totals, "sum =", sum(totals.values()))
        return

    payload = load_json(LOCATIONS_PATH)
    locations = payload["locations"]
    our_branches = load_our_branches()
    our_by_uid = {b["branch_uid"]: b for b in our_branches if b.get("branch_uid")}
    cheapersal_stores = load_cheapersal_stores()
    cheapersal_index = build_cheapersal_index(cheapersal_stores)

    accepted, review_candidates, ambiguous = compute_fill(locations, our_by_uid, cheapersal_index)

    print(f"accepted (same city): {len(accepted)}")
    print(f"review candidates (blank/differing city): {len(review_candidates)}")
    print(f"ambiguous (1-1 guard): {len(ambiguous)}")

    for uid, fill in accepted.items():
        locations[uid] = {"lat": fill["lat"], "lon": fill["lon"], "city": fill["city"],
                           "method": "cheapersal"}

    # Sanity check runs against the state AFTER the cheapersal fill, so it
    # also covers the new fills, not just the two geocode passes.
    flagged = sanity_check(locations, cheapersal_stores)
    nulled = []
    for f in flagged:
        uid = f["uid"]
        prior = locations[uid]
        nulled.append({**f, "prior_method": prior.get("method")})
        locations[uid] = None

    payload["_meta"]["third_pass"] = {
        "description": "KAN-34 cheapersal fill: exact-address match (after "
                        "meaning-preserving normalisation only) against the "
                        "third-party cheapersal store list, for branches "
                        "still unresolved after both geocoding passes. Also "
                        "carries the post-fill sanity check that nulls any "
                        "geocode more than 20km from a known store in its "
                        "own city, or outside Israel.",
        "date": time.strftime("%Y-%m-%d"),
        "accepted": len(accepted),
        "review_candidates": len(review_candidates),
        "ambiguous": len(ambiguous),
        "sanity_nulled": len(nulled),
    }
    save_json(LOCATIONS_PATH, payload)

    out = {
        "accepted": {uid: {**accepted[uid], **{"our_address": our_by_uid[uid].get("address"),
                                                "our_city": our_by_uid[uid].get("city")}}
                     for uid in accepted},
        "review_candidates": {uid: {**review_candidates[uid],
                                     **{"our_address": our_by_uid[uid].get("address"),
                                        "our_city": our_by_uid[uid].get("city")}}
                               for uid in review_candidates},
        "ambiguous": ambiguous,
        "sanity_nulled": nulled,
    }
    scratch_out = os.path.join(ROOT, "scripts_scratch", "cheapersal_fill_result.json")
    save_json(scratch_out, out)
    print(f"sanity-check nulled: {len(nulled)}")
    for f in nulled:
        b = our_by_uid.get(f["uid"], {})
        print(f"  {f['uid']} {b.get('address')!r} {b.get('city')!r} -> {f['reason']} "
              f"{f.get('distance_km', '')} prior={f['prior_method']}")
    print(f"wrote {scratch_out}")

    totals = write_review()
    print(totals, "sum =", sum(totals.values()))


if __name__ == "__main__":
    main()
