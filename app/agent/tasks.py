"""DB-backed task leases with fencing tokens.

Every state change by a worker is conditioned on
``id = :id AND status = 'running' AND lease_owner = :owner AND fencing_token = :token``.
Claiming increments ``fencing_token``, so once a lease is reclaimed the previous
holder's writes match zero rows (and checkpoint inserts are rejected by the
``trg_task_checkpoints_fence`` trigger). There is never more than one executor
whose writes can take effect.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from app.agent.finalize import on_task_terminal
from app.persistence.audit import audit

TERMINAL_INCIDENT_ESCALATION = ("dead_lettered", "failed", "escalated")
STALE_FENCE_SQLSTATE = "SF001"  # raised by trg_task_checkpoints_fence


class LeaseLost(Exception):
    """The fenced write matched no row: another executor owns the task now."""


@dataclass(frozen=True)
class Lease:
    task_id: uuid.UUID
    incident_id: uuid.UUID
    owner: str
    token: int
    attempt: int
    max_attempts: int


def claim(engine: Engine, task_id: uuid.UUID, owner: str, ttl_seconds: float) -> Lease | None:
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "UPDATE tasks SET status='running', lease_owner=:o, "
                    "lease_expires_at = now() + make_interval(secs => :ttl), "
                    "fencing_token = fencing_token + 1, attempt = attempt + 1, "
                    "next_attempt_at = NULL "
                    "WHERE id = :id AND attempt < max_attempts AND ("
                    "  status = 'queued' "
                    "  OR (status = 'retry_scheduled' AND next_attempt_at <= now()) "
                    # parked tasks are claimable only once explicitly scheduled
                    # (next_attempt_at set by the dispatcher or a pause backoff)
                    "  OR (status IN ('awaiting_investigation','awaiting_policy',"
                    "                 'waiting_approval') AND next_attempt_at <= now()) "
                    "  OR (status = 'running' AND lease_expires_at < now())) "
                    "RETURNING incident_id, fencing_token, attempt, max_attempts"
                ),
                {"id": task_id, "o": owner, "ttl": ttl_seconds},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        lease = Lease(
            task_id,
            uuid.UUID(str(row["incident_id"])),
            owner,
            int(row["fencing_token"]),
            int(row["attempt"]),
            int(row["max_attempts"]),
        )
        audit(
            conn,
            actor_type="system",
            actor_id=owner,
            action="task_claimed",
            entity_type="task",
            entity_id=task_id,
            details={"fencing_token": lease.token, "attempt": lease.attempt},
        )
    return lease


def dead_letter_if_exhausted(engine: Engine, task_id: uuid.UUID, actor: str) -> bool:
    """A task whose lease expired after its final attempt (executor crashed)
    cannot be claimed again; move it to dead_lettered and escalate."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE tasks SET status='dead_lettered', outcome='attempts_exhausted', "
                "last_error=COALESCE(last_error, 'lease expired on final attempt'), "
                "completed_at=now(), lease_owner=NULL, lease_expires_at=NULL, "
                "fencing_token=fencing_token+1 "
                "WHERE id=:id AND attempt >= max_attempts AND ("
                "  (status='running' AND lease_expires_at < now()) "
                "  OR (status='retry_scheduled' AND next_attempt_at <= now())) "
                "RETURNING incident_id"
            ),
            {"id": task_id},
        ).scalar_one_or_none()
        if row is None:
            return False
        _escalate_incident(conn, uuid.UUID(str(row)), actor, "task dead-lettered")
        audit(
            conn,
            actor_type="system",
            actor_id=actor,
            action="task_dead_lettered",
            entity_type="task",
            entity_id=task_id,
            details={"reason": "attempts_exhausted"},
        )
        on_task_terminal(
            conn,
            task_id=task_id,
            incident_id=uuid.UUID(str(row)),
            status="dead_lettered",
            outcome="attempts_exhausted",
            actor=actor,
        )
    return True


def renew(engine: Engine, lease: Lease, ttl_seconds: float) -> bool:
    with engine.begin() as conn:
        n = conn.execute(
            text(
                "UPDATE tasks SET lease_expires_at = now() + make_interval(secs => :ttl) "
                "WHERE id=:id AND status='running' AND lease_owner=:o AND fencing_token=:t "
                "AND lease_expires_at > now()"
            ),
            {"id": lease.task_id, "o": lease.owner, "t": lease.token, "ttl": ttl_seconds},
        ).rowcount
    return bool(n)


def checkpoint(conn: Connection, lease: Lease, step: int, state: str, data: dict[str, Any]) -> None:
    """Upsert a step checkpoint. The DB trigger rejects stale fencing tokens,
    surfaced here as LeaseLost (the caller's transaction must roll back)."""
    try:
        _write_checkpoint(conn, lease, step, state, data)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == STALE_FENCE_SQLSTATE:
            raise LeaseLost(f"task {lease.task_id} token {lease.token}") from exc
        raise


def _write_checkpoint(
    conn: Connection, lease: Lease, step: int, state: str, data: dict[str, Any]
) -> None:
    conn.execute(
        text(
            "INSERT INTO task_checkpoints (task_id, step, state, fencing_token, data) "
            "VALUES (:t, :s, :st, :f, CAST(:d AS jsonb)) "
            "ON CONFLICT (task_id, step) DO UPDATE SET state=EXCLUDED.state, "
            "fencing_token=EXCLUDED.fencing_token, data=EXCLUDED.data, created_at=now()"
        ),
        {
            "t": lease.task_id,
            "s": step,
            "st": state,
            "f": lease.token,
            "d": json.dumps(data, default=str),
        },
    )


def transition(
    conn: Connection,
    lease: Lease,
    status: str,
    *,
    outcome: str,
    error: str | None = None,
    next_attempt_in: float | None = None,
    refund_attempt: bool = False,
) -> None:
    """Fenced state change that releases the lease. Raises LeaseLost.

    ``refund_attempt``: the claim counted an attempt, but the task is being
    parked because the AI provider is unavailable to us (auth, quota, rate
    limit) - not because the task failed - so the attempt is given back."""
    terminal = status in ("escalated", "failed", "resolved", "dead_lettered")
    n = conn.execute(
        text(
            "UPDATE tasks SET status=:st, outcome=:oc, last_error=:err, "
            "attempt = attempt - CASE WHEN :refund THEN 1 ELSE 0 END, "
            "lease_owner=NULL, lease_expires_at=NULL, "
            "next_attempt_at = CASE WHEN CAST(:nxt AS double precision) IS NULL THEN NULL "
            "  ELSE now() + make_interval(secs => CAST(:nxt AS double precision)) END, "
            "completed_at = CASE WHEN :term THEN now() ELSE completed_at END "
            "WHERE id=:id AND status='running' AND lease_owner=:o AND fencing_token=:t"
        ),
        {
            "st": status,
            "oc": outcome,
            "err": error,
            "nxt": next_attempt_in,
            "term": terminal,
            "refund": refund_attempt,
            "id": lease.task_id,
            "o": lease.owner,
            "t": lease.token,
        },
    ).rowcount
    if n != 1:
        raise LeaseLost(f"task {lease.task_id} token {lease.token}")
    if status in TERMINAL_INCIDENT_ESCALATION:
        _escalate_incident(conn, lease.incident_id, lease.owner, f"task {status}")
    audit(
        conn,
        actor_type="system",
        actor_id=lease.owner,
        action=f"task_{status}",
        entity_type="task",
        entity_id=lease.task_id,
        details={
            "outcome": outcome,
            "fencing_token": lease.token,
            "attempt": lease.attempt,
            "error": error,
            "next_attempt_in": next_attempt_in,
        },
    )
    if terminal:
        # Step 10 in the SAME transaction: durable report job + outcome notification.
        on_task_terminal(
            conn,
            task_id=lease.task_id,
            incident_id=lease.incident_id,
            status=status,
            outcome=outcome,
            actor=lease.owner,
        )


def _escalate_incident(conn: Connection, incident_id: uuid.UUID, actor: str, reason: str) -> None:
    n = conn.execute(
        text(
            "UPDATE incidents SET status='escalated' WHERE id=:i AND status IN "
            "('open','investigating','remediating','waiting_approval')"
        ),
        {"i": incident_id},
    ).rowcount
    if n:
        audit(
            conn,
            actor_type="system",
            actor_id=actor,
            action="incident_escalated",
            entity_type="incident",
            entity_id=incident_id,
            details={"reason": reason},
        )


def pin_model(conn: Connection, lease: Lease, model_id: str) -> str:
    """Pin the model to the task on first investigation (fenced). A task that is
    already pinned keeps its model: model changes only affect new tasks."""
    row = conn.execute(
        text(
            "UPDATE tasks SET model_id = COALESCE(model_id, :m) "
            "WHERE id=:id AND status='running' AND lease_owner=:o AND fencing_token=:t "
            "RETURNING model_id"
        ),
        {"m": model_id, "id": lease.task_id, "o": lease.owner, "t": lease.token},
    ).scalar_one_or_none()
    if row is None:
        raise LeaseLost(f"task {lease.task_id} token {lease.token}")
    return str(row)
