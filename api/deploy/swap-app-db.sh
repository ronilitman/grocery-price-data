#!/usr/bin/env bash
# Swap in a freshly pushed app.db, atomically and safely (KAN-11).
#
# Runs as root via the sudoers drop-in (api/deploy/deploy-swap-app-db.sudoers)
# that lets the dedicated 'deploy' account execute exactly this script with
# NOPASSWD. Nightly CI scp's app.db.zst + app.db.json into
# /srv/grocery/incoming/ over Tailscale SSH as user 'deploy', then runs:
#   ssh deploy@grocery-api.tail1b4121.ts.net sudo /srv/grocery/bin/swap-app-db.sh
#
# 'deploy' (not 'grocery', the API's service account) lands the SSH session
# on purpose: 'grocery' has shell /usr/sbin/nologin and, more importantly,
# is what an API RCE would give an attacker - it must never be a path to
# root. 'deploy' owns nothing on this box but its own upload directory, and
# even that untrusted input is never read in place - see "quarantine"
# below. The script itself is root-owned and not writable by 'deploy' (see
# install.sh) so the sudoers grant cannot be used to run anything but this
# exact, reviewed file.
#
# Every decision is logged to the journal (`journalctl -t swap-app-db`) so a
# 2am failure is diagnosable without SSHing in with a debugger. On any
# failure *before* the swap, live.db is left completely untouched. On a
# failure to come back up *after* swapping (the health poll times out), this
# rolls back to prev.db and restarts again.

set -euo pipefail
umask 077

DATA_DIR=/srv/grocery/data
INCOMING_DIR=/srv/grocery/incoming        # deploy:deploy 0700 - untrusted input
WORK_DIR=/srv/grocery/.swap-work          # root:root 0700 - this run's quarantined copy
COMPRESSED="$WORK_DIR/app.db.zst"
MANIFEST="$WORK_DIR/app.db.json"
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

cleanup_work() {
    rm -rf "$WORK_DIR"
}

if [ "$(id -u)" -ne 0 ]; then
    fail "must run as root (via sudo)"
fi

# One swap at a time - a manual test run and a nightly CI run should never
# race each other over live.db/prev.db/next.db.
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another swap-app-db run is already in progress"
chmod 600 "$LOCK_FILE"

# --- quarantine: relocate before ever looking at the content ----------------
#
# INCOMING_DIR is owned by 'deploy', a lower-privilege account than root, so
# its files are untrusted input - 'deploy' could plant a symlink there (e.g.
# app.db.json -> /etc/shadow) hoping a root-run script reads or writes
# through it. `mv` (rename()) doesn't dereference its source - it moves the
# directory entry itself, symlink or not - so renaming into a directory
# 'deploy' cannot write to (WORK_DIR, root:root 0700) *before* any check
# closes the TOCTOU window a check-then-open would leave open: once moved,
# 'deploy' can no longer swap the underlying file out from under us, so the
# symlink test below is checking the thing we're about to use, not a
# snapshot of it. INCOMING_DIR is never read from again this run.
rm -rf "$WORK_DIR"
mkdir -m 0700 "$WORK_DIR"
if ! mv -T "$INCOMING_DIR/app.db.zst" "$COMPRESSED" 2>/dev/null \
   || ! mv -T "$INCOMING_DIR/app.db.json" "$MANIFEST" 2>/dev/null; then
    cleanup_work
    fail "missing incoming files: expected $INCOMING_DIR/app.db.zst and app.db.json"
fi

for f in "$COMPRESSED" "$MANIFEST"; do
    if [ -L "$f" ]; then
        cleanup_work
        fail "refusing symlink from incoming/: $f"
    fi
    if [ ! -f "$f" ]; then
        cleanup_work
        fail "not a regular file: $f"
    fi
done

manifest_sha=$(jq -er '.sha256' "$MANIFEST" 2>/dev/null) || { cleanup_work; fail "manifest missing/invalid sha256: $MANIFEST"; }
manifest_built_at=$(jq -er '.built_at' "$MANIFEST" 2>/dev/null) || { cleanup_work; fail "manifest missing/invalid built_at: $MANIFEST"; }
log "manifest: built_at=$manifest_built_at sha256=$manifest_sha"

# --- step 1: already current? ------------------------------------------------
if [ -f "$LIVE_SHA" ] && [ "$(cat "$LIVE_SHA")" = "$manifest_sha" ]; then
    log "already current (sha256 $manifest_sha matches live.db) - nothing to do"
    cleanup_work
    exit 0
fi

# --- step 2: decompress + verify ---------------------------------------------
rm -f "$NEXT_DB"

# --long matches the --long used to compress (both default to a 27-bit
# window) - without it zstd refuses to decode a long-distance-matched frame.
if ! zstd -d --long -q -f -o "$NEXT_DB" "$COMPRESSED" 2>/dev/null; then
    rm -f "$NEXT_DB"
    cleanup_work
    fail "decompress failed: $COMPRESSED"
fi

next_sha=$(sha256sum "$NEXT_DB" | awk '{print $1}')
if [ "$next_sha" != "$manifest_sha" ]; then
    rm -f "$NEXT_DB"
    cleanup_work
    fail "sha256 mismatch: manifest says $manifest_sha, decompressed file is $next_sha"
fi
log "sha256 verified: $next_sha"

quick_check=$(sqlite3 "$NEXT_DB" "PRAGMA quick_check;" 2>&1) || {
    rm -f "$NEXT_DB"
    cleanup_work
    fail "quick_check errored: $quick_check"
}
if [ "$quick_check" != "ok" ]; then
    rm -f "$NEXT_DB"
    cleanup_work
    fail "quick_check failed: $quick_check"
fi
log "quick_check: ok"

product_count=$(sqlite3 "$NEXT_DB" "SELECT COUNT(*) FROM products;" 2>&1) || {
    rm -f "$NEXT_DB"
    cleanup_work
    fail "could not count products: $product_count"
}
if ! [ "$product_count" -gt "$MIN_PRODUCTS" ] 2>/dev/null; then
    rm -f "$NEXT_DB"
    cleanup_work
    fail "products count too low: '$product_count' (need > $MIN_PRODUCTS)"
fi
log "products: $product_count"

next_built_at=$(sqlite3 "$NEXT_DB" "SELECT value FROM meta WHERE key='built_at';" 2>&1) || {
    rm -f "$NEXT_DB"
    cleanup_work
    fail "could not read meta.built_at from next.db: $next_built_at"
}
if [ -z "$next_built_at" ]; then
    rm -f "$NEXT_DB"
    cleanup_work
    fail "next.db has no meta.built_at"
fi

live_built_at=""
if [ -f "$LIVE_DB" ]; then
    live_built_at=$(sqlite3 "$LIVE_DB" "SELECT value FROM meta WHERE key='built_at';" 2>/dev/null || echo "")
    # ISO-8601 timestamps (all '...+00:00', see api/README.md) compare
    # correctly as plain strings - no date parsing needed.
    if [ -n "$live_built_at" ] && [[ ! "$next_built_at" > "$live_built_at" ]]; then
        rm -f "$NEXT_DB"
        cleanup_work
        fail "next.db built_at ($next_built_at) is not newer than live.db's ($live_built_at)"
    fi
fi
log "built_at: $next_built_at (live was: ${live_built_at:-none})"

# --- step 3: swap -------------------------------------------------------------
#
# root:grocery 0640 throughout - 'grocery' (the API) only ever needs to
# *read* these via a read-only sqlite connection; nothing running as
# 'grocery' has write access to $DATA_DIR at all, which is what closes off
# the escalation this whole rework exists to fix (a service-account
# compromise planting a symlink here for a root-run script to follow).
if [ -f "$LIVE_DB" ]; then
    mv -f "$LIVE_DB" "$PREV_DB"
    chown root:grocery "$PREV_DB"
    chmod 0640 "$PREV_DB"
fi
mv -f "$NEXT_DB" "$LIVE_DB"
chown root:grocery "$LIVE_DB"
chmod 0640 "$LIVE_DB"
echo "$manifest_sha" > "$LIVE_SHA"
chown root:root "$LIVE_SHA"
chmod 0600 "$LIVE_SHA"

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
            cleanup_work
            exit 0
        fi
    fi
    sleep 2
done

# The new live.db did not come up healthy within the deadline - roll back.
log "FAIL: /health did not report built_at=$next_built_at within ${HEALTH_TIMEOUT_SECS}s (last seen: '${health_built_at}') - rolling back"
if [ -f "$PREV_DB" ]; then
    mv -f "$PREV_DB" "$LIVE_DB"
    chown root:grocery "$LIVE_DB"
    chmod 0640 "$LIVE_DB"
    # live.sha256 must not keep claiming the rolled-back build is current,
    # or a retry of the same manifest would wrongly take the "already
    # current" shortcut in step 1.
    rm -f "$LIVE_SHA"
    systemctl restart "$SERVICE"
fi
cleanup_work
fail "rolled back to prev.db after health check timeout"
