"""Approval HTTP API (authn, roles, CSRF defences, replay) and worker-level
duplicate delivery, against real PostgreSQL/Redis."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from app.agent.investigation import InvestigationStage
from app.agent.mock_gateway import DeterministicMockGateway
from app.agent.pipeline import TaskPipeline
from app.agent.worker import Worker
from app.api.health import ReadinessChecks
from app.api.main import create_app
from app.persistence.outbox import publish_pending, schedule_policy_tasks
from app.persistence.streams import StreamNames, ensure_group
from app.safety import approvals
from app.safety.remediation import RemediationStage
from tests.integration.helpers import one, rows
from tests.integration.test_investigation import ops_client as investigation_ops
from tests.integration.test_remediation import (  # noqa: F401 - fixtures
    APPROVAL_KEY,
    FakeTime,
    investigated,
    isolate,
    lease_for,
    ops,
    s,
    stage,
    world,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def pending(engine, s, world):
    s2 = s.model_copy(update={"remediation_auto_enabled": False})
    _, client, _ = world
    _, tid, _ = investigated(engine, s2)
    stage(s2, client).run(engine, lease_for(engine, tid))
    a = rows(engine, "SELECT id, action_fingerprint FROM approvals WHERE task_id=:t", t=tid)[0]
    return a, tid, s2


@pytest.fixture
def api(engine, s):
    app = create_app(s, ReadinessChecks({}), engine=engine, redis_client=None)
    return TestClient(app)


def token(engine, role):
    import uuid

    with engine.begin() as conn:
        return approvals.create_operator(conn, f"{role}-{uuid.uuid4().hex[:6]}", role, "test")


def post(api, a, tok, decision="approve", fp=None, headers=None, raw=None):
    h = {"Authorization": f"Bearer {tok}", **(headers or {})}
    if raw is not None:
        return api.post(f"/v1/approvals/{a['id']}/{decision}", content=raw, headers=h)
    return api.post(
        f"/v1/approvals/{a['id']}/{decision}",
        json={"action_fingerprint": fp or a["action_fingerprint"], "reason": "test"},
        headers=h,
    )


def test_unauthenticated_and_forged_tokens_rejected(api, pending):
    a, _, _ = pending
    assert api.get("/v1/approvals").status_code == 401
    assert (
        api.post(
            f"/v1/approvals/{a['id']}/approve", json={"action_fingerprint": a["action_fingerprint"]}
        ).status_code
        == 401
    )
    assert post(api, a, "sop_forged-token").status_code == 401


def test_viewer_can_read_but_not_decide(engine, api, pending):
    a, _, _ = pending
    tok = token(engine, "viewer")
    listed = api.get("/v1/approvals", headers={"Authorization": f"Bearer {tok}"}).json()
    assert str(a["id"]) in [i["id"] for i in listed["items"]]
    assert post(api, a, tok).status_code == 403


def test_approver_decides_once_replay_conflicts(engine, api, pending):
    a, tid, _ = pending
    tok = token(engine, "approver")
    r = post(api, a, tok)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert post(api, a, tok).status_code == 409  # replay
    assert post(api, a, tok, decision="reject").status_code == 409
    row = rows(
        engine,
        "SELECT status, decided_by, decision_signature FROM approvals WHERE id=:i",
        i=a["id"],
    )[0]
    assert row["status"] == "approved" and row["decision_signature"]
    assert one(engine, "SELECT next_attempt_at IS NOT NULL FROM tasks WHERE id=:t", t=tid)


def test_wrong_fingerprint_conflicts(engine, api, pending):
    a, _, _ = pending
    r = post(api, a, token(engine, "approver"), fp="0" * 64)
    assert r.status_code == 409 and "fingerprint_mismatch" in r.text


def test_csrf_defences(engine, api, pending):
    a, _, _ = pending
    tok = token(engine, "approver")
    form = post(
        api,
        a,
        tok,
        raw=f"action_fingerprint={a['action_fingerprint']}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    assert post(api, a, tok, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert post(api, a, tok, headers={"Origin": "https://evil.example"}).status_code == 403
    assert one(engine, "SELECT status FROM approvals WHERE id=:i", i=a["id"]) == "pending"
    assert post(api, a, tok, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200


def test_disabled_operator_rejected(engine, api, pending):
    a, _, _ = pending
    import uuid

    name = f"gone-{uuid.uuid4().hex[:6]}"
    with engine.begin() as conn:
        tok = approvals.create_operator(conn, name, "approver", "test")
        approvals.disable_operator(conn, name, "test")
    assert post(api, a, tok).status_code == 401


def test_signing_key_missing_fails_closed(engine, pending):
    a, _, s2 = pending
    app = create_app(
        s2.model_copy(update={"approval_signing_key": None}),
        ReadinessChecks({}),
        engine=engine,
        redis_client=None,
    )
    r = post(TestClient(app), a, token(engine, "approver"))
    assert r.status_code == 503
    assert one(engine, "SELECT status FROM approvals WHERE id=:i", i=a["id"]) == "pending"


def test_incident_detail_exposes_policy_trail(engine, api, s, pending):
    _, tid, _ = pending
    iid = one(engine, "SELECT incident_id FROM tasks WHERE id=:t", t=tid)
    read = s.api_read_token
    app = create_app(
        s.model_copy(update={"api_read_token": SecretStr("r" * 40)}),
        ReadinessChecks({}),
        engine=engine,
        redis_client=None,
    )
    body = (
        TestClient(app)
        .get(f"/v1/incidents/{iid}", headers={"Authorization": "Bearer " + "r" * 40})
        .json()
    )
    assert body["policy_decisions"][0]["decision"] == "REQUIRE_APPROVAL"
    assert body["approvals"][0]["status"] == "pending" and read is None


def test_duplicate_stream_delivery_never_restarts_twice(engine, rclient, settings, s, world):
    docker, client, _ = world
    cfg = settings.model_copy(
        update={
            "ai_gateway": "mock",
            "remediation_auto_enabled": True,
            "remediation_environment": "isolated-demo",
            "executor_token": s.executor_token,
            "action_signing_key": s.action_signing_key,
            "approval_signing_key": s.approval_signing_key,
            "verify_readiness_deadline_seconds": 10,
            "verify_probe_interval_seconds": 1,
        }
    )
    _, tid, _ = investigated(engine, cfg)
    ft = FakeTime()
    pipe = TaskPipeline(
        InvestigationStage(cfg, lambda st, m: DeterministicMockGateway(), investigation_ops()),
        RemediationStage(cfg, client, ops(), clock=ft.clock, wait=ft.wait),
    )
    w = Worker(cfg, engine, rclient, stage=pipe)
    names = StreamNames.from_prefix(cfg.stream_prefix)
    ensure_group(rclient, names)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET published_at=now() "
                "WHERE published_at IS NULL AND aggregate_id <> :t"
            ),
            {"t": tid},
        )
    schedule_policy_tasks(engine)
    publish_pending(engine, rclient, names)
    for _ in range(3):  # duplicate deliveries of the same task
        rclient.xadd(names.tasks, {"task_id": str(tid)})
    while w.poll_once(block_ms=100):
        pass
    assert one(engine, "SELECT status FROM tasks WHERE id=:t", t=tid) == "resolved"
    assert docker.restarts == 1 and w.counters["duplicates"] >= 3
    assert rclient.xpending(names.tasks, names.group)["pending"] == 0
