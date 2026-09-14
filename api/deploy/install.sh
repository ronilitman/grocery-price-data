#!/usr/bin/env bash
# One-time (idempotent) setup for the grocery-api VM.
#
# The VM has no public IPv4 (see api/README.md - cost, not security) and so
# no path to GitHub; this installs OS packages, creates the `grocery` user,
# /srv/grocery/{app,venv,data,data/incoming,bin} and the venv, and installs
# the nightly app.db swap script (KAN-11) as a root-owned command `grocery`
# may run via sudo. Code is never `git clone`d here - api/ and scripts/ are
# copied from the Mac by api/deploy/deploy.sh, which also installs
# api/requirements.txt into the venv this script creates and
# installs/restarts the grocery-api systemd unit.
#
# This script is piped over stdin (see below) so it has no access to sibling
# repo files - the two files it installs verbatim (the swap script and its
# sudoers grant) must be staged at fixed /tmp paths first, by scp. Run over
# the IAP tunnel, from the repo root, e.g.:
#
#   gcloud compute scp api/deploy/swap-app-db.sh \
#     api/deploy/grocery-swap-app-db.sudoers \
#     grocery-api:/tmp/ --project gen-lang-client-0902689301 \
#     --zone us-central1-a --tunnel-through-iap
#   gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
#     --zone us-central1-a --tunnel-through-iap --command "sudo bash -s" \
#     < api/deploy/install.sh
#
# Both commands are idempotent - safe to re-run on a rebuilt VM.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo)" >&2
  exit 1
fi

echo "==> python3-venv, sqlite3, zstd, jq"
apt-get update -qq
apt-get install -y -qq python3-venv sqlite3 zstd jq

echo "==> system user 'grocery'"
if ! id -u grocery >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /srv/grocery --shell /usr/sbin/nologin grocery
else
  echo "    already exists"
fi

mkdir -p /srv/grocery/app /srv/grocery/venv /srv/grocery/data /srv/grocery/data/incoming
chown -R grocery:grocery /srv/grocery

echo "==> venv"
if [ ! -x /srv/grocery/venv/bin/python3 ]; then
  sudo -u grocery python3 -m venv /srv/grocery/venv
else
  echo "    already exists"
fi

echo "==> swap-app-db.sh (root-owned, not writable by grocery)"
STAGED_SCRIPT=/tmp/swap-app-db.sh
STAGED_SUDOERS=/tmp/grocery-swap-app-db.sudoers
if [ ! -f "$STAGED_SCRIPT" ] || [ ! -f "$STAGED_SUDOERS" ]; then
  echo "missing $STAGED_SCRIPT or $STAGED_SUDOERS - scp them up first (see the" >&2
  echo "comment at the top of this file), then re-run install.sh" >&2
  exit 1
fi
mkdir -p /srv/grocery/bin
chown root:root /srv/grocery/bin
chmod 0755 /srv/grocery/bin
install -o root -g root -m 0755 "$STAGED_SCRIPT" /srv/grocery/bin/swap-app-db.sh

echo "==> sudoers drop-in: grocery may run swap-app-db.sh as root, nothing else"
# Validate in a scratch file first - visudo -c on a bad /etc/sudoers.d file
# can lock out sudo entirely.
install -m 0440 "$STAGED_SUDOERS" /tmp/grocery-swap-app-db.sudoers.check
if ! visudo -cf /tmp/grocery-swap-app-db.sudoers.check >/dev/null; then
  rm -f /tmp/grocery-swap-app-db.sudoers.check
  echo "grocery-swap-app-db.sudoers failed visudo -c - not installed" >&2
  exit 1
fi
install -o root -g root -m 0440 /tmp/grocery-swap-app-db.sudoers.check /etc/sudoers.d/grocery-swap-app-db
rm -f /tmp/grocery-swap-app-db.sudoers.check "$STAGED_SCRIPT" "$STAGED_SUDOERS"

echo "==> done. Run api/deploy/deploy.sh from the Mac next to ship code and start the service."
