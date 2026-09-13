from fastapi.testclient import TestClient

from api.main import RATE_LIMIT_BURST, app


def test_burst_past_the_limit_gets_429s(monkeypatch, fixture_db):
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = TestClient(app)

    statuses = [client.get("/health").status_code for _ in range(int(RATE_LIMIT_BURST) + 20)]

    assert statuses.count(200) <= RATE_LIMIT_BURST
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
