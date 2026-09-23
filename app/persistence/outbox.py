"""Transactional outbox -> Redis Streams publication and task re-dispatch.

Crash windows (at-least-once):
* crash after DB commit, before publish  -> row stays unpublished -> published later.
* crash after XADD, before marking row   -> transaction rolls back -> row published
  again -> duplicate stream message. Workers are idempotent (DB lease/claim), so
  duplicates cannot duplicate work.
* Redis unavailable -> row keeps ``published_at IS NULL`` with backoff; PostgreSQL
  retains all pending work.
* Redis loses an acknowledged-to-us message (e.g. restart inside the AOF fsync
  window) -> ``reconcile_stuck_tasks`` re-dispatches tasks that made no progress.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import redis
from sqlalchemy import Engine, text

from app.backoff import backoff_seconds
from app.persistence.audit import audit
from app.persistence.streams import StreamNames

log = logging.getLogger("sentinelops.outbox")

Hook = Callable[[str], None]


def _no_hook(_point: str) -> None:
    return None


@dataclass
class PublishResult:
    published: int = 0
    failed: int = 0


def publish_pending(
    engine: Engine,
    client: redis.Redis,
    names: StreamNames,
    *,
    batch_size: int = 50,
    maxlen: int = 100_000,
    retry_base: float = 1.0,
    retry_max: float = 60.0,
    hook: Hook = _no_hook,
) -> PublishResult:
    res = PublishResult()
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT id, aggregate_id, event_type, dedup_key, payload, publish_attempts "
                    "FROM outbox_events WHERE published_at IS NULL AND next_attempt_at <= now() "
                    "ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"n": batch_size},
            )
            .mappings()
            .all()
        )
        for r in rows:
            fields: dict[Any, Any] = {
                "event_id": str(r["id"]),
                "dedup_key": r["dedup_key"],
                "event_type": r["event_type"],
                "task_id": str(r["aggregate_id"]),
                "payload": json.dumps(r["payload"], sort_keys=True),
            }
            try:
                msg_id = client.xadd(
                    names.stream_for(r["event_type"]), fields, maxlen=maxlen, approximate=True
                )
            except redis.RedisError as exc:
                attempts = r["publish_attempts"] + 1
                delay = backoff_seconds(attempts, retry_base, retry_max)
                conn.execute(
                    text(
                        "UPDATE outbox_events SET publish_attempts=:a, last_error=:e, "
                        "next_attempt_at = now() + make_interval(secs => :d) WHERE id=:id"
                    ),
                    {"a": attempts, "e": type(exc).__name__, "d": delay, "id": r["id"]},
                )
                res.failed += 1
                log.warning(
                    "outbox publish failed; will retry",
                    extra={
                        "event_id": str(r["id"]),
                        "attempts": attempts,
                        "retry_in_s": delay,
                        "error_type": type(exc).__name__,
                    },
                )
                break  # Redis is likely down; keep remaining rows for the next cycle
            hook("after_xadd")
            conn.execute(
                text(
                    "UPDATE outbox_events SET published_at=now(), stream_message_id=:m, "
                    "publish_attempts=publish_attempts+1, last_error=NULL WHERE id=:id"
                ),
                {"m": str(msg_id), "id": r["id"]},
            )
            res.published += 1
    return res


def schedule_due_retries(engine: Engine) -> int:
    """Enqueue an outbox event for each retry_scheduled task that is now due.
    The dedup key includes the attempt number, so this is idempotent."""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, "
                    "dedup_key, payload) "
                    "SELECT 'task', t.id, 'task.dispatch', "
                    "'task:' || t.id || ':retry:' || t.attempt, "
                    "jsonb_build_object('task_id', t.id, 'incident_id', t.incident_id, "
                    "'reason', 'retry') "
                    "FROM tasks t WHERE t.status='retry_scheduled' AND t.next_attempt_at <= now() "
                    "ON CONFLICT (dedup_key) DO NOTHING RETURNING aggregate_id"
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


def reconcile_stuck_tasks(engine: Engine, redispatch_after_seconds: float) -> int:
    """Backstop for lost stream messages: re-dispatch tasks that are queued with
    no pending outbox row and no progress, or running with a long-expired lease.
    One re-dispatch per task per ``redispatch_after`` window (dedup key bucket)."""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, "
                    "dedup_key, payload) "
                    "SELECT 'task', t.id, 'task.dispatch', "
                    "'task:' || t.id || ':reconcile:' || t.fencing_token || ':' || "
                    "floor(extract(epoch FROM now()) / :w)::bigint, "
                    "jsonb_build_object('task_id', t.id, 'incident_id', t.incident_id, "
                    "'reason', 'reconcile') "
                    "FROM tasks t "
                    "WHERE ((t.status = 'queued' "
                    "        AND t.updated_at < now() - make_interval(secs => :w))"
                    "   OR (t.status IN ('awaiting_investigation','awaiting_policy',"
                    "                    'waiting_approval') "
                    "       AND t.next_attempt_at < now() - make_interval(secs => :w))"
                    "   OR (t.status = 'running' "
                    "       AND t.lease_expires_at < now() - make_interval(secs => :w))) "
                    "AND NOT EXISTS (SELECT 1 FROM outbox_events o WHERE o.aggregate_id = t.id "
                    "   AND (o.published_at IS NULL "
                    "        OR o.published_at > now() - make_interval(secs => :w))) "
                    "ON CONFLICT (dedup_key) DO NOTHING RETURNING aggregate_id"
                ),
                {"w": redispatch_after_seconds},
            )
            .scalars()
            .all()
        )
        for task_id in rows:
            audit(
                conn,
                actor_type="system",
                actor_id="dispatcher",
                action="task_redispatched",
                entity_type="task",
                entity_id=task_id,
                details={"reason": "reconcile"},
            )
    if rows:
        log.warning("re-dispatched stuck tasks", extra={"count": len(rows)})
    return len(rows)


def schedule_parked_investigations(engine: Engine) -> int:
    """Re-dispatch parked investigation tasks through the normal outbox path.

    * Tasks parked indefinitely (``next_attempt_at IS NULL``: no AI configured,
      e.g. every Phase 2 task) become due once an active model selection exists.
    * Tasks paused with a backoff (auth/quota/rate limit) become due when their
      ``next_attempt_at`` passes.
    One outbox event per task per fencing token (dedup key), so a parked task is
    never investigated twice concurrently; the DB claim is the final guard.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tasks SET next_attempt_at = now() "
                "WHERE status = 'awaiting_investigation' AND next_attempt_at IS NULL "
                "AND EXISTS (SELECT 1 FROM model_config WHERE is_active)"
            )
        )
        rows = (
            conn.execute(
                text(
                    "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, "
                    "dedup_key, payload) "
                    "SELECT 'task', t.id, 'task.dispatch', "
                    "'task:' || t.id || ':investigate:' || t.fencing_token, "
                    "jsonb_build_object('task_id', t.id, 'incident_id', t.incident_id, "
                    "'reason', 'investigate') "
                    "FROM tasks t WHERE t.status = 'awaiting_investigation' "
                    "AND t.next_attempt_at <= now() "
                    "ON CONFLICT (dedup_key) DO NOTHING RETURNING aggregate_id"
                )
            )
            .scalars()
            .all()
        )
    if rows:
        log.info("parked investigations re-dispatched", extra={"count": len(rows)})
    return len(rows)


def schedule_policy_tasks(engine: Engine) -> int:
    """Dispatch investigated tasks to the policy stage, and waiting-approval tasks
    whose approval window has closed (so expiry fails closed), through the same
    outbox -> Redis path. Dedup key includes the fencing token."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tasks SET next_attempt_at = now() "
                "WHERE status = 'awaiting_policy' AND next_attempt_at IS NULL"
            )
        )
        rows = (
            conn.execute(
                text(
                    "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, "
                    "dedup_key, payload) "
                    "SELECT 'task', t.id, 'task.dispatch', "
                    "'task:' || t.id || ':' || t.status || ':' || t.fencing_token, "
                    "jsonb_build_object('task_id', t.id, 'incident_id', t.incident_id, "
                    "'reason', t.status) "
                    "FROM tasks t WHERE t.status IN ('awaiting_policy','waiting_approval') "
                    "AND t.next_attempt_at <= now() "
                    "ON CONFLICT (dedup_key) DO NOTHING RETURNING aggregate_id"
                )
            )
            .scalars()
            .all()
        )
    if rows:
        log.info("policy-stage tasks dispatched", extra={"count": len(rows)})
    return len(rows)
