#!/usr/bin/env bash
# Deploy code to the grocery-api VM by copying it from the Mac.
#
# The VM has no public IPv4 (see api/README.md) so it cannot reach GitHub -
# `git clone`/`git pull` don't work there. Instead this tars api/ and
# scripts/ from the local checkout, copies the tarball up over the IAP
# tunnel, unpacks it into /srv/grocery/app, installs api/requirements.txt
# into the venv, and restarts the service. Safe to re-run - every step is
# idempotent (the tarball is unpacked over the previous copy, pip install is
# a no-op when nothing changed, and `systemctl restart` just restarts).
#
# Run this from the Mac, from the repo root (or the worktree root):
#   api/deploy/deploy.sh
#
# The one-time VM setup (packages, the `grocery` user, /srv/grocery dirs,
# the systemd unit) is api/deploy/install.sh, over the same IAP tunnel.

set -euo pipefail

PROJECT="${GROCERY_PROJECT:-gen-lang-client-0902689301}"
ZONE="${GROCERY_ZONE:-us-central1-a}"
INSTANCE="${GROCERY_INSTANCE:-grocery-api}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_TAR="$(mktemp /tmp/grocery-deploy-XXXXXX.tar.gz)"
trap 'rm -f "$TMP_TAR"' EXIT

echo "==> packing api/ and scripts/ from $REPO_ROOT"
# COPYFILE_DISABLE keeps macOS's tar from writing AppleDouble/xattr entries
# that GNU tar on the VM only warns about and ignores.
COPYFILE_DISABLE=1 tar -C "$REPO_ROOT" -czf "$TMP_TAR" \
  --exclude='__pycache__' --exclude='*.pyc' \
  api scripts

echo "==> copying to the VM over IAP"
gcloud compute scp "$TMP_TAR" "$INSTANCE:/tmp/grocery-deploy.tar.gz" \
  --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap

echo "==> unpacking, installing requirements, restarting the service"
gcloud compute ssh "$INSTANCE" \
  --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
  --command "
    set -euo pipefail
    sudo -u grocery mkdir -p /srv/grocery/app
    # scp lands the tarball owned by the SSH user, mode 0600 - unreadable to
    # 'grocery'. chmod it before switching user.
    sudo chmod 644 /tmp/grocery-deploy.tar.gz
    sudo -u grocery tar -xzf /tmp/grocery-deploy.tar.gz -C /srv/grocery/app
    sudo rm -f /tmp/grocery-deploy.tar.gz
    sudo -u grocery /srv/grocery/venv/bin/pip install -q --upgrade pip
    sudo -u grocery /srv/grocery/venv/bin/pip install -q -r /srv/grocery/app/api/requirements.txt
    sudo install -m 644 /srv/grocery/app/api/deploy/grocery-api.service /etc/systemd/system/grocery-api.service
    sudo systemctl daemon-reload
    sudo systemctl enable grocery-api
    sudo systemctl restart grocery-api
    sleep 1
    echo grocery-api: \$(systemctl is-active grocery-api)
  "

echo "==> done"
