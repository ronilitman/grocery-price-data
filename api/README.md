# api/

A small FastAPI service that serves the catalogue (`app.db`, built by
KAN-6 — not yet available) read-only, over HTTPS, from the free GCP
`e2-micro` box `grocery-api`. Full design: KAN-4.

This subtask (KAN-10) only builds the VM, TLS and `GET /health`. `/search`,
`/product` and the rest are later subtasks (KAN-12, KAN-13); the nightly
build-and-swap is KAN-11.

## Branch currently on the VM

**Production tracks `main` once this PR is merged.** Until then, the VM at
`grocery-api` runs the feature branch `kan-10-vm-api-skeleton` directly
(pushed to the public repo, not merged) — this subtask is under review, and
the review gate says not to merge or push to `main` first, but VM/GCP setup
is allowed. Whoever merges the PR should re-run `install.sh` with
`GROCERY_BRANCH=main` (its default) to move the checkout on the VM back to
`main`.

## The `meta` table

KAN-6 owns `app.db`'s real schema and was not available while building this.
`GET /health` assumes a single-row `meta` table:

```sql
CREATE TABLE meta (
    built_at    TEXT,   -- e.g. "2026-09-13T02:00:00Z"
    chain_as_of TEXT    -- JSON object, chain id -> ISO date; "{}" if none are stale
);
```

`chain_as_of` is stored as a JSON string (matching the shape already
published in the JSON contract's `index.json` — see the root `CLAUDE.md`)
and decoded before being returned. If KAN-6 lands a different shape, this is
the one query in `api/main.py`'s `health()` to update.

## Deploying / redeploying

SSH in and run the installer (idempotent — safe to re-run for updates):

```bash
gcloud compute ssh grocery-api --project gen-lang-client-0902689301 --zone us-central1-a \
  --command "sudo GROCERY_BRANCH=kan-10-vm-api-skeleton bash -s" < api/deploy/install.sh
```

Drop `GROCERY_BRANCH=...` (or set it to `main`) once this is merged. It:

* installs Caddy (official apt repo), `python3-venv`, `sqlite3`, `git`;
* creates the `grocery` system user and `/srv/grocery/{app,venv,data}`;
* clones or updates the repo checkout at `/srv/grocery/app`;
* builds `/srv/grocery/venv` and installs `api/requirements.txt` into it;
* installs and (re)starts the `grocery-api` systemd unit and Caddy.

A redeploy of code only (no VM changes) is the same command — it re-pulls
the branch, reinstalls dependencies, and restarts the service.

## Seeding `live.db`

The API reads `/srv/grocery/data/live.db` (override with the `APP_DB` env
var — the tests do this to point at a fixture). KAN-6 (the real builder)
wasn't available for this subtask, so `live.db` on the VM right now is a
minimal hand-built SQLite file: a `meta` table with one row and a `products`
table with a few sample rows — enough to exercise `/health`, nothing more.
It will be replaced by KAN-6's real `app.db` and, later, KAN-11's nightly
pull.

## Tests

```bash
python3 -m pip install -r api/requirements-dev.txt
python3 -m pytest tests/api -q
```

## Rate limiting

No Cloudflare sits in front of this box (parked, per the KAN-4 design), so
there's no free abuse protection either. `api/main.py` enforces a per-client-IP
token bucket (20 req/s, burst 40) in-process and returns 429 past it. This
runs against the whole box, in Python, ahead of Caddy — Caddy proper has no
built-in rate-limit directive without a third-party plugin build, which was
more moving parts than this needed.
