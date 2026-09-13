#!/usr/bin/env bash
# One-time (idempotent) setup for the grocery-api VM.
#
# The VM has no public IPv4 (see api/README.md - cost, not security) and so
# no path to GitHub; this only installs OS packages and creates the
# `grocery` user, /srv/grocery/{app,venv,data} and the venv itself. Code is
# never `git clone`d here - it is copied from the Mac by api/deploy/deploy.sh,
# which also installs api/requirements.txt into the venv this script creates
# and installs/restarts the grocery-api systemd unit.
#
# Run over the IAP tunnel, e.g.:
#   gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
#     --zone us-central1-a --tunnel-through-iap --command "sudo bash -s" \
#     < api/deploy/install.sh

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo)" >&2
  exit 1
fi

echo "==> python3-venv, sqlite3, zstd"
apt-get update -qq
apt-get install -y -qq python3-venv sqlite3 zstd

echo "==> system user 'grocery'"
if ! id -u grocery >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /srv/grocery --shell /usr/sbin/nologin grocery
else
  echo "    already exists"
fi

mkdir -p /srv/grocery/app /srv/grocery/venv /srv/grocery/data
chown -R grocery:grocery /srv/grocery

echo "==> venv"
if [ ! -x /srv/grocery/venv/bin/python3 ]; then
  sudo -u grocery python3 -m venv /srv/grocery/venv
else
  echo "    already exists"
fi

echo "==> done. Run api/deploy/deploy.sh from the Mac next to ship code and start the service."
