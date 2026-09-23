"""Outbox delivery, Redis recovery, leases/fencing, retries and dead-lettering."""

import uuid

import pytest
import redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.agent import tasks
from app.agent.stages import IntakeStage, PermanentError, StageResult, TransientError
from app.agent.worker import Worker
from app.persistence.outbox import publish_pending, reconcile_stuck_tasks, schedule_due_retries
from app.persistence.streams import StreamNames, ensure_group
from tests.integration.helpers import expire_lease, make_due, one, open_task, rows

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def quiesce_previous_tests(engine):
    """Tests in a module share one database: park leftovers so each test only
    sees its own outbox rows and tasks."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE outbox_events SET published_at = now() WHERE published_at IS NULL")
        )
        conn.execute(
            text(
                "UPDATE tasks SET status='failed', outcome='test_isolation', "
                "lease_owner=NULL, lease_expires_at=NULL "
                "WHERE status NOT IN ('escalated','failed','resolved','dead_lettered')"
            )
        )


class Crash(Exception):
    """Simulated process crash at a specific point."""


def crash_at(point):
    def hook(p):
        if p == point:
            raise Crash(p)

    return hook


def publish(engine, rclient, settings, **kw):
    names = StreamNames.from_prefix(settings.stream_prefix)
    ensure_group(rclient, names)
    return publish_pending(engine, rclient, names, retry_base=1, retry_max=2, **kw)


def pending(rclient, settings):
    n = StreamNames.from_prefix(settings.stream_prefix)
    return rclient.xpending(n.tasks, n.group)["pending"]


def task(engine, tid):
    return rows(engine, "SELECT * FROM tasks WHERE id=:t", t=tid)[0]


def intake_count(engine, tid):
    return one(
        engine,
        "SELECT count(*) FROM audit_events WHERE entity_id=:t "
        "AND action='task_awaiting_investigation'",
        t=tid,
    )


# --- outbox ---------------------------------------------------------------------


def test_crash_after_commit_before_publish_is_eventually_published(engine, rclient, settings):
    _, tid = open_task(engine)  # committed; dispatcher "crashed" before publishing
    assert (
        one(engine, "SELECT published_at FROM outbox_events WHERE aggregate_id=:t", t=tid) is None
    )
    assert publish(engine, rclient, settings).published >= 1
    ob = rows(
        engine,
        "SELECT published_at, stream_message_id FROM outbox_events WHERE aggregate_id=:t",
        t=tid,
    )[0]
    assert ob["published_at"] is not None and ob["stream_message_id"]
    Worker(settings, engine, rclient).poll_once(block_ms=100)
    assert task(engine, tid)["status"] == "awaiting_investigation"


def test_crash_after_publish_before_mark_cannot_duplicate_work(engine, rclient, settings):
    _, tid = open_task(engine)
    with pytest.raises(Crash):
        publish(engine, rclient, settings, hook=crash_at("after_xadd"))
    assert (
        one(engine, "SELECT published_at FROM outbox_events WHERE aggregate_id=:t", t=tid) is None
    )  # mark rolled back
    publish(engine, rclient, settings)  # re-published -> duplicate stream message
    names = StreamNames.from_prefix(settings.stream_prefix)
    msgs = [m for m in rclient.xrange(names.tasks) if m[1]["task_id"] == str(tid)]
    assert len(msgs) == 2
    w = Worker(settings, engine, rclient)
    while w.poll_once(block_ms=100):
        pass
    assert w.counters["processed"] == 1 and w.counters["duplicates"] == 1
    assert intake_count(engine, tid) == 1
    assert one(engine, "SELECT count(*) FROM task_checkpoints WHERE task_id=:t", t=tid) == 1
    assert pending(rclient, settings) == 0


def test_redis_outage_retains_work_in_db_and_retries(engine, rclient, settings):
    _, tid = open_task(engine)
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.5, decode_responses=True)
    names = StreamNames.from_prefix(settings.stream_prefix)
    res = publish_pending(engine, dead, names, retry_base=1, retry_max=2)
    assert res.failed == 1 and res.published == 0
    ob = rows(engine, "SELECT * FROM outbox_events WHERE aggregate_id=:t", t=tid)[0]
    assert ob["published_at"] is None and ob["publish_attempts"] == 1
    assert ob["last_error"] == "ConnectionError" and ob["next_attempt_at"] > ob["created_at"]
    assert task(engine, tid)["status"] == "queued"  # DB keeps the pending work
    # not due yet -> nothing published even with Redis back
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE outbox_events SET next_attempt_at=now() WHERE aggregate_id=:t"), {"t": tid}
        )
    assert publish(engine, rclient, settings).published >= 1
    Worker(settings, engine, rclient).poll_once(block_ms=100)
    assert task(engine, tid)["status"] == "awaiting_investigation"


def test_lost_stream_message_is_reconciled(engine, rclient, settings):
    _, tid = open_task(engine)
    publish(engine, rclient, settings)
    names = StreamNames.from_prefix(settings.stream_prefix)
    rclient.delete(names.tasks)  # Redis lost the message (e.g. fsync window)
    ensure_group(rclient, names)
    with engine.begin() as conn:  # time passes with no progress (bypass updated_at trigger)
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            text("UPDATE tasks SET updated_at = now() - interval '1 hour' WHERE id=:t"), {"t": tid}
        )
        conn.execute(
            text(
                "UPDATE outbox_events SET published_at = now() - interval '1 hour' "
                "WHERE aggregate_id=:t"
            ),
            {"t": tid},
        )
    assert reconcile_stuck_tasks(engine, settings.redispatch_after_seconds) >= 1
    assert reconcile_stuck_tasks(engine, settings.redispatch_after_seconds) == 0  # deduped
    publish(engine, rclient, settings)
    Worker(settings, engine, rclient).poll_once(block_ms=100)
    assert task(engine, tid)["status"] == "awaiting_investigation"


# --- worker delivery and recovery ------------------------------------------------


def test_normal_processing_checkpoints_and_acks(engine, rclient, settings):
    iid, tid = open_task(engine)
    publish(engine, rclient, settings)
    w = Worker(settings, engine, rclient)
    assert w.poll_once(block_ms=100) >= 1
    t = task(engine, tid)
    assert (t["status"], t["outcome"], t["attempt"], t["fencing_token"]) == (
        "awaiting_investigation",
        "intake_complete",
        1,
        1,
    )
    assert t["lease_owner"] is None
    cp = rows(
        engine,
        "SELECT step, state, fencing_token, data FROM task_checkpoints WHERE task_id=:t",
        t=tid,
    )
    assert cp[0]["step"] == 3 and cp[0]["state"] == "intake_complete"
    assert cp[0]["data"]["evidence_records"] == 1
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "open"
    assert pending(rclient, settings) == 0


def test_worker_crash_after_commit_before_ack_is_recovered(engine, rclient, settings):
    _, tid = open_task(engine)
    publish(engine, rclient, settings)
    a = Worker(settings, engine, rclient, consumer="a", hook=crash_at("before_ack"))
    with pytest.raises(Crash):
        a.poll_once(block_ms=100)
    assert task(engine, tid)["status"] == "awaiting_investigation"  # outcome committed
    assert pending(rclient, settings) == 1  # but never ACKed
    b = Worker(
        settings,
        engine,
        rclient,
        consumer="b",
    )
    b.s = settings.model_copy(update={"pending_idle_seconds": 0.001})
    b.recover_pending()
    assert b.counters["reclaimed"] == 1 and b.counters["duplicates"] == 1
    assert pending(rclient, settings) == 0 and intake_count(engine, tid) == 1


def test_worker_crash_mid_task_is_recovered_by_another_worker(engine, rclient, settings):
    _, tid = open_task(engine)
    publish(engine, rclient, settings)
    a = Worker(settings, engine, rclient, consumer="a", hook=crash_at("after_claim"))
    with pytest.raises(Crash):
        a.poll_once(block_ms=100)
    t = task(engine, tid)
    assert (t["status"], t["lease_owner"], t["fencing_token"]) == ("running", "a", 1)
    b = Worker(settings, engine, rclient, consumer="b")
    b.s = settings.model_copy(update={"pending_idle_seconds": 0.001})
    b.recover_pending()  # lease still valid -> not claimable, message ACKed as duplicate?
    assert task(engine, tid)["lease_owner"] == "a"
    # the message was acked by b; the reconciler/lease-expiry path must still recover it
    expire_lease(engine, tid)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET lease_expires_at = now() - interval '1 hour' WHERE id=:t"),
            {"t": tid},
        )
        conn.execute(
            text(
                "UPDATE outbox_events SET published_at = now() - interval '1 hour' "
                "WHERE aggregate_id=:t"
            ),
            {"t": tid},
        )
    assert reconcile_stuck_tasks(engine, settings.redispatch_after_seconds) == 1
    publish(engine, rclient, settings)
    b.poll_once(block_ms=100)
    t = task(engine, tid)
    assert (t["status"], t["fencing_token"], t["attempt"]) == ("awaiting_investigation", 2, 2)


def test_expired_lease_message_reclaimed_via_pending_recovery(engine, rclient, settings):
    _, tid = open_task(engine)
    publish(engine, rclient, settings)
    a = Worker(settings, engine, rclient, consumer="a", hook=crash_at("after_claim"))
    with pytest.raises(Crash):
        a.poll_once(block_ms=100)
    expire_lease(engine, tid)
    b = Worker(settings, engine, rclient, consumer="b")
    b.s = settings.model_copy(update={"pending_idle_seconds": 0.001})
    b.recover_pending()
    assert b.counters["reclaimed"] == 1 and b.counters["processed"] == 1
    assert task(engine, tid)["status"] == "awaiting_investigation"
    assert pending(rclient, settings) == 0


# --- fencing ----------------------------------------------------------------------


def test_two_workers_racing_only_current_fencing_holder_advances(engine, rclient, settings):
    _, tid = open_task(engine)
    lease_a = tasks.claim(engine, tid, "a", 60)
    assert lease_a is not None and lease_a.token == 1
    assert tasks.claim(engine, tid, "b", 60) is None  # valid lease: no competing executor
    expire_lease(engine, tid)
    lease_b = tasks.claim(engine, tid, "b", 60)
    assert lease_b is not None and lease_b.token == 2
    # stale executor A: checkpoint rejected by trigger, transition rejected by fence
    with pytest.raises(DBAPIError, match="stale fencing token"):  # DB-level guard itself
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO task_checkpoints (task_id, step, state, "
                    "fencing_token) VALUES (:t, 3, 'x', 1)"
                ),
                {"t": tid},
            )
    with pytest.raises(tasks.LeaseLost):
        with engine.begin() as conn:
            tasks.checkpoint(conn, lease_a, 3, "intake_complete", {})
    with pytest.raises(tasks.LeaseLost):
        with engine.begin() as conn:
            tasks.transition(conn, lease_a, "awaiting_investigation", outcome="stale")
    assert tasks.renew(engine, lease_a, 60) is False
    with pytest.raises(tasks.LeaseLost):
        IntakeStage().run(engine, lease_a)
    # current holder B succeeds
    assert IntakeStage().run(engine, lease_b).status == "awaiting_investigation"
    cp = rows(engine, "SELECT fencing_token FROM task_checkpoints WHERE task_id=:t", t=tid)
    assert cp == [{"fencing_token": 2}]
    assert task(engine, tid)["outcome"] == "intake_complete"


def test_heartbeat_keeps_lease_during_long_stage(engine, rclient, settings):
    open_task(engine)
    publish(engine, rclient, settings)
    import time

    class Slow:
        def run(self, eng, lease):
            time.sleep(3.5)  # > lease_ttl (2s); heartbeat every 1s must renew
            assert tasks.claim(eng, lease.task_id, "intruder", 2) is None
            return IntakeStage().run(eng, lease)

    w = Worker(settings, engine, rclient, stage=Slow())
    w.poll_once(block_ms=100)
    assert w.counters["processed"] == 1 and w.counters["lease_lost"] == 0


# --- retry, dead letter, failure -----------------------------------------------------


class Flaky:
    def __init__(self, fail_times):
        self.left = fail_times

    def run(self, eng, lease):
        if self.left > 0:
            self.left -= 1
            raise TransientError("dependency timeout")
        return IntakeStage().run(eng, lease)


def drive(engine, rclient, settings, worker, tid, rounds=6):
    for _ in range(rounds):
        make_due(engine, tid)
        schedule_due_retries(engine)
        publish(engine, rclient, settings)
        worker.poll_once(block_ms=100)


def test_transient_error_retries_with_backoff_then_succeeds(engine, rclient, settings):
    _, tid = open_task(engine)
    publish(engine, rclient, settings)
    w = Worker(settings, engine, rclient, stage=Flaky(1))
    w.poll_once(block_ms=100)
    t = task(engine, tid)
    assert (t["status"], t["last_error"], t["attempt"]) == ("retry_scheduled", "TransientError", 1)
    delay = (t["next_attempt_at"] - t["updated_at"]).total_seconds()
    assert 0.5 < delay <= 1.5  # base backoff 1s for attempt 1
    assert schedule_due_retries(engine) == 0  # not due yet
    drive(engine, rclient, settings, w, tid, rounds=1)
    t = task(engine, tid)
    assert (t["status"], t["attempt"]) == ("awaiting_investigation", 2)
    assert w.counters["retries"] == 1


def test_exhausted_retries_dead_letter_and_escalate(engine, rclient, settings):
    iid, tid = open_task(engine)
    publish(engine, rclient, settings)
    w = Worker(settings, engine, rclient, stage=Flaky(99))
    w.poll_once(block_ms=100)
    drive(engine, rclient, settings, w, tid, rounds=3)
    t = task(engine, tid)
    assert (t["status"], t["attempt"], t["outcome"]) == ("dead_lettered", 3, "attempts_exhausted")
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "escalated"
    names = StreamNames.from_prefix(settings.stream_prefix)
    dlq = rclient.xrange(names.dead_letter)
    assert [m[1]["task_id"] for m in dlq] == [str(tid)]
    assert pending(rclient, settings) == 0
    assert (
        one(
            engine,
            "SELECT count(*) FROM audit_events WHERE entity_id=:t "
            "AND action='task_retry_scheduled'",
            t=tid,
        )
        == 2
    )


def test_crash_on_final_attempt_dead_letters_on_next_delivery(engine, rclient, settings):
    iid, tid = open_task(engine, max_attempts=1)
    publish(engine, rclient, settings)
    a = Worker(settings, engine, rclient, consumer="a", hook=crash_at("after_claim"))
    with pytest.raises(Crash):
        a.poll_once(block_ms=100)
    expire_lease(engine, tid)
    b = Worker(settings, engine, rclient, consumer="b")
    b.s = settings.model_copy(update={"pending_idle_seconds": 0.001})
    b.recover_pending()
    assert task(engine, tid)["status"] == "dead_lettered"
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "escalated"


def test_permanent_error_fails_task_without_retry(engine, rclient, settings):
    iid, tid = open_task(engine)
    publish(engine, rclient, settings)

    class Broken:
        def run(self, eng, lease):
            raise PermanentError("invalid input")

    Worker(settings, engine, rclient, stage=Broken()).poll_once(block_ms=100)
    t = task(engine, tid)
    assert (t["status"], t["attempt"], t["last_error"]) == ("failed", 1, "PermanentError")
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "escalated"


def test_malformed_message_dead_lettered_and_acked(engine, rclient, settings):
    names = StreamNames.from_prefix(settings.stream_prefix)
    ensure_group(rclient, names)
    rclient.xadd(names.tasks, {"task_id": "not-a-uuid"})
    Worker(settings, engine, rclient).poll_once(block_ms=100)
    assert rclient.xlen(names.dead_letter) == 1 and pending(rclient, settings) == 0


def test_unknown_task_message_is_acked_without_effect(engine, rclient, settings):
    names = StreamNames.from_prefix(settings.stream_prefix)
    ensure_group(rclient, names)
    rclient.xadd(names.tasks, {"task_id": str(uuid.uuid4())})
    w = Worker(settings, engine, rclient)
    w.poll_once(block_ms=100)
    assert w.counters["duplicates"] == 1 and pending(rclient, settings) == 0


def test_incident_resolved_before_intake_completes_task_as_resolved(engine, rclient, settings):
    iid, tid = open_task(engine)
    lease = tasks.claim(engine, tid, "a", 60)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE incidents SET status='resolved', resolved_at=now(), "
                "resolution='manual' WHERE id=:i"
            ),
            {"i": iid},
        )
    assert IntakeStage().run(engine, lease) == StageResult("resolved", "incident_no_longer_active")
