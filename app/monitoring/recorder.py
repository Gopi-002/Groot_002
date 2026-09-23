"""Durable recording of checks and incident creation (workflow steps 2-3).

``record_check`` runs in ONE transaction: health check row, detection state,
and - on threshold breach - incident + task + evidence + outbox event + audit.
Either all of it commits or none of it does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, text

from app.monitoring.detector import (
    DETECTED_TYPES,
    Action,
    Decision,
    Thresholds,
    TypeState,
    reset_stale,
    step,
)
from app.monitoring.probe import CheckResult
from app.persistence.audit import audit
from app.reporting.jobs import request_report

log = logging.getLogger("sentinelops.monitor.recorder")

ACTOR = "monitor"
SEVERITY = {"unavailable": "high", "http_error": "high", "high_latency": "medium"}
ACTIVE_SQL = "('open','investigating','remediating','waiting_approval','escalated')"


@dataclass
class RecordOutcome:
    check_id: uuid.UUID
    opened_incidents: list[uuid.UUID] = field(default_factory=list)
    created_tasks: list[uuid.UUID] = field(default_factory=list)
    bumped_incidents: list[uuid.UUID] = field(default_factory=list)
    resolved_incidents: list[uuid.UUID] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)


def upsert_service(engine: Engine, name: str, base_url: str) -> uuid.UUID:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO services (name, base_url) VALUES (:n, :u) ON CONFLICT (name) "
                "DO UPDATE SET base_url = EXCLUDED.base_url RETURNING id"
            ),
            {"n": name, "u": base_url},
        ).scalar_one()
    return uuid.UUID(str(row))


def _load_states(conn: Connection, service_id: uuid.UUID) -> dict[str, TypeState]:
    conn.execute(
        text(
            "INSERT INTO detection_state (service_id, failure_type) "
            "SELECT :s, t FROM unnest(CAST(:types AS text[])) AS t "
            "ON CONFLICT (service_id, failure_type) DO NOTHING"
        ),
        {"s": service_id, "types": list(DETECTED_TYPES)},
    )
    rows = conn.execute(
        text(
            "SELECT failure_type, armed, consecutive_count, healthy_streak, streak_check_ids, "
            "first_failure_at, last_failure_at, last_check_at FROM detection_state "
            "WHERE service_id = :s ORDER BY failure_type FOR UPDATE"
        ),
        {"s": service_id},
    ).mappings()
    return {
        r["failure_type"]: TypeState(
            failure_type=r["failure_type"],
            armed=r["armed"],
            consecutive_count=r["consecutive_count"],
            healthy_streak=r["healthy_streak"],
            streak_check_ids=tuple(r["streak_check_ids"] or ()),
            first_failure_at=r["first_failure_at"],
            last_failure_at=r["last_failure_at"],
            last_check_at=r["last_check_at"],
        )
        for r in rows
    }


def _save_state(
    conn: Connection,
    service_id: uuid.UUID,
    st: TypeState,
    incident_id: uuid.UUID | None,
    touch_incident: bool,
) -> None:
    conn.execute(
        text(
            "UPDATE detection_state SET armed=:armed, consecutive_count=:cc, "  # noqa: S608 - only a constant column clause is concatenated
            "healthy_streak=:hs, streak_check_ids=CAST(:ids AS uuid[]), "
            "first_failure_at=:ff, last_failure_at=:lf, last_check_at=:lc"
            + (", active_incident_id=:inc" if touch_incident else "")
            + " WHERE service_id=:s AND failure_type=:t"
        ),
        {
            "armed": st.armed,
            "cc": st.consecutive_count,
            "hs": st.healthy_streak,
            "ids": [str(i) for i in st.streak_check_ids],
            "ff": st.first_failure_at,
            "lf": st.last_failure_at,
            "lc": st.last_check_at,
            "inc": incident_id,
            "s": service_id,
            "t": st.failure_type,
        },
    )


def _insert_check(conn: Connection, service_id: uuid.UUID, c: CheckResult) -> uuid.UUID:
    row = conn.execute(
        text(
            "INSERT INTO health_checks (service_id, checked_at, outcome, http_status, "
            "latency_ms, error_type) VALUES (:s, :at, :o, :h, :l, :e) RETURNING id"
        ),
        {
            "s": service_id,
            "at": c.checked_at,
            "o": c.outcome,
            "h": c.http_status,
            "l": c.latency_ms,
            "e": c.error_type,
        },
    ).scalar_one()
    return uuid.UUID(str(row))


def _canonical(obj: Any) -> tuple[str, str]:
    body = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return body, hashlib.sha256(body.encode()).hexdigest()


def _open_incident(
    conn: Connection,
    service_id: uuid.UUID,
    d: Decision,
    at: datetime,
    thresholds: Thresholds,
    max_attempts: int,
    out: RecordOutcome,
) -> uuid.UUID:
    st = d.state
    row = conn.execute(
        text(
            "INSERT INTO incidents (service_id, incident_type, severity, summary, "  # noqa: S608 - ACTIVE_SQL is a module constant
            "first_failure_at, last_failure_at, opened_at, last_seen_at) "
            "VALUES (:s, :t, :sev, :sum, :ff, :lf, :at, :at) "
            f"ON CONFLICT (service_id, incident_type) WHERE status IN {ACTIVE_SQL} "
            "DO UPDATE SET occurrence_count = incidents.occurrence_count + 1, "
            "last_seen_at = GREATEST(incidents.last_seen_at, EXCLUDED.last_seen_at), "
            "last_failure_at = EXCLUDED.last_failure_at "
            "RETURNING id, (xmax = 0) AS inserted"
        ),
        {
            "s": service_id,
            "t": d.failure_type,
            "sev": SEVERITY[d.failure_type],
            "sum": f"{thresholds.for_type(d.failure_type)} consecutive {d.failure_type} checks",
            "ff": st.first_failure_at or at,
            "lf": st.last_failure_at or at,
            "at": at,
        },
    ).one()
    incident_id, inserted = uuid.UUID(str(row.id)), bool(row.inserted)
    if not inserted:
        out.bumped_incidents.append(incident_id)
        audit(
            conn,
            actor_type="system",
            actor_id=ACTOR,
            action="incident_recurrence",
            entity_type="incident",
            entity_id=incident_id,
            details={"failure_type": d.failure_type},
        )
        return incident_id

    checks = (
        conn.execute(
            text(
                "SELECT id, checked_at, outcome, http_status, latency_ms, error_type "
                "FROM health_checks WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY checked_at"
            ),
            {"ids": [str(i) for i in d.evidence_check_ids]},
        )
        .mappings()
        .all()
    )
    evidence = {
        "rule": {
            "failure_type": d.failure_type,
            "threshold": thresholds.for_type(d.failure_type),
            "consecutive": True,
        },
        "first_failure_at": st.first_failure_at,
        "last_failure_at": st.last_failure_at,
        "checks": [dict(c) for c in checks],
    }
    body, digest = _canonical(evidence)
    task_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    "INSERT INTO tasks (incident_id, idempotency_key, max_attempts) "
                    "VALUES (:i, :k, :m) RETURNING id"
                ),
                {"i": incident_id, "k": f"incident:{incident_id}:intake", "m": max_attempts},
            ).scalar_one()
        )
    )
    conn.execute(
        text(
            "INSERT INTO evidence (incident_id, task_id, source, content, content_sha256) "
            "VALUES (:i, :t, 'health_check', CAST(:c AS jsonb), :h) "
            "ON CONFLICT (incident_id, source, content_sha256) DO NOTHING"
        ),
        {"i": incident_id, "t": task_id, "c": body, "h": digest},
    )
    conn.execute(
        text(
            "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, dedup_key, "
            "payload) VALUES ('task', :t, 'task.dispatch', :k, CAST(:p AS jsonb))"
        ),
        {
            "t": task_id,
            "k": f"task:{task_id}:dispatch:initial",
            "p": json.dumps({"task_id": str(task_id), "incident_id": str(incident_id)}),
        },
    )
    audit(
        conn,
        actor_type="system",
        actor_id=ACTOR,
        action="incident_opened",
        entity_type="incident",
        entity_id=incident_id,
        details={
            "failure_type": d.failure_type,
            "task_id": str(task_id),
            "evidence_sha256": digest,
        },
    )
    out.opened_incidents.append(incident_id)
    out.created_tasks.append(task_id)
    return incident_id


def _bump(
    conn: Connection,
    service_id: uuid.UUID,
    d: Decision,
    at: datetime,
    thresholds: Thresholds,
    max_attempts: int,
    out: RecordOutcome,
) -> uuid.UUID:
    row = conn.execute(
        text(
            "UPDATE incidents SET occurrence_count = occurrence_count + 1, "  # noqa: S608 - ACTIVE_SQL is a module constant
            "last_seen_at = GREATEST(last_seen_at, :at), last_failure_at = :at "
            f"WHERE service_id=:s AND incident_type=:t AND status IN {ACTIVE_SQL} RETURNING id"
        ),
        {"s": service_id, "t": d.failure_type, "at": at},
    ).scalar_one_or_none()
    if row is None:
        # Disarmed but no active incident (e.g. closed manually): open a new one.
        return _open_incident(
            conn,
            service_id,
            replace(d, evidence_check_ids=d.state.streak_check_ids),
            at,
            thresholds,
            max_attempts,
            out,
        )
    out.bumped_incidents.append(uuid.UUID(str(row)))
    return uuid.UUID(str(row))


def _auto_resolve(
    conn: Connection,
    service_id: uuid.UUID,
    failure_type: str,
    healthy_streak: int,
    out: RecordOutcome,
) -> None:
    """Resolve an incident that recovered by itself once recovery hysteresis is
    met: status 'open' or 'investigating' with no task currently running (AI
    findings stay persisted). Remediating / awaiting-approval / escalated
    incidents are never auto-resolved. Lock order: incident, then task
    (same order as the worker) to avoid deadlocks."""
    ids = (
        conn.execute(
            text(
                "UPDATE incidents i SET status='resolved', resolved_at=now(), "
                "resolution='auto_recovered' "
                "WHERE service_id=:s AND incident_type=:t AND status IN ('open','investigating') "
                "AND NOT EXISTS (SELECT 1 FROM tasks k WHERE k.incident_id=i.id "
                "AND k.status='running') RETURNING id"
            ),
            {"s": service_id, "t": failure_type},
        )
        .scalars()
        .all()
    )
    for raw in ids:
        iid = uuid.UUID(str(raw))
        closed = (
            conn.execute(
                text(
                    "UPDATE tasks SET status='resolved', outcome='incident_auto_recovered', "
                    "completed_at=now(), fencing_token=fencing_token+1, lease_owner=NULL, "
                    "lease_expires_at=NULL, next_attempt_at=NULL WHERE incident_id=:i "
                    "AND status IN ('queued','retry_scheduled','awaiting_investigation',"
                    "'awaiting_policy') RETURNING id"
                ),
                {"i": iid},
            )
            .scalars()
            .all()
        )
        # Step 9/10: the incident history gets a report even when it self-healed.
        request_report(
            conn,
            incident_id=iid,
            task_id=uuid.UUID(str(closed[0])) if closed else None,
            reason="incident_auto_recovered",
            actor=ACTOR,
        )
        audit(
            conn,
            actor_type="system",
            actor_id=ACTOR,
            action="incident_auto_resolved",
            entity_type="incident",
            entity_id=iid,
            details={"failure_type": failure_type, "healthy_streak": healthy_streak},
        )
        out.resolved_incidents.append(iid)


def record_check(
    engine: Engine,
    service_id: uuid.UUID,
    check: CheckResult,
    thresholds: Thresholds,
    *,
    stale_after: timedelta,
    max_attempts: int,
) -> RecordOutcome:
    with engine.begin() as conn:
        states = _load_states(conn, service_id)
        for t, st in states.items():
            if st.last_check_at is not None and check.checked_at - st.last_check_at > stale_after:
                log.info("stale detection state reset", extra={"failure_type": t})
                states[t] = reset_stale(st)
        check_id = _insert_check(conn, service_id, check)
        out = RecordOutcome(check_id=check_id)
        new_states, decisions = step(states, check, check_id, thresholds)
        out.decisions = decisions
        linked: dict[str, uuid.UUID | None] = {}
        for d in decisions:
            if d.action is Action.OPEN:
                linked[d.failure_type] = _open_incident(
                    conn, service_id, d, check.checked_at, thresholds, max_attempts, out
                )
            elif d.action is Action.BUMP:
                linked[d.failure_type] = _bump(
                    conn, service_id, d, check.checked_at, thresholds, max_attempts, out
                )
            elif d.action is Action.REARM:
                linked[d.failure_type] = None
            elif d.action is Action.RECOVERED:
                _auto_resolve(conn, service_id, d.failure_type, d.state.healthy_streak, out)
        for t, st in new_states.items():
            _save_state(conn, service_id, st, linked.get(t), t in linked)
    return out


def prune_health_checks(engine: Engine, retention_days: int) -> int:
    with engine.begin() as conn:
        res = conn.execute(
            text("DELETE FROM health_checks WHERE checked_at < now() - make_interval(days => :d)"),
            {"d": retention_days},
        )
    return int(res.rowcount or 0)
