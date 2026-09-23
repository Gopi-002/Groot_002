import pytest
from fastapi.testclient import TestClient

from app.api.health import ReadinessChecks
from app.api.main import create_app
from app.config import Settings
from app.observability.metrics import Metric, render

TOKEN = "t" * 40
PATHS = [
    "/v1/incidents",
    "/v1/incidents/0b6c3a9e-2c1c-4e7a-9a0e-8a7f7f6d1b2c",
    "/v1/tasks/0b6c3a9e-2c1c-4e7a-9a0e-8a7f7f6d1b2c",
    "/v1/metrics",
]


class ExplodingEngine:
    """Proves auth runs before any data access."""

    def connect(self):
        raise AssertionError("database touched before authentication")


def client(token: str | None) -> TestClient:
    s = Settings(api_read_token=token) if token else Settings()
    app = create_app(s, ReadinessChecks({}), engine=ExplodingEngine(), redis_client=None)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", PATHS)
def test_fails_closed_when_token_unconfigured(base_env, path):
    r = client(None).get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("header", [None, "Bearer wrong-token", f"Basic {TOKEN}", TOKEN])
def test_rejects_missing_or_wrong_token(base_env, path, header):
    headers = {"Authorization": header} if header else {}
    r = client(TOKEN).get(path, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in r.text


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_write_methods(base_env, method):
    r = getattr(client(TOKEN), method)(
        "/v1/incidents", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert r.status_code in (401, 405)


def test_invalid_uuid_rejected_after_auth(base_env):
    r = client(TOKEN).get("/v1/tasks/not-a-uuid", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 422


def test_metrics_render_prometheus_format():
    out = render(
        [
            Metric("x_total", "counter", "help x", [({"a": "b"}, 3.0)]),
            Metric("y", "gauge", "help y"),
        ]
    )
    assert '# TYPE x_total counter\nx_total{a="b"} 3\n' in out
    assert out.endswith("y 0\n")
