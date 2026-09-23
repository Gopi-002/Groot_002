"""Phase 3 on the live compose stack (steps 1-5). Opt-in: RUN_RESILIENCE=1.

Uses the DETERMINISTIC MOCK model (clearly labelled, not Claude) because no
Anthropic credentials/cost authorization are available. Everything else is
real: monitor, PostgreSQL, outbox, Redis Streams, worker leases, the restricted
ops-reader reading the real demo-app container, evidence, validation.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from collections.abc import Iterator

import pytest

from tests.resilience.helpers import API, DEMO, ROOT, dc, env_file, http, psql, wait_ready
from tests.resilience.test_phase2_pipeline import FAST, until

pytestmark = [
    pytest.mark.resilience,
    pytest.mark.skipif(os.environ.get("RUN_RESILIENCE") != "1", reason="RUN_RESILIENCE!=1"),
]

AI_KEYS = ("SENTINEL_AI_GATEWAY", "SENTINEL_AI_PAUSE_SECONDS", *FAST)


def recreate(service: str, overrides: dict[str, str]) -> None:
    env = {k: v for k, v in os.environ.items() if k not in AI_KEYS}
    env.update(overrides)
    subprocess.run(
        ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "--wait", service],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env=env,
        timeout=180,
    )


def onboard_mock() -> str:
    env = {**os.environ, "SENTINEL_AI_GATEWAY": "mock"}
    out = subprocess.run(
        ["docker", "compose", "run", "--rm", "-T", "onboard"],
        cwd=ROOT,
        input="2\n1\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=180,
    )
    return out.stdout


def inject(mode: str) -> None:
    token = {"X-Demo-Token": env_file()["DEMO_INJECTION_TOKEN"]}
    assert http("POST", f"{DEMO}/simulate-failure", {"mode": mode}, token)[0] == 200


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {env_file()['SENTINEL_API_READ_TOKEN']}"}


def new_incident(since: str) -> str:
    return psql(
        "SELECT i.id FROM incidents i JOIN services s ON s.id=i.service_id "
        f"WHERE s.name='demo-app' AND i.opened_at > '{since}' "
        "AND i.incident_type='http_error' ORDER BY i.opened_at LIMIT 1"
    )


def reset_active() -> None:
    psql(
        "UPDATE tasks SET status='failed', outcome='test_reset', lease_owner=NULL, "
        "lease_expires_at=NULL WHERE status NOT IN ('escalated','failed','resolved',"
        "'dead_lettered')"
    )
    psql(
        "UPDATE incidents SET status='closed', resolved_at=now(), resolution='manual' "
        "WHERE status NOT IN ('resolved','closed')"
    )
    psql(
        "UPDATE detection_state SET armed=true, consecutive_count=0, healthy_streak=0, "
        "streak_check_ids='{}', active_incident_id=NULL"
    )


@pytest.fixture(scope="module", autouse=True)
def mock_ai_stack() -> Iterator[None]:
    wait_ready()
    inject("none")
    reset_active()
    recreate("monitor", FAST)
    recreate("worker", {"SENTINEL_AI_GATEWAY": "mock", "SENTINEL_AI_PAUSE_SECONDS": "5"})
    out = onboard_mock()
    assert "MOCK AI GATEWAY ACTIVE" in out and "Selected model: mock-investigator-v1" in out
    yield
    inject("none")
    psql("UPDATE model_config SET is_active=false")
    reset_active()
    recreate("worker", {})
    recreate("monitor", {})


def test_subscription_disabled_in_live_onboarding():
    out = subprocess.run(
        ["docker", "compose", "run", "--rm", "-T", "onboard"],
        cwd=ROOT,
        input="1\n3\n",
        text=True,
        capture_output=True,
        timeout=180,
    ).stdout
    assert "1. Claude Subscription  [unavailable]" in out
    assert "Subscription integration unavailable for this application" in out


def test_end_to_end_failure_to_validated_investigation():
    since = psql("SELECT now()")
    inject("http_500")
    try:
        iid = until(lambda: new_incident(since), timeout=45)
        until(
            lambda: (
                psql(f"SELECT v.status FROM investigations v WHERE v.incident_id='{iid}'")
                == "completed"
            ),
            timeout=90,
        )
    finally:
        inject("none")
    tid = psql(f"SELECT id FROM tasks WHERE incident_id='{iid}'")
    # Phase 3 guarantee: the investigation stage ends in awaiting_policy with the
    # model pinned. Since Phase 4 the policy stage then continues automatically; with
    # this fixture's worker (remediation environment NOT declared) it must DENY
    # (ENV-2) and escalate - and never execute anything.
    assert psql(f"SELECT model_id FROM tasks WHERE id='{tid}'") == "mock-investigator-v1"
    assert psql(f"SELECT state FROM task_checkpoints WHERE task_id='{tid}' AND step=5") == (
        "investigation_complete"
    )
    status = psql(f"SELECT status || ',' || COALESCE(outcome,'') FROM tasks WHERE id='{tid}'")
    assert status in {"awaiting_policy,investigation_complete", "escalated,policy_denied"}
    if status.startswith("escalated"):
        assert "ENV-2" in psql(
            f"SELECT rule_ids::text FROM policy_decisions WHERE task_id='{tid}' AND decision='DENY'"
        )
    # real evidence gathered through the restricted ops-reader from the real container
    status = psql(
        f"SELECT content->'data'->>'state' FROM evidence WHERE task_id='{tid}' "
        "AND tool_name='get_container_status'"
    )
    logs = psql(
        f"SELECT content::text FROM evidence WHERE task_id='{tid}' "
        "AND tool_name='get_application_logs'"
    )
    assert status == "running"
    assert "simulated internal error" in logs  # the demo app's real log line
    assert (
        psql(
            f"SELECT count(*) FROM audit_events WHERE action='ai_tool_call' "
            f"AND details->>'task_id'='{tid}'"
        )
        == "3"
    )
    code, body = http("GET", f"{API}/v1/incidents/{iid}", headers=auth())
    inv = body["investigations"][0]
    assert code == 200 and inv["status"] == "completed" and inv["auth_mode"] == "mock"
    assert inv["result"]["proposed_action"]["action"] == "restart_demo_app"
    cited = set(inv["result"]["evidence_ids"])
    assert cited <= {e["id"] for e in body["evidence"]}
    # proposal only: nothing was executed
    assert psql(f"SELECT count(*) FROM action_attempts WHERE incident_id='{iid}'") == "0"
    assert psql(f"SELECT status FROM incidents WHERE id='{iid}'") in {
        "investigating",
        "resolved",
        "escalated",
    }


def test_missing_credentials_pause_ai_while_monitoring_continues_then_resume():
    reset_active()
    psql("UPDATE model_config SET is_active=false")
    psql(
        "INSERT INTO model_config (auth_mode, model_id, selected_by) "
        "VALUES ('api_key', 'unverified-test-model', 'resilience-test')"
    )
    recreate("worker", {"SENTINEL_AI_PAUSE_SECONDS": "5"})  # real gateway, no key stored
    since = psql("SELECT now()")
    inject("http_500")
    try:
        iid = until(lambda: new_incident(since), timeout=45)
        tid = until(lambda: psql(f"SELECT id FROM tasks WHERE incident_id='{iid}'"), timeout=15)
        until(
            lambda: (
                psql(f"SELECT outcome FROM tasks WHERE id='{tid}'")
                == "ai_paused_credentials_missing"
            ),
            timeout=60,
        )
        assert psql(f"SELECT status || ',' || attempt FROM tasks WHERE id='{tid}'") == (
            "awaiting_investigation,0"
        )  # task preserved, no attempt consumed
        checks = int(psql(f"SELECT count(*) FROM health_checks WHERE checked_at > '{since}'"))
        time.sleep(5)
        assert int(psql(f"SELECT count(*) FROM health_checks WHERE checked_at > '{since}'")) > (
            checks
        )  # monitoring continues
        req = urllib.request.Request(f"{API}/v1/metrics", headers=auth())
        with urllib.request.urlopen(req, timeout=10) as r:
            metrics = r.read().decode()
        assert 'sentinel_ai_paused_tasks{reason="ai_paused_credentials_missing"}' in metrics
        assert psql(f"SELECT count(*) FROM investigations WHERE task_id='{tid}'") == "0"
        # restore AI: mock gateway + selection -> the parked task resumes by itself
        recreate("worker", {"SENTINEL_AI_GATEWAY": "mock", "SENTINEL_AI_PAUSE_SECONDS": "5"})
        assert "Selected model: mock-investigator-v1" in onboard_mock()
        until(  # investigation completed after AI was restored (Phase 3 guarantee)
            lambda: psql(f"SELECT status FROM investigations WHERE task_id='{tid}'") == "completed",
            timeout=90,
        )
    finally:
        inject("none")
    assert psql(f"SELECT model_id FROM tasks WHERE id='{tid}'") == "mock-investigator-v1"


def test_no_secrets_in_any_service_logs():
    secrets = [
        v
        for k, v in env_file().items()
        if k
        in {
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "DEMO_INJECTION_TOKEN",
            "SENTINEL_API_READ_TOKEN",
            "SENTINEL_OPS_READER_TOKEN",
        }
        and v
    ]
    assert len(secrets) == 5
    logs = dc(
        "logs", "--no-color", "api", "monitor", "dispatcher", "worker", "ops-reader", "demo-app"
    )
    for s in secrets:
        assert s not in logs
