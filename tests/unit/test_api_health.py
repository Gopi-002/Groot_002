import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.health import ReadinessChecks
from app.api.main import create_app
from app.config import Settings


def _ok() -> None:
    return None


def _fail() -> None:
    raise ConnectionError("password=hunter2-should-not-leak")


def _client(checks: dict, env: str = "development") -> TestClient:
    s = Settings(environment=env)
    return TestClient(create_app(s, ReadinessChecks(checks)), raise_server_exceptions=False)


def test_live_and_ready_when_dependencies_ok(base_env):
    c = _client({"database": _ok, "redis": _ok})
    assert c.get("/health/live").json() == {"status": "alive"}
    r = c.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"] == {"database": "ok", "redis": "ok"}


def test_liveness_independent_of_dependencies(base_env):
    c = _client({"database": _fail, "redis": _ok})
    assert c.get("/health/live").status_code == 200
    r = c.get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "checks": {"database": "fail", "redis": "ok"}}
    assert "hunter2" not in r.text


def test_hung_dependency_bounded_by_deadline(base_env):
    gate = threading.Event()
    checks = ReadinessChecks({"database": lambda: gate.wait(10), "redis": _ok}, timeout_seconds=0.3)
    c = TestClient(create_app(Settings(), checks))
    start = time.perf_counter()
    r = c.get("/health/ready")
    elapsed = time.perf_counter() - start
    gate.set()
    assert r.status_code == 503 and r.json()["checks"] == {"database": "timeout", "redis": "ok"}
    assert elapsed < 2
    assert c.get("/health/live").status_code == 200


def test_error_envelope_for_unknown_route(base_env):
    r = _client({}).get("/incidents")
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "not_found" and body["request_id"] == r.headers["x-request-id"]


def test_unhandled_error_envelope_hides_details(base_env):
    app = create_app(Settings(), ReadinessChecks({}))

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("token=internal-secret-value")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "internal-secret-value" not in r.text


@pytest.mark.parametrize("path", ["/incidents", "/admin", "/tasks", "/approvals"])
def test_no_incident_or_admin_endpoints(base_env, path):
    assert _client({}).get(path).status_code == 404


def test_production_hides_docs_and_dashboard(base_env, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PASSWORD", "a-very-long-db-password-123")
    monkeypatch.setenv("SENTINEL_REDIS_PASSWORD", "a-very-long-redis-password-456")
    c = _client({}, env="production")
    for path in ("/docs", "/openapi.json", "/dashboard/"):
        assert c.get(path).status_code == 404


def test_dashboard_shell_marked_not_production_ready(base_env):
    r = _client({}).get("/dashboard/")
    assert r.status_code == 200 and "NOT PRODUCTION-READY" in r.text


def test_request_id_propagated_when_valid_uuid(base_env):
    rid = "0b6c3a9e-2c1c-4e7a-9a0e-8a7f7f6d1b2c"
    r = _client({}).get("/health/live", headers={"x-request-id": rid})
    assert r.headers["x-request-id"] == rid
    r2 = _client({}).get("/health/live", headers={"x-request-id": "<script>"})
    assert r2.headers["x-request-id"] != "<script>"
