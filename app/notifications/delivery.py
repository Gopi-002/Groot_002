"""Durable notification delivery (run by the ``notifier`` service).

  notification_events (logical, deduplicated)
      -> fan_out: one delivery row per configured channel (idempotent)
      -> claim_due: lease (attempt++); crashed senders' leases expire
      -> channel.send (outside any DB transaction; bounded timeout)
      -> record_result (fenced on owner + attempt):
           delivered | pending (jittered backoff) | dead_lettered (bounded)

At-least-once: a crash after the HTTP call but before recording resends the
same event with the same ``Idempotency-Key``. A provider outage only delays
delivery; incident processing never waits for, or is rolled back by, it.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass

from sqlalchemy import Engine, text

from app.backoff import jittered_backoff
from app.notifications.channels import DeliveryResult, Message, NotificationChannel
from app.observability.logging import log_context
from app.persistence.audit import audit

log = logging.getLogger("sentinelops.notify.delivery")


@dataclass(frozen=True)
class Delivery:
    id: uuid.UUID
    event_id: uuid.UUID
    channel: str
    attempt: int
    max_attempts: int
    owner: str
    message: Message


def fan_out(engine: Engine, channels: list[str], max_attempts: int, batch: int = 100) -> int:
    with engine.begin() as conn:
        ids = (
            conn.execute(
                text(
                    "SELECT id FROM notification_events WHERE fanned_out_at IS NULL "
                    "ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"n": batch},
            )
            .scalars()
            .all()
        )
        for eid in ids:
            for ch in channels:
                conn.execute(
                    text(
                        "INSERT INTO notification_deliveries (event_id, channel, max_attempts) "
                        "VALUES (:e, :c, :m) ON CONFLICT (event_id, channel) DO NOTHING"
                    ),
                    {"e": eid, "c": ch, "m": max_attempts},
                )
            conn.execute(
                text("UPDATE notification_events SET fanned_out_at = now() WHERE id=:e"),
                {"e": eid},
            )
    return len(ids)


def dead_letter_expired(engine: Engine, actor: str) -> int:
    """Leases that expired on the final attempt cannot be claimed again."""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "UPDATE notification_deliveries SET status='dead_lettered', lease_owner=NULL, "
                    "lease_expires_at=NULL, last_error=COALESCE(last_error, 'lease expired') "
                    "WHERE status='sending' AND lease_expires_at < now() "
                    "AND attempt >= max_attempts RETURNING id, event_id, channel"
                )
            )
            .mappings()
            .all()
        )
        for r in rows:
            audit(
                conn,
                actor_type="system",
                actor_id=actor,
                action="notification_dead_lettered",
                entity_type="notification",
                entity_id=r["event_id"],
                details={"channel": r["channel"], "reason": "lease expired on final attempt"},
            )
    return len(rows)


def claim_due(engine: Engine, owner: str, ttl_seconds: float, batch: int = 20) -> list[Delivery]:
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "UPDATE notification_deliveries d SET status='sending', lease_owner=:o, "
                    "lease_expires_at = now() + make_interval(secs => :ttl), attempt = attempt + 1 "
                    "FROM notification_events e WHERE e.id = d.event_id AND d.id IN ("
                    "  SELECT id FROM notification_deliveries WHERE attempt < max_attempts AND ("
                    "    (status='pending' AND next_attempt_at <= now()) "
                    "    OR (status='sending' AND lease_expires_at < now())) "
                    "  ORDER BY next_attempt_at LIMIT :n FOR UPDATE SKIP LOCKED) "
                    "RETURNING d.id, d.event_id, d.channel, d.attempt, d.max_attempts, "
                    "e.event_type, e.severity, e.created_at, e.payload"
                ),
                {"o": owner, "ttl": ttl_seconds, "n": batch},
            )
            .mappings()
            .all()
        )
    return [
        Delivery(
            id=r["id"],
            event_id=r["event_id"],
            channel=r["channel"],
            attempt=int(r["attempt"]),
            max_attempts=int(r["max_attempts"]),
            owner=owner,
            message=Message(
                r["event_id"], r["event_type"], r["severity"], r["created_at"], dict(r["payload"])
            ),
        )
        for r in rows
    ]


def record_result(
    engine: Engine,
    d: Delivery,
    result: DeliveryResult,
    *,
    retry_base: float,
    retry_max: float,
    rng: random.Random,
) -> str:
    """Fenced on (owner, attempt): a stale sender cannot overwrite a newer attempt."""
    if result.ok:
        status, delay = "delivered", None
    elif result.retryable and d.attempt < d.max_attempts:
        status, delay = "pending", jittered_backoff(d.attempt, retry_base, retry_max, rng)
    else:
        status, delay = "dead_lettered", None
    with engine.begin() as conn:
        n = conn.execute(
            text(
                "UPDATE notification_deliveries SET status=:st, lease_owner=NULL, "
                "lease_expires_at=NULL, last_error=:err, last_http_status=:hs, "
                "delivered_at = CASE WHEN :st = 'delivered' THEN now() ELSE NULL END, "
                "next_attempt_at = CASE WHEN CAST(:d AS double precision) IS NULL "
                "  THEN next_attempt_at "
                "  ELSE now() + make_interval(secs => CAST(:d AS double precision)) END "
                "WHERE id=:id AND status='sending' AND lease_owner=:o AND attempt=:a"
            ),
            {
                "st": status,
                "err": result.error,
                "hs": result.http_status,
                "d": delay,
                "id": d.id,
                "o": d.owner,
                "a": d.attempt,
            },
        ).rowcount
        if n != 1:
            return "lease_lost"
        action = {
            "delivered": "notification_delivered",
            "pending": "notification_failed",
            "dead_lettered": "notification_dead_lettered",
        }[status]
        audit(
            conn,
            actor_type="system",
            actor_id=d.owner,
            action=action,
            entity_type="notification",
            entity_id=d.event_id,
            details={
                "channel": d.channel,
                "attempt": d.attempt,
                "http_status": result.http_status,
                "error": result.error,
                "retry_in_s": delay,
            },
        )
    return status


def deliver_due(
    engine: Engine,
    channels: dict[str, NotificationChannel],
    *,
    owner: str,
    lease_ttl: float,
    retry_base: float,
    retry_max: float,
    rng: random.Random,
    batch: int = 20,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    dead_letter_expired(engine, owner)
    for d in claim_due(engine, owner, lease_ttl, batch):
        ch = channels.get(d.channel)
        with log_context(
            notification_id=str(d.event_id), incident_id=d.message.payload.get("incident_id")
        ):
            if ch is None:
                result = DeliveryResult(False, True, None, "channel_not_configured")
            else:
                try:
                    result = ch.send(d.message)
                except Exception as exc:  # a channel bug must not kill the notifier
                    result = DeliveryResult(False, True, None, type(exc).__name__)
            status = record_result(
                engine, d, result, retry_base=retry_base, retry_max=retry_max, rng=rng
            )
            level = logging.INFO if status == "delivered" else logging.WARNING
            log.log(
                level,
                "notification delivery",
                extra={
                    "channel": d.channel,
                    "status": status,
                    "attempt": d.attempt,
                    "http_status": result.http_status,
                    "error": result.error,
                },
            )
        counts[status] = counts.get(status, 0) + 1
    return counts


def prune(engine: Engine, retention_days: int) -> int:
    """Retention: drop finished deliveries/events older than the window (the
    append-only audit trail keeps the record that they happened)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM notification_deliveries WHERE status IN ('delivered','dead_lettered') "
                "AND updated_at < now() - make_interval(days => :d)"
            ),
            {"d": retention_days},
        )
        n = conn.execute(
            text(
                "DELETE FROM notification_events e WHERE e.created_at < now() - "
                "make_interval(days => :d) AND e.fanned_out_at IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM notification_deliveries d WHERE d.event_id = e.id)"
            ),
            {"d": retention_days},
        ).rowcount
    return int(n or 0)
