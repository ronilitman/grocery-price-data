"""Harvest a store snapshot from the cheapersal/additlist compareItemCode API.

KAN-34 step 3. This is a manual, one-off collection of a third-party chain's
store list, for Roni to look at and eventually match against our own
branches - matching is a separate, later step and is deliberately NOT done
here. This script is not part of the nightly build and must never be wired
into a GitHub Actions workflow: it hits a live third-party endpoint and is
meant to be run by hand, occasionally, from a laptop.

Why probing works at all: compareItemCode answers "which stores near this
point carry this barcode", not "list all your stores" - there is no such
endpoint. So no single call is a full census. Probing many points spread
across the country and unioning the `stores` arrays by `_id` approximates
one, because the result set is distance-influenced (probing only from Tel
Aviv undercounts everywhere else).

Measured in production before this change (KAN-34): the barcode dimension
is worthless. Five different barcodes probed from the same Tel Aviv point
returned the *identical* 465 stores every time - the first probe added all
465, the next four added zero. `stores` is every store near the probe
location, not the stores stocking the queried barcode; there is no
distance/radius parameter, but those 465 stores span roughly a 10 km
radius around the point. So this harvest uses one widely-stocked barcode
for every probe and spends its whole call budget on spreading probe
*locations* across the country instead.

Facts established before writing this (do not rediscover them by reading a
raw response - see the module docstring in KAN-34's step-3 brief):
  * `location` is sent as [lon, lat].
  * `supermarketParentId` is present on some stores only; kept when present.
  * There is no postcode in their payload.
  * `distance` is relative to the location sent in that call and is
    meaningless across calls, so it is dropped before storing.
  * A barcode with nothing nearby is a clean empty `stores` list, not an
    error - only network/HTTP failures are treated as probe failures.

The host rate-limits hard and bans by IP at the TCP level (connections
refused on every port, ~0.1s, no HTTP reached at all - not a 429). Measured
ban duration was ~40 minutes. So this script persists progress after every
successful probe and, on a connection failure, sleeps out the ban and
resumes rather than treating it as fatal. It is safe to kill at any point;
re-running it picks up from outputs/cheapersal_harvest_state.json (not
committed - see .gitignore) instead of starting over.

A connection failure (URLError/OSError/TimeoutError - no HTTP reached) is
the only thing treated as the IP ban. A 404 from probe() is a genuine,
expected HTTP response meaning "no store near here stocks this barcode" and
is handled as a normal zero-result probe, not an error. 429 and 5xx are
real HTTP responses too - a server-side throttle or overload, not the IP
ban - so they get the same wait-and-retry treatment but are logged
separately. Any other 4xx is recorded and skipped, without retrying it.

Usage:
    python3 scripts/harvest_store_snapshot.py
    python3 scripts/harvest_store_snapshot.py --out data/cheapersal_stores.json --report review/cheapersal_harvest.md
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

ENDPOINT = "https://price-api.additlist.com/compareItemCode/"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "data", "cheapersal_stores.json")
DEFAULT_REPORT = os.path.join(ROOT, "review", "cheapersal_harvest.md")

# One barcode is used for every probe - measured to make no difference to
# the result (see the module docstring). 7290004131074 was probe 1 in
# production and is known-good. The rest of this list is kept only as a
# record of what else was verified against the live endpoint, in case the
# single barcode ever needs replacing (e.g. discontinued).
BARCODES = {
    "milk (Tnuva 3% carton)": "7290004131074",
}
OTHER_VERIFIED_BARCODES_NOT_USED = {
    "Bamba (Osem 80g)": "7290000066318",
    "bread (Angel sliced, 750g)": "7290000379104",
    "eggs (Tnuva L, 12ct)": "7290000604022",
    "cleaning (Sano Spark dish soap, 1L)": "7290108350531",
    "pharmacy (Acamol To-Go, 16ct)": "7290000801483",
}

# Probe points spread across the country so ~10 km circles around them
# cover the populated parts of Israel - real town centers, not a blind
# lat/lon grid, so calls aren't wasted on sea or empty desert. Ordered
# roughly by population, big centers first, so a run that gets cut short
# still covers the most people per call; sparser regions (Arava, small
# Negev and Galilee towns) are last.
LOCATIONS = {
    # Gush Dan / central coastal plain (largest metro, and the Haredi
    # centers embedded in it).
    "Tel Aviv": (34.7818, 32.0853),
    "Petah Tikva": (34.8878, 32.0917),
    "Rishon LeZion": (34.7913, 31.9730),
    "Ashdod": (34.6446, 31.8044),
    "Netanya": (34.8600, 32.3215),
    "Bnei Brak": (34.8330, 32.0807),
    "Holon": (34.7792, 32.0158),
    "Ramat Gan": (34.8248, 32.0684),
    "Rehovot": (34.8094, 31.8947),
    "Bat Yam": (34.7503, 32.0171),
    "Ashkelon": (34.5715, 31.6688),
    "Kfar Saba": (34.9068, 32.1750),
    "Herzliya": (34.8438, 32.1663),
    "Modiin": (35.0128, 31.8969),
    "Lod": (34.8967, 31.9516),
    "Ramla": (34.8667, 31.9282),
    "Raanana": (34.8714, 32.1836),
    "Givatayim": (34.8113, 32.0722),
    "Hod HaSharon": (34.8878, 32.1553),
    "Modiin Illit": (35.0453, 31.9319),
    "Rosh HaAyin": (34.9573, 32.0956),
    "Elad": (34.9553, 32.0500),
    "Yavne": (34.7422, 31.8783),
    "Nes Ziona": (34.7969, 31.9294),

    # Jerusalem corridor, and Beitar Illit / Beit Shemesh.
    "Jerusalem": (35.2137, 31.7683),
    "Beit Shemesh": (34.9887, 31.7455),
    "Beitar Illit": (35.1128, 31.6997),
    "Mevaseret Zion": (35.1508, 31.8031),
    "Maale Adumim": (35.2977, 31.7728),

    # Haifa and the Krayot.
    "Haifa": (34.9896, 32.7940),
    "Kiryat Ata": (35.1097, 32.8000),
    "Kiryat Motzkin": (35.0764, 32.8356),
    "Kiryat Bialik": (35.0872, 32.8281),
    "Kiryat Yam": (35.0686, 32.8461),
    "Nesher": (35.0389, 32.7669),
    "Tirat Carmel": (34.9722, 32.7614),

    # Sharon (between Netanya and Haifa) and the Shfela.
    "Hadera": (34.9184, 32.4340),
    "Umm al-Fahm": (35.1531, 32.5175),
    "Pardes Hanna-Karkur": (34.9722, 32.4736),
    "Tayibe": (35.0122, 32.2667),
    "Tira": (34.9508, 32.2350),
    "Baqa al-Gharbiyye": (35.0442, 32.4142),
    "Zichron Yaakov": (34.9522, 32.5731),
    "Or Akiva": (34.9167, 32.5081),
    "Binyamina": (34.9500, 32.5192),
    "Gedera": (34.7736, 31.8144),
    "Kiryat Malachi": (34.7454, 31.7295),
    "Kiryat Gat": (34.7642, 31.6100),

    # Lower Galilee, Jezreel valley and the Arab towns there.
    "Nazareth": (35.3035, 32.7021),
    "Nof HaGalil": (35.3178, 32.7106),
    "Afula": (35.2897, 32.6078),
    "Tiberias": (35.5312, 32.7922),
    "Sakhnin": (35.2986, 32.8656),
    "Shfaram": (35.1697, 32.8056),
    "Karmiel": (35.2969, 32.9186),
    "Migdal HaEmek": (35.2419, 32.6767),
    "Yokneam": (35.1108, 32.6564),
    "Beit Shean": (35.4989, 32.4964),

    # Upper Galilee and the western Galilee coast.
    "Nahariya": (35.0989, 33.0072),
    "Safed": (35.4967, 32.9647),
    "Maalot-Tarshiha": (35.2708, 33.0175),
    "Kiryat Shmona": (35.5697, 33.2075),

    # Negev, down through the Arava to Eilat, and the Bedouin towns.
    "Beer Sheva": (34.7913, 31.2530),
    "Rahat": (34.7519, 31.3925),
    "Netivot": (34.5900, 31.4231),
    "Sderot": (34.5964, 31.5253),
    "Ofakim": (34.6200, 31.3153),
    "Dimona": (35.0333, 31.0692),
    "Arad": (35.2131, 31.2589),
    "Mitzpe Ramon": (34.8014, 30.6097),
    "Eilat": (34.9482, 29.5581),
}

DELAY_SECONDS = 60.0  # the host bans by IP after ~11 requests at 1.5s
                       # spacing; 60s is a deliberately conservative default
DEFAULT_PATIENCE = 20  # consecutive no-new-store probes before giving up.
                        # "Plateau" now means geographic saturation, not
                        # barcode saturation - Gush Dan alone has ~24 probe
                        # points a few km apart, so a run of overlapping
                        # zero/low-new probes inside one dense cluster is
                        # expected and must not stop a run that still has
                        # whole unvisited regions (Galilee, Negev) left.

BAN_WAIT_SECONDS = 45 * 60  # measured: a ban lifted within ~40 minutes; pad it
MAX_BAN_CYCLES = 6  # give up if banned this many times in a row with no
                     # successful probe in between

# Resume state: the union of stores and which locations have already been
# probed, written after every successful probe. Not committed - see
# .gitignore (outputs/).
DEFAULT_STATE = os.path.join(ROOT, "outputs", "cheapersal_harvest_state.json")


def probe(barcode, lon, lat, timeout=20):
    """One call to compareItemCode. Returns its `stores` list (possibly
    empty). Raises urllib.error.* on a network/HTTP failure."""
    body = json.dumps({"itemCode": barcode, "city": None,
                        "location": {"lon": lon, "lat": lat}}).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data.get("stores", []) or []


def _pair_key(city):
    return city


def _migrate_done_pairs(done_pairs):
    """Old state files (before this change) keyed done_pairs by
    "city\x1ebarcode label", because the harvest used to probe several
    barcodes per city. Production measured that the barcode never changes
    the result (see the module docstring - 5 barcodes from Tel Aviv
    returned the identical 465 stores), so any old-format entry for a city
    already captured that location's stores. Collapse those keys down to
    the city alone instead of discarding the progress they represent. A
    key with no "\x1e" is already the new format and passes through
    unchanged."""
    return {key.split("\x1e", 1)[0] for key in done_pairs}


def load_state(path):
    """Read the resume state, or a fresh empty one if there is none yet."""
    if not os.path.exists(path):
        return {"stores": {}, "done_pairs": [], "zero_streak": 0, "log": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state, path):
    """Write the resume state atomically (write to a temp file, then
    rename), so a kill mid-write can't corrupt it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def harvest(barcodes=None, locations=None, patience=DEFAULT_PATIENCE, delay=DELAY_SECONDS,
            state_path=DEFAULT_STATE, sleep_fn=time.sleep,
            ban_wait=BAN_WAIT_SECONDS, max_ban_cycles=MAX_BAN_CYCLES):
    """Union stores by `_id` across every probed location.

    One barcode, one probe per location, in `locations` dict order (big
    population centers first - see LOCATIONS). Stops early once `patience`
    consecutive probes add nothing new - continuing past a plateau is just
    hammering a third-party API for no reason. Note that "plateau" now
    means geographic saturation, since the barcode no longer varies.

    Resumable: progress (the stores collected so far, and which locations
    are already done) is loaded from `state_path` at the start and saved
    back after every successful probe, so a kill and restart loses nothing
    and doesn't re-spend probes already done. A state file from before this
    change (keyed by city+barcode) is migrated automatically - see
    `_migrate_done_pairs`.

    A connection failure (refused / DNS / timeout - anything short of a
    valid HTTP response) is treated as a ban: probing stops, the process
    sleeps `ban_wait` seconds, then the same probe is retried. This is not
    fatal - the sleep and retry happen inline. If `max_ban_cycles` bans in
    a row pass with no successful probe in between, harvesting gives up so
    it can't run forever.

    Returns (stores_by_id, log, stopped_early, gave_up). `log` is one dict
    per probe, in order, with enough detail to build a coverage curve and a
    per-probe report: city, barcode label, barcode, ok, returned, new,
    total_after, and error (if any).
    """
    barcodes = barcodes or BARCODES
    locations = locations or LOCATIONS

    state = load_state(state_path)
    stores = state.get("stores", {})
    done_pairs = _migrate_done_pairs(state.get("done_pairs", []))
    log = state.get("log", [])
    zero_streak = state.get("zero_streak", 0)

    pairs = [
        (city, lon, lat, label, barcode)
        for city, (lon, lat) in locations.items()
        for label, barcode in barcodes.items()
    ]

    stopped_early = False
    gave_up = False
    ban_cycles_no_progress = 0
    progressed_since_last_ban = False

    i = 0
    while i < len(pairs):
        city, lon, lat, label, barcode = pairs[i]
        key = _pair_key(city)
        if key in done_pairs:
            i += 1
            continue

        entry = {"city": city, "barcode_label": label, "barcode": barcode}
        try:
            result = probe(barcode, lon, lat)
        except urllib.error.HTTPError as e:
            # We reached their server and it answered with a status - the
            # opposite of the IP ban, which never reaches HTTP at all.
            if e.code == 404:
                # A clean "no store stocks this item here" answer, not an
                # error - falls straight through to the normal success path.
                result = []
                entry["note"] = "404: no stores for this barcode here"
            elif e.code == 429 or 500 <= e.code < 600:
                entry.update(ok=False, error=str(e), returned=0, new=0, total_after=len(stores))
                log.append(entry)
                print(f"{city:12s} {label:38s} HTTP {e.code} - throttled/server "
                      "error, backing off (not the IP ban)")

                if progressed_since_last_ban:
                    ban_cycles_no_progress = 0
                    progressed_since_last_ban = False
                ban_cycles_no_progress += 1

                if ban_cycles_no_progress >= max_ban_cycles:
                    gave_up = True
                    print(f"Gave up: {ban_cycles_no_progress} backoff cycles in a "
                          "row with no successful probe in between.")
                    save_state({"stores": stores, "done_pairs": sorted(done_pairs),
                                "zero_streak": zero_streak, "log": log}, state_path)
                    break

                print(f"Waiting {ban_wait}s before retrying...")
                sleep_fn(ban_wait)
                continue  # retry the same pair, don't advance i
            else:
                entry.update(ok=False, error=str(e), returned=0, new=0, total_after=len(stores))
                log.append(entry)
                done_pairs.add(key)
                print(f"{city:12s} {label:38s} HTTP {e.code} - recording and "
                      "moving on (not retried)")
                save_state({"stores": stores, "done_pairs": sorted(done_pairs),
                            "zero_streak": zero_streak, "log": log}, state_path)
                i += 1
                continue  # no retry, no sleep - this pair is just done
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            entry.update(ok=False, error=str(e), returned=0, new=0, total_after=len(stores))
            log.append(entry)
            print(f"{city:12s} {label:38s} connection failure ({e}) - treating as a ban")

            if progressed_since_last_ban:
                ban_cycles_no_progress = 0
                progressed_since_last_ban = False
            ban_cycles_no_progress += 1

            if ban_cycles_no_progress >= max_ban_cycles:
                gave_up = True
                print(f"Gave up: {ban_cycles_no_progress} bans in a row with no "
                      "successful probe in between.")
                save_state({"stores": stores, "done_pairs": sorted(done_pairs),
                            "zero_streak": zero_streak, "log": log}, state_path)
                break

            print(f"Waiting {ban_wait}s for the ban to lift...")
            sleep_fn(ban_wait)
            continue  # retry the same pair, don't advance i

        new = 0
        for s in result:
            sid = s.get("_id")
            if not sid:
                continue
            if sid not in stores:
                new += 1
            clean = dict(s)
            clean.pop("distance", None)
            stores[sid] = clean
        entry.update(ok=True, returned=len(result), new=new, total_after=len(stores))
        zero_streak = 0 if new else zero_streak + 1
        progressed_since_last_ban = True

        log.append(entry)
        done_pairs.add(key)
        note = f"  ({entry['note']})" if entry.get("note") else ""
        print(f"{city:12s} {label:38s} +{entry['new']:<4d} total={len(stores)}{note}")

        save_state({"stores": stores, "done_pairs": sorted(done_pairs),
                    "zero_streak": zero_streak, "log": log}, state_path)

        i += 1
        if zero_streak >= patience:
            stopped_early = True
            break
        sleep_fn(delay)

    return stores, log, stopped_early, gave_up


def write_snapshot(stores, path=DEFAULT_OUT, barcodes=None, locations=None):
    barcodes = barcodes or BARCODES
    locations = locations or LOCATIONS
    doc = {
        "_meta": {
            "description": "Store list harvested from the cheapersal/additlist "
                            "price-comparison API, for KAN-34. A manual one-off "
                            "snapshot of a third-party service, not a scrape we "
                            "run - re-run scripts/harvest_store_snapshot.py by "
                            "hand to refresh it.",
            "endpoint": ENDPOINT,
            "harvested": date.today().isoformat(),
            "barcodes_used": dict(barcodes),
            "probe_locations": {city: {"lon": lon, "lat": lat} for city, (lon, lat) in locations.items()},
            "store_count": len(stores),
        },
        "stores": stores,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def load_snapshot(path=DEFAULT_OUT):
    """Read the committed snapshot and return {_id: store_dict}, without the
    _meta header."""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    return doc["stores"]


def write_report(stores, log, stopped_early, path=DEFAULT_REPORT):
    by_chain = {}
    for s in stores.values():
        by_chain[s.get("chainName", "?")] = by_chain.get(s.get("chainName", "?"), 0) + 1
    ranked = sorted(by_chain.items(), key=lambda kv: (-kv[1], kv[0]))

    lines = []
    lines.append("# Cheapersal/additlist store harvest (KAN-34 step 3)")
    lines.append("")
    lines.append(f"Harvested {date.today().isoformat()} from `{ENDPOINT}` "
                  "(a manual one-off snapshot of a third-party service).")
    lines.append("")
    lines.append(f"**Total unique stores harvested: {len(stores)}**")
    lines.append("")
    lines.append("## Per-chain counts")
    lines.append("")
    lines.append("| chainName | stores harvested |")
    lines.append("|---|---|")
    for name, count in ranked:
        lines.append(f"| {name} | {count} |")
    lines.append("")
    lines.append("## Coverage curve (running total after each probe)")
    lines.append("")
    lines.append("| # | city | barcode | returned | new | total |")
    lines.append("|---|---|---|---|---|---|")
    for i, e in enumerate(log, 1):
        status = str(e["returned"]) if e.get("ok", True) else f"ERROR: {e.get('error')}"
        lines.append(f"| {i} | {e['city']} | {e['barcode_label']} | {status} | {e['new']} | {e['total_after']} |")
    lines.append("")
    if stopped_early:
        lines.append("Stopped early: coverage plateaued (several consecutive probes added nothing new).")
    else:
        lines.append("Ran every planned location probe without hitting the plateau patience limit.")
    lines.append("")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--patience", type=int, default=DEFAULT_PATIENCE)
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS)
    parser.add_argument("--state", default=DEFAULT_STATE,
                         help="resume-state file (progress so far); not committed")
    args = parser.parse_args()

    stores, log, stopped_early, gave_up = harvest(
        patience=args.patience, delay=args.delay, state_path=args.state)
    write_snapshot(stores, path=args.out)
    write_report(stores, log, stopped_early, path=args.report)
    print(f"\n{len(stores)} unique stores written to {args.out}")
    print(f"Report written to {args.report}")
    if gave_up:
        print(f"\nGave up after {MAX_BAN_CYCLES} consecutive bans with no "
              f"progress. Re-run the same command to resume from {args.state}.")


if __name__ == "__main__":
    main()
