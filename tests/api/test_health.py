from fastapi.testclient import TestClient

from api.main import app
from conftest import FIXTURE_BUILT_AT, FIXTURE_CHAIN_AS_OF, FIXTURE_PRODUCTS


def test_health_ok(monkeypatch, fixture_db):
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = TestClient(app)

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "built_at": FIXTURE_BUILT_AT,
        "chain_as_of": FIXTURE_CHAIN_AS_OF,
        "products": len(FIXTURE_PRODUCTS),
    }


def test_health_missing_db_returns_503(monkeypatch, tmp_path):
    missing = tmp_path / "no-such-file.db"
    monkeypatch.setenv("APP_DB", str(missing))
    client = TestClient(app)

    resp = client.get("/health")

    assert resp.status_code == 503
    assert str(missing) in resp.json()["error"]


def test_health_ignores_v_query_param(monkeypatch, fixture_db):
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = TestClient(app)

    resp = client.get("/health", params={"v": "some-build-stamp"})

    assert resp.status_code == 200
    assert resp.json()["built_at"] == FIXTURE_BUILT_AT


def test_health_sets_cache_control(monkeypatch, fixture_db):
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = TestClient(app)

    resp = client.get("/health")

    assert resp.headers["cache-control"] == "public, max-age=86400"


def test_cors_header_present_for_allowed_origin(monkeypatch, fixture_db):
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = TestClient(app)

    resp = client.get(
        "/health", headers={"Origin": "https://gen-lang-client-0902689301.web.app"}
    )

    assert (
        resp.headers.get("access-control-allow-origin")
        == "https://gen-lang-client-0902689301.web.app"
    )


def test_cors_header_absent_for_disallowed_origin(monkeypatch, fixture_db):
    monkeypatch.setenv("APP_DB", str(fixture_db))
    client = TestClient(app)

    resp = client.get("/health", headers={"Origin": "https://example.com"})

    assert "access-control-allow-origin" not in resp.headers
