"""FastAPI skeleton for the grocery-price-data catalogue API.

This is KAN-10: the VM, Tailscale Funnel (HTTPS) and a `/health` endpoint.
`app.db` is built by `scripts/build_app_db.py` (KAN-6). `GET /search` is
KAN-12 (`api/search.py`). `/meta`, `/product`, `/generic` and `POST
/products` are KAN-13 (`api/products.py`). `GET /categories` and `GET
/categories/{id}/products` are KAN-17 (`api/categories.py`). The nightly
build-and-swap (KAN-11) is a separate subtask and deliberately not built
here.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Overridable so tests (and any future ad-hoc check) can point at a fixture
# DB instead of the real one on the VM. Read lazily (not cached at import
# time) so tests can change it per-case via monkeypatch.setenv.
DEFAULT_DB_PATH = "/srv/grocery/data/live.db"


def db_path() -> str:
    return os.environ.get("APP_DB", DEFAULT_DB_PATH)

# grocery-list-app's origins (Firebase Hosting + its firebaseapp.com alias)
# plus the Vite dev servers. Nothing else - see KAN-10's spec.
ALLOWED_ORIGINS = [
    "https://gen-lang-client-0902689301.web.app",
    "https://gen-lang-client-0902689301.firebaseapp.com",
    "http://localhost:5173",
    # KAN-17's own dev server (Categories page), run on its own port per the
    # 2026-09-14 spec update so it doesn't collide with KAN-15's 5173/5175.
    "http://localhost:5174",
]

# Nothing caches this response today. Sending the header from day one is
# what lets a CDN be dropped in front later with no client change (see the
# design doc linked from KAN-4). Every endpoint also accepts and ignores a
# `v` query parameter - the client's build stamp - for the same reason.
CACHE_CONTROL = "public, max-age=86400"

RATE_LIMIT_PER_SECOND = 20.0
RATE_LIMIT_BURST = 40.0


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_connection() -> sqlite3.Connection:
    """Open the catalogue DB read-only.

    Raises sqlite3.OperationalError if the file is missing or unreadable;
    callers turn that into a 503 rather than a 500.
    """
    uri = f"file:{db_path()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only=1")
    return conn


# ---------------------------------------------------------------------------
# Rate limiting - a token bucket per client IP.
#
# There is no Cloudflare in front of this box (parked - see the KAN-4 design
# doc), so there is no free abuse protection either. This is intentionally
# simple: one process, one uvicorn worker (the box has 969 MB RAM), so an
# in-memory dict is enough for two users and whatever else finds the IP.
# ---------------------------------------------------------------------------


class _Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = tokens
        self.last = last


# Funnel proxies every request through the tailnet, so the TCP peer FastAPI
# sees is always 127.0.0.1 - keying the bucket on request.client.host would
# put every real client in one shared bucket. Funnel sets X-Forwarded-For to
# the actual client's tailnet IP, but that header is a client-supplied string
# to anything that can reach the box directly, so it is only trusted when the
# TCP peer is loopback - i.e. it can only have been added by something
# running on the VM itself.
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def client_ip_for(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in LOOPBACK_HOSTS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Rightmost entry, not leftmost: the leftmost is whatever the
            # caller sent, so behind a proxy that appends, every request could
            # claim a fresh address and dodge the limit. Funnel currently
            # replaces the header (a burst with forged values still hits 429),
            # and the rightmost entry is the real client either way.
            return forwarded.split(",")[-1].strip()
    return peer


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        rate: float = RATE_LIMIT_PER_SECOND,
        burst: float = RATE_LIMIT_BURST,
    ) -> None:
        super().__init__(app)
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def dispatch(self, request: Request, call_next):
        client_ip = client_ip_for(request)
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(client_ip)
            if bucket is None:
                bucket = _Bucket(self.burst, now)
                self._buckets[client_ip] = bucket
            else:
                elapsed = now - bucket.last
                bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
                bucket.last = now
            if bucket.tokens < 1:
                allowed = False
            else:
                bucket.tokens -= 1
                allowed = True
        if not allowed:
            return JSONResponse(status_code=429, content={"error": "rate limit exceeded"})
        return await call_next(request)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="grocery-price-data API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

from api.search import router as search_router  # noqa: E402  (KAN-12)
from api.products import router as products_router  # noqa: E402  (KAN-13)
from api.deals import router as deals_router  # noqa: E402  (KAN-15)
from api.categories import router as categories_router  # noqa: E402  (KAN-17)

app.include_router(search_router)
app.include_router(products_router)
app.include_router(deals_router)
app.include_router(categories_router)


@app.middleware("http")
async def add_cache_control(request: Request, call_next):
    response = await call_next(request)
    if response.status_code < 400:
        response.headers["Cache-Control"] = CACHE_CONTROL
    return response


@app.get("/health")
def health(v: Optional[str] = None):
    """Liveness + freshness check.

    Returns `built_at`, `chain_as_of` (chain -> age info, empty if none are
    stale) and a `products` count, read from the `meta` and `products`
    tables. 503 (not 500) when `live.db` is missing or unreadable, naming
    the file, so a monitoring check can tell "not deployed yet" from "the
    API crashed".
    """
    path = db_path()
    if not Path(path).exists():
        return JSONResponse(
            status_code=503,
            content={"error": f"database file missing: {path}"},
        )
    try:
        conn = get_connection()
        try:
            meta = dict(
                conn.execute(
                    "SELECT key, value FROM meta WHERE key IN ('built_at', 'chain_as_of')"
                ).fetchall()
            )
            built_at = meta.get("built_at")
            if built_at is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": f"meta table is empty: {path}"},
                )
            chain_as_of_raw = meta.get("chain_as_of")
            chain_as_of = json.loads(chain_as_of_raw) if chain_as_of_raw else {}
            (products,) = conn.execute("SELECT COUNT(*) FROM products").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": f"database unreadable: {path} ({exc})"},
        )
    return {"built_at": built_at, "chain_as_of": chain_as_of, "products": products}
