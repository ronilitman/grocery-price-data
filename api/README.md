# api/

A small FastAPI service that serves the catalogue (`app.db`, built by
`scripts/build_app_db.py`, KAN-6) read-only, over HTTPS, from the free GCP
`e2-micro` box `grocery-api`. Full design: KAN-4.

KAN-10 built the VM, HTTPS via Tailscale Funnel, and `GET /health`. KAN-12
added `GET /search` (`api/search.py`). KAN-13 added `GET /meta`,
`GET /product/{barcode}`, `GET /generic/{key}` and `POST /products`
(`api/products.py`, `api/catalog.py`) - everything grocery-list-app's
`src/prices.js` reads from the published JSON shards except name search.
The nightly build-and-swap is KAN-11.

## Architecture

```
Browser ──https://grocery-api.tail1b4121.ts.net──▶ Tailscale Funnel ──encrypted tunnel──▶ VM (IPv6 egress only) ──▶ uvicorn 127.0.0.1:8000
```

The VM has **no public IPv4** on purpose - Google bills every public IPv4 on
a VM (static or ephemeral) at ~$3.60/month, and the free tier only covers one
hour a month. External IPv6 is free and gives the VM internet egress
(Debian mirrors, PyPI, Tailscale). GitHub has no IPv6, so the VM cannot
`git clone` - see "Deploying" below for how code gets there instead.

Tailscale Funnel terminates TLS with a Tailscale-managed (Let's Encrypt)
certificate and proxies straight to `127.0.0.1:8000` - no reverse proxy to
run ourselves, no open firewall ports, no domain. The only inbound firewall
rule is IAP SSH
(`tcp:22` from `35.235.240.0/20`, Google's IAP range) on `grocery-vpc`;
Tailscale needs no inbound rule of its own.

## SSH (via IAP, no public IP needed)

```bash
gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
  --zone us-central1-a --tunnel-through-iap
```

## Deploying

One-time VM setup (packages, the `grocery` user, `/srv/grocery/{app,venv,data}`,
the venv) - idempotent, safe to re-run:

```bash
gcloud compute scp api/deploy/swap-app-db.sh api/deploy/grocery-swap-app-db.sudoers \
  grocery-api:/tmp/ --project gen-lang-client-0902689301 \
  --zone us-central1-a --tunnel-through-iap
gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
  --zone us-central1-a --tunnel-through-iap --command "sudo bash -s" \
  < api/deploy/install.sh
```

(install.sh runs entirely over stdin, so it has no access to sibling repo
files - the swap script and its sudoers grant are staged at fixed `/tmp`
paths by the `scp` first. Both commands are idempotent.)

Shipping code (and every redeploy after) - run from the Mac, from the repo
root:

```bash
api/deploy/deploy.sh
```

It tars `api/` and `scripts/`, copies the tarball to the VM over the IAP
tunnel (`gcloud compute scp`, not `git`), unpacks it into `/srv/grocery/app`,
installs `api/requirements.txt` into the venv, and restarts the
`grocery-api` systemd unit. Idempotent - re-running it is how you deploy a
code change.

## The `meta` table

`scripts/build_app_db.py` (KAN-6) writes `meta` as key/value rows, not a
single row with `built_at`/`chain_as_of` columns:

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- rows: ('built_at', '2026-09-13T15:38:00+00:00'), ('chain_as_of', '{}'), ...
```

`chain_as_of`'s value is a JSON-encoded object (chain id -> ISO date; `{}` if
none are stale), matching the shape already published in the JSON contract's
`index.json` (see the root `CLAUDE.md`). `GET /health` reads both keys and
decodes `chain_as_of`.

## Nightly app.db push and swap (KAN-11)

The API reads `/srv/grocery/data/live.db` (override with the `APP_DB` env
var - the tests do this to point at a fixture). Every night, after
`.github/workflows/build.yml`'s `publish` job has already deployed the JSON
shards to Pages, it also:

1. Builds `app.db` from the same `prices.db` (`scripts/build_app_db.py`) and
   a manifest describing it (`scripts/app_db_manifest.py` - `built_at`,
   `sha256` of the uncompressed file, byte size, row counts for `products`,
   `chain_prices` and `deals`).
2. Compresses it with `zstd -19 --long -T0` (see the workflow for why zstd
   over gzip: mainly `--long`'s cross-file matching on a ~500 MB SQLite file
   with a lot of repetition - roughly 5x smaller than the uncompressed file,
   markedly better than gzip on the same data. The VM's `install.sh` installs
   the same `zstd` so decompression uses the identical tool/flags).
3. Joins the tailnet as an ephemeral `tag:ci` node (`tailscale/github-action`)
   and `scp`s `app.db.zst` + `app.db.json` to
   `grocery@grocery-api.tail1b4121.ts.net:/srv/grocery/data/incoming/` over
   Tailscale SSH - no key, authorised by the tailnet policy (see "Owner
   prerequisites" below).
4. Runs `ssh grocery@grocery-api.tail1b4121.ts.net sudo /srv/grocery/bin/swap-app-db.sh`,
   which does the actual swap (see "The swap script" below).

Every step from "Build app.db" onward runs **after** `deploy-pages` and is
`continue-on-error: true`, and `prices.db` is moved to `$RUNNER_TEMP` rather
than deleted before app.db is built - a broken build or an unreachable VM can
never turn tonight's shard publish red. Until the owner has added the two
`TS_OAUTH_*` repository secrets (see below), the credentials-check step
prints a `::notice::` and every push step is skipped - the workflow change is
safe to merge before that happens. The job summary always records whether
app.db was published, its sizes and the swap result.

### The swap script

`api/deploy/swap-app-db.sh`, installed at `/srv/grocery/bin/swap-app-db.sh`
(root-owned, mode 0755, **not** writable by `grocery`) by `api/deploy/install.sh`.
`grocery` may run it as root, and only it, via the sudoers drop-in
`api/deploy/grocery-swap-app-db.sudoers` (installed at
`/etc/sudoers.d/grocery-swap-app-db`) - so the nightly SSH session can trigger
a swap without ever having a real root shell.

Given `/srv/grocery/data/incoming/app.db.zst` + `app.db.json`, it:

1. Exits early ("already current") if the manifest's `sha256` matches
   `/srv/grocery/data/live.sha256` - a re-run changes nothing.
2. Decompresses to `next.db` and verifies, in order: `sha256`,
   `PRAGMA quick_check = ok`, `products > 200,000`, and that `meta.built_at`
   is newer than `live.db`'s (ISO-8601 strings compare correctly as plain
   text - see the `meta` table section above). Any failure here deletes
   `next.db` and leaves `live.db` completely untouched.
3. Only once everything above passes: `live.db` -> `prev.db`, `next.db` ->
   `live.db`, writes `live.sha256`, `chown`s to `grocery`, restarts
   `grocery-api`, and polls `/health` for up to 60s until its `built_at`
   matches. A poll timeout rolls back to `prev.db`, restarts again, and exits
   non-zero.

Every decision is logged with `logger -t swap-app-db` - see "Logs" below. A
`flock` on `/srv/grocery/data/.swap-app-db.lock` keeps a manual run and a
nightly run from ever racing each other.

### Running a swap manually (over IAP)

```bash
# Build + describe an app.db, from the repo root:
python3 scripts/build_app_db.py --db prices.db --out app.db
python3 scripts/app_db_manifest.py --db app.db --out app.db.json
zstd -19 --long -T0 app.db -o app.db.zst

# Land both files in incoming/ (scp can't write there directly as your own
# gcloud/IAP identity - it isn't `grocery` - so stage via /tmp and move+chown
# as root; the nightly job instead scp's directly as `grocery` over
# Tailscale SSH, which owns that directory):
gcloud compute scp app.db.zst app.db.json grocery-api:/tmp/ \
  --project gen-lang-client-0902689301 --zone us-central1-a --tunnel-through-iap
gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
  --zone us-central1-a --tunnel-through-iap --command "
    sudo mv /tmp/app.db.zst /tmp/app.db.json /srv/grocery/data/incoming/
    sudo chown grocery:grocery /srv/grocery/data/incoming/app.db.zst /srv/grocery/data/incoming/app.db.json
    sudo -u grocery sudo /srv/grocery/bin/swap-app-db.sh"
```

Then check `https://grocery-api.tail1b4121.ts.net/health` for the new
`built_at`, and `sudo journalctl -t swap-app-db -n 30` for the decision log.

### Rolling back to `prev.db`

A failed health poll already does this automatically. To do it by hand (e.g.
a bad build that still passed every check):

```bash
gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
  --zone us-central1-a --tunnel-through-iap --command "
    sudo mv /srv/grocery/data/live.db /tmp/bad.db
    sudo mv /srv/grocery/data/prev.db /srv/grocery/data/live.db
    sudo mv /tmp/bad.db /srv/grocery/data/prev.db
    sudo rm -f /srv/grocery/data/live.sha256
    sudo chown grocery:grocery /srv/grocery/data/live.db /srv/grocery/data/prev.db
    sudo systemctl restart grocery-api"
```

`live.sha256` is removed because it would otherwise still name the bad
build's hash, and swap-app-db.sh's "already current" check (step 1 above)
would then wrongly treat a retry of that same bad manifest as a no-op.

### Owner prerequisites

Three things only the owner can do; the workflow change is written to be
safe to merge before any of them exist (see above).

**1. Tailnet policy** - two tags and an access + SSH rule, in the Tailscale
admin console's Access Controls (policy file):

```json
{
  "tagOwners": {
    "tag:ci": ["autogroup:admin"],
    "tag:server": ["autogroup:admin"]
  },
  "acls": [
    {"action": "accept", "src": ["tag:ci"], "dst": ["tag:server:*"]}
  ],
  "ssh": [
    {
      "action": "accept",
      "src": ["tag:ci"],
      "dst": ["tag:server"],
      "users": ["grocery"]
    }
  ]
}
```

Merge these into the existing policy JSON rather than replacing it.

**2. Tag the VM** as `tag:server` - either in the admin Machines page, or by
approving `sudo tailscale up --advertise-tags=tag:server --ssh` run on the
VM (this repo does not do this).

**3. An OAuth client** - Tailscale admin console, Settings -> OAuth clients ->
Generate. Scope: write access to Auth Keys. Tag: `tag:ci`. Add its client ID
and secret as GitHub repository secrets (Settings -> Secrets and variables ->
Actions) named exactly:

- `TS_OAUTH_CLIENT_ID`
- `TS_OAUTH_SECRET`

Once all three are done, the next `workflow_dispatch` (or nightly cron) run
pushes and swaps app.db automatically - no further change needed here.

## Tailscale Funnel

```bash
sudo tailscale up --hostname=grocery-api --ssh   # one-time, needs owner login
sudo tailscale funnel --bg 8000                  # needs Funnel + HTTPS certs
                                                  # enabled on the tailnet first
tailscale funnel status                          # confirm it's on
```

Public URL: `https://grocery-api.tail1b4121.ts.net` -> `http://127.0.0.1:8000`.
Both Funnel and `tailscaled` come back on their own after a reboot - no
extra service to enable.

## Logs

```bash
sudo journalctl -u grocery-api -f       # the API
sudo journalctl -u tailscaled -f        # Tailscale / Funnel
sudo journalctl -t swap-app-db -n 30    # the last nightly/manual swap decision log
```

CI's own job summary (the "Job summary - nightly app.db push" step) records
whether app.db was built, its sizes and the swap outcome for every run,
without needing to SSH in at all.

## Tests

```bash
python3 -m pip install -r api/requirements-dev.txt
python3 -m pytest tests/api -q
```

## Rate limiting

No Cloudflare sits in front of this box (parked, per the KAN-4 design), so
there's no free abuse protection either. `api/main.py` enforces a
per-client-IP token bucket (20 req/s, burst 40) in-process and returns 429
past it.

Funnel proxies every request through the tailnet, so the TCP peer FastAPI
sees is always `127.0.0.1` - keying the bucket on the socket peer would put
every real client in one shared bucket. Funnel sets `X-Forwarded-For` to the
actual client's IP, but that header is only trusted when the TCP peer is
loopback - i.e. it can only have been added by something running on the VM
itself. A caller that reaches the app directly (bypassing Funnel) can't spoof
it to dodge the limit, because for a non-loopback peer the header is ignored
entirely and the socket peer is used instead. See `tests/api/test_rate_limit.py`
for both cases.
