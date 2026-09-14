#!/usr/bin/env bash
# One-time (idempotent) setup for the grocery-api VM.
#
# The VM has no public IPv4 (see api/README.md - cost, not security) and so
# no path to GitHub; this installs OS packages, creates the `grocery`
# service account and the `deploy` account, /srv/grocery/{app,venv,data,bin}
# + /srv/grocery/incoming and the venv, and installs the nightly app.db swap
# script (KAN-11) as a root-owned command `deploy` may run via sudo. Code is
# never `git clone`d here - api/ and scripts/ are copied from the Mac by
# api/deploy/deploy.sh, which also installs api/requirements.txt into the
# venv this script creates and installs/restarts the grocery-api systemd
# unit.
#
# Two accounts, two trust levels - this is the fix for a review finding
# (KAN-11 rework): `grocery` only ever runs the API and has shell
# /usr/sbin/nologin, so an API RCE lands in an account that cannot even open
# an interactive shell, let alone reach root. `deploy` is what CI's nightly
# Tailscale SSH session logs in as; it owns nothing on this box but its own
# upload directory (/srv/grocery/incoming, 0700) and has exactly one sudo
# right (see the sudoers drop-in below). Neither account can write to
# /srv/grocery/data at all - only root (running swap-app-db.sh) can - which
# is what stops a compromise of either account from planting a symlink for
# that root-run script to follow.
#
# This script is piped over stdin (see below) so it has no access to sibling
# repo files - the two files it installs verbatim (the swap script and its
# sudoers grant) must be staged at fixed /tmp paths first, by scp. Run over
# the IAP tunnel, from the repo root, e.g.:
#
#   gcloud compute scp api/deploy/swap-app-db.sh \
#     api/deploy/deploy-swap-app-db.sudoers \
#     grocery-api:/tmp/ --project gen-lang-client-0902689301 \
#     --zone us-central1-a --tunnel-through-iap
#   gcloud compute ssh grocery-api --project gen-lang-client-0902689301 \
#     --zone us-central1-a --tunnel-through-iap --command "sudo bash -s" \
#     < api/deploy/install.sh
#
# Both commands are idempotent - safe to re-run on a rebuilt VM, or to pick
# up a permissions fix on an existing one.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo)" >&2
  exit 1
fi

echo "==> python3-venv, sqlite3, zstd, jq"
apt-get update -qq
apt-get install -y -qq python3-venv sqlite3 zstd jq

echo "==> system user 'grocery' (runs the API only - nologin)"
if ! id -u grocery >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /srv/grocery --shell /usr/sbin/nologin grocery
else
  echo "    already exists"
fi

echo "==> user 'deploy' (CI's Tailscale SSH login for the nightly swap only)"
if ! id -u deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash deploy
else
  echo "    already exists"
fi
# No password login, ever - only Tailscale SSH's tailnet-identity auth (once
# the owner's ACL/SSH rule permits it) gets in as this user.
passwd -l deploy >/dev/null

mkdir -p /srv/grocery/app /srv/grocery/venv /srv/grocery/data
chown grocery:grocery /srv/grocery/app /srv/grocery/venv
# /srv/grocery itself must be root-owned too. It is grocery's home, and a
# directory's owner can rename any entry in it - even a root-owned one - so
# while grocery owned it, it could move data/ (or the swap script's
# .swap-work) aside and put its own directory full of symlinks in its place,
# undoing every permission below. pip's cache stays writable in ~/.cache.
chown root:root /srv/grocery
chmod 0755 /srv/grocery
mkdir -p /srv/grocery/.cache
chown grocery:grocery /srv/grocery/.cache
# root:grocery 0750, not grocery:grocery - 'grocery' (the API) only ever
# needs to *read* live.db (a read-only sqlite connection); it must not be
# able to write here at all. This directory, and only this directory
# (before this rework it was grocery-writable throughout), is exactly what
# let a compromised 'grocery' plant a symlink for the root-run swap script
# to follow.
chown root:grocery /srv/grocery/data
chmod 0750 /srv/grocery/data
# Superseded by /srv/grocery/incoming (deploy-owned) below - remove only if
# still empty, so a rerun here never discards anything unexpected.
rmdir /srv/grocery/data/incoming 2>/dev/null || true

echo "==> /srv/grocery/incoming (deploy:deploy 0700 - CI's upload landing zone)"
mkdir -p /srv/grocery/incoming
chown deploy:deploy /srv/grocery/incoming
chmod 0700 /srv/grocery/incoming

echo "==> tightening any pre-existing live.db/prev.db/live.sha256 ownership"
for f in /srv/grocery/data/live.db /srv/grocery/data/prev.db; do
  if [ -e "$f" ]; then
    chown root:grocery "$f"
    chmod 0640 "$f"
  fi
done
if [ -e /srv/grocery/data/live.sha256 ]; then
  chown root:root /srv/grocery/data/live.sha256
  chmod 0600 /srv/grocery/data/live.sha256
fi

echo "==> venv"
if [ ! -x /srv/grocery/venv/bin/python3 ]; then
  sudo -u grocery python3 -m venv /srv/grocery/venv
else
  echo "    already exists"
fi

echo "==> swap-app-db.sh (root-owned, not writable by deploy or grocery)"
STAGED_SCRIPT=/tmp/swap-app-db.sh
STAGED_SUDOERS=/tmp/deploy-swap-app-db.sudoers
if [ ! -f "$STAGED_SCRIPT" ] || [ ! -f "$STAGED_SUDOERS" ]; then
  echo "missing $STAGED_SCRIPT or $STAGED_SUDOERS - scp them up first (see the" >&2
  echo "comment at the top of this file), then re-run install.sh" >&2
  exit 1
fi
mkdir -p /srv/grocery/bin
chown root:root /srv/grocery/bin
chmod 0755 /srv/grocery/bin
install -o root -g root -m 0755 "$STAGED_SCRIPT" /srv/grocery/bin/swap-app-db.sh

echo "==> sudoers drop-in: deploy may run swap-app-db.sh as root, nothing else"
# Revoke the old (pre-rework) grant if a previous install left it - grocery
# must end up with zero sudo rights.
rm -f /etc/sudoers.d/grocery-swap-app-db
# Validate in a scratch file first - visudo -c on a bad /etc/sudoers.d file
# can lock out sudo entirely.
install -m 0440 "$STAGED_SUDOERS" /tmp/deploy-swap-app-db.sudoers.check
if ! visudo -cf /tmp/deploy-swap-app-db.sudoers.check >/dev/null; then
  rm -f /tmp/deploy-swap-app-db.sudoers.check
  echo "deploy-swap-app-db.sudoers failed visudo -c - not installed" >&2
  exit 1
fi
install -o root -g root -m 0440 /tmp/deploy-swap-app-db.sudoers.check /etc/sudoers.d/deploy-swap-app-db
rm -f /tmp/deploy-swap-app-db.sudoers.check "$STAGED_SCRIPT" "$STAGED_SUDOERS"

echo "==> done. Run api/deploy/deploy.sh from the Mac next to ship code and start the service."
