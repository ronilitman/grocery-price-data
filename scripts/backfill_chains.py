"""Fill gaps in chain_dbs/ from earlier runs, and record how old each chain is.

A chain can produce nothing for reasons that have nothing to do with its data
being wrong: the exit node was asleep, the chain's own host refused us, or the
chain published an empty file. Rebuilding from scratch every night turns any of
those into deletion - the branches simply vanish from the site, which is worse
than showing yesterday's prices.

So a missing chain is filled from the newest artifact still in retention, and
its age is written to _freshness.json for merge_db to carry into the catalog.
The client can then say "as of Tuesday" rather than implying today.

A chain absent from *every* recent run stays absent; this only resurrects what
was working recently.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

PREFIX = "db-"


def artifact_index(repo):
    out = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/actions/artifacts?per_page=100",
         "--jq", ".artifacts[] | select(.expired == false) | "
                 "[.name, .workflow_run.id, .created_at] | @tsv"],
        capture_output=True, text=True)
    if out.returncode != 0:
        print(f"[backfill] cannot list artifacts: {out.stderr[:300]}", file=sys.stderr)
        return {}
    newest = {}
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].startswith(PREFIX):
            continue
        name, run_id, created = parts
        chain = name[len(PREFIX):]
        if chain not in newest or created > newest[chain][1]:
            newest[chain] = (run_id, created)
    return newest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="chain_dbs")
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    present = {f[:-3].upper() for f in os.listdir(args.dir) if f.endswith(".db")}
    print(f"[backfill] {len(present)} chain(s) built by this run")

    freshness = {chain: now for chain in present}
    recovered = []

    for chain, (run_id, created) in sorted(artifact_index(args.repo).items()):
        if chain.upper() in present:
            continue
        got = subprocess.run(
            ["gh", "run", "download", run_id, "-n", f"{PREFIX}{chain}", "-D", args.dir],
            capture_output=True, text=True)
        if got.returncode != 0:
            print(f"[backfill] {chain}: no usable artifact ({got.stderr.strip()[:120]})",
                  file=sys.stderr)
            continue
        freshness[chain.upper()] = created
        recovered.append((chain, created))

    for chain, created in recovered:
        print(f"[backfill] {chain}: carried forward from {created}")
    if not recovered:
        print("[backfill] nothing to carry forward")

    with open(os.path.join(args.dir, "_freshness.json"), "w", encoding="utf-8") as handle:
        json.dump(freshness, handle, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
