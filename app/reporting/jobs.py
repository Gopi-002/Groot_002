"""Durable report jobs: requested in the SAME transaction as the terminal task
transition, dispatched through the transactional outbox to the Redis reports
stream, executed under a DB lease with a fencing token (same contract as tasks).

States: pending -> generating -> validated | fallback   (report persisted)
                               -> pending (retry, bounded, jittered backoff)
                               -> failed  (attempts exhausted; alert + notification)
Idempotency: one job per dedup key (one per terminal task); one report per job
(``reports.job_id`` unique); a report row and the job's terminal status are
committed together by a fenced write, so a duplicate delivery, a crash before
or after persistence, or a stale worker can never create a second report.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

from app.persistence.audit import audit

EVENT_TYPE = "report.generate"
AGGREGATE = "report_job"


class JobLeaseLost(Exception):
    """A fenced write matched no row: another worker owns the job now."""


@dataclass(frozen=True)
class JobLease:
    job_id: uuid.UUID
    incident_id: uuid.UUID
    task_id: uuid.UUID | None
    owner: str
    token: int
    attempt: int
    max_attempts: int
    model_id: str | None
    auth_mode: str | None
    progress: dict[str, Any]

    @property
    def final_attempt(self) -> bool:
        return self.attempt >= self.max_attempts


def enqueue_dispatch(
    conn: Connection, job_id: uuid.UUID, key: str, delay_seconds: float = 0.0
) -> None:
    conn.execute(
        text(
            "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, dedup_key, "
            "payload, next_attempt_at) VALUES (:agg, :j, :et, :k, CAST(:p AS jsonb), "
            "now() + make_interval(secs => :d)) ON CONFLICT (dedup_key) DO NOTHING"
        ),
        {
            "agg": AGGREGATE,
            "j": job_id,
            "et": EVENT_TYPE,
            "k": key,
            "p": json.dumps({"report_job_id": str(job_id)}),
            "d": max(0.0, delay_seconds),
        },
    )


def request_report(
    conn: Connection,
    *,
    incident_id: uuid.UUID,
    task_id: uuid.UUID | None,
    reason: str,
    actor: str,
    max_attempts: int = 4,
) -> uuid.UUID | None:
    """Idempotent: one report job per (task, reason). Returns the new job id, or
    None if it already existed."""
    key = f"report:{incident_id}:{task_id or '-'}:{reason}"
    row = conn.execute(
        text(
            "INSERT INTO report_jobs (incident_id, task_id, dedup_key, reason, max_attempts) "
            "VALUES (:i, :t, :k, :r, :m) ON CONFLICT (dedup_key) DO NOTHING RETURNING id"
        ),
        {"i": incident_id, "t": task_id, "k": key, "r": reason, "m": max_attempts},
    ).scalar_one_or_none()
    if row is None:
        return None
    job_id = uuid.UUID(str(row))
    enqueue_dispatch(conn, job_id, f"report_job:{job_id}:initial")
    audit(
        conn,
        actor_type="system",
        actor_id=actor,
        action="report_requested",
        entity_type="report_job",
        entity_id=job_id,
        details={"incident_id": str(incident_id), "task_id": str(task_id), "reason": reason},
    )
    return job_id


def claim(engine: Engine, job_id: uuid.UUID, owner: str, ttl_seconds: float) -> JobLease | None:
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "UPDATE report_jobs SET status='generating', lease_owner=:o, "
                    "lease_expires_at = now() + make_interval(secs => :ttl), "
                    "fencing_token = fencing_token + 1, attempt = attempt + 1 "
                    "WHERE id=:id AND attempt < max_attempts AND ("
                    "  (status='pending' AND next_attempt_at <= now()) "
                    "  OR (status='generating' AND lease_expires_at < now())) "
                    "RETURNING incident_id, task_id, fencing_token, attempt, max_attempts, "
                    "model_id, auth_mode, progress"
                ),
                {"id": job_id, "o": owner, "ttl": ttl_seconds},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        lease = JobLease(
            job_id,
            uuid.UUID(str(row["incident_id"])),
            uuid.UUID(str(row["task_id"])) if row["task_id"] else None,
            owner,
            int(row["fencing_token"]),
            int(row["attempt"]),
            int(row["max_attempts"]),
            row["model_id"],
            row["auth_mode"],
            dict(row["progress"] or {}),
        )
        audit(
            conn,
            actor_type="system",
            actor_id=owner,
            action="report_job_claimed",
            entity_type="report_job",
            entity_id=job_id,
            details={"fencing_token": lease.token, "attempt": lease.attempt},
        )
    return lease


def renew(engine: Engine, lease: JobLease, ttl_seconds: float) -> bool:
    with engine.begin() as conn:
        n = conn.execute(
            text(
                "UPDATE report_jobs SET lease_expires_at = now() + make_interval(secs => :ttl) "
                "WHERE id=:id AND status='generating' AND lease_owner=:o AND fencing_token=:t "
                "AND lease_expires_at > now()"
            ),
            {"id": lease.job_id, "o": lease.owner, "t": lease.token, "ttl": ttl_seconds},
        ).rowcount
    return bool(n)


def _fenced(conn: Connection, lease: JobLease, sql_set: str, params: dict[str, Any]) -> None:
    n = conn.execute(
        text(
            f"UPDATE report_jobs SET {sql_set} "  # noqa: S608 - fixed fragments only
            "WHERE id=:id AND status='generating' AND lease_owner=:o AND fencing_token=:t"
        ),
        {**params, "id": lease.job_id, "o": lease.owner, "t": lease.token},
    ).rowcount
    if n != 1:
        raise JobLeaseLost(f"report job {lease.job_id} token {lease.token}")


def save_progress(
    engine: Engine,
    lease: JobLease,
    progress: dict[str, Any],
    *,
    model_id: str | None = None,
    auth_mode: str | None = None,
) -> None:
    """Checkpoint (pinned model, attempts, rejections) - fenced."""
    with engine.begin() as conn:
        _fenced(
            conn,
            lease,
            "progress = CAST(:p AS jsonb), model_id = COALESCE(model_id, :m), "
            "auth_mode = COALESCE(auth_mode, :a)",
            {"p": json.dumps(progress, default=str), "m": model_id, "a": auth_mode},
        )


def complete(conn: Connection, lease: JobLease, status: str, report_id: uuid.UUID) -> None:
    _fenced(
        conn,
        lease,
        "status=:st, report_id=:r, lease_owner=NULL, lease_expires_at=NULL, "
        "completed_at=now(), last_error=NULL",
        {"st": status, "r": report_id},
    )


def retry_later(
    engine: Engine,
    lease: JobLease,
    delay_seconds: float,
    error: str,
    *,
    refund_attempt: bool = False,
) -> None:
    """Back to pending with a future due time, plus a delayed outbox dispatch."""
    with engine.begin() as conn:
        _fenced(
            conn,
            lease,
            "status='pending', lease_owner=NULL, lease_expires_at=NULL, last_error=:e, "
            "attempt = attempt - CASE WHEN :refund THEN 1 ELSE 0 END, "
            "next_attempt_at = now() + make_interval(secs => :d)",
            {"e": error[:300], "d": delay_seconds, "refund": refund_attempt},
        )
        enqueue_dispatch(
            conn,
            lease.job_id,
            f"report_job:{lease.job_id}:retry:{lease.token}",
            delay_seconds,
        )
        audit(
            conn,
            actor_type="system",
            actor_id=lease.owner,
            action="report_retry_scheduled",
            entity_type="report_job",
            entity_id=lease.job_id,
            details={"attempt": lease.attempt, "error": error[:300], "retry_in_s": delay_seconds},
        )


def fail_if_exhausted(engine: Engine, job_id: uuid.UUID, actor: str) -> bool:
    """A job whose lease expired on its final attempt can't be claimed again:
    mark it failed (the incident history itself is untouched) and notify."""
    from app.notifications.events import notify_report_failed

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE report_jobs SET status='failed', lease_owner=NULL, "
                "lease_expires_at=NULL, completed_at=now(), fencing_token=fencing_token+1, "
                "last_error=COALESCE(last_error, 'lease expired on final attempt') "
                "WHERE id=:id AND attempt >= max_attempts AND ("
                "  (status='generating' AND lease_expires_at < now()) "
                "  OR (status='pending' AND next_attempt_at <= now())) RETURNING incident_id"
            ),
            {"id": job_id},
        ).scalar_one_or_none()
        if row is None:
            return False
        audit(
            conn,
            actor_type="system",
            actor_id=actor,
            action="report_job_failed",
            entity_type="report_job",
            entity_id=job_id,
            details={"reason": "attempts_exhausted"},
        )
        notify_report_failed(conn, job_id, uuid.UUID(str(row)), "attempts_exhausted")
    return True


def schedule_report_jobs(engine: Engine, redispatch_after_seconds: float) -> int:
    """Dispatcher backstop: re-dispatch report jobs whose message was lost or
    whose worker died (expired lease). One per job per window (dedup key)."""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, "
                    "dedup_key, payload) "
                    "SELECT 'report_job', j.id, 'report.generate', "
                    "'report_job:' || j.id || ':reconcile:' || j.fencing_token || ':' || "
                    "floor(extract(epoch FROM now()) / :w)::bigint, "
                    "jsonb_build_object('report_job_id', j.id, 'reason', 'reconcile') "
                    "FROM report_jobs j WHERE ("
                    "  (j.status = 'pending' "
                    "   AND j.next_attempt_at < now() - make_interval(secs => :g))"
                    "  OR (j.status = 'generating' AND j.lease_expires_at < now())) "
                    "AND NOT EXISTS (SELECT 1 FROM outbox_events o WHERE o.aggregate_id = j.id "
                    "   AND (o.published_at IS NULL "
                    "        OR o.published_at > now() - make_interval(secs => :w))) "
                    "ON CONFLICT (dedup_key) DO NOTHING RETURNING aggregate_id"
                ),
                {"w": redispatch_after_seconds, "g": min(30.0, redispatch_after_seconds)},
            )
            .scalars()
            .all()
        )
        for job_id in rows:
            audit(
                conn,
                actor_type="system",
                actor_id="dispatcher",
                action="report_job_redispatched",
                entity_type="report_job",
                entity_id=job_id,
                details={"reason": "reconcile"},
            )
    return len(rows)
