"""KAN-34 geocode second pass: clean the 956 "no result" addresses and
re-query Nominatim (see scripts/geocode_cleanup.py for the rules).

The 1,177 already-geocoded and 191 no-address branches are untouched. This
script only ever looks at branches scripts/geocode_branches.py already
classified "no_result" in outputs/geocode_state.json.

Why this reads review/geocode_review.md instead of re-running
extract_branch_records: this worktree has no dumps/ (gitignored, and the
raw STORE_FILE snapshots from the first pass are gone) - but the first pass
already wrote every no_result branch's chain/address/city into the "No
result" table in review/geocode_review.md, keyed by chain_id/store_id, which
joins back to branch_uid via data/branch_ids.json exactly the way
geocode_branches.py itself would. That table is address and city as
separate fields (not the concatenated query string in geocode_state.json),
which is what the cleanup rules need.

Persists after every lookup to outputs/geocode_cleanup_state.json (gitignored,
same as geocode_state.json) so a kill mid-run loses nothing. Run:

    python3 scripts/geocode_recover.py

Then scripts/geocode_recover.py --finalize merges hits into
data/branch_locations.json and rewrites review/geocode_review.md.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geocode_cleanup as gc  # noqa: E402
from geocode_branches import geocode, RATE_LIMIT_SECONDS, MAX_RETRIES  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
STATE_PATH = os.path.join(ROOT, "outputs", "geocode_state.json")
CLEANUP_STATE_PATH = os.path.join(ROOT, "outputs", "geocode_cleanup_state.json")
BRANCH_IDS_PATH = os.path.join(DATA_DIR, "branch_ids.json")
LOCATIONS_PATH = os.path.join(DATA_DIR, "branch_locations.json")
REVIEW_PATH = os.path.join(ROOT, "review", "geocode_review.md")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _parse_no_result_table(review_text):
    """Yields (chain_name, chain_id, store_id, address, city) per row.

    Handles the one known row whose Address field itself contains a literal
    "|" (an embedded URL), which splits into extra columns: everything
    between the always-present chain_id/store_id and the trailing City
    column is rejoined into the address, exactly reversing what the split
    did.
    """
    in_section = False
    for line in review_text.splitlines():
        if line.startswith("## No result"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section or not line.startswith("|") or line.startswith("|---"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if parts[0] == "Chain":
            continue
        if len(parts) < 5:
            continue
        chain_name, chain_id, store_id = parts[0], parts[1], parts[2]
        city = parts[-1]
        address = "|".join(parts[3:-1])
        yield chain_name, chain_id, store_id, address, city


def _original_review_text(review_path=REVIEW_PATH):
    """The first pass's committed review.md, not whatever is on disk now.

    finalize() below rewrites review.md's "No result" section down to
    whatever is still unrecovered, and renames it to "Still no result" -
    running this script a second time (to pick up a rule fix, say) must not
    parse that shrunk, renamed table as if it were the full 956. `git show
    HEAD:...` always returns the original first-pass content regardless of
    how many times finalize() has already run, which is what makes this
    whole script safely re-runnable. Falls back to the working-tree file
    only if git has no HEAD copy (e.g. before the first commit).
    """
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{os.path.relpath(review_path, ROOT)}"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        return result.stdout
    except (subprocess.CalledProcessError, OSError):
        with open(review_path, "r", encoding="utf-8") as f:
            return f.read()


def load_no_result_records(review_path=REVIEW_PATH, branch_ids_path=BRANCH_IDS_PATH,
                            state_path=STATE_PATH):
    """branch_uid -> {chain_name, chain_id, store_id, address, city} for every
    branch the first pass classified "no_result". Cross-checked against
    outputs/geocode_state.json so a mismatch is loud, not silent.
    """
    branch_ids = load_json(branch_ids_path)["ids"]
    state = load_json(state_path)
    expected = {uid for uid, v in state.items() if v.get("status") == "no_result"}

    records = {}
    text = _original_review_text(review_path)
    for chain_name, chain_id, store_id, address, city in _parse_no_result_table(text):
        branch_uid = branch_ids.get(f"{chain_id}|{store_id}")
        if not branch_uid:
            print(f"[geocode_recover] no branch_uid for {chain_id}|{store_id}, skipping",
                  file=sys.stderr)
            continue
        records[branch_uid] = {
            "chain_name": chain_name,
            "chain_id": chain_id,
            "store_id": store_id,
            "address": address,
            "city": city or None,
        }

    missing = expected - records.keys()
    extra = records.keys() - expected
    if missing or extra:
        print(f"[geocode_recover] WARNING: review.md/geocode_state.json mismatch - "
              f"{len(missing)} missing, {len(extra)} unexpected", file=sys.stderr)
    return records


def run(records=None, cleanup_state_path=CLEANUP_STATE_PATH, sleep_fn=time.sleep,
        max_retries=MAX_RETRIES, geocode_fn=geocode):
    """Try the cleaned, then street+number, query for every pending branch.

    A branch already in cleanup_state is never re-attempted (resume). A
    branch with no available new query (cleaning changed nothing and there
    is no house number to fall back to) costs no network call: recorded
    immediately as "no_new_attempt".
    """
    if records is None:
        records = load_no_result_records()
    state = load_json(cleanup_state_path) if os.path.exists(cleanup_state_path) else {}

    pending = [uid for uid in records if uid not in state]
    last_request = 0.0
    done = 0
    for uid in pending:
        rec = records[uid]
        attempts = gc.progressive_attempts(rec["address"], rec["city"])
        if not attempts:
            state[uid] = {"status": "no_new_attempt"}
            save_state(state, cleanup_state_path)
            continue

        outcome = None
        for attempt in attempts:
            retries = 0
            while True:
                elapsed = time.monotonic() - last_request
                if elapsed < RATE_LIMIT_SECONDS:
                    sleep_fn(RATE_LIMIT_SECONDS - elapsed)
                try:
                    results = geocode_fn(attempt["query"])
                    last_request = time.monotonic()
                except urllib.error.HTTPError as e:
                    last_request = time.monotonic()
                    if e.code == 429 or 500 <= e.code < 600:
                        retries += 1
                        if retries > max_retries:
                            print(f"[geocode_recover] giving up on {uid} this run "
                                  f"after {retries - 1} retries (HTTP {e.code})",
                                  file=sys.stderr)
                            outcome = "giveup"
                            break
                        sleep_fn(min(60, 2 ** retries))
                        continue
                    print(f"[geocode_recover] HTTP {e.code} for {uid}, skipping this run",
                          file=sys.stderr)
                    outcome = "giveup"
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    last_request = time.monotonic()
                    retries += 1
                    if retries > max_retries:
                        print(f"[geocode_recover] giving up on {uid} this run "
                              f"after {retries - 1} retries ({e})", file=sys.stderr)
                        outcome = "giveup"
                        break
                    sleep_fn(min(60, 2 ** retries))
                    continue
                else:
                    if results:
                        top = results[0]
                        outcome = {
                            "status": "geocoded",
                            "lat": float(top["lat"]),
                            "lon": float(top["lon"]),
                            "city": rec["city"],
                            "stage": attempt["stage"],
                            "rules": attempt["rules"],
                            "query": attempt["query"],
                        }
                    break
            if outcome == "giveup":
                break
            if isinstance(outcome, dict):
                break  # hit - stop at the first successful stage

        if outcome == "giveup":
            continue  # not persisted - retried fresh next run, like geocode_branches
        if outcome is None:
            outcome = {"status": "no_result", "tried": [a["stage"] for a in attempts]}
        state[uid] = outcome
        save_state(state, cleanup_state_path)
        done += 1
        if done % 50 == 0:
            print(f"[geocode_recover] {done}/{len(pending)} pending branches processed")

    return records, state


def finalize(records=None, cleanup_state=None, locations_path=LOCATIONS_PATH,
             review_path=REVIEW_PATH):
    """Merge cleanup-pass hits into data/branch_locations.json and rewrite
    review/geocode_review.md. Read-modify-write on both, run once after
    `run()` has processed every pending branch (or as many as the time
    budget allowed - anything still pending is simply not merged yet).
    """
    if records is None:
        records = load_no_result_records()
    if cleanup_state is None:
        cleanup_state = load_json(CLEANUP_STATE_PATH)

    recovered = {uid: v for uid, v in cleanup_state.items() if v.get("status") == "geocoded"}

    locations = load_json(locations_path)
    # Every branch_uid in `records` is reset here, not just newly-recovered
    # ones: a rule fix (like the bare-number-address guard added during this
    # pass's own sanity check) can remove a uid from `recovered` between one
    # run and the next, and the stale "geocoded" entry from the earlier,
    # wrong run must not survive - it goes back to null, never left as a
    # leftover guess.
    for uid in records:
        if uid in recovered:
            hit = recovered[uid]
            locations["locations"][uid] = {
                "lat": hit["lat"],
                "lon": hit["lon"],
                "city": hit["city"],
                "method": "nominatim",
            }
        else:
            locations["locations"][uid] = None
    locations["_meta"]["second_pass"] = {
        "description": "KAN-34 cleanup pass: cleaned address (and, for some, "
                        "street+house-number only) re-queried for every branch "
                        "the first pass left as no_result. See "
                        "review/geocode_review.md for which rule recovered each.",
        "date": time.strftime("%Y-%m-%d"),
        "recovered": len(recovered),
    }
    tmp = locations_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(locations, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, locations_path)

    _rewrite_review(records, cleanup_state, recovered, review_path)
    return recovered


def _parse_per_chain_table(review_text):
    """chain_name -> {total, geocoded, no_result, no_address} from the
    existing '## Per-chain' section, so unrelated chains' rows are carried
    forward unchanged.
    """
    per_chain = {}
    in_section = False
    for line in review_text.splitlines():
        if line.startswith("## Per-chain"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section or not line.startswith("|") or line.startswith("|---"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if parts[0] == "Chain":
            continue
        name, total, geocoded, no_result, no_address = parts
        per_chain[name] = {
            "total": int(total), "geocoded": int(geocoded),
            "no_result": int(no_result), "no_address": int(no_address),
        }
    return per_chain


def _rewrite_review(records, cleanup_state, recovered, review_path):
    # The first pass's own baseline, not the working-tree file - see
    # _original_review_text. Re-deriving the per-chain table from this fixed
    # baseline plus the *current* full `recovered` set (rather than editing
    # the working file's numbers in place) is what makes re-running this
    # script idempotent: run it twice with the same cleanup_state and every
    # count comes out identical, instead of drifting further each time.
    original = _original_review_text(review_path)
    per_chain = _parse_per_chain_table(original)

    for uid in recovered:
        chain_name = records[uid]["chain_name"]
        counts = per_chain[chain_name]
        counts["no_result"] -= 1
        counts["geocoded"] += 1

    rule_counts = {}
    stage_counts = {"cleaned": 0, "street_number": 0}
    for hit in recovered.values():
        stage_counts[hit["stage"]] = stage_counts.get(hit["stage"], 0) + 1
        for rule in hit["rules"]:
            rule_counts[rule] = rule_counts.get(rule, 0) + 1

    still_null = []
    for uid, rec in records.items():
        if uid in recovered:
            continue
        still_null.append((rec["chain_name"], rec["chain_id"], rec["store_id"],
                            rec["address"], rec["city"] or ""))

    lines = ["# Geocode review (KAN-34)", "",
             "Nominatim geocoding of every branch's own address. `no result` means "
             "Nominatim was queried and came back empty; `no address` means the "
             "chain never gave this branch one. Neither is a guess - both are "
             "`null` in `data/branch_locations.json`.", "",
             "A second pass (see below) cleaned the addresses the first pass "
             "could not parse and re-queried Nominatim for those alone.", "",
             "## Per-chain", "",
             "| Chain | Branches | Geocoded | No result | No address |",
             "|---|---:|---:|---:|---:|"]
    for chain_name in sorted(per_chain):
        c = per_chain[chain_name]
        lines.append(f"| {chain_name} | {c['total']} | {c['geocoded']} | "
                      f"{c['no_result']} | {c['no_address']} |")

    lines += ["", "## Cleanup pass", "",
              f"Of the {len(records)} branches left `no result` by the first pass, "
              f"{len(recovered)} were recovered by cleaning the address and/or "
              "narrowing it to street+house-number, then re-querying Nominatim. "
              "Each cleaned/narrowed query is only sent when it actually differs "
              "from the one that already missed.", "",
              "By stage (which attempt hit):", "",
              "| Stage | Recovered |", "|---|---:|"]
    for stage in ("cleaned", "street_number"):
        lines.append(f"| {stage} | {stage_counts.get(stage, 0)} |")
    lines += ["", "By rule (how many recoveries had this rule fire - a recovery "
              "can count for more than one rule):", "",
              "| Rule | Recoveries |", "|---|---:|"]
    for rule in sorted(rule_counts):
        lines.append(f"| {rule} | {rule_counts[rule]} |")

    lines += ["", f"## Still no result ({len(still_null)})", "",
              "Nominatim was queried for the original address, the cleaned "
              "address (if different), and street+house-number (if available "
              "and different) - all missed. Still `null`.", "",
              "| Chain | chain_id | store_id | Address | City |",
              "|---|---|---|---|---|"]
    for chain_name, chain_id, store_id, address, city in sorted(still_null):
        lines.append(f"| {chain_name} | {chain_id} | {store_id} | {address} | {city} |")

    with open(review_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finalize", action="store_true",
                         help="Skip the geocoding loop; merge whatever cleanup "
                              "state already exists into the outputs.")
    args = parser.parse_args()

    records = load_no_result_records()
    if not args.finalize:
        records, state = run(records=records)
    recovered = finalize(records=records)
    print(f"[geocode_recover] {len(recovered)}/{len(records)} no_result branches recovered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
