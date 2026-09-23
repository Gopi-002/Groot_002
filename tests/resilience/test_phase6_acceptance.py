"""Phase 6 live acceptance scenarios on the compose stack (RUN_RESILIENCE=1).

Mock model only (not Claude, no paid calls). Each scenario records sanitized
evidence (ids, decisions, counts, statuses - never secrets) to
``$PHASE6_EVIDENCE_DIR/phase6-live-scenarios.json`` when that variable is set, and
the healthy-path report body is written as the sample report.

A  healthy baseline: checks recorded, no incident
B  demo container STOPPED: 'unavailable' detected, exactly one incident
D  healthy path: detect -> investigate -> ALLOW -> one real restart -> verify ->
   resolved -> validated report -> signed notifications -> monitoring continues
E  blocked: autonomy/environment not declared -> DENY, 0 attempts, ledger unchanged
AP approval: wrong fingerprint 409, concurrent approvers -> exactly one wins,
   resume -> pre-execution re-check -> one restart -> verified
F  failed recovery (sticky failure): exactly one restart, verification fails,
   no second restart, incident open, report without recovery claim, critical alert
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.resilience.helpers import API, dc, env_file, http, psql, wait_ready
from tests.resilience.test_phase2_pipeline import FAST, until
from tests.resilience.test_phase3_ai import onboard_mock, reset_active
from tests.resilience.test_phase4_remediation import (
    APPROVAL,
    AUTO,
    BLOCKED,
    decide,
    demo_started_at,
    inject,
    ledger_count,
    operator_token,
    pending_approval,
    recreate,
    start_incident,
    task_state,
)

pytestmark = [
    pytest.mark.resilience,
    pytest.mark.skipif(os.environ.get("RUN_RESILIENCE") != "1", reason="RUN_RESILIENCE!=1"),
]
EVIDENCE: dict[str, Any] = {}


def read_h() -> dict[str, str]:
    return {"Authorization": f"Bearer {env_file()['SENTINEL_API_READ_TOKEN']}"}


def report(iid: str, timeout: float = 150) -> dict:
    def ready() -> dict | None:
        code, body = http("GET", f"{API}/v1/incidents/{iid}/report", headers=read_h())
        return body if code == 200 and body.get("status") in ("validated", "fallback") else None

    return until(ready, timeout=timeout, every=2)


def events(iid: str) -> dict[str, str]:
    out = psql(
        "SELECT string_agg(e.event_type || '=' || COALESCE(d.status,'-'), ',' "
        "ORDER BY e.created_at) "
        "FROM notification_events e LEFT JOIN notification_deliveries d ON d.event_id=e.id "
        f"AND d.channel='webhook' WHERE e.incident_id='{iid}'"
    )
    return dict(p.split("=", 1) for p in out.split(",")) if out else {}


def evidence_for(iid: str, tid: str) -> dict[str, Any]:
    q = psql
    return {
        "incident_id": iid,
        "task_id": tid,
        "incident": q(
            f"SELECT status || ':' || COALESCE(resolution,'-') FROM incidents WHERE id='{iid}'"
        ),
        "task": task_state(tid),
        "investigation": q(
            f"SELECT id || ' ' || status || ' model=' || model_id || ' auth=' || auth_mode || "
            f"' tools=' || tool_calls FROM investigations WHERE task_id='{tid}'"
        ),
        "tools_used": q(
            "SELECT string_agg(tool_name || ':' || (content->>'status'), ',' "
            "ORDER BY collected_at) "
            f"FROM evidence WHERE incident_id='{iid}' AND source='tool'"
        ),
        "policy": q(
            "SELECT string_agg(phase || '=' || decision || rule_ids::text, ' ' ORDER BY "
            f"evaluated_at) FROM policy_decisions WHERE task_id='{tid}'"
        ),
        "action": q(
            f"SELECT action_id || ' ' || status FROM action_attempts WHERE incident_id='{iid}'"
        ),
        "verification": q(
            f"SELECT status || ': ' || reason FROM verifications WHERE incident_id='{iid}'"
        ),
        "audit_actions": q(
            "SELECT string_agg(DISTINCT action, ',') FROM audit_events WHERE entity_id IN "
            f"(SELECT '{iid}'::uuid UNION SELECT '{tid}'::uuid "
            "UNION SELECT id FROM action_attempts "
            f"WHERE incident_id='{iid}' UNION SELECT id FROM reports WHERE incident_id='{iid}')"
        ),
    }


def save_evidence() -> None:
    d = os.environ.get("PHASE6_EVIDENCE_DIR")
    if not d:
        return
    Path(d).mkdir(parents=True, exist_ok=True)
    (Path(d) / "phase6-live-scenarios.json").write_text(json.dumps(EVIDENCE, indent=2))


@pytest.fixture(scope="module", autouse=True)
def stack() -> Iterator[None]:
    wait_ready()
    inject("none")
    reset_active()
    recreate("monitor", FAST)
    recreate("executor", {"EXEC_MAX_RESTARTS_PER_HOUR": "20"})
    recreate("worker", AUTO)
    assert "Selected model: mock-investigator-v1" in onboard_mock()
    EVIDENCE["captured_at"] = psql("SELECT now()")
    EVIDENCE["model"] = "mock-investigator-v1 (TEST/DEMO ONLY - not Claude)"
    yield
    save_evidence()
    inject("none")
    psql("UPDATE model_config SET is_active=false")
    reset_active()
    for svc in ("executor", "worker", "monitor"):
        recreate(svc, {})


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    reset_active()
    inject("none")
    time.sleep(3)
    yield
    inject("none")


def test_a_healthy_baseline_records_checks_and_opens_no_incident():
    ts = psql("SELECT now()")
    time.sleep(12)
    checks = int(psql(f"SELECT count(*) FROM health_checks WHERE checked_at > '{ts}'"))
    outcomes = psql(
        f"SELECT string_agg(DISTINCT outcome, ',') FROM health_checks WHERE checked_at > '{ts}'"
    )
    incidents = int(psql(f"SELECT count(*) FROM incidents WHERE opened_at > '{ts}'"))
    assert checks >= 4 and outcomes == "healthy" and incidents == 0
    EVIDENCE["A_healthy_baseline"] = {
        "checks": checks,
        "outcomes": outcomes,
        "incidents": incidents,
    }


def test_b_demo_container_down_opens_exactly_one_unavailable_incident():
    recreate("worker", BLOCKED)  # no restart: only detection is under test
    try:
        ts = psql("SELECT now()")
        dc("stop", "demo-app")
        iid = until(
            lambda: psql(
                "SELECT string_agg(i.id::text, ',') FROM incidents i JOIN services s ON "
                f"s.id=i.service_id WHERE s.name='demo-app' AND i.opened_at > '{ts}'"
            ),
            timeout=60,
        )
        time.sleep(10)  # failures keep coming: still exactly one incident
        rows = psql(
            "SELECT count(*) || ' ' || string_agg(incident_type || ':' || occurrence_count, ',') "
            "FROM incidents i JOIN services s ON s.id=i.service_id WHERE s.name='demo-app' "
            f"AND i.opened_at > '{ts}'"
        )
        n, detail = rows.split(" ", 1)
        assert n == "1" and detail.startswith("unavailable:")
        assert int(detail.split(":")[1]) > 3  # later failures only bumped the count
        EVIDENCE["B_container_down"] = {"incident_id": iid, "incidents": int(n), "detail": detail}
    finally:
        dc("start", "demo-app")
        recreate("worker", AUTO)


def test_d_healthy_path_end_to_end_with_evidence():
    before = demo_started_at()
    ledger_before = ledger_count()
    ts = psql("SELECT now()")
    iid, tid = start_incident()
    until(lambda: task_state(tid) == "resolved:recovery_verified", timeout=150)
    rep = report(iid)
    assert rep["status"] == "validated" and rep["report"]["version"] == 1
    until(lambda: events(iid).get("report_ready") == "delivered", timeout=60)
    ev = evidence_for(iid, tid)
    ev.update(
        restarts_executed=ledger_count() - ledger_before,
        container_restarted=demo_started_at() != before,
        report={
            k: rep["report"][k]
            for k in ("id", "version", "generation_mode", "model_id", "auth_mode", "record_sha256")
        },
        notifications_webhook=events(iid),
        checks_after_resolution=0,
    )
    assert ev["restarts_executed"] == 1 and ev["container_restarted"]
    assert ev["policy"] == "proposal=ALLOW{AUT-1} pre_execution=ALLOW{AUT-1}"
    assert ev["incident"] == "resolved:remediated" and ev["verification"].startswith("passed")
    assert ev["notifications_webhook"]["remediation_performed"] == "delivered"
    assert {
        "incident_opened",
        "policy_decision",
        "action_execution_intent",
        "action_succeeded",
        "recovery_verified",
        "report_generated",
    } <= set(ev["audit_actions"].split(","))
    t2 = psql("SELECT now()")
    until(
        lambda: int(psql(f"SELECT count(*) FROM health_checks WHERE checked_at > '{t2}'")) >= 3,
        timeout=30,
    )
    ev["checks_after_resolution"] = int(
        psql(f"SELECT count(*) FROM health_checks WHERE checked_at > '{t2}'")
    )
    ev["detection_to_resolution_seconds"] = float(
        psql(f"SELECT extract(epoch FROM resolved_at - opened_at) FROM incidents WHERE id='{iid}'")
    )
    ev["injected_at_or_after"] = ts
    EVIDENCE["D_healthy_path"] = ev
    d = os.environ.get("PHASE6_EVIDENCE_DIR")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        (Path(d) / "healthy-path-report.md").write_text(rep["report"]["body"])


def test_e_blocked_action_denied_with_no_side_effect_and_accurate_report():
    recreate("worker", BLOCKED)
    try:
        before, total = demo_started_at(), ledger_count()
        iid, tid = start_incident()
        until(lambda: task_state(tid) == "escalated:policy_denied", timeout=120)
        rep = report(iid)
        ev = evidence_for(iid, tid)
        assert "DENY" in ev["policy"] and "ENV-2" in ev["policy"]
        assert psql(f"SELECT count(*) FROM action_attempts WHERE incident_id='{iid}'") == "0"
        assert ledger_count() == total and demo_started_at() == before
        assert ev["incident"].startswith("escalated")
        body = rep["report"]["body"]
        assert "- no action was executed" in body and "OPEN, owned by a human" in body
        until(lambda: events(iid).get("incident_escalated") == "delivered", timeout=60)
        ev.update(
            action_attempts=0,
            ledger_delta=ledger_count() - total,
            report_status=rep["status"],
            notifications_webhook=events(iid),
        )
        EVIDENCE["E_blocked"] = ev
    finally:
        recreate("worker", AUTO)


def test_approval_wrong_fingerprint_concurrent_approvers_then_one_restart():
    recreate("worker", APPROVAL)
    try:
        before, total = demo_started_at(), ledger_count()
        iid, tid = start_incident()
        approval_id, fp = pending_approval(tid)
        until(lambda: events(iid).get("approval_required") == "delivered", timeout=60)
        tokens = [operator_token() for _ in range(4)]
        assert decide(approval_id, "0" * 64, tokens[0]) == 409  # wrong fingerprint
        results: list[int] = []
        threads = [
            threading.Thread(target=lambda t=t: results.append(decide(approval_id, fp, t)))
            for t in tokens
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert sorted(results) == [200, 409, 409, 409]  # exactly one decision wins
        until(lambda: task_state(tid) == "resolved:recovery_verified", timeout=150)
        assert decide(approval_id, fp, tokens[0]) == 409  # late / replayed decision
        ev = evidence_for(iid, tid)
        assert (
            "REQUIRE_APPROVAL{APR-0}" in ev["policy"]
            and "pre_execution=ALLOW{APR-1}" in ev["policy"]
        )
        assert ledger_count() - total == 1 and demo_started_at() != before
        rep = report(iid)
        decider = psql(f"SELECT decided_by FROM approvals WHERE id='{approval_id}'")
        assert f"decided by `{decider}`" in rep["report"]["body"]
        ev.update(
            approval_id=approval_id,
            concurrent_results=sorted(results),
            restarts_executed=ledger_count() - total,
            report_status=rep["status"],
        )
        EVIDENCE["AP_approval"] = ev
    finally:
        recreate("worker", AUTO)


def test_f_failed_recovery_one_restart_no_recovery_claim_critical_alert():
    total = ledger_count()
    iid, tid = start_incident_sticky()
    try:
        until(lambda: task_state(tid) == "escalated:recovery_failed", timeout=180)
        time.sleep(10)  # no second restart may follow
        ev = evidence_for(iid, tid)
        assert ledger_count() - total == 1
        assert ev["verification"].startswith("failed") and ev["incident"].startswith("escalated")
        rep = report(iid)
        body = rep["report"]["body"]
        summary = body.split("## Summary")[1].split("##")[0]
        assert "recovered" not in summary and "resolved as remediated" not in body
        assert "Recovery verification failed" in body
        until(lambda: events(iid).get("recovery_verification_failed") == "delivered", timeout=60)
        sev = psql(
            f"SELECT severity FROM notification_events WHERE incident_id='{iid}' "
            "AND event_type='recovery_verification_failed'"
        )
        assert sev == "critical"
        ev.update(
            restarts_executed=ledger_count() - total,
            report_status=rep["status"],
            notification_severity=sev,
            notifications_webhook=events(iid),
        )
        EVIDENCE["F_failed_recovery"] = ev
    finally:
        inject("none")


def start_incident_sticky() -> tuple[str, str]:
    token = {"X-Demo-Token": env_file()["DEMO_INJECTION_TOKEN"]}
    since = psql("SELECT now()")
    assert (
        http(
            "POST",
            "http://127.0.0.1:8001/simulate-failure",
            {"mode": "http_500", "sticky": True},
            token,
        )[0]
        == 200
    )
    iid = until(
        lambda: psql(
            "SELECT i.id FROM incidents i JOIN services s ON s.id=i.service_id WHERE "
            f"s.name='demo-app' AND i.opened_at > '{since}' "
            "AND i.incident_type='http_error' LIMIT 1"
        ),
        timeout=45,
    )
    tid = until(lambda: psql(f"SELECT id FROM tasks WHERE incident_id='{iid}'"), timeout=20)
    return iid, tid
