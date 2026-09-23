"""Durable notifications, deterministic alerts, degraded-mode status and
Phase 5 metrics against real PostgreSQL. Webhook deliveries go to an in-process
mock transport (no network)."""

from __future__ import annotations

import json
import random
import re
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from app.api.health import ReadinessChecks
from app.api.main import create_app
from app.notifications import alerts
from app.notifications.channels import DeliveryResult, LogChannel, WebhookChannel
from app.notifications.delivery import claim_due, fan_out, record_result
from app.notifications.events import enqueue
from app.notifications.service import Notifier
from app.observability.metrics import BOUNDED_LABELS
from tests.integration.helpers import one, open_task, rows

pytestmark = pytest.mark.integration
READ = "r" * 40
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Receiver:
    """In-process webhook receiver with a switchable failure mode."""

    def __init__(self) -> None:
        self.status = 200
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status == 0:
            raise httpx.ConnectError("provider down")
        return httpx.Response(self.status)

    def keys(self) -> list[str]:
        return [r.headers["Idempotency-Key"] for r in self.requests]


@pytest.fixture
def ns(base_env):
    from app.config import Settings

    return Settings(
        notify_channels=("log", "webhook"),
        notify_webhook_url="https://hooks.example.com/x",
        notify_webhook_allowed_hosts=("hooks.example.com",),
        notify_webhook_secret=SecretStr("w" * 40),
        notify_max_attempts=3,
        notify_retry_base_seconds=1,
        notify_retry_max_seconds=2,
        alert_eval_seconds=1,
        api_read_token=SecretStr(READ),
    )


@pytest.fixture(autouse=True)
def quiet(engine):
    """Shared module DB: finish other tests' deliveries so each test sees its own."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE notification_events SET fanned_out_at=now() WHERE fanned_out_at IS NULL")
        )
        conn.execute(
            text(
                "UPDATE notification_deliveries SET status='delivered', delivered_at=now(), "
                "lease_owner=NULL, lease_expires_at=NULL WHERE status IN ('pending','sending')"
            )
        )


def notifier(engine, ns, receiver, name="n1"):
    web = WebhookChannel(
        ns.notify_webhook_url,
        ns.notify_webhook_secret,
        client=httpx.Client(transport=httpx.MockTransport(receiver.handler)),
    )
    return Notifier(
        ns, engine, {"log": LogChannel(), "webhook": web}, instance=name, rng=random.Random(1)
    )


def new_event(engine, event_type="test", key=None, **payload):
    with engine.begin() as conn:
        return enqueue(
            conn,
            event_type=event_type,
            severity="info",
            dedup_key=key or f"t:{uuid.uuid4()}",
            payload={"title": "t", **payload},
        )


def deliveries(engine, eid):
    return {
        r["channel"]: r
        for r in rows(engine, "SELECT * FROM notification_deliveries WHERE event_id=:e", e=eid)
    }


def make_due(engine):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE notification_deliveries SET next_attempt_at=now() WHERE status='pending'")
        )


# --- delivery -----------------------------------------------------------------------------------


def test_event_is_delivered_once_per_channel_signed_and_audited(engine, ns):
    rec = Receiver()
    eid = new_event(engine)
    n = notifier(engine, ns, rec)
    n.cycle()
    d = deliveries(engine, eid)
    assert {k: v["status"] for k, v in d.items()} == {"log": "delivered", "webhook": "delivered"}
    assert rec.keys() == [str(eid)]
    assert rec.requests[0].headers["X-SentinelOps-Signature"].startswith("sha256=")
    n.cycle()  # nothing left to do: no duplicate sends
    assert len(rec.requests) == 1
    assert (
        one(
            engine,
            "SELECT count(*) FROM audit_events WHERE action='notification_delivered' "
            "AND entity_id=:e",
            e=eid,
        )
        == 2
    )


def test_duplicate_events_are_deduplicated(engine, ns):
    key = f"dup:{uuid.uuid4()}"
    first = new_event(engine, key=key)
    assert new_event(engine, key=key) is None
    rec = Receiver()
    n = notifier(engine, ns, rec)
    n.cycle()
    n.cycle()
    assert rec.keys() == [str(first)]
    assert one(engine, "SELECT count(*) FROM notification_events WHERE dedup_key=:k", k=key) == 1
    # fanning out again never creates a second delivery row
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE notification_events SET fanned_out_at=NULL WHERE id=:e"), {"e": first}
        )
    fan_out(engine, ["log", "webhook"], 3)
    assert len(deliveries(engine, first)) == 2


def test_provider_outage_retries_with_backoff_then_delivers(engine, ns):
    rec = Receiver()
    rec.status = 503
    eid = new_event(engine)
    n = notifier(engine, ns, rec)
    n.cycle()
    w = deliveries(engine, eid)["webhook"]
    assert (w["status"], w["attempt"], w["last_error"], w["last_http_status"]) == (
        "pending",
        1,
        "http_503",
        503,
    )
    assert deliveries(engine, eid)["log"]["status"] == "delivered"  # other channels unaffected
    n.cycle()  # not due yet: backoff respected
    assert len(rec.requests) == 1
    rec.status = 200
    make_due(engine)
    n.cycle()
    w = deliveries(engine, eid)["webhook"]
    assert (w["status"], w["attempt"]) == ("delivered", 2)
    assert rec.keys() == [str(eid), str(eid)]  # same idempotency key on the retry


def test_retries_are_bounded_then_dead_lettered_and_alerted(engine, ns):
    rec = Receiver()
    rec.status = 0  # connection refused
    eid = new_event(engine)
    n = notifier(engine, ns, rec)
    for _ in range(4):
        n.cycle()
        make_due(engine)
    w = deliveries(engine, eid)["webhook"]
    assert (w["status"], w["attempt"]) == ("dead_lettered", 3)
    assert len(rec.requests) == 3
    assert (
        one(
            engine,
            "SELECT count(*) FROM audit_events WHERE action='notification_dead_lettered' "
            "AND entity_id=:e",
            e=eid,
        )
        == 1
    )
    assert alerts.evaluate(engine, ns)["notifications_failing"] is True


def test_client_errors_are_not_retried(engine, ns):
    rec = Receiver()
    rec.status = 400
    eid = new_event(engine)
    notifier(engine, ns, rec).cycle()
    assert deliveries(engine, eid)["webhook"]["status"] == "dead_lettered"
    assert len(rec.requests) == 1


def test_crash_after_send_before_record_redelivers_same_idempotency_key(engine, ns):
    rec = Receiver()
    eid = new_event(engine)
    fan_out(engine, ["webhook"], 3)
    (d,) = [x for x in claim_due(engine, "crashy", 30) if x.event_id == eid]
    web = notifier(engine, ns, rec).channels["webhook"]
    assert web.send(d.message).ok  # the provider got it ... then the notifier died
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE notification_deliveries SET lease_expires_at = now() - interval '1 s' "
                "WHERE event_id=:e"
            ),
            {"e": eid},
        )
    notifier(engine, ns, rec, "n2").cycle()
    assert deliveries(engine, eid)["webhook"]["status"] == "delivered"
    assert rec.keys() == [str(eid), str(eid)]  # at-least-once; receiver de-duplicates
    # the stale sender cannot overwrite the newer attempt's outcome
    assert (
        record_result(
            engine,
            d,
            DeliveryResult(False, True, 500, "late"),
            retry_base=1,
            retry_max=2,
            rng=random.Random(0),
        )
        == "lease_lost"
    )
    assert deliveries(engine, eid)["webhook"]["status"] == "delivered"


def test_pending_notifications_survive_notifier_restart(engine, ns):
    rec = Receiver()
    rec.status = 503
    eid = new_event(engine)
    notifier(engine, ns, rec, "before-restart").cycle()
    rec.status = 200
    make_due(engine)
    notifier(engine, ns, rec, "after-restart").cycle()  # a brand-new process
    assert deliveries(engine, eid)["webhook"]["status"] == "delivered"


def test_concurrent_notifiers_send_each_delivery_once(engine, ns):
    import threading

    rec = Receiver()
    ids = [new_event(engine) for _ in range(12)]
    n1, n2 = notifier(engine, ns, rec, "a"), notifier(engine, ns, rec, "b")
    fan_out(engine, ["log", "webhook"], 3)
    t = [threading.Thread(target=n.cycle) for n in (n1, n2)]
    for x in t:
        x.start()
    for x in t:
        x.join()
    n1.cycle()
    sent = [k for k in rec.keys() if uuid.UUID(k) in ids]
    assert sorted(sent) == sorted(str(i) for i in ids)


def test_payloads_never_contain_secrets(engine, ns):
    secrets = ["w" * 40, READ, "sk-ant-api03-ZZZ", "sop_Zzzzzzzzzzzzzzzzzzzzzzzz"]
    new_event(engine, summary="token=sk-ant-api03-ZZZ Bearer sop_Zzzzzzzzzzzzzzzzzzzzzzzz")
    rec = Receiver()
    notifier(engine, ns, rec).cycle()
    blob = json.dumps(
        [r["payload"] for r in rows(engine, "SELECT payload FROM notification_events")]
    )
    blob += "".join(r.content.decode() for r in rec.requests)
    for sec in secrets:
        assert sec not in blob


# --- alerts ------------------------------------------------------------------------------------


def alert_events(engine, name):
    return rows(
        engine,
        "SELECT event_type, severity FROM notification_events WHERE payload->>'alert' = :a "
        "ORDER BY created_at",
        a=name,
    )


def test_alerts_fire_once_and_resolve_once(engine, ns):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM alert_state WHERE name='backup_failed'"))
        conn.execute(text("INSERT INTO backup_runs (status, error) VALUES ('failed', 'disk full')"))
    assert alerts.evaluate(engine, ns)["backup_failed"] is True
    alerts.evaluate(engine, ns)  # still firing: no duplicate notification
    assert [e["event_type"] for e in alert_events(engine, "backup_failed")] == ["system_degraded"]
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO backup_runs (status, completed_at) VALUES ('succeeded', now())")
        )
    assert alerts.evaluate(engine, ns)["backup_failed"] is False
    assert [e["event_type"] for e in alert_events(engine, "backup_failed")] == [
        "system_degraded",
        "alert_resolved",
    ]


def test_monitor_silence_and_queue_backlog_alerts(engine, ns):
    from tests.integration.helpers import new_service

    sid = new_service(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM health_checks"))
        conn.execute(
            text(
                "INSERT INTO health_checks (service_id, checked_at, outcome) "
                "VALUES (:s, now() - interval '10 minutes', 'healthy')"
            ),
            {"s": sid},
        )
        conn.execute(
            text(
                "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, dedup_key, "
                "next_attempt_at) VALUES ('task', gen_random_uuid(), 'task.dispatch', :k, "
                "now() - interval '10 minutes')"
            ),
            {"k": f"stuck:{uuid.uuid4()}"},
        )
    fired = alerts.evaluate(engine, ns)
    assert fired["monitor_silent"] and fired["queue_backlog"]
    assert alert_events(engine, "monitor_silent")[0]["severity"] == "critical"
    with engine.begin() as conn:
        conn.execute(text("UPDATE outbox_events SET published_at=now() WHERE published_at IS NULL"))
        conn.execute(
            text("INSERT INTO health_checks (service_id, outcome) VALUES (:s, 'healthy')"),
            {"s": sid},
        )
    fired = alerts.evaluate(engine, ns)
    assert not fired["monitor_silent"] and not fired["queue_backlog"]


def test_expired_ai_auth_yields_actionable_alert_and_degraded_status(engine, ns):
    _, tid = open_task(engine)
    with engine.begin() as conn:
        conn.execute(  # legal edges only (the DB guards the lifecycle): queued->running->parked
            text(
                "UPDATE tasks SET status='running', lease_owner='x', "
                "lease_expires_at=now() + interval '1 minute' WHERE id=:t"
            ),
            {"t": tid},
        )
        conn.execute(
            text(
                "UPDATE tasks SET status='awaiting_investigation', lease_owner=NULL, "
                "lease_expires_at=NULL, outcome='ai_paused_authentication_failed', attempt=0 "
                "WHERE id=:t"
            ),
            {"t": tid},
        )
    fired = alerts.evaluate(engine, ns)
    assert fired["ai_auth_failed"] and fired["ai_paused"]
    ev = rows(
        engine,
        "SELECT event_type, payload FROM notification_events "
        "WHERE payload->>'alert'='ai_auth_failed' "
        "ORDER BY created_at DESC LIMIT 1",
    )[0]
    assert ev["event_type"] == "ai_paused"
    assert "onboard set-key" in json.dumps(ev["payload"])
    api = TestClient(create_app(ns, ReadinessChecks({}), engine=engine, redis_client=None))
    st = api.get("/v1/system/status", headers={"Authorization": f"Bearer {READ}"}).json()
    assert st["overall"] == "degraded" and "AI_DEGRADED" in st["modes"]
    assert st["components"]["database"]["status"] == "ok"
    assert "REDIS_UNAVAILABLE" in st["modes"]  # no redis client in this test app
    with engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET status='failed' WHERE id=:t"), {"t": tid})
    assert not alerts.evaluate(engine, ns)["ai_auth_failed"]


def test_stalled_incident_and_budget_alerts(engine, ns):
    iid, tid = open_task(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE incidents SET opened_at = now() - interval '3 hours', "
                "last_seen_at = now() - interval '3 hours' WHERE id=:i"
            ),
            {"i": iid},
        )
        conn.execute(text("ALTER TABLE tasks DISABLE TRIGGER trg_tasks_updated_at"))
        conn.execute(
            text("UPDATE tasks SET updated_at = now() - interval '3 hours' WHERE id=:t"), {"t": tid}
        )
        conn.execute(text("ALTER TABLE tasks ENABLE TRIGGER trg_tasks_updated_at"))
    assert alerts.evaluate(engine, ns)["stalled_incident"] is True
    with engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET status='failed' WHERE id=:t"), {"t": tid})
        conn.execute(
            text("UPDATE incidents SET status='closed', resolved_at=now() WHERE id=:i"), {"i": iid}
        )
        conn.execute(
            text(
                "INSERT INTO ai_usage (stage, model_id, auth_mode, outcome, input_tokens, "
                "latency_ms) "
                "VALUES ('investigation', 'mock-investigator-v1', 'mock', 'ok', 5000, 1)"
            )
        )
    ns2 = ns.model_copy(update={"ai_daily_max_tokens": 1000})
    assert alerts.evaluate(engine, ns2)["ai_budget_exhausted"] is True


# --- metrics --------------------------------------------------------------------------------------


REQUIRED = [
    "sentinel_monitor_success_ratio",
    "sentinel_detection_latency_seconds",
    "sentinel_incidents_detected_total",
    "sentinel_incidents_active",
    "sentinel_incidents_resolved_total",
    "sentinel_incidents_escalated_total",
    "sentinel_ai_calls_total",
    "sentinel_ai_usage_tokens_total",
    "sentinel_ai_call_latency_seconds_sum",
    "sentinel_ai_auth_errors_total",
    "sentinel_ai_paused_tasks",
    "sentinel_policy_decisions_total",
    "sentinel_policy_rule_hits_total",
    "sentinel_actions_total",
    "sentinel_duplicate_actions_prevented_total",
    "sentinel_verifications_total",
    "sentinel_verification_duration_seconds_sum",
    "sentinel_recovery_rate",
    "sentinel_queue_lag",
    "sentinel_task_retries_total",
    "sentinel_tasks_dead_lettered_total",
    "sentinel_outbox_unpublished",
    "sentinel_reports_total",
    "sentinel_report_jobs",
    "sentinel_report_validation_rejections_total",
    "sentinel_report_ai_failures_total",
    "sentinel_notification_deliveries",
    "sentinel_notification_retries_total",
    "sentinel_notifications_dead_lettered_total",
    "sentinel_alerts_firing",
    "sentinel_backup_last_success_age_seconds",
    "sentinel_service_heartbeat_age_seconds",
    "sentinel_api_auth_failures_total",
]


def test_metrics_exposed_with_bounded_labels(engine, ns):
    new_event(engine)
    notifier(engine, ns, Receiver()).cycle()
    api = TestClient(create_app(ns, ReadinessChecks({}), engine=engine, redis_client=None))
    assert api.get("/v1/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    body = api.get("/v1/metrics", headers={"Authorization": f"Bearer {READ}"}).text
    names = {
        line.split("{")[0].split(" ")[0]
        for line in body.splitlines()
        if line.startswith("sentinel_")
    }
    missing = [m for m in REQUIRED if m not in names]
    assert not missing, missing
    assert 'sentinel_api_auth_failures_total{kind="reader"}' in body
    for line in body.splitlines():
        if line.startswith("sentinel_") and "{" in line:
            labels = dict(re.findall(r'(\w+)="([^"]*)"', line.split("{", 1)[1].rsplit("}", 1)[0]))
            for k, v in labels.items():
                assert k in BOUNDED_LABELS, (k, line)
                allowed = BOUNDED_LABELS[k]
                assert allowed is None or v in allowed, line
                assert not UUID_RE.search(v), f"unbounded id label: {line}"
