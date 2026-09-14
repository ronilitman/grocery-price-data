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
gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
  --zone us-central1-a --tunnel-through-iap --command "sudo bash -s" \
  < api/deploy/install.sh
```

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

## Seeding / updating `live.db`

The API reads `/srv/grocery/data/live.db` (override with the `APP_DB` env
var - the tests do this to point at a fixture). It is built with
`scripts/build_app_db.py` from a merged `prices.db` and copied up with
`gcloud compute scp --tunnel-through-iap`:

```bash
python3 scripts/build_app_db.py --db prices.db --out app.db
gcloud compute scp app.db grocery-api:/tmp/app.db \
  --project gen-lang-client-0902689301 --zone us-central1-a --tunnel-through-iap
gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
  --zone us-central1-a --tunnel-through-iap --command "
    sudo chmod 644 /tmp/app.db
    sudo -u grocery mv /tmp/app.db /srv/grocery/data/live.db
    sudo systemctl restart grocery-api"
```

The nightly automated pull-and-swap is KAN-11 - out of scope here.

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
sudo journalctl -u grocery-api -f     # the API
sudo journalctl -u tailscaled -f      # Tailscale / Funnel
```

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
