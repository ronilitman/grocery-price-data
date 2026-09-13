from fastapi.testclient import TestClient

from api.main import RATE_LIMIT_BURST, app


def test_burst_past_the_limit_gets_429s(monkeypatch, fixture_db):
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = TestClient(app)

    statuses = [client.get("/health").status_code for _ in range(int(RATE_LIMIT_BURST) + 20)]

    assert statuses.count(200) <= RATE_LIMIT_BURST
    assert 429 in statuses
