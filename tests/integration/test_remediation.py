"""Policy -> approval -> execution -> verification against real PostgreSQL, the
REAL executor app (signing + SQLite ledger) over a simulated Docker API, with
controlled crash injection. Every test asserts the exact number of restarts."""

from __future__ import annotations

import threading
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from app.agent import tasks
from app.agent.investigation import InvestigationStage
from app.agent.mock_gateway import DeterministicMockGateway
from app.agent.model_config import ModelSelection, save_selection
from app.agent.pipeline import TaskPipeline
from app.agent.stages import TransientError
from app.agent.tools import OpsClient
from app.config import Environment, Settings
from app.executor.ledger import Ledger
from app.executor.main import ExecSettings, create_executor_app
from app.persistence.outbox import schedule_policy_tasks
from app.safety import approvals
from app.safety.approvals import DecideOutcome
from app.safety.executor_client import ExecutorClient
from app.safety.policy import action_id_for
from app.safety.remediation import RemediationStage
from tests.integration.helpers import Clock, expire_lease, feed, new_service, one, rows
from tests.integration.test_investigation import ops_client as investigation_ops
from tests.support.fake_docker import FakeDocker

pytestmark = pytest.mark.integration

EXEC_TOKEN, ACTION_KEY, APPROVAL_KEY = "e" * 40, "a" * 40, "p" * 40


class Crash(BaseException):
    """Simulated process death."""


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def wait(self, s: float) -> None:
        self.t += s


def ops(probe_ok: bool = True, error_lines: int = 0) -> OpsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/probe"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "ok": probe_ok,
                        "http_status": 200 if probe_ok else 500,
                        "latency_ms": 12.0,
                    },
                },
            )
        if request.url.path.endswith("/logs"):
            lines = [
                {
                    "ts": "t",
                    "stream": "stdout",
                    "line": '{"level": "ERROR", "msg": "simulated internal error"}',
                }
                for _ in range(error_lines)
            ]
            return httpx.Response(200, json={"status": "ok", "data": lines})
        return httpx.Response(200, json={"status": "unavailable", "reason": "n/a"})

    return OpsClient(
        "http://ops",
        SecretStr("k" * 40),
        5,
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ops"),
    )


@pytest.fixture
def s(base_env):
    return Settings(
        ai_gateway="mock",
        remediation_auto_enabled=True,
        remediation_environment="isolated-demo",
        executor_token=SecretStr(EXEC_TOKEN),
        action_signing_key=SecretStr(ACTION_KEY),
        approval_signing_key=SecretStr(APPROVAL_KEY),
        verify_readiness_deadline_seconds=10,
        verify_probe_interval_seconds=1,
    )


@pytest.fixture
def world(tmp_path):
    """Real executor app + simulated Docker + real ledger."""
    docker = FakeDocker()
    es = ExecSettings(
        token=EXEC_TOKEN,
        signing_key=ACTION_KEY,
        ledger_path=tmp_path / "ledger.sqlite3",
        max_restarts_per_hour=10,
    )
    app = create_executor_app(es, docker.client(), Ledger(es.ledger_path))
    client = ExecutorClient(
        "http://testserver",
        SecretStr(EXEC_TOKEN),
        SecretStr(ACTION_KEY),
        30,
        60,
        client=TestClient(app),
    )
    return docker, client, tmp_path


@pytest.fixture(autouse=True)
def isolate(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tasks SET status='failed', lease_owner=NULL, "
                "lease_expires_at=NULL WHERE status NOT IN "
                "('escalated','failed','resolved','dead_lettered')"
            )
        )
        conn.execute(
            text(
                "UPDATE incidents SET status='closed', resolved_at=now(), "
                "resolution='manual' WHERE status NOT IN ('resolved','closed')"
            )
        )
        save_selection(conn, ModelSelection("mock", "mock-investigator-v1"), "test")
        # Tests share one DB: age earlier tests' restarts out of the hourly window so
        # the (real, unmodified) LIM-2 rate limit only sees this test's actions.
        conn.execute(
            text(
                "UPDATE action_attempts SET started_at = started_at - "
                "interval '2 hours' WHERE started_at > now() - interval '2 hours'"
            )
        )


def fresh_failure(engine, service_id, outcome="unhealthy"):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO health_checks (service_id, checked_at, outcome, "
                "http_status, latency_ms) VALUES (:s, now(), :o, 500, 5)"
            ),
            {"s": service_id, "o": outcome},
        )


def investigated(engine, s):
    """Real incident -> task -> completed mock investigation (proposes restart)."""
    sid = new_service(engine)
    with engine.begin() as conn:  # make it the trusted demo target's service name
        conn.execute(
            text(
                "UPDATE services SET name='demo-app' || '' WHERE id=:s AND NOT "
                "EXISTS (SELECT 1 FROM services WHERE name='demo-app')"
            ),
            {"s": sid},
        )
        demo = conn.execute(text("SELECT id FROM services WHERE name='demo-app'")).scalar_one()
    out = feed(engine, demo, ["unhealthy"] * 3, Clock())[-1]
    if not out.opened_incidents:  # the demo-app detection state may be mid-streak
        conn_iid = one(
            engine,
            "SELECT id FROM incidents WHERE service_id=:s AND status NOT IN "
            "('resolved','closed') ORDER BY opened_at DESC LIMIT 1",
            s=demo,
        )
        tid = one(engine, "SELECT id FROM tasks WHERE incident_id=:i", i=conn_iid)
        iid = conn_iid
    else:
        iid, tid = out.opened_incidents[0], out.created_tasks[0]
    lease = tasks.claim(engine, tid, "inv", 60)
    assert lease is not None
    InvestigationStage(s, lambda st, m: DeterministicMockGateway(), investigation_ops()).run(
        engine, lease
    )
    assert one(engine, "SELECT status FROM tasks WHERE id=:t", t=tid) == "awaiting_policy"
    fresh_failure(engine, demo)
    return iid, tid, demo


def lease_for(engine, tid, owner="w"):
    schedule_policy_tasks(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tasks SET next_attempt_at=now() WHERE id=:t AND status IN "
                "('awaiting_policy','waiting_approval')"
            ),
            {"t": tid},
        )
    lease = tasks.claim(engine, tid, owner, 60)
    assert lease is not None
    return lease


def stage(s, client, o=None):
    ft = FakeTime()
    return RemediationStage(s, client, o or ops(), clock=ft.clock, wait=ft.wait)


def state(engine, tid, iid):
    return {
        "task": one(
            engine, "SELECT status || ':' || COALESCE(outcome,'') FROM tasks WHERE id=:t", t=tid
        ),
        "incident": one(
            engine,
            "SELECT status || ':' || COALESCE(resolution,'') FROM incidents WHERE id=:i",
            i=iid,
        ),
        "attempt": one(engine, "SELECT status FROM action_attempts WHERE incident_id=:i", i=iid),
        "verification": one(engine, "SELECT status FROM verifications WHERE incident_id=:i", i=iid),
    }


def decisions(engine, tid):
    return [
        (r["phase"], r["decision"], r["rule_ids"])
        for r in rows(
            engine,
            "SELECT phase, decision, rule_ids FROM policy_decisions WHERE task_id=:t "
            "ORDER BY evaluated_at",
            t=tid,
        )
    ]


def make_approver(engine, name=None, role="approver"):
    name = name or f"op-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        token = approvals.create_operator(conn, name, role, "test")
        op = approvals.authenticate_operator(conn, token)
    return op, token


# --- autonomous path ---------------------------------------------------------------------


def test_preauthorized_restart_executes_once_and_verifies(engine, s, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)
    res = stage(s, client).run(engine, lease_for(engine, tid))
    assert (res.status, res.outcome) == ("resolved", "recovery_verified")
    assert state(engine, tid, iid) == {
        "task": "resolved:recovery_verified",
        "incident": "resolved:remediated",
        "attempt": "succeeded",
        "verification": "passed",
    }
    assert docker.restarts == 1
    assert decisions(engine, tid) == [
        ("proposal", "ALLOW", ["AUT-1"]),
        ("pre_execution", "ALLOW", ["AUT-1"]),
    ]
    a = rows(engine, "SELECT * FROM action_attempts WHERE incident_id=:i", i=iid)[0]
    assert a["action_id"] == action_id_for(iid) and a["pre_state"] != a["post_state"]
    trail = [
        r["action"]
        for r in rows(
            engine,
            "SELECT action FROM audit_events WHERE "
            "action IN ('policy_decision','action_execution_intent','action_succeeded',"
            "'recovery_verified') ORDER BY occurred_at",
        )
    ]
    assert trail[-3:] == ["action_execution_intent", "action_succeeded", "recovery_verified"]
    steps = [
        r["step"]
        for r in rows(
            engine, "SELECT step FROM task_checkpoints WHERE task_id=:t ORDER BY step", t=tid
        )
    ]
    assert {6, 7, 8} <= set(steps)
    assert (
        one(
            engine,
            "SELECT count(*) FROM evidence WHERE incident_id=:i AND source='verification'",
            i=iid,
        )
        == 1
    )


def test_denied_actions_never_execute(engine, s, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)
    with engine.begin() as conn:  # model proposed an unauthorized target
        conn.execute(
            text(
                "UPDATE investigations SET result = jsonb_set(result, "
                "'{proposed_action,target_service}', '\"postgres\"') WHERE task_id=:t"
            ),
            {"t": tid},
        )
    res = stage(s, client).run(engine, lease_for(engine, tid))
    assert (res.status, res.outcome) == ("escalated", "policy_denied")
    assert docker.restarts == 0 and state(engine, tid, iid)["attempt"] is None
    assert "TGT-1" in decisions(engine, tid)[0][2]
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "escalated"


def test_unsupported_action_in_stored_result_denied(engine, s, world):
    docker, client, _ = world
    _, tid, _ = investigated(engine, s)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE investigations SET result = jsonb_set(result, "
                "'{proposed_action,action}', '\"run_shell\"') WHERE task_id=:t"
            ),
            {"t": tid},
        )
    stage(s, client).run(engine, lease_for(engine, tid))
    assert docker.restarts == 0 and decisions(engine, tid)[0][2] == ["ACT-1"]


def test_production_never_restarts(engine, s, world):
    docker, client, _ = world
    _, tid, _ = investigated(engine, s)
    prod = s.model_copy(
        update={"environment": Environment.PRODUCTION, "remediation_auto_enabled": False}
    )
    stage(prod, client).run(engine, lease_for(engine, tid))
    assert docker.restarts == 0 and "ENV-1" in decisions(engine, tid)[0][2]


def test_app_recovered_before_remediation_is_not_restarted(engine, s, world):
    docker, client, _ = world
    iid, tid, sid = investigated(engine, s)
    fresh_failure(engine, sid, outcome="healthy")
    res = stage(s, client).run(engine, lease_for(engine, tid))
    assert (res.status, res.outcome) == ("resolved", "service_recovered_before_action")
    assert docker.restarts == 0
    # the monitor (not the executor) decides recovery: incident left for auto-resolve
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "investigating"


def test_incident_closed_before_execution_skips(engine, s, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE incidents SET status='resolved', resolved_at=now(), "
                "resolution='manual' WHERE id=:i"
            ),
            {"i": iid},
        )
    res = stage(s, client).run(engine, lease_for(engine, tid))
    assert res.outcome == "incident_no_longer_active" and docker.restarts == 0


def test_failed_recovery_escalates_without_second_restart(engine, s, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)
    res = stage(s, client, ops(probe_ok=False)).run(engine, lease_for(engine, tid))
    assert (res.status, res.outcome) == ("escalated", "recovery_failed")
    assert state(engine, tid, iid) == {
        "task": "escalated:recovery_failed",
        "incident": "escalated:",
        "attempt": "succeeded",
        "verification": "failed",
    }
    assert docker.restarts == 1
    # terminal: the failed verification can never be re-run into a closed incident
    assert tasks.claim(engine, tid, "again", 60) is None
    assert docker.restarts == 1


def test_new_critical_errors_fail_verification(engine, s, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)
    res = stage(s, client, ops(error_lines=2)).run(engine, lease_for(engine, tid))
    assert res.outcome == "recovery_failed" and docker.restarts == 1
    reason = one(engine, "SELECT reason FROM verifications WHERE incident_id=:i", i=iid)
    assert "critical error" in reason


def test_restart_failure_recorded_and_escalated(engine, s, world):
    docker, client, _ = world
    docker.fail_restart = True
    iid, tid, _ = investigated(engine, s)
    res = stage(s, client).run(engine, lease_for(engine, tid))
    assert (res.status, res.outcome) == ("escalated", "restart_failed")
    assert state(engine, tid, iid)["attempt"] == "failed"
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "escalated"


def test_second_restart_for_incident_blocked_by_database(engine, s, world):
    docker, client, _ = world
    iid, tid, sid = investigated(engine, s)
    stage(s, client).run(engine, lease_for(engine, tid))
    with pytest.raises(Exception, match="uq_action_attempts_one_per_incident"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO action_attempts (action_id, task_id, incident_id, "
                    "target_service_id, action_type, fencing_token) VALUES "
                    "(:a, :t, :i, :s, 'restart_demo_app', 1)"
                ),
                {"a": uuid.uuid4(), "t": tid, "i": iid, "s": sid},
            )
    assert docker.restarts == 1


# --- approvals ----------------------------------------------------------------------------


@pytest.fixture
def s_approval(s):
    return s.model_copy(update={"remediation_auto_enabled": False})


def test_approval_required_parks_without_holding_worker(engine, s_approval, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s_approval)
    before = one(engine, "SELECT attempt FROM tasks WHERE id=:t", t=tid)  # investigation's
    res = stage(s_approval, client).run(engine, lease_for(engine, tid))
    assert (res.status, res.outcome) == ("waiting_approval", "approval_required")
    t = rows(engine, "SELECT * FROM tasks WHERE id=:t", t=tid)[0]
    # lease released (worker not held); waiting did not consume a retry attempt
    assert t["lease_owner"] is None and t["next_attempt_at"] is not None
    assert t["attempt"] == before
    a = rows(engine, "SELECT * FROM approvals WHERE task_id=:t", t=tid)[0]
    assert (a["status"], a["action_id"], a["target_service"], a["proposed_action"]) == (
        "pending",
        action_id_for(iid),
        "demo-app",
        "restart_demo_app",
    )
    assert a["action_fingerprint"] and a["risk"] and a["expires_at"] > a["requested_at"]
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "waiting_approval"
    assert docker.restarts == 0 and decisions(engine, tid) == [
        ("proposal", "REQUIRE_APPROVAL", ["APR-0"])
    ]
    # an early re-dispatch while still pending just re-parks (no execution)
    with engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET next_attempt_at=now() WHERE id=:t"), {"t": tid})
    res2 = stage(s_approval, client).run(engine, tasks.claim(engine, tid, "w2", 60))
    assert res2.status == "waiting_approval" and docker.restarts == 0
    assert one(engine, "SELECT count(*) FROM approvals WHERE task_id=:t", t=tid) == 1


def test_approved_action_resumes_revalidates_and_executes(engine, s_approval, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s_approval)
    stage(s_approval, client).run(engine, lease_for(engine, tid))
    a = rows(engine, "SELECT id, action_fingerprint FROM approvals WHERE task_id=:t", t=tid)[0]
    op, _ = make_approver(engine)
    with engine.begin() as conn:
        assert (
            approvals.decide(
                conn,
                approval_id=a["id"],
                operator=op,
                approve=True,
                fingerprint=a["action_fingerprint"],
                reason="ok",
                key=SecretStr(APPROVAL_KEY),
            )
            is DecideOutcome.DECIDED
        )
    # the decision scheduled the task through the outbox; the worker resumes it
    assert (
        one(
            engine,
            "SELECT count(*) FROM outbox_events WHERE aggregate_id=:t AND "
            "dedup_key LIKE '%approval%'",
            t=tid,
        )
        == 1
    )
    lease = tasks.claim(engine, tid, "w3", 60)
    res = stage(s_approval, client).run(engine, lease)
    assert res.outcome == "recovery_verified" and docker.restarts == 1
    assert [d[:2] for d in decisions(engine, tid)][-2:] == [
        ("proposal", "ALLOW"),
        ("pre_execution", "ALLOW"),
    ]
    assert (
        rows(engine, "SELECT approval_id FROM action_attempts WHERE incident_id=:i", i=iid)[0][
            "approval_id"
        ]
        == a["id"]
    )


def test_app_recovers_while_approval_pending_is_not_restarted(engine, s_approval, world):
    docker, client, _ = world
    iid, tid, sid = investigated(engine, s_approval)
    stage(s_approval, client).run(engine, lease_for(engine, tid))
    a = rows(engine, "SELECT id, action_fingerprint FROM approvals WHERE task_id=:t", t=tid)[0]
    fresh_failure(engine, sid, outcome="healthy")  # recovered on its own meanwhile
    op, _ = make_approver(engine)
    with engine.begin() as conn:
        approvals.decide(
            conn,
            approval_id=a["id"],
            operator=op,
            approve=True,
            fingerprint=a["action_fingerprint"],
            reason="ok",
            key=SecretStr(APPROVAL_KEY),
        )
    res = stage(s_approval, client).run(engine, tasks.claim(engine, tid, "w", 60))
    assert res.outcome == "service_recovered_before_action" and docker.restarts == 0
    assert decisions(engine, tid)[-1][2] == ["HLT-2"]  # re-checked right before the action
    # handed back to the monitor's hysteresis, not left stuck waiting for approval
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "investigating"


def test_rejected_approval_never_executes(engine, s_approval, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s_approval)
    stage(s_approval, client).run(engine, lease_for(engine, tid))
    a = rows(engine, "SELECT id, action_fingerprint FROM approvals WHERE task_id=:t", t=tid)[0]
    op, _ = make_approver(engine)
    with engine.begin() as conn:
        approvals.decide(
            conn,
            approval_id=a["id"],
            operator=op,
            approve=False,
            fingerprint=a["action_fingerprint"],
            reason="no",
            key=SecretStr(APPROVAL_KEY),
        )
    res = stage(s_approval, client).run(engine, tasks.claim(engine, tid, "w", 60))
    assert res.outcome == "approval_rejected" and docker.restarts == 0
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "escalated"


def test_expired_approval_fails_closed_and_late_decision_rejected(engine, s_approval, world):
    docker, client, _ = world
    _, tid, _ = investigated(engine, s_approval)
    stage(s_approval, client).run(engine, lease_for(engine, tid))
    a = rows(engine, "SELECT id, action_fingerprint FROM approvals WHERE task_id=:t", t=tid)[0]
    with engine.begin() as conn:  # nobody answered in time
        conn.execute(
            text(
                "UPDATE approvals SET requested_at = now() - interval '2 hours', "
                "expires_at = now() - interval '1 second' WHERE id=:a"
            ),
            {"a": a["id"]},
        )
    op, _ = make_approver(engine)
    with engine.begin() as conn:
        late = approvals.decide(
            conn,
            approval_id=a["id"],
            operator=op,
            approve=True,
            fingerprint=a["action_fingerprint"],
            reason="late",
            key=SecretStr(APPROVAL_KEY),
        )
    assert late is DecideOutcome.EXPIRED
    res = stage(s_approval, client).run(engine, lease_for(engine, tid))
    assert res.outcome == "approval_expired" and docker.restarts == 0
    assert one(engine, "SELECT status FROM approvals WHERE id=:a", a=a["id"]) == "expired"


def test_forged_approval_row_is_not_authorization(engine, s_approval, world):
    docker, client, _ = world
    _, tid, _ = investigated(engine, s_approval)
    stage(s_approval, client).run(engine, lease_for(engine, tid))
    op, _ = make_approver(engine)
    with engine.begin() as conn:  # someone with DB access flips the row directly
        conn.execute(
            text(
                "UPDATE approvals SET status='approved', decided_at=now(), "
                "decided_by=:n WHERE task_id=:t"
            ),
            {"n": op.name, "t": tid},
        )
    res = stage(s_approval, client).run(engine, lease_for(engine, tid))
    assert res.status == "escalated" and docker.restarts == 0
    assert "APR-5" in decisions(engine, tid)[-1][2]


def test_changed_action_fingerprint_invalidates_approval(engine, s_approval, world):
    docker, client, _ = world
    _, tid, _ = investigated(engine, s_approval)
    stage(s_approval, client).run(engine, lease_for(engine, tid))
    a = rows(engine, "SELECT id, action_fingerprint FROM approvals WHERE task_id=:t", t=tid)[0]
    op, _ = make_approver(engine)
    with engine.begin() as conn:
        approvals.decide(
            conn,
            approval_id=a["id"],
            operator=op,
            approve=True,
            fingerprint=a["action_fingerprint"],
            reason="ok",
            key=SecretStr(APPROVAL_KEY),
        )
    changed = s_approval.model_copy(update={"remediation_max_restarts_per_hour": 2})
    res = stage(changed, client).run(engine, tasks.claim(engine, tid, "w", 60))
    assert res.status == "escalated" and docker.restarts == 0
    assert "APR-4" in decisions(engine, tid)[-1][2]


def test_wrong_fingerprint_or_viewer_cannot_decide(engine, s_approval, world):
    _, client, _ = world
    _, tid, _ = investigated(engine, s_approval)
    stage(s_approval, client).run(engine, lease_for(engine, tid))
    a = rows(engine, "SELECT id FROM approvals WHERE task_id=:t", t=tid)[0]
    op, _ = make_approver(engine)
    with engine.begin() as conn:
        out = approvals.decide(
            conn,
            approval_id=a["id"],
            operator=op,
            approve=True,
            fingerprint="0" * 64,
            reason=None,
            key=SecretStr(APPROVAL_KEY),
        )
    assert out is DecideOutcome.FINGERPRINT_MISMATCH


def test_concurrent_decisions_exactly_one_wins(engine, s_approval, world):
    _, client, _ = world
    _, tid, _ = investigated(engine, s_approval)
    stage(s_approval, client).run(engine, lease_for(engine, tid))
    a = rows(engine, "SELECT id, action_fingerprint FROM approvals WHERE task_id=:t", t=tid)[0]
    ops_ = [make_approver(engine)[0] for _ in range(6)]
    results: list[Any] = []
    barrier = threading.Barrier(len(ops_))

    def go(op, approve):
        barrier.wait()
        with engine.begin() as conn:
            results.append(
                approvals.decide(
                    conn,
                    approval_id=a["id"],
                    operator=op,
                    approve=approve,
                    fingerprint=a["action_fingerprint"],
                    reason=None,
                    key=SecretStr(APPROVAL_KEY),
                )
            )

    threads = [threading.Thread(target=go, args=(op, i % 2 == 0)) for i, op in enumerate(ops_)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(DecideOutcome.DECIDED) == 1
    assert results.count(DecideOutcome.ALREADY_DECIDED) == len(ops_) - 1


# --- crash recovery / idempotency ------------------------------------------------------------


class CrashingExecutor:
    """Wraps the real client; crashes the worker at a chosen point."""

    def __init__(self, real: ExecutorClient, when: str) -> None:
        self.real, self.when = real, when
        self.configured = True

    def state(self):
        if self.when == "before_execution":
            raise Crash("died after reservation")
        return self.real.state()

    def get_action(self, action_id):
        return self.real.get_action(action_id)

    def restart(self, action_id, token, fp):
        if self.when == "before_request":
            raise Crash("died after recording intent, before the request left")
        out = self.real.restart(action_id, token, fp)
        if self.when == "after_execution":
            raise Crash("died after the restart, before recording it")
        return out


def run_crash(engine, s, world, when):
    _, client, _ = world
    iid, tid, _ = investigated(engine, s)
    with pytest.raises(Crash):
        stage(s, CrashingExecutor(client, when)).run(engine, lease_for(engine, tid, "a"))
    expire_lease(engine, tid)
    lease = tasks.claim(engine, tid, "b", 60)
    assert lease is not None
    return iid, tid, lease


def test_crash_after_reservation_before_execution(engine, s, world):
    docker, client, _ = world
    iid, _, lease = run_crash(engine, s, world, "before_execution")
    assert (
        one(engine, "SELECT status FROM action_attempts WHERE incident_id=:i", i=iid) == "pending"
    )
    res = stage(s, client).run(engine, lease)
    assert res.outcome == "recovery_verified" and docker.restarts == 1


def test_crash_before_request_reached_executor(engine, s, world):
    docker, client, _ = world
    iid, _, lease = run_crash(engine, s, world, "before_request")
    assert (
        one(engine, "SELECT status FROM action_attempts WHERE incident_id=:i", i=iid) == "executing"
    )
    res = stage(s, client).run(engine, lease)  # ledger has no entry -> policy re-check -> run
    assert res.outcome == "recovery_verified" and docker.restarts == 1


def test_crash_after_execution_before_recording_reconciles_no_repeat(engine, s, world):
    docker, client, _ = world
    iid, _, lease = run_crash(engine, s, world, "after_execution")
    assert docker.restarts == 1
    res = stage(s, client).run(engine, lease)
    assert res.outcome == "recovery_verified" and docker.restarts == 1  # NOT restarted again
    assert (
        rows(engine, "SELECT result FROM action_attempts WHERE incident_id=:i", i=iid)[0]["result"][
            "recorded_via"
        ]
        == "reconciled_from_ledger"
    )


def test_executor_interrupted_mid_action_outcome_unknown_escalates(engine, s, world):
    docker, client, tmp = world
    iid, tid, lease = run_crash(engine, s, world, "before_request")
    fp = one(engine, "SELECT action_fingerprint FROM action_attempts WHERE incident_id=:i", i=iid)
    Ledger(tmp / "ledger.sqlite3").reserve(str(action_id_for(iid)), 1, fp, {}, 10)
    res = stage(s, client).run(engine, lease)
    assert (res.status, res.outcome) == ("escalated", "action_outcome_unknown")
    assert docker.restarts == 0  # never blindly re-issued
    assert state(engine, tid, iid)["attempt"] == "unknown"


def test_executor_interrupted_but_restart_observed_is_reconciled(engine, s, world):
    docker, client, tmp = world
    iid, tid, lease = run_crash(engine, s, world, "before_request")
    fp = one(engine, "SELECT action_fingerprint FROM action_attempts WHERE incident_id=:i", i=iid)
    Ledger(tmp / "ledger.sqlite3").reserve(str(action_id_for(iid)), 1, fp, {}, 10)
    docker.restarts += 1  # the container did restart before the executor died
    from datetime import timedelta

    docker.started_at += timedelta(minutes=1)
    res = stage(s, client).run(engine, lease)
    assert res.outcome == "recovery_verified" and docker.restarts == 1
    assert state(engine, tid, iid)["attempt"] == "reconciled"


def test_crash_after_recording_before_verification(engine, s, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)

    class DiesInVerification(RemediationStage):
        def _probe(self):
            raise Crash("died during verification")

    with pytest.raises(Crash):
        DiesInVerification(s, client, ops()).run(engine, lease_for(engine, tid, "a"))
    assert state(engine, tid, iid)["attempt"] == "succeeded" and docker.restarts == 1
    expire_lease(engine, tid)
    res = stage(s, client).run(engine, tasks.claim(engine, tid, "b", 60))
    assert res.outcome == "recovery_verified" and docker.restarts == 1


def test_stale_worker_cannot_execute(engine, s, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)
    stale = lease_for(engine, tid, "old")
    expire_lease(engine, tid)
    current = tasks.claim(engine, tid, "new", 60)
    stage(s, client).run(engine, current)
    assert docker.restarts == 1
    with pytest.raises((tasks.LeaseLost, Exception)):
        stage(s, client).run(engine, stale)
    # even a direct executor call with the stale fencing token is refused
    fp = one(engine, "SELECT action_fingerprint FROM action_attempts WHERE incident_id=:i", i=iid)
    from app.safety.executor_client import ExecutorRefused

    with pytest.raises(ExecutorRefused, match="stale"):
        client.restart(action_id_for(iid), stale.token, fp)
    assert docker.restarts == 1


def test_executor_unavailable_before_execution_is_transient(engine, s, world):
    docker, _, _ = world
    iid, tid, _ = investigated(engine, s)
    down = ExecutorClient("http://127.0.0.1:1", SecretStr(EXEC_TOKEN), SecretStr(ACTION_KEY), 1)
    with pytest.raises(TransientError):
        stage(s, down).run(engine, lease_for(engine, tid))
    assert state(engine, tid, iid)["attempt"] == "pending" and docker.restarts == 0


def test_pipeline_routes_by_durable_state(engine, s, world):
    docker, client, _ = world
    _, tid, _ = investigated(engine, s)
    inv = InvestigationStage(s, lambda st, m: DeterministicMockGateway(), investigation_ops())
    pipe = TaskPipeline(inv, stage(s, client))
    assert pipe.run(engine, lease_for(engine, tid)).outcome == "recovery_verified"
    assert docker.restarts == 1
