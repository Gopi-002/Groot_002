import logging
import time
import tracemalloc

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from demo_application.main import DemoSettings, _client_allowed, create_demo_app

TOKEN = "demo-token-0123456789abcdef"


@pytest.fixture
def demo_settings(base_env) -> DemoSettings:
    return DemoSettings(
        env="demo", failure_injection_enabled=True, injection_token=TOKEN, timeout_delay_seconds=0.3
    )


@pytest.fixture
def client(demo_settings) -> TestClient:
    return TestClient(create_demo_app(demo_settings))


def _inject(client, mode, **kw):
    return client.post(
        "/simulate-failure", json={"mode": mode, **kw}, headers={"X-Demo-Token": TOKEN}
    )


def test_healthy_by_default(client):
    assert client.get("/health").status_code == 200
    m = client.get("/metrics").json()
    assert m["failure_mode"] == "none" and m["failure_injection_available"] is True


def test_http_500_mode(client):
    assert _inject(client, "http_500").status_code == 200
    r = client.get("/health")
    assert r.status_code == 500
    assert client.get("/metrics").json()["health_failures_total"] == 1


def test_timeout_mode_delays(client):
    _inject(client, "timeout")
    start = time.perf_counter()
    assert client.get("/health").status_code == 200
    assert time.perf_counter() - start >= 0.3


def test_memory_log_mode_logs_without_allocating(client, caplog):
    _inject(client, "memory_log")
    tracemalloc.start()
    with caplog.at_level(logging.WARNING, logger="demo_application"):
        assert client.get("/health").status_code == 200
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert any("SIMULATED memory pressure" in r.getMessage() for r in caplog.records)
    assert peak < 20 * 1024 * 1024  # nowhere near the reported 544MB
    assert client.get("/metrics").json()["simulated_memory_mb"] == 544.0


def test_reset_to_none(client):
    _inject(client, "http_500")
    _inject(client, "none")
    assert client.get("/health").status_code == 200


def test_mode_expires_after_duration(client):
    _inject(client, "http_500", duration_seconds=1)
    assert client.get("/health").status_code == 500
    time.sleep(1.1)
    assert client.get("/health").status_code == 200


def test_invalid_mode_and_duration_rejected(client):
    assert _inject(client, "rm_rf").status_code == 422
    assert _inject(client, "http_500", duration_seconds=100000).status_code == 422


def test_requires_token(client):
    assert client.post("/simulate-failure", json={"mode": "http_500"}).status_code == 401
    bad = client.post(
        "/simulate-failure",
        json={"mode": "http_500"},
        headers={"X-Demo-Token": "wrong-token-000000000"},
    )
    assert bad.status_code == 401
    assert client.get("/health").status_code == 200


def test_public_client_rejected(demo_settings):
    c = TestClient(create_demo_app(demo_settings), client=("8.8.8.8", 5555))
    assert _inject(c, "http_500").status_code == 403


@pytest.mark.parametrize(
    ("host", "ok"),
    [("127.0.0.1", True), ("172.18.0.5", True), ("::1", True), ("8.8.8.8", False), (None, False)],
)
def test_client_allowlist(host, ok):
    assert _client_allowed(host) is ok


def test_production_config_has_no_failure_endpoint(base_env):
    app = create_demo_app(DemoSettings(env="production"))
    c = TestClient(app)
    assert _inject(c, "http_500").status_code == 404
    assert c.get("/metrics").json()["failure_injection_available"] is False
    assert not any(getattr(r, "path", "") == "/simulate-failure" for r in app.routes)


def test_default_settings_are_safe(base_env):
    s = DemoSettings()
    assert s.env == "production" and s.injection_active is False


def test_injection_cannot_be_enabled_in_production(base_env):
    with pytest.raises(ValidationError, match="production"):
        DemoSettings(env="production", failure_injection_enabled=True, injection_token=TOKEN)


def test_injection_requires_strong_token(base_env):
    with pytest.raises(ValidationError, match="TOKEN"):
        DemoSettings(env="demo", failure_injection_enabled=True, injection_token="short")


def test_demo_instances_isolated(demo_settings):
    a, b = TestClient(create_demo_app(demo_settings)), TestClient(create_demo_app(demo_settings))
    _inject(a, "http_500")
    assert a.get("/health").status_code == 500 and b.get("/health").status_code == 200
