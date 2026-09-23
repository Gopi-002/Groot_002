"""Phase 2 end-to-end on the live compose stack (steps 1-3). Opt-in: RUN_RESILIENCE=1.

The monitor is recreated with an accelerated cadence (2 s interval, 1.5 s probe
timeout, 1 s latency threshold) so detection takes seconds, not minutes; the
default 30 s / 5 s / 2 s contract is restored afterwards.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests.resilience.helpers import API, DEMO, dc, env_file, http, psql, wait_ready

pytestmark = [
    pytest.mark.resilience,
    pytest.mark.skipif(os.environ.get("RUN_RESILIENCE") != "1", reason="RUN_RESILIENCE!=1"),
]

FAST = {
    "SENTINEL_MONITOR_INTERVAL_SECONDS": "2",
    "SENTINEL_PROBE_TIMEOUT_SECONDS": "1.5",
    "SENTINEL_LATENCY_THRESHOLD_SECONDS": "1",
}


def recreate_monitor(overrides: dict[str, str]) -> None:
    env = {**os.environ, **overrides}
    for k in FAST:
        if k not in overrides:
            env.pop(k, None)
    import subprocess

    subprocess.run(
        ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "--wait", "monitor"],
        check=True,
        capture_output=True,
        env=env,
        timeout=180,
    )


def until(fn: Callable[[], Any], timeout: float = 60, every: float = 1) -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def inject(mode: str) -> None:
    token = {"X-Demo-Token": env_file()["DEMO_INJECTION_TOKEN"]}
    assert http("POST", f"{DEMO}/simulate-failure", {"mode": mode}, token)[0] == 200


def q(sql: str) -> str:
    return psql(sql)


def demo_incidents(since: str, itype: str | None = None) -> list[str]:
    type_clause = f"AND i.incident_type='{itype}'" if itype else ""
    out = q(
        "SELECT i.id FROM incidents i JOIN services s ON s.id=i.service_id "
        f"WHERE s.name='demo-app' AND i.opened_at > '{since}' {type_clause} "
        "ORDER BY i.opened_at"
    )
    return [line for line in out.splitlines() if line]


def task_status(incident_id: str) -> str:
    return q(f"SELECT status FROM tasks WHERE incident_id='{incident_id}'")


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {env_file()['SENTINEL_API_READ_TOKEN']}"}


@pytest.fixture(scope="module", autouse=True)
def fast_monitor() -> Iterator[None]:
    wait_ready()
    inject("none")
    # close anything a previous run left active so detection starts clean
    q(
        "UPDATE tasks SET status='failed', outcome='test_reset', lease_owner=NULL, "
        "lease_expires_at=NULL WHERE status NOT IN ('escalated','failed','resolved',"
        "'dead_lettered')"
    )
    q(
        "UPDATE incidents SET status='closed', resolved_at=now(), resolution='manual' "
        "WHERE status NOT IN ('resolved','closed')"
    )
    q(
        "UPDATE detection_state SET armed=true, consecutive_count=0, healthy_streak=0, "
        "streak_check_ids='{}', active_incident_id=NULL"
    )
    recreate_monitor(FAST)
    yield
    inject("none")
    recreate_monitor({})


STATE: dict[str, str] = {}  # incident ids shared between ordered tests in this module


def now_db() -> str:
    return q("SELECT now()")


def test_all_phase2_services_healthy():
    ps = dc("ps", "--format", "{{.Service}} {{.Status}}")
    for svc in ("api", "monitor", "dispatcher", "worker", "demo-app", "postgres", "redis"):
        line = next(ln for ln in ps.splitlines() if ln.startswith(svc + " "))
        assert "(healthy)" in line, line


def test_failure_detected_once_dispatched_and_not_duplicated():
    since = now_db()
    inject("http_500")
    try:
        ids = until(lambda: demo_incidents(since, "http_error"), timeout=45)
        assert len(ids) == 1
        iid = ids[0]
        STATE["first_http_error"] = iid
        until(lambda: task_status(iid) == "awaiting_investigation", timeout=30)
        assert (
            q(
                f"SELECT count(*) FROM outbox_events o JOIN tasks t ON t.id=o.aggregate_id "
                f"WHERE t.incident_id='{iid}' AND o.published_at IS NOT NULL"
            )
            == "1"
        )
        first_count = int(q(f"SELECT occurrence_count FROM incidents WHERE id='{iid}'"))
        time.sleep(8)  # ~4 more failing checks
        assert demo_incidents(since, "http_error") == [iid]  # no duplicate
        assert int(q(f"SELECT occurrence_count FROM incidents WHERE id='{iid}'")) > first_count
        code, body = http("GET", f"{API}/v1/incidents/{iid}", headers=auth())
        assert code == 200 and body["incident"]["status"] == "open"
        assert body["tasks"][0]["status"] == "awaiting_investigation"
        assert http("GET", f"{API}/v1/incidents/{iid}")[0] == 401
    finally:
        inject("none")


def test_recovery_rearm_then_later_separate_incident():
    first = STATE["first_http_error"]  # opened by the previous test in THIS run
    until(
        lambda: q(f"SELECT resolution FROM incidents WHERE id='{first}'") == "auto_recovered",
        timeout=30,
    )
    assert task_status(first) == "resolved"
    since = now_db()
    inject("http_500")
    try:
        ids = until(lambda: demo_incidents(since, "http_error"), timeout=45)
        assert ids and ids[0] != first
    finally:
        inject("none")
    until(lambda: q(f"SELECT status FROM incidents WHERE id='{ids[0]}'") == "resolved", timeout=30)


def test_timeout_mode_detected_as_unavailable():
    since = now_db()
    inject("timeout")
    try:
        ids = until(lambda: demo_incidents(since, "unavailable"), timeout=60)
        assert len(ids) == 1
    finally:
        inject("none")
    until(lambda: q(f"SELECT status FROM incidents WHERE id='{ids[0]}'") == "resolved", timeout=45)


def test_redis_outage_db_retains_work_then_delivers():
    since = now_db()
    dc("stop", "redis")
    try:
        inject("http_500")
        ids = until(lambda: demo_incidents(since, "http_error"), timeout=45)
        iid = ids[0]
        # monitoring and incident creation continue; publication is retried from the DB
        until(
            lambda: q(
                f"SELECT publish_attempts FROM outbox_events o JOIN tasks t "
                f"ON t.id=o.aggregate_id WHERE t.incident_id='{iid}' "
                "AND o.published_at IS NULL AND o.publish_attempts > 0"
            ),
            timeout=30,
        )
        assert task_status(iid) == "queued"
        assert http("GET", f"{API}/health/ready", timeout=10)[0] == 503
        checks_before = int(q(f"SELECT count(*) FROM health_checks WHERE checked_at > '{since}'"))
        time.sleep(4)
        assert int(q(f"SELECT count(*) FROM health_checks WHERE checked_at > '{since}'")) > (
            checks_before
        )
    finally:
        dc("start", "redis")
    until(lambda: task_status(iid) == "awaiting_investigation", timeout=90)
    inject("none")
    until(lambda: q(f"SELECT status FROM incidents WHERE id='{iid}'") == "resolved", timeout=45)


def test_worker_down_then_task_recovered_on_restart():
    since = now_db()
    dc("stop", "worker")
    try:
        inject("http_500")
        iid = until(lambda: demo_incidents(since, "http_error"), timeout=45)[0]
        until(
            lambda: (
                q(
                    f"SELECT count(*) FROM outbox_events o JOIN tasks t "
                    f"ON t.id=o.aggregate_id WHERE t.incident_id='{iid}' "
                    "AND o.published_at IS NOT NULL"
                )
                == "1"
            ),
            timeout=30,
        )
        time.sleep(3)
        assert task_status(iid) == "queued"  # published, nobody consuming
    finally:
        dc("start", "worker")
    until(lambda: task_status(iid) == "awaiting_investigation", timeout=60)
    inject("none")
    until(lambda: q(f"SELECT status FROM incidents WHERE id='{iid}'") == "resolved", timeout=45)


def test_monitor_keeps_probing_through_database_outage():
    """Probing never stalls on the DB; checks taken during the outage are recorded
    afterwards with their original timestamps."""
    from datetime import UTC, datetime

    outage_start = datetime.now(UTC).isoformat()
    dc("stop", "postgres")
    try:
        time.sleep(10)  # ~5 probes at the 2 s test cadence
        outage_end = datetime.now(UTC).isoformat()
        logs = dc("logs", "--no-color", "--since", "12s", "monitor")
        assert "buffered for retry" in logs or "presumed leader" in logs
        assert "unhealthy" not in dc("ps", "--format", "{{.Status}}", "monitor")
    finally:
        dc("start", "postgres")
    wait_ready()
    during = (
        f"SELECT count(*) FROM health_checks WHERE checked_at > '{outage_start}' "
        f"AND checked_at < '{outage_end}'"
    )
    until(lambda: int(q(during)) >= 3, timeout=90)


def test_queue_metrics_exposed():
    import urllib.request

    req = urllib.request.Request(f"{API}/v1/metrics", headers=auth())
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
    assert 'sentinel_incidents_detected_total{incident_type="http_error"}' in body
    assert 'sentinel_incidents_detected_total{incident_type="unavailable"}' in body
    assert "sentinel_redis_up 1" in body
    age = next(ln for ln in body.splitlines() if ln.startswith("sentinel_last_check_age_seconds"))
    assert 0 <= float(age.split()[1]) < 75
