import math
import time

from fastapi.testclient import TestClient

from api.main import RATE_LIMIT_BURST, RATE_LIMIT_PER_SECOND, app


def test_burst_past_the_limit_gets_429s(monkeypatch, fixture_db):
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = TestClient(app)

    started = time.monotonic()
    statuses = [client.get("/health").status_code for _ in range(int(RATE_LIMIT_BURST) + 20)]
    elapsed = time.monotonic() - started

    # The bucket keeps refilling while the burst runs, so a slower machine
    # legitimately lets a few more through than the burst size. Allowing for
    # exactly that refill keeps the test honest without making it flaky: it
    # still fails if the limiter lets the whole burst through.
    allowed = RATE_LIMIT_BURST + math.ceil(RATE_LIMIT_PER_SECOND * elapsed)
    assert statuses.count(200) <= allowed
    assert 429 in statuses


def _client_with_peer(peer_ip: str) -> TestClient:
    """A TestClient whose ASGI-visible TCP peer is `peer_ip`.

    FastAPI's own TestClient hardcodes the peer to the literal "testclient"
    with no override hook, so these two tests - which need to control
    request.client.host to exercise the loopback-vs-not branch in
    client_ip_for() - wrap the app in a thin ASGI shim that rewrites
    scope["client"] before every request reaches it.
    """

    async def app_with_forced_peer(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(peer_ip, 12345))
        await app(scope, receive, send)

    return TestClient(app_with_forced_peer)


def test_x_forwarded_for_trusted_when_peer_is_loopback(monkeypatch, fixture_db):
    """Behind Funnel every request's TCP peer is 127.0.0.1; X-Forwarded-For
    must be trusted there so each real client gets its own bucket."""
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = _client_with_peer("127.0.0.1")
    statuses_a = [
        client.get("/health", headers={"X-Forwarded-For": "100.64.1.1"}).status_code
        for _ in range(int(RATE_LIMIT_BURST) + 20)
    ]
    # A different forwarded client shares the loopback peer but must get
    # its own bucket, so it is unaffected by client A's burst above.
    status_b = client.get("/health", headers={"X-Forwarded-For": "100.64.1.2"}).status_code

    assert 429 in statuses_a
    assert status_b == 200


def test_x_forwarded_for_ignored_when_peer_is_not_loopback(monkeypatch, fixture_db):
    """A direct (non-Funnel) caller cannot use X-Forwarded-For to dodge the
    limit by claiming a fresh IP on every request - the header is only
    trusted when it could only have been added by Funnel on the box itself."""
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = _client_with_peer("203.0.113.9")
    statuses_a = [
        client.get("/health", headers={"X-Forwarded-For": "10.2.2.1"}).status_code
        for _ in range(int(RATE_LIMIT_BURST) + 20)
    ]
    # Same TCP peer, different spoofed header - still the same bucket,
    # because the header is ignored for a non-loopback peer.
    status_b = client.get("/health", headers={"X-Forwarded-For": "10.2.2.2"}).status_code

    assert 429 in statuses_a
    assert status_b == 429


def test_forged_leftmost_forwarded_entry_does_not_dodge_the_limit(monkeypatch, fixture_db):
    """If a proxy appends to a client-supplied X-Forwarded-For, the leftmost
    entry is attacker-controlled. Rotating it on every request must not give
    each request a fresh bucket - only the rightmost, proxy-added entry counts."""
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = _client_with_peer("127.0.0.1")
    statuses = [
        client.get(
            "/health",
            headers={"X-Forwarded-For": f"10.9.0.{i % 250}, 100.64.1.1"},
        ).status_code
        for i in range(int(RATE_LIMIT_BURST) + 20)
    ]

    assert 429 in statuses
