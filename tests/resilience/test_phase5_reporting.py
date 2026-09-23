"""Phase 5 on the LIVE compose stack (steps 1-10). Opt-in: RUN_RESILIENCE=1.

The model is the deterministic MOCK (not Claude; no paid calls). Everything else
is real: monitor, PostgreSQL, outbox, Redis Streams (tasks AND reports streams),
worker leases, deterministic policy, the restricted executor restarting the REAL
demo-app container, verification, the report stage + validator, the notifier
delivering signed webhooks to the dev/test notify-sink, backup and restore.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator

import pytest

from tests.resilience.helpers import API, DEMO, ROOT, dc, env_file, http, psql, wait_ready
from tests.resilience.test_phase2_pipeline import FAST, until
from tests.resilience.test_phase3_ai import onboard_mock, reset_active
from tests.resilience.test_phase4_remediation import (
    AUTO,
    BASE,
    BLOCKED,
    demo_started_at,
    inject,
    ledger_count,
    recreate,
    start_incident,
    task_state,
)

pytestmark = [
    pytest.mark.resilience,
    pytest.mark.skipif(os.environ.get("RUN_RESILIENCE") != "1", reason="RUN_RESILIENCE!=1"),
]

NOTIFY_FAST = {
    "SENTINEL_NOTIFY_RETRY_BASE_SECONDS": "2",
    "SENTINEL_NOTIFY_RETRY_MAX_SECONDS": "4",
    "SENTINEL_ALERT_EVAL_SECONDS": "3",
}
STATE: dict[str, str] = {}


def read_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {env_file()['SENTINEL_API_READ_TOKEN']}"}


def sink_entries() -> list[dict]:
    out = dc(
        "exec",
        "-T",
        "notify-sink",
        "sh",
        "-c",
        "cat /tmp/notify-sink.jsonl 2>/dev/null",
        check=False,
    )
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def sink_for(event_id: str) -> list[dict]:
    return [e for e in sink_entries() if e["idempotency_key"] == event_id]


def report_of(iid: str) -> dict:
    code, body = http("GET", f"{API}/v1/incidents/{iid}/report", headers=read_headers())
    assert code == 200
    return body


def wait_report(iid: str, timeout: float = 120) -> dict:
    return until(
        lambda: (r := report_of(iid)) and r["status"] in ("validated", "fallback") and r,
        timeout=timeout,
        every=2,
    )


def event_id(iid: str, event_type: str) -> str:
    return until(
        lambda: psql(
            f"SELECT id FROM notification_events WHERE incident_id='{iid}' "
            f"AND event_type='{event_type}' LIMIT 1"
        ),
        timeout=60,
    )


def delivered(eid: str, channel: str = "webhook") -> bool:
    return (
        psql(
            f"SELECT status FROM notification_deliveries WHERE event_id='{eid}' "
            f"AND channel='{channel}'"
        )
        == "delivered"
    )


def checks_since(ts: str) -> int:
    return int(psql(f"SELECT count(*) FROM health_checks WHERE checked_at > '{ts}'"))


@pytest.fixture(scope="module", autouse=True)
def stack() -> Iterator[None]:
    wait_ready()
    inject("none")
    reset_active()
    recreate("monitor", FAST)
    recreate("executor", {"EXEC_MAX_RESTARTS_PER_HOUR": "20"})
    recreate("notifier", NOTIFY_FAST)
    recreate("worker", AUTO)
    assert "Selected model: mock-investigator-v1" in onboard_mock()
    yield
    inject("none")
    psql("UPDATE model_config SET is_active=false")
    reset_active()
    for svc in ("executor", "worker", "monitor", "notifier"):
        recreate(svc, {})


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    reset_active()
    inject("none")
    time.sleep(3)
    yield
    inject("none")


# --- end-to-end demonstration ----------------------------------------------------------------


def test_e2e_autonomous_restart_report_notification_and_monitoring_continues():
    before = demo_started_at()
    iid, tid = start_incident()
    until(lambda: task_state(tid) == "resolved:recovery_verified", timeout=150)
    assert demo_started_at() != before  # the REAL container was restarted exactly once
    action_id = psql(f"SELECT action_id FROM action_attempts WHERE incident_id='{iid}'")
    assert ledger_count(action_id) == 1
    rep = wait_report(iid)
    assert rep["status"] == "validated"
    r = rep["report"]
    assert (r["generation_mode"], r["model_id"], r["auth_mode"], r["version"]) == (
        "ai",
        "mock-investigator-v1",
        "mock",
        1,
    )
    body = r["body"]
    assert "(TEST/DEMO ONLY - not Claude)" in body and "(EXECUTED)" in body
    assert "**passed**" in body and "resolved as remediated" in body
    assert r["validation"]["validated"] is True
    # notifications: remediation performed + report ready, delivered SIGNED to the sink
    for et in ("remediation_performed", "report_ready"):
        eid = event_id(iid, et)
        until(lambda eid=eid: delivered(eid), timeout=60)
        got = sink_for(eid)
        assert len([g for g in got if not g["duplicate"]]) == 1 and got[0]["signed"]
    # monitoring continued throughout and after
    ts = psql("SELECT now()")
    until(lambda: checks_since(ts) >= 3, timeout=30)
    STATE.update(iid=iid, tid=tid, action_id=action_id, report_id=r["id"])
    code, st = http("GET", f"{API}/v1/system/status", headers=read_headers())
    assert code == 200 and st["components"]["executor"]["status"] == "ok"


def test_escalation_report_for_blocked_action_never_claims_recovery():
    recreate("worker", BLOCKED)
    try:
        before = demo_started_at()
        iid, tid = start_incident()
        until(lambda: task_state(tid).startswith("escalated:policy_denied"), timeout=120)
        rep = wait_report(iid)
        body = rep["report"]["body"]
        assert "- no action was executed" in body and "**DENY**" in body
        assert (
            "OPEN, owned by a human" in body
            and "recovered" not in rep["report"]["body"].split("## Summary")[1].split("##")[0]
        )
        eid = event_id(iid, "incident_escalated")
        until(lambda: delivered(eid), timeout=60)
        assert demo_started_at() == before
    finally:
        recreate("worker", AUTO)


def test_ai_unavailable_fallback_report_while_monitoring_continues():
    # the selection is the mock, but this worker is not configured for it: every AI
    # stage fails closed with CredentialsMissing (no silent provider substitution)
    recreate("worker", {**BASE, "SENTINEL_AI_GATEWAY": "anthropic"})
    try:
        ts = psql("SELECT now()")
        token = {"X-Demo-Token": env_file()["DEMO_INJECTION_TOKEN"]}
        assert (
            http(
                "POST",
                f"{DEMO}/simulate-failure",
                {"mode": "http_500", "duration_seconds": 25},
                token,
            )[0]
            == 200
        )
        iid = until(
            lambda: psql(
                "SELECT i.id FROM incidents i JOIN services s ON s.id=i.service_id WHERE "
                f"s.name='demo-app' AND i.opened_at > '{ts}' LIMIT 1"
            ),
            timeout=45,
        )
        tid = until(lambda: psql(f"SELECT id FROM tasks WHERE incident_id='{iid}'"), timeout=20)
        until(
            lambda: task_state(tid) == "awaiting_investigation:ai_paused_credentials_missing",
            timeout=60,
        )
        # alert fires and is delivered; monitoring keeps recording checks
        until(
            lambda: psql("SELECT status FROM alert_state WHERE name='ai_auth_failed'") == "firing",
            timeout=30,
        )
        # the failure self-heals; the monitor's hysteresis resolves the incident
        until(
            lambda: psql(f"SELECT resolution FROM incidents WHERE id='{iid}'") == "auto_recovered",
            timeout=90,
        )
        rep = wait_report(iid)
        assert rep["status"] == "fallback"
        assert rep["report"]["fallback_reason"] == "ai_unavailable_credentials_missing"
        assert (
            rep["report"]["model_id"] is None and "DETERMINISTIC FALLBACK" in rep["report"]["body"]
        )
        assert checks_since(ts) >= 10
        code, st = http("GET", f"{API}/v1/system/status", headers=read_headers())
        assert code == 200 and st["components"]["monitor"]["status"] == "ok"
    finally:
        recreate("worker", AUTO)


def test_notification_outage_retries_and_restart_with_pending_notifications():
    dc("stop", "notify-sink")
    try:
        out = subprocess.run(
            ["docker", "compose", "run", "--rm", "-T", "onboard", "notify-test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        eid = out.stdout.split("Test notification ")[1].split(" ")[0]
        until(
            lambda: (
                int(
                    psql(
                        f"SELECT attempt FROM notification_deliveries WHERE event_id='{eid}' "
                        "AND channel='webhook'"
                    )
                    or 0
                )
                >= 2
            ),
            timeout=60,
        )
        row = psql(
            f"SELECT status || ':' || COALESCE(last_error,'') FROM notification_deliveries "
            f"WHERE event_id='{eid}' AND channel='webhook'"
        )
        assert row.startswith("pending:") and row.split(":", 1)[1]  # retry scheduled with a reason
        assert delivered(eid, "log")  # other channels are unaffected by the outage
        dc("restart", "notifier")  # restart while the delivery is still pending
    finally:
        dc("start", "notify-sink")
    until(lambda: delivered(eid), timeout=90)
    assert len([g for g in sink_for(eid) if not g["duplicate"]]) == 1
    assert (
        int(
            psql(
                f"SELECT count(*) FROM audit_events WHERE entity_id='{eid}' "
                "AND action='notification_failed'"
            )
        )
        >= 1
    )


def test_duplicate_report_and_notification_work_is_idempotent():
    iid, job = STATE["iid"], psql(f"SELECT id FROM report_jobs WHERE incident_id='{STATE['iid']}'")
    stream = "sentinel:reports"
    for _ in range(3):  # duplicate deliveries of an already-completed report job
        dc(
            "exec",
            "-T",
            "redis",
            "sh",
            "-c",
            f'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli XADD {stream} "*" task_id {job} '
            "event_type report.generate",
        )
    ev = psql(
        f"SELECT dedup_key FROM notification_events WHERE incident_id='{iid}' "
        "AND event_type='report_ready'"
    )
    psql(
        "INSERT INTO notification_events (event_type, severity, dedup_key, payload) VALUES "
        f"('report_ready', 'info', '{ev}', '{{}}') ON CONFLICT (dedup_key) DO NOTHING"
    )
    time.sleep(8)
    assert psql(f"SELECT count(*) FROM reports WHERE incident_id='{iid}'") == "1"
    assert psql(f"SELECT count(*) FROM notification_events WHERE dedup_key='{ev}'") == "1"
    eid = psql(f"SELECT id FROM notification_events WHERE dedup_key='{ev}'")
    # durable delivery records (the sink's tmpfs log was reset by the outage test):
    # one delivery per channel, each sent in exactly one attempt
    assert (
        psql(
            "SELECT string_agg(channel || '=' || status || '/' || attempt, ',' ORDER BY channel) "
            f"FROM notification_deliveries WHERE event_id='{eid}'"
        )
        == "log=delivered/1,webhook=delivered/1"
    )
    assert len([g for g in sink_for(eid) if not g["duplicate"]]) <= 1
    assert psql(f"SELECT status FROM report_jobs WHERE id='{job}'") == "validated"


def test_worker_killed_during_reporting_recovers_to_one_report():
    recreate("worker", {**BLOCKED, "SENTINEL_TEST_REPORT_DELAY_SECONDS": "30"})
    try:
        iid, tid = start_incident()
        until(lambda: task_state(tid).startswith("escalated"), timeout=120)
        until(
            lambda: (
                psql(f"SELECT status FROM report_jobs WHERE incident_id='{iid}'") == "generating"
            ),
            timeout=60,
        )
        cid = dc("ps", "-q", "worker").strip()
        subprocess.run(["docker", "kill", "-s", "KILL", cid], check=True, capture_output=True)
        recreate("worker", AUTO)  # a new worker (no delay); lease expiry + reclaim
        rep = wait_report(iid, timeout=240)
        assert rep["report"]["version"] == 1
        assert psql(f"SELECT count(*) FROM reports WHERE incident_id='{iid}'") == "1"
        assert int(psql(f"SELECT attempt FROM report_jobs WHERE incident_id='{iid}'")) >= 2
    finally:
        recreate("worker", AUTO)


def test_redis_outage_delays_but_never_loses_report_work():
    iid = STATE["iid"]
    dc("stop", "redis")
    try:
        subprocess.run(
            ["docker", "compose", "run", "--rm", "-T", "onboard", "report-request", iid],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        time.sleep(5)
        assert (
            psql(f"SELECT count(*) FROM report_jobs WHERE incident_id='{iid}' AND status='pending'")
            == "1"
        )
        assert (
            int(
                psql(
                    "SELECT count(*) FROM outbox_events WHERE published_at IS NULL "
                    "AND event_type='report.generate'"
                )
            )
            >= 1
        )
    finally:
        dc("start", "redis")
    until(
        lambda: psql(f"SELECT max(version) FROM reports WHERE incident_id='{iid}'") == "2",
        timeout=180,
        every=3,
    )


def test_postgres_interruption_notifier_survives_and_delivers():
    out = subprocess.run(
        ["docker", "compose", "run", "--rm", "-T", "onboard", "notify-test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    eid = out.stdout.split("Test notification ")[1].split(" ")[0]
    started = dc("ps", "-q", "notifier").strip()
    dc("stop", "postgres")
    time.sleep(8)
    dc("start", "postgres")
    wait_ready(120)
    until(lambda: delivered(eid), timeout=90)
    assert dc("ps", "-q", "notifier").strip() == started  # never crashed / replaced


def test_backup_and_restore_preserve_history_and_executor_idempotency():
    iid, action_id = STATE["iid"], STATE["action_id"]
    snapshot_sql = (
        "SELECT json_build_object("
        f"'reports', (SELECT count(*) FROM reports WHERE incident_id='{iid}'),"
        f"'report_body_md5', (SELECT md5(string_agg(body, '' ORDER BY version)) FROM reports "
        f"  WHERE incident_id='{iid}'),"
        f"'policy', (SELECT count(*) FROM policy_decisions WHERE incident_id='{iid}'),"
        f"'actions', (SELECT string_agg(status, ',') FROM action_attempts "
        f"  WHERE incident_id='{iid}'),"
        f"'verifications', (SELECT string_agg(status, ',') FROM verifications "
        f"  WHERE incident_id='{iid}'),"
        f"'events', (SELECT count(*) FROM notification_events WHERE incident_id='{iid}'),"
        f"'evidence', (SELECT count(*) FROM evidence WHERE incident_id='{iid}'),"
        f"'incident', (SELECT status || ':' || resolution FROM incidents WHERE id='{iid}'),"
        "'audit', (SELECT count(*) FROM audit_events))"
    )
    before = json.loads(psql(snapshot_sql))
    out = subprocess.run(
        ["scripts/backup.sh"], cwd=ROOT, capture_output=True, text=True, timeout=300, check=True
    ).stdout
    bdir = out.split("backup written: ")[1].split()[0]
    # disaster: lose the database AND the executor ledger
    for svc in ("worker", "dispatcher", "monitor", "notifier", "api", "executor"):
        dc("stop", svc)
    psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    dc(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "executor",
        "sh",
        "-c",
        "rm -f /var/lib/sentinel-executor/ledger.sqlite3*",
    )
    res = subprocess.run(
        ["scripts/restore.sh", bdir],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "CONFIRM_RESTORE": "yes"},
    )
    assert res.returncode == 0, res.stderr[-2000:]
    wait_ready(120)
    after = json.loads(psql(snapshot_sql))
    audit_after = after.pop("audit")
    assert audit_after >= before.pop("audit")  # + backup/restore audit events
    assert after == before
    assert ledger_count(action_id) == 1
    status = dc(
        "exec",
        "-T",
        "executor",
        "python",
        "-c",
        "import sqlite3; c=sqlite3.connect('/var/lib/sentinel-executor/ledger.sqlite3');"
        f"print(c.execute(\"SELECT status FROM actions WHERE action_id='{action_id}'\")"
        ".fetchone()[0])",
    ).strip()
    assert status == "completed"
    # a signed, otherwise valid request for the already-executed action is NOT re-run
    fp = psql(f"SELECT action_fingerprint FROM action_attempts WHERE action_id='{action_id}'")
    started = demo_started_at()
    replay = dc(
        "exec",
        "-T",
        "worker",
        "python",
        "-c",
        "import uuid,json\nfrom app.config import get_settings\n"
        "from app.safety.executor_client import ExecutorClient\ns=get_settings()\n"
        "c=ExecutorClient(s.executor_url,s.executor_token,s.action_signing_key,30)\n"
        f"print(json.dumps(c.restart(uuid.UUID('{action_id}'), 10**6, '{fp}')))",
    )
    body = json.loads(replay.strip().splitlines()[-1])
    assert body["replayed"] is True and body["status"] == "completed"
    time.sleep(3)
    assert demo_started_at() == started  # no second restart after restore
    assert psql(f"SELECT status FROM tasks WHERE id='{STATE['tid']}'") == "resolved"
    assert int(psql("SELECT count(*) FROM audit_events WHERE action='restore_completed'")) >= 1


def test_no_secrets_in_any_phase5_service_logs():
    keys = {
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "SENTINEL_API_READ_TOKEN",
        "SENTINEL_OPS_READER_TOKEN",
        "SENTINEL_EXECUTOR_TOKEN",
        "SENTINEL_ACTION_SIGNING_KEY",
        "SENTINEL_APPROVAL_SIGNING_KEY",
        "SENTINEL_NOTIFY_WEBHOOK_SECRET",
        "DEMO_INJECTION_TOKEN",
    }
    secrets = [v for k, v in env_file().items() if k in keys and v]
    assert len(secrets) == len(keys)
    logs = dc(
        "logs",
        "--no-color",
        "api",
        "worker",
        "dispatcher",
        "monitor",
        "notifier",
        "notify-sink",
        "executor",
        "ops-reader",
        check=False,
    )
    assert len(logs.splitlines()) > 50  # non-trivial (containers may be fresh after the restore)
    for s in secrets:
        assert s not in logs
    blob = psql("SELECT string_agg(payload::text, '') FROM notification_events") + psql(
        "SELECT string_agg(body, '') FROM reports"
    )
    for s in secrets:
        assert s not in blob
