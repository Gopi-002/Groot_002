"""The real Worker (Redis Streams + leases) running the AI investigation stage."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.agent.gateway import ProviderUnavailable
from app.agent.investigation import InvestigationStage
from app.agent.mock_gateway import DeterministicMockGateway, ScriptedGateway
from app.agent.model_config import ModelSelection, save_selection
from app.agent.stages import IntakeStage
from app.agent.worker import Worker
from app.api.health import ReadinessChecks
from app.api.main import create_app
from app.persistence.outbox import (
    publish_pending,
    schedule_due_retries,
    schedule_parked_investigations,
)
from app.persistence.streams import StreamNames, ensure_group
from tests.integration.helpers import make_due, one, open_task, rows
from tests.integration.test_investigation import ops_client

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def quiesce(engine):
    with engine.begin() as conn:
        conn.execute(text("UPDATE outbox_events SET published_at=now() WHERE published_at IS NULL"))
        conn.execute(
            text(
                "UPDATE tasks SET status='failed', lease_owner=NULL, "
                "lease_expires_at=NULL WHERE status NOT IN "
                "('escalated','failed','resolved','dead_lettered')"
            )
        )
        save_selection(conn, ModelSelection("mock", "mock-investigator-v1"), "test")


def ai(settings):
    return settings.model_copy(update={"ai_gateway": "mock"})


def pump(engine, rclient, settings, worker, rounds=3):
    names = StreamNames.from_prefix(settings.stream_prefix)
    ensure_group(rclient, names)
    for _ in range(rounds):
        schedule_due_retries(engine)
        schedule_parked_investigations(engine)
        publish_pending(engine, rclient, names)
        worker.poll_once(block_ms=100)


def test_end_to_end_queue_to_investigation(engine, rclient, settings):
    _, tid = open_task(engine)
    gw = DeterministicMockGateway()
    stage = InvestigationStage(ai(settings), lambda s, m: gw, ops_client())
    w = Worker(ai(settings), engine, rclient, stage=stage)
    pump(engine, rclient, settings, w, rounds=1)
    assert one(engine, "SELECT status FROM tasks WHERE id=:t", t=tid) == "awaiting_policy"
    assert one(engine, "SELECT status FROM investigations WHERE task_id=:t", t=tid) == "completed"
    names = StreamNames.from_prefix(settings.stream_prefix)
    assert rclient.xpending(names.tasks, names.group)["pending"] == 0


def test_duplicate_message_after_completion_is_acked_without_rerun(engine, rclient, settings):
    _, tid = open_task(engine)
    gw = DeterministicMockGateway()
    w = Worker(
        ai(settings),
        engine,
        rclient,
        stage=InvestigationStage(ai(settings), lambda s, m: gw, ops_client()),
    )
    pump(engine, rclient, settings, w, rounds=1)
    calls = len(gw.requests)
    names = StreamNames.from_prefix(settings.stream_prefix)
    rclient.xadd(names.tasks, {"task_id": str(tid)})  # redelivered duplicate
    w.poll_once(block_ms=100)
    assert len(gw.requests) == calls and w.counters["duplicates"] == 1
    assert one(engine, "SELECT count(*) FROM investigations WHERE task_id=:t", t=tid) == 1


def test_provider_outage_bounded_retries_then_dead_letter(engine, rclient, settings):
    iid, tid = open_task(engine)
    gw = ScriptedGateway([ProviderUnavailable("529")] * 5)
    w = Worker(
        ai(settings),
        engine,
        rclient,
        stage=InvestigationStage(ai(settings), lambda s, m: gw, ops_client()),
    )
    names = StreamNames.from_prefix(settings.stream_prefix)
    ensure_group(rclient, names)
    publish_pending(engine, rclient, names)
    w.poll_once(block_ms=100)
    assert one(engine, "SELECT status FROM tasks WHERE id=:t", t=tid) == "retry_scheduled"
    for _ in range(3):
        make_due(engine, tid)
        pump(engine, rclient, settings, w, rounds=1)
    t = rows(engine, "SELECT status, attempt FROM tasks WHERE id=:t", t=tid)[0]
    assert (t["status"], t["attempt"]) == ("dead_lettered", 3)
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "escalated"
    assert len(gw.requests) == 3  # exactly max_attempts provider calls, no hammering


def test_phase2_parked_task_is_resumed_once_ai_configured(engine, rclient, settings):
    _, tid = open_task(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_config SET is_active=false"))
    phase2 = Worker(settings, engine, rclient, stage=IntakeStage())
    pump(engine, rclient, settings, phase2, rounds=1)
    t = rows(engine, "SELECT status, next_attempt_at FROM tasks WHERE id=:t", t=tid)[0]
    assert (t["status"], t["next_attempt_at"]) == ("awaiting_investigation", None)
    assert schedule_parked_investigations(engine) == 0  # not configured: stays parked
    with engine.begin() as conn:
        save_selection(conn, ModelSelection("mock", "mock-investigator-v1"), "test")
    gw = DeterministicMockGateway()
    w = Worker(
        ai(settings),
        engine,
        rclient,
        stage=InvestigationStage(ai(settings), lambda s, m: gw, ops_client()),
    )
    pump(engine, rclient, settings, w, rounds=2)
    assert one(engine, "SELECT status FROM tasks WHERE id=:t", t=tid) == "awaiting_policy"
    assert one(engine, "SELECT count(*) FROM investigations WHERE task_id=:t", t=tid) == 1


def test_api_exposes_investigation_and_ai_metrics(engine, rclient, settings):
    iid, _ = open_task(engine)
    gw = DeterministicMockGateway()
    w = Worker(
        ai(settings),
        engine,
        rclient,
        stage=InvestigationStage(ai(settings), lambda s, m: gw, ops_client()),
    )
    pump(engine, rclient, settings, w, rounds=1)
    from fastapi.testclient import TestClient

    token = "r" * 48
    s = settings.model_copy(update={"api_read_token": SecretStr(token)})
    c = TestClient(create_app(s, ReadinessChecks({}), engine=engine, redis_client=rclient))
    h = {"Authorization": f"Bearer {token}"}
    detail = c.get(f"/v1/incidents/{iid}", headers=h).json()
    assert detail["investigations"][0]["status"] == "completed"
    assert detail["tasks"][0]["model_id"] == "mock-investigator-v1"
    body = c.get("/v1/metrics", headers=h).text
    assert 'sentinel_investigations_total{status="completed"}' in body
    assert 'sentinel_ai_tool_calls_total{tool="get_incident"}' in body
    assert "sentinel_ai_tokens_total" in body and "# TYPE sentinel_ai_paused_tasks" in body
