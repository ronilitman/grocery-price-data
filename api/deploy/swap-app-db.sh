#!/usr/bin/env bash
# Swap in a freshly pushed app.db, atomically and safely (KAN-11).
#
# Runs as root via the sudoers drop-in (api/deploy/grocery-swap-app-db.sudoers)
# that lets the 'grocery' user execute exactly this script with NOPASSWD.
# Nightly CI scp's app.db.zst + app.db.json into /srv/grocery/data/incoming/
# over Tailscale SSH as user grocery, then runs:
#   ssh grocery@grocery-api sudo /srv/grocery/bin/swap-app-db.sh
#
# The script itself is root-owned and not writable by grocery (see install.sh)
# so the sudoers grant cannot be used to run anything but this exact,
# reviewed file.
#
# Every decision is logged to the journal (`journalctl -t swap-app-db`) so a
# 2am failure is diagnosable without SSHing in with a debugger. On any
# failure *before* the swap, live.db is left completely untouched. On a
# failure to come back up *after* swapping (the health poll times out), this
# rolls back to prev.db and restarts again.

set -euo pipefail

DATA_DIR=/srv/grocery/data
INCOMING_DIR="$DATA_DIR/incoming"
COMPRESSED="$INCOMING_DIR/app.db.zst"
MANIFEST="$INCOMING_DIR/app.db.json"
LIVE_DB="$DATA_DIR/live.db"
PREV_DB="$DATA_DIR/prev.db"
NEXT_DB="$DATA_DIR/next.db"
LIVE_SHA="$DATA_DIR/live.sha256"
LOCK_FILE="$DATA_DIR/.swap-app-db.lock"
SERVICE=grocery-api
HEALTH_URL=http://127.0.0.1:8000/health
HEALTH_TIMEOUT_SECS=60
MIN_PRODUCTS=200000

log() {
    logger -t swap-app-db -- "$*"
    echo "swap-app-db: $*"
}

fail() {
    log "FAIL: $*"
    exit 1
}

cleanup_incoming() {
    rm -f "$COMPRESSED" "$MANIFEST"
}

# One swap at a time - a manual test run and a nightly CI run should never
# race each other over live.db/prev.db/next.db.
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another swap-app-db run is already in progress"

if [ "$(id -u)" -ne 0 ]; then
    fail "must run as root (via sudo)"
fi

if [ ! -f "$COMPRESSED" ] || [ ! -f "$MANIFEST" ]; then
    fail "missing incoming files: expected $COMPRESSED and $MANIFEST"
fi

manifest_sha=$(jq -er '.sha256' "$MANIFEST" 2>/dev/null) || fail "manifest missing/invalid sha256: $MANIFEST"
manifest_built_at=$(jq -er '.built_at' "$MANIFEST" 2>/dev/null) || fail "manifest missing/invalid built_at: $MANIFEST"
log "manifest: built_at=$manifest_built_at sha256=$manifest_sha"

# --- step 1: already current? ------------------------------------------------
if [ -f "$LIVE_SHA" ] && [ "$(cat "$LIVE_SHA")" = "$manifest_sha" ]; then
    log "already current (sha256 $manifest_sha matches live.db) - nothing to do"
    cleanup_incoming
    exit 0
fi

# --- step 2: decompress + verify ---------------------------------------------
rm -f "$NEXT_DB"

# --long matches the --long used to compress (both default to a 27-bit
# window) - without it zstd refuses to decode a long-distance-matched frame.
if ! zstd -d --long -q -f -o "$NEXT_DB" "$COMPRESSED" 2>/dev/null; then
    rm -f "$NEXT_DB"
    fail "decompress failed: $COMPRESSED"
fi

next_sha=$(sha256sum "$NEXT_DB" | awk '{print $1}')
if [ "$next_sha" != "$manifest_sha" ]; then
    rm -f "$NEXT_DB"
    fail "sha256 mismatch: manifest says $manifest_sha, decompressed file is $next_sha"
fi
log "sha256 verified: $next_sha"

quick_check=$(sqlite3 "$NEXT_DB" "PRAGMA quick_check;" 2>&1) || {
    rm -f "$NEXT_DB"
    fail "quick_check errored: $quick_check"
}
if [ "$quick_check" != "ok" ]; then
    rm -f "$NEXT_DB"
    fail "quick_check failed: $quick_check"
fi
log "quick_check: ok"

product_count=$(sqlite3 "$NEXT_DB" "SELECT COUNT(*) FROM products;" 2>&1) || {
    rm -f "$NEXT_DB"
    fail "could not count products: $product_count"
}
if ! [ "$product_count" -gt "$MIN_PRODUCTS" ] 2>/dev/null; then
    rm -f "$NEXT_DB"
    fail "products count too low: '$product_count' (need > $MIN_PRODUCTS)"
fi
log "products: $product_count"

next_built_at=$(sqlite3 "$NEXT_DB" "SELECT value FROM meta WHERE key='built_at';" 2>&1) || {
    rm -f "$NEXT_DB"
    fail "could not read meta.built_at from next.db: $next_built_at"
}
if [ -z "$next_built_at" ]; then
    rm -f "$NEXT_DB"
    fail "next.db has no meta.built_at"
fi

live_built_at=""
if [ -f "$LIVE_DB" ]; then
    live_built_at=$(sqlite3 "$LIVE_DB" "SELECT value FROM meta WHERE key='built_at';" 2>/dev/null || echo "")
    # ISO-8601 timestamps (all '...+00:00', see api/README.md) compare
    # correctly as plain strings - no date parsing needed.
    if [ -n "$live_built_at" ] && [[ ! "$next_built_at" > "$live_built_at" ]]; then
        rm -f "$NEXT_DB"
        fail "next.db built_at ($next_built_at) is not newer than live.db's ($live_built_at)"
    fi
fi
log "built_at: $next_built_at (live was: ${live_built_at:-none})"

# --- step 3: swap -------------------------------------------------------------
if [ -f "$LIVE_DB" ]; then
    mv -f "$LIVE_DB" "$PREV_DB"
fi
mv -f "$NEXT_DB" "$LIVE_DB"
echo "$manifest_sha" > "$LIVE_SHA"
chown grocery:grocery "$LIVE_DB" "$LIVE_SHA"
[ -f "$PREV_DB" ] && chown grocery:grocery "$PREV_DB"

log "swapped in app.db built_at=$next_built_at sha256=$manifest_sha - restarting $SERVICE"
systemctl restart "$SERVICE"

deadline=$((SECONDS + HEALTH_TIMEOUT_SECS))
health_built_at=""
until [ "$SECONDS" -ge "$deadline" ]; do
    body=$(curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null || true)
    if [ -n "$body" ]; then
        health_built_at=$(echo "$body" | jq -r '.built_at' 2>/dev/null || echo "")
        if [ "$health_built_at" = "$next_built_at" ]; then
            log "swap complete and verified: /health built_at=$health_built_at"
            cleanup_incoming
            exit 0
        fi
    fi
    sleep 2
done

# The new live.db did not come up healthy within the deadline - roll back.
log "FAIL: /health did not report built_at=$next_built_at within ${HEALTH_TIMEOUT_SECS}s (last seen: '${health_built_at}') - rolling back"
if [ -f "$PREV_DB" ]; then
    mv -f "$PREV_DB" "$LIVE_DB"
    # live.sha256 must not keep claiming the rolled-back build is current,
    # or a retry of the same manifest would wrongly take the "already
    # current" shortcut in step 1.
    rm -f "$LIVE_SHA"
    chown grocery:grocery "$LIVE_DB"
    systemctl restart "$SERVICE"
fi
cleanup_incoming
fail "rolled back to prev.db after health check timeout"
