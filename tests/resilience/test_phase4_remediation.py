"""Phase 4 on the LIVE compose stack (steps 1-8). Opt-in: RUN_RESILIENCE=1.

The model is the deterministic MOCK (not Claude; no paid calls). Everything else
is real: monitor, PostgreSQL, outbox, Redis Streams, worker leases, the
deterministic policy, the authenticated approval API, the restricted executor
restarting the REAL demo-app container through Docker, and verification.

Restarts are counted from the executor's own durable ledger and from the
container's StartedAt, never inferred.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator

import pytest

from tests.resilience.helpers import API, DEMO, ROOT, dc, env_file, http, psql, wait_ready
from tests.resilience.test_phase2_pipeline import FAST, until
from tests.resilience.test_phase3_ai import onboard_mock, reset_active

pytestmark = [
    pytest.mark.resilience,
    pytest.mark.skipif(os.environ.get("RUN_RESILIENCE") != "1", reason="RUN_RESILIENCE!=1"),
]

BASE = {
    "SENTINEL_AI_GATEWAY": "mock",
    "SENTINEL_AI_PAUSE_SECONDS": "5",
    "SENTINEL_REMEDIATION_MAX_RESTARTS_PER_HOUR": "20",
    "SENTINEL_VERIFY_READINESS_DEADLINE_SECONDS": "30",
}
AUTO = {
    **BASE,
    "SENTINEL_REMEDIATION_AUTO_ENABLED": "true",
    "SENTINEL_REMEDIATION_ENVIRONMENT": "isolated-demo",
}
APPROVAL = {**BASE, "SENTINEL_REMEDIATION_ENVIRONMENT": "isolated-demo"}
BLOCKED = {**BASE}  # environment NOT declared -> ENV-2 DENY
MANAGED = (
    *FAST,
    *AUTO,
    "SENTINEL_APPROVAL_TTL_SECONDS",
    "EXEC_MAX_RESTARTS_PER_HOUR",
    "EXEC_TEST_PRE_RESTART_DELAY_SECONDS",
)


def recreate(service: str, overrides: dict[str, str]) -> None:
    env = {k: v for k, v in os.environ.items() if k not in MANAGED}
    env.update(overrides)
    subprocess.run(
        ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "--wait", service],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env=env,
        timeout=240,
    )


def inject(mode: str, sticky: bool = False) -> None:
    token = {"X-Demo-Token": env_file()["DEMO_INJECTION_TOKEN"]}
    code, _ = http("POST", f"{DEMO}/simulate-failure", {"mode": mode, "sticky": sticky}, token)
    assert code == 200


def demo_started_at() -> str:
    cid = dc("ps", "-q", "demo-app").strip()
    return subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", cid],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def ledger_count(action_id: str | None = None) -> int:
    where = f" WHERE action_id='{action_id}'" if action_id else ""
    out = dc(
        "exec",
        "-T",
        "executor",
        "python",
        "-c",
        "import sqlite3; c=sqlite3.connect('/var/lib/sentinel-executor/ledger.sqlite3');"
        f'print(c.execute("SELECT count(*) FROM actions{where}").fetchone()[0])',
    )
    return int(out.strip())


def new_incident(since: str) -> str:
    return psql(
        "SELECT i.id FROM incidents i JOIN services s ON s.id=i.service_id "
        f"WHERE s.name='demo-app' AND i.opened_at > '{since}' "
        "AND i.incident_type='http_error' ORDER BY i.opened_at LIMIT 1"
    )


def task_of(iid: str) -> str:
    return until(lambda: psql(f"SELECT id FROM tasks WHERE incident_id='{iid}'"), timeout=20)


def task_state(tid: str) -> str:
    return psql(f"SELECT status || ':' || COALESCE(outcome,'') FROM tasks WHERE id='{tid}'")


def start_incident() -> tuple[str, str]:
    since = psql("SELECT now()")
    inject("http_500")
    iid = until(lambda: new_incident(since), timeout=45)
    return iid, task_of(iid)


def operator_token(role: str = "approver") -> str:
    name = f"it-{uuid.uuid4().hex[:8]}"
    out = subprocess.run(
        ["docker", "compose", "run", "--rm", "-T", "onboard", "operator-add", name, role],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    return next(line.strip() for line in out.splitlines() if line.strip().startswith("sop_"))


def decide(approval_id: str, fingerprint: str, token: str, verb: str = "approve") -> int:
    req = urllib.request.Request(
        f"{API}/v1/approvals/{approval_id}/{verb}",
        method="POST",
        data=json.dumps({"action_fingerprint": fingerprint, "reason": "live test"}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def pending_approval(tid: str) -> tuple[str, str]:
    row = until(
        lambda: psql(
            f"SELECT id || ',' || action_fingerprint FROM approvals "
            f"WHERE task_id='{tid}' AND status='pending'"
        ),
        timeout=90,
    )
    a, fp = row.split(",")
    return a, fp


@pytest.fixture(scope="module", autouse=True)
def stack() -> Iterator[None]:
    wait_ready()
    inject("none")
    reset_active()
    recreate("monitor", FAST)
    recreate("executor", {"EXEC_MAX_RESTARTS_PER_HOUR": "20"})
    recreate("worker", AUTO)
    assert "Selected model: mock-investigator-v1" in onboard_mock()
    yield
    inject("none")
    psql("UPDATE model_config SET is_active=false")
    reset_active()
    recreate("executor", {})
    recreate("worker", {})
    recreate("monitor", {})


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    reset_active()
    inject("none")
    time.sleep(3)
    yield
    inject("none")


STATE: dict[str, str] = {}


def test_allowed_autonomous_restart_end_to_end():
    before = demo_started_at()
    iid, tid = start_incident()
    until(lambda: task_state(tid) == "resolved:recovery_verified", timeout=150)
    STATE["allowed_task"] = tid
    assert psql(f"SELECT status || ':' || resolution FROM incidents WHERE id='{iid}'") == (
        "resolved:remediated"
    )
    decisions = psql(
        f"SELECT string_agg(phase || '=' || decision || rule_ids::text, ' ' "
        f"ORDER BY evaluated_at) FROM policy_decisions WHERE task_id='{tid}'"
    )
    assert decisions == "proposal=ALLOW{AUT-1} pre_execution=ALLOW{AUT-1}"
    action_id = psql(f"SELECT action_id FROM action_attempts WHERE incident_id='{iid}'")
    assert psql(f"SELECT status FROM action_attempts WHERE incident_id='{iid}'") == "succeeded"
    assert ledger_count(action_id) == 1
    assert demo_started_at() != before  # the REAL container was restarted
    assert psql(f"SELECT status FROM verifications WHERE incident_id='{iid}'") == "passed"
    probes = psql(
        f"SELECT jsonb_array_length(observations) FROM verifications WHERE incident_id='{iid}'"
    )
    assert int(probes) >= 4  # >= 3 probes + log scan
    assert http("GET", f"{DEMO}/health")[0] == 200


def test_duplicate_deliveries_do_not_restart_again():
    tid = STATE["allowed_task"]
    iid = psql(f"SELECT incident_id FROM tasks WHERE id='{tid}'")
    action_id = psql(f"SELECT action_id FROM action_attempts WHERE incident_id='{iid}'")
    before, total = demo_started_at(), ledger_count()
    pw = env_file()["REDIS_PASSWORD"]
    for _ in range(3):
        dc(
            "exec",
            "-T",
            "-e",
            f"REDISCLI_AUTH={pw}",
            "redis",
            "redis-cli",
            "XADD",
            "sentinel:tasks",
            "*",
            "task_id",
            tid,
        )
    time.sleep(8)
    assert ledger_count(action_id) == 1 and ledger_count() == total
    assert demo_started_at() == before
    assert task_state(tid) == "resolved:recovery_verified"


def test_blocked_action_does_not_execute():
    recreate("worker", BLOCKED)
    before, total = demo_started_at(), ledger_count()
    iid, tid = start_incident()
    until(lambda: task_state(tid) == "escalated:policy_denied", timeout=120)
    assert "ENV-2" in psql(f"SELECT rule_ids::text FROM policy_decisions WHERE task_id='{tid}'")
    assert psql(f"SELECT count(*) FROM action_attempts WHERE incident_id='{iid}'") == "0"
    assert ledger_count() == total and demo_started_at() == before
    assert psql(f"SELECT status FROM incidents WHERE id='{iid}'") == "escalated"  # stays open
    recreate("worker", AUTO)


def test_approval_required_then_authenticated_approval_executes():
    recreate("worker", APPROVAL)
    before = demo_started_at()
    iid, tid = start_incident()
    approval_id, fp = pending_approval(tid)
    assert task_state(tid) == "waiting_approval:approval_required"
    assert psql(f"SELECT lease_owner IS NULL FROM tasks WHERE id='{tid}'") == "t"
    assert psql(f"SELECT count(*) FROM action_attempts WHERE incident_id='{iid}'") == "0"
    viewer, approver = operator_token("viewer"), operator_token("approver")
    assert decide(approval_id, fp, "sop_not-a-real-token") == 401
    assert decide(approval_id, fp, viewer) == 403
    assert decide(approval_id, "0" * 64, approver) == 409  # changed fingerprint
    assert decide(approval_id, fp, approver) == 200
    assert decide(approval_id, fp, approver) == 409  # replay
    until(lambda: task_state(tid) == "resolved:recovery_verified", timeout=150)
    assert demo_started_at() != before
    assert psql(f"SELECT approval_id FROM action_attempts WHERE incident_id='{iid}'") == (
        approval_id
    )
    recreate("worker", AUTO)


def test_approval_expiry_fails_closed():
    recreate("worker", {**APPROVAL, "SENTINEL_APPROVAL_TTL_SECONDS": "30"})
    before, total = demo_started_at(), ledger_count()
    _, tid = start_incident()
    approval_id, fp = pending_approval(tid)
    until(lambda: task_state(tid) == "escalated:approval_expired", timeout=90)
    assert psql(f"SELECT status FROM approvals WHERE id='{approval_id}'") == "expired"
    assert decide(approval_id, fp, operator_token()) == 409  # too late
    assert ledger_count() == total and demo_started_at() == before
    recreate("worker", AUTO)


def test_app_recovers_before_remediation_is_not_restarted():
    recreate("worker", APPROVAL)
    before, total = demo_started_at(), ledger_count()
    iid, tid = start_incident()
    approval_id, fp = pending_approval(tid)
    inject("none")  # recovers on its own while the approval is pending
    time.sleep(8)  # several healthy checks recorded by the monitor
    assert decide(approval_id, fp, operator_token()) == 200
    until(lambda: task_state(tid) == "resolved:service_recovered_before_action", timeout=90)
    assert ledger_count() == total and demo_started_at() == before
    # the monitor's hysteresis (not the remediation stage) closes the incident
    until(
        lambda: psql(f"SELECT resolution FROM incidents WHERE id='{iid}'") == "auto_recovered",
        timeout=60,
    )
    recreate("worker", AUTO)


def test_failed_recovery_escalates_without_second_restart():
    before, total = demo_started_at(), ledger_count()
    since = psql("SELECT now()")
    inject("http_500", sticky=True)  # survives the restart: restart cannot fix it
    try:
        iid = until(lambda: new_incident(since), timeout=45)
        tid = task_of(iid)
        until(lambda: task_state(tid) == "escalated:recovery_failed", timeout=180)
    finally:
        inject("none", sticky=False)
    assert ledger_count() == total + 1  # exactly one restart
    assert demo_started_at() != before
    assert psql(f"SELECT status FROM verifications WHERE incident_id='{iid}'") == "failed"
    assert psql(f"SELECT status FROM incidents WHERE id='{iid}'") == "escalated"  # stays open
    assert psql(f"SELECT resolution IS NULL FROM incidents WHERE id='{iid}'") == "t"


def test_worker_killed_during_execution_reconciles_without_duplicate():
    recreate(
        "executor",
        {"EXEC_MAX_RESTARTS_PER_HOUR": "20", "EXEC_TEST_PRE_RESTART_DELAY_SECONDS": "10"},
    )
    try:
        total = ledger_count()
        iid, tid = start_incident()
        until(
            lambda: (
                psql(f"SELECT status FROM action_attempts WHERE incident_id='{iid}'") == "executing"
            ),
            timeout=120,
            every=0.5,
        )
        dc("kill", "worker")  # the worker dies while the executor is mid-action
        dc("start", "worker")
        until(lambda: task_state(tid) == "resolved:recovery_verified", timeout=300, every=3)
        action_id = psql(f"SELECT action_id FROM action_attempts WHERE incident_id='{iid}'")
        assert ledger_count(action_id) == 1 and ledger_count() == total + 1
        via = psql(f"SELECT result->>'recorded_via' FROM action_attempts WHERE incident_id='{iid}'")
        assert via in {"reconciled_from_ledger", "executor_response"}
    finally:
        recreate("executor", {"EXEC_MAX_RESTARTS_PER_HOUR": "20"})
        recreate("worker", AUTO)  # plain `compose up` would reset the test config


def test_executor_unavailable_never_executes_and_escalates():
    recreate("worker", AUTO)
    dc("stop", "executor")
    try:
        before = demo_started_at()
        iid, tid = start_incident()
        until(lambda: task_state(tid).startswith("dead_lettered"), timeout=180, every=3)
        assert psql(f"SELECT status FROM action_attempts WHERE incident_id='{iid}'") == "pending"
        assert demo_started_at() == before
        assert psql(f"SELECT status FROM incidents WHERE id='{iid}'") == "escalated"
    finally:
        dc("start", "executor")
        until(lambda: "healthy" in dc("ps", "--format", "{{.Status}}", "executor"), timeout=60)


def test_postgres_and_redis_interruptions_while_awaiting_approval():
    recreate("worker", APPROVAL)
    try:
        _, tid = start_incident()
        approval_id, fp = pending_approval(tid)
        inject("http_500")  # keep failing so the pre-execution re-check still allows
        dc("restart", "postgres")
        wait_ready()
        dc("stop", "redis")
        try:
            assert decide(approval_id, fp, operator_token()) == 200  # DB + outbox only
            time.sleep(3)
            assert task_state(tid).startswith("waiting_approval")  # Redis down: queued
        finally:
            dc("start", "redis")
        until(lambda: task_state(tid) == "resolved:recovery_verified", timeout=180, every=3)
    finally:
        recreate("worker", AUTO)


def test_privilege_boundaries_live():
    for svc in ("worker", "api", "monitor", "dispatcher"):
        out = dc(
            "exec",
            "-T",
            svc,
            "python",
            "-c",
            "import os; print(os.path.exists('/var/run/docker.sock'))",
        )
        assert out.strip() == "False", svc
    probe = (
        "import socket,sys\ntry:\n socket.create_connection(('executor',8003),2); "
        "print('REACHABLE')\nexcept OSError: print('unreachable')"
    )
    for svc in ("api", "monitor", "demo-app"):
        assert dc("exec", "-T", svc, "python", "-c", probe).strip() == "unreachable", svc
    assert dc("exec", "-T", "worker", "python", "-c", probe).strip() == "REACHABLE"
