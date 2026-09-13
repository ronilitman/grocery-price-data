#!/usr/bin/env bash
# Idempotent VM setup for the grocery-price-data catalogue API.
#
# Installs Caddy (TLS via sslip.io + Let's Encrypt), python3-venv, sqlite3;
# creates the `grocery` system user and /srv/grocery/{app,venv,data};
# clones (or updates) the repo checkout the service runs from; builds the
# venv; installs the systemd unit and Caddyfile; and (re)starts both
# services. Safe to re-run.
#
# Usage: sudo GROCERY_BRANCH=<branch> ./install.sh
# GROCERY_BRANCH defaults to main. Production tracks main once KAN-10 is
# merged - see api/README.md for why this subtask deploys a feature branch
# instead.

set -euo pipefail

REPO_URL="${GROCERY_REPO_URL:-https://github.com/ronilitman/grocery-price-data.git}"
BRANCH="${GROCERY_BRANCH:-main}"
APP_DIR=/srv/grocery/app
VENV_DIR=/srv/grocery/venv
DATA_DIR=/srv/grocery/data

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo)" >&2
  exit 1
fi

echo "==> Caddy (official apt repo)"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    -o /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
else
  echo "    already installed: $(caddy version)"
fi

echo "==> python3-venv, sqlite3, git"
apt-get install -y -qq python3-venv sqlite3 git

echo "==> system user 'grocery'"
if ! id -u grocery >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /srv/grocery --shell /usr/sbin/nologin grocery
else
  echo "    already exists"
fi

mkdir -p "$APP_DIR" "$DATA_DIR"
chown -R grocery:grocery /srv/grocery

echo "==> checkout $REPO_URL @ $BRANCH -> $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  sudo -u grocery git -C "$APP_DIR" fetch origin "$BRANCH"
  sudo -u grocery git -C "$APP_DIR" checkout "$BRANCH"
  sudo -u grocery git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  sudo -u grocery git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> venv + pip install"
if [ ! -d "$VENV_DIR" ]; then
  sudo -u grocery python3 -m venv "$VENV_DIR"
fi
sudo -u grocery "$VENV_DIR/bin/pip" install --upgrade pip -q
sudo -u grocery "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/api/requirements.txt"

echo "==> systemd unit"
install -m 644 "$APP_DIR/api/deploy/grocery-api.service" /etc/systemd/system/grocery-api.service
systemctl daemon-reload
systemctl enable grocery-api
systemctl restart grocery-api

echo "==> Caddyfile"
install -m 644 "$APP_DIR/api/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl enable caddy
systemctl reload caddy 2>/dev/null || systemctl restart caddy

echo "==> done. grocery-api: $(systemctl is-active grocery-api), caddy: $(systemctl is-active caddy)"
