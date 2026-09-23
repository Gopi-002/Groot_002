"""Read-only status API against real PostgreSQL/Redis."""

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.health import ReadinessChecks
from app.api.main import create_app
from app.persistence.outbox import publish_pending
from app.persistence.streams import StreamNames, ensure_group
from tests.integration.helpers import open_task

pytestmark = pytest.mark.integration

TOKEN = "r" * 48
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(engine, rclient, settings):
    s = settings.model_copy(update={"api_read_token": SecretStr(TOKEN)})
    app = create_app(s, ReadinessChecks({}), engine=engine, redis_client=rclient)
    return TestClient(app)


def test_incident_and_task_readable_with_token(api, engine):
    iid, tid = open_task(engine)
    lst = api.get("/v1/incidents", params={"status": "open"}, headers=AUTH)
    assert lst.status_code == 200 and str(iid) in [i["id"] for i in lst.json()["items"]]
    detail = api.get(f"/v1/incidents/{iid}", headers=AUTH).json()
    assert detail["incident"]["incident_type"] == "http_error"
    assert [t["id"] for t in detail["tasks"]] == [str(tid)]
    assert detail["evidence"][0]["content"]["rule"]["threshold"] == 3
    task = api.get(f"/v1/tasks/{tid}", headers=AUTH).json()
    assert task["task"]["status"] == "queued" and task["checkpoints"] == []
    assert "lease_owner" not in task["task"]


def test_unauthenticated_gets_nothing(api, engine):
    iid, _ = open_task(engine)
    r = api.get(f"/v1/incidents/{iid}")
    assert r.status_code == 401 and str(iid) not in r.text


def test_not_found_and_validation(api):
    assert api.get(f"/v1/incidents/{uuid.uuid4()}", headers=AUTH).status_code == 404
    assert api.get(f"/v1/tasks/{uuid.uuid4()}", headers=AUTH).status_code == 404
    assert api.get("/v1/incidents", params={"status": "bogus"}, headers=AUTH).status_code == 422
    assert api.get("/v1/incidents", params={"limit": 1000}, headers=AUTH).status_code == 422


def test_metrics_report_checks_incidents_and_queue_lag(api, engine, rclient, settings):
    open_task(engine)
    body = api.get("/v1/metrics", headers=AUTH).text
    assert 'sentinel_incidents_detected_total{incident_type="http_error"}' in body
    assert 'sentinel_health_checks_retained{outcome="unhealthy"}' in body
    assert "sentinel_outbox_unpublished" in body and "sentinel_redis_up 1" in body
    names = StreamNames.from_prefix(settings.stream_prefix)
    ensure_group(rclient, names)
    publish_pending(engine, rclient, names)
    body = api.get("/v1/metrics", headers=AUTH).text
    lag = next(line for line in body.splitlines() if line.startswith("sentinel_queue_lag "))
    assert float(lag.split()[1]) >= 1  # published but not yet consumed
    for name in (
        "sentinel_task_retries_total",
        "sentinel_tasks_dead_lettered_total",
        "sentinel_last_check_age_seconds",
        "sentinel_queue_pending",
    ):
        assert f"# TYPE {name}" in body
