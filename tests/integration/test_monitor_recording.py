"""Workflow steps 1-3 against real PostgreSQL."""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.monitoring import recorder
from app.monitoring.recorder import prune_health_checks, record_check
from tests.integration.helpers import STALE, TH, Clock, check, feed, new_service, one, rows

pytestmark = pytest.mark.integration


def counts(engine, sid):
    return {
        "checks": one(engine, "SELECT count(*) FROM health_checks WHERE service_id=:s", s=sid),
        "incidents": one(engine, "SELECT count(*) FROM incidents WHERE service_id=:s", s=sid),
        "tasks": one(
            engine,
            "SELECT count(*) FROM tasks t JOIN incidents i ON i.id=t.incident_id "
            "WHERE i.service_id=:s",
            s=sid,
        ),
        "outbox": one(
            engine,
            "SELECT count(*) FROM outbox_events o JOIN tasks t "
            "ON t.id=o.aggregate_id JOIN incidents i ON i.id=t.incident_id "
            "WHERE i.service_id=:s",
            s=sid,
        ),
    }


def test_every_check_persisted_healthy_creates_nothing(engine):
    sid = new_service(engine)
    feed(engine, sid, ["healthy"] * 5, Clock())
    assert counts(engine, sid) == {"checks": 5, "incidents": 0, "tasks": 0, "outbox": 0}
    ts = rows(engine, "SELECT checked_at FROM health_checks WHERE service_id=:s", s=sid)
    assert all(r["checked_at"].utcoffset() == timedelta(0) for r in ts)


def test_three_failures_create_exactly_one_incident_task_and_outbox(engine):
    sid = new_service(engine)
    outs = feed(engine, sid, ["unhealthy"] * 3, Clock())
    assert [len(o.opened_incidents) for o in outs] == [0, 0, 1]
    assert counts(engine, sid) == {"checks": 3, "incidents": 1, "tasks": 1, "outbox": 1}
    iid, tid = outs[2].opened_incidents[0], outs[2].created_tasks[0]
    inc = rows(engine, "SELECT * FROM incidents WHERE id=:i", i=iid)[0]
    assert (inc["incident_type"], inc["status"], inc["severity"]) == ("http_error", "open", "high")
    assert inc["first_failure_at"] < inc["last_failure_at"]
    task = rows(engine, "SELECT * FROM tasks WHERE id=:t", t=tid)[0]
    assert (task["status"], task["idempotency_key"]) == ("queued", f"incident:{iid}:intake")
    ob = rows(engine, "SELECT * FROM outbox_events WHERE aggregate_id=:t", t=tid)[0]
    assert ob["published_at"] is None and ob["payload"]["incident_id"] == str(iid)
    ev = rows(engine, "SELECT * FROM evidence WHERE incident_id=:i", i=iid)
    assert len(ev) == 1 and ev[0]["source"] == "health_check"
    content = ev[0]["content"]
    assert content["rule"] == {"failure_type": "http_error", "threshold": 3, "consecutive": True}
    assert [c["http_status"] for c in content["checks"]] == [500, 500, 500]
    assert (
        one(
            engine,
            "SELECT count(*) FROM audit_events WHERE entity_id=:i AND action='incident_opened'",
            i=iid,
        )
        == 1
    )


def test_subsequent_failures_do_not_duplicate(engine):
    sid = new_service(engine)
    feed(engine, sid, ["timeout"] * 12, Clock())
    c = counts(engine, sid)
    assert (c["incidents"], c["tasks"], c["outbox"]) == (1, 1, 1)
    inc = rows(
        engine, "SELECT occurrence_count, incident_type FROM incidents WHERE service_id=:s", s=sid
    )[0]
    assert inc == {"occurrence_count": 10, "incident_type": "unavailable"}


def test_latency_threshold_creates_incident(engine):
    sid = new_service(engine)
    feed(engine, sid, ["degraded"] * 3, Clock())
    inc = rows(engine, "SELECT incident_type, severity FROM incidents WHERE service_id=:s", s=sid)
    assert inc == [{"incident_type": "high_latency", "severity": "medium"}]


def test_healthy_rearm_auto_resolves_and_allows_later_separate_incident(engine):
    sid, clock = new_service(engine), Clock()
    first = feed(engine, sid, ["unhealthy"] * 3, clock)[-1]
    feed(engine, sid, ["healthy", "healthy"], clock)
    assert (
        one(engine, "SELECT status FROM incidents WHERE id=:i", i=first.opened_incidents[0])
        == "open"
    )
    out = feed(engine, sid, ["healthy"], clock)[-1]
    assert out.resolved_incidents == first.opened_incidents
    r = rows(
        engine,
        "SELECT status, resolution, resolved_at FROM incidents WHERE id=:i",
        i=first.opened_incidents[0],
    )[0]
    assert (r["status"], r["resolution"]) == ("resolved", "auto_recovered")
    assert rows(
        engine, "SELECT status, outcome FROM tasks WHERE id=:t", t=first.created_tasks[0]
    ) == [{"status": "resolved", "outcome": "incident_auto_recovered"}]
    second = feed(engine, sid, ["unhealthy"] * 3, clock)[-1]
    assert second.opened_incidents and second.opened_incidents != first.opened_incidents
    assert counts(engine, sid)["incidents"] == 2


def test_escalated_incident_not_auto_resolved_and_not_duplicated(engine):
    sid, clock = new_service(engine), Clock()
    iid = feed(engine, sid, ["unhealthy"] * 3, clock)[-1].opened_incidents[0]
    with engine.begin() as conn:
        conn.execute(text("UPDATE incidents SET status='escalated' WHERE id=:i"), {"i": iid})
    feed(engine, sid, ["healthy"] * 4, clock)
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "escalated"
    out = feed(engine, sid, ["unhealthy"] * 3, clock)[-1]  # re-armed, breach again
    assert out.opened_incidents == [] and out.bumped_incidents == [iid]
    assert counts(engine, sid)["incidents"] == 1


def test_incident_creation_is_atomic(engine, monkeypatch):
    sid, clock = new_service(engine), Clock()
    feed(engine, sid, ["unhealthy"] * 2, clock)
    real = recorder.audit

    def failing_audit(conn, **kw):
        if kw["action"] == "incident_opened":
            raise RuntimeError("crash inside the transaction")
        return real(conn, **kw)

    monkeypatch.setattr(recorder, "audit", failing_audit)
    with pytest.raises(RuntimeError):
        feed(engine, sid, ["unhealthy"], clock)
    # nothing from the failed transaction persisted: not the check, incident, task or outbox
    assert counts(engine, sid) == {"checks": 2, "incidents": 0, "tasks": 0, "outbox": 0}
    assert (
        one(
            engine,
            "SELECT consecutive_count FROM detection_state WHERE service_id=:s "
            "AND failure_type='http_error'",
            s=sid,
        )
        == 2
    )
    monkeypatch.setattr(recorder, "audit", real)
    assert feed(engine, sid, ["unhealthy"], clock)[-1].opened_incidents  # retry succeeds


def test_concurrent_recorders_open_one_incident(engine):
    sid = new_service(engine)
    clock = Clock()
    ats = [clock.next() for _ in range(12)]
    errors: list[BaseException] = []

    def worker(chunk):
        try:
            for at in chunk:
                record_check(
                    engine, sid, check("unhealthy", at), TH, stale_after=STALE, max_attempts=3
                )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(ats[i::4],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    c = counts(engine, sid)
    assert (c["checks"], c["incidents"], c["tasks"], c["outbox"]) == (12, 1, 1, 1)


def test_restart_continues_streak_within_window(engine):
    """Detection state lives in PostgreSQL: a monitor restart continues the streak."""
    sid, clock = new_service(engine), Clock()
    feed(engine, sid, ["unhealthy"] * 2, clock)
    # a brand-new recorder call (as after process restart) sees persisted state
    assert feed(engine, sid, ["unhealthy"], clock)[-1].opened_incidents


def test_gap_longer_than_stale_window_resets_streak(engine):
    sid, clock = new_service(engine), Clock()
    feed(engine, sid, ["unhealthy"] * 2, clock)
    clock.t += timedelta(minutes=10)  # monitor was down
    out = feed(engine, sid, ["unhealthy"], clock)[-1]
    assert out.opened_incidents == []
    assert (
        one(
            engine,
            "SELECT consecutive_count FROM detection_state WHERE service_id=:s "
            "AND failure_type='http_error'",
            s=sid,
        )
        == 1
    )


def test_retention_prunes_only_old_checks(engine):
    sid = new_service(engine)
    feed(engine, sid, ["healthy"], Clock())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO health_checks (service_id, checked_at, outcome) "
                "VALUES (:s, now() - interval '30 days', 'healthy')"
            ),
            {"s": sid},
        )
    assert prune_health_checks(engine, 7) >= 1
    assert one(engine, "SELECT count(*) FROM health_checks WHERE service_id=:s", s=sid) == 1


def test_unknown_service_rejected(engine):
    with pytest.raises(Exception):  # noqa: B017 - FK violation from the driver
        feed(engine, uuid.uuid4(), ["healthy"], Clock())
