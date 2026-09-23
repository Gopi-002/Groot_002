"""Logical notification events, enqueued in the SAME transaction as the state
change they describe (transactional outbox): a notification can never be lost
when the change commits, and a provider outage can never roll the change back.

Payloads are built deterministically from records - never from model prose -
through a key whitelist plus redaction, so secrets (API keys, operator tokens,
signing keys, credentials, action fingerprints) and raw logs cannot leak.
Each event has a dedup key: re-running the same transition, a duplicate
delivery, or a worker recovery yields exactly one logical event.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from app.observability.logging import redact, redact_text
from app.persistence.audit import audit

log = logging.getLogger("sentinelops.notifications")

EVENT_TYPES = frozenset(
    {
        "approval_required",
        "incident_escalated",
        "remediation_performed",
        "recovery_verification_failed",
        "ai_paused",
        "task_dead_lettered",
        "system_degraded",
        "alert_resolved",
        "report_ready",
        "report_failed",
        "test",
    }
)
SEVERITIES = ("info", "warning", "critical")

# The ONLY keys a payload may carry. Anything else is dropped.
SAFE_KEYS = frozenset(
    {
        "title",
        "summary",
        "incident_id",
        "service",
        "incident_type",
        "incident_status",
        "severity_of_incident",
        "task_id",
        "task_status",
        "outcome",
        "approval_id",
        "proposed_action",
        "target_service",
        "risk",
        "expires_at",
        "instructions",
        "report_id",
        "report_version",
        "generation_mode",
        "fallback_reason",
        "report_job_id",
        "alert",
        "since",
        "details",
        "runbook",
    }
)
SAFE_DETAIL_KEYS = frozenset(
    {
        "age_seconds",
        "count",
        "threshold_seconds",
        "reasons",
        "oldest_age_seconds",
        "paused_tasks",
        "last_status",
        "daily_usage",
        "daily_limit",
        "services",
        "status",
        "scope",
    }
)


def _safe_value(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, list | tuple):
        return [_safe_value(x) for x in list(v)[:20]]
    return redact_text(str(v))[:600]


def sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in redact(payload).items():
        if k not in SAFE_KEYS:
            continue
        if k == "details" and isinstance(v, dict):
            out[k] = {dk: _safe_value(dv) for dk, dv in v.items() if dk in SAFE_DETAIL_KEYS}
        else:
            out[k] = _safe_value(v)
    return out


def enqueue(
    conn: Connection,
    *,
    event_type: str,
    severity: str,
    dedup_key: str,
    payload: dict[str, Any],
    incident_id: uuid.UUID | None = None,
    actor: str = "system",
) -> uuid.UUID | None:
    """Insert one logical event (idempotent by ``dedup_key``). Returns its id if new."""
    if event_type not in EVENT_TYPES or severity not in SEVERITIES:
        raise ValueError(f"invalid notification {event_type}/{severity}")
    body = sanitize({**payload, "incident_id": incident_id})
    row = conn.execute(
        text(
            "INSERT INTO notification_events (event_type, severity, dedup_key, incident_id, "
            "payload) VALUES (:t, :s, :k, :i, CAST(:p AS jsonb)) "
            "ON CONFLICT (dedup_key) DO NOTHING RETURNING id"
        ),
        {
            "t": event_type,
            "s": severity,
            "k": dedup_key[:300],
            "i": incident_id,
            "p": json.dumps(body, default=str),
        },
    ).scalar_one_or_none()
    if row is None:
        return None
    event_id = uuid.UUID(str(row))
    audit(
        conn,
        actor_type="system",
        actor_id=actor,
        action="notification_queued",
        entity_type="notification",
        entity_id=event_id,
        details={"event_type": event_type, "severity": severity},
    )
    return event_id


def _incident(conn: Connection, incident_id: uuid.UUID) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                "SELECT s.name AS service, i.incident_type, i.status AS incident_status, "
                "i.severity AS severity_of_incident FROM incidents i "
                "JOIN services s ON s.id = i.service_id WHERE i.id = :i"
            ),
            {"i": incident_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else {}


# --- event builders ---------------------------------------------------------------------------


def notify_task_terminal(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    incident_id: uuid.UUID,
    status: str,
    outcome: str | None,
    actor: str,
) -> uuid.UUID | None:
    facts = _incident(conn, incident_id)
    base = {**facts, "task_id": task_id, "task_status": status, "outcome": outcome}
    svc = facts.get("service", "?")
    if status == "resolved" and outcome == "recovery_verified":
        approved = conn.execute(
            text("SELECT 1 FROM approvals WHERE incident_id=:i AND status='approved' LIMIT 1"),
            {"i": incident_id},
        ).first()
        how = "after human approval" if approved else "autonomously (preauthorized)"
        return enqueue(
            conn,
            event_type="remediation_performed",
            severity="info",
            dedup_key=f"task:{task_id}:remediation_performed",
            incident_id=incident_id,
            actor=actor,
            payload={
                **base,
                "title": f"{svc}: restart performed {how}; recovery verified",
                "summary": "The restricted executor restarted the demo application once and "
                "the deterministic verifier confirmed recovery.",
            },
        )
    if status == "escalated" and outcome == "recovery_failed":
        return enqueue(
            conn,
            event_type="recovery_verification_failed",
            severity="critical",
            dedup_key=f"task:{task_id}:recovery_verification_failed",
            incident_id=incident_id,
            actor=actor,
            payload={
                **base,
                "title": f"{svc}: restart did not restore health - incident OPEN, human needed",
                "summary": "Recovery verification failed after the single permitted restart. "
                "No further automated action will be taken.",
                "runbook": "docs/runbook.md#recovery-verification-failed",
            },
        )
    if status == "dead_lettered":
        return enqueue(
            conn,
            event_type="task_dead_lettered",
            severity="critical",
            dedup_key=f"task:{task_id}:dead_lettered",
            incident_id=incident_id,
            actor=actor,
            payload={
                **base,
                "title": f"{svc}: task dead-lettered after exhausting retries - human needed",
                "summary": "The incident task failed repeatedly and was dead-lettered; the "
                "incident is escalated and remains open.",
                "runbook": "docs/runbook.md#dead-lettered-tasks",
            },
        )
    if status in ("escalated", "failed"):
        return enqueue(
            conn,
            event_type="incident_escalated",
            severity="warning"
            if outcome in ("policy_denied", "escalated_by_proposal")
            else "critical",
            dedup_key=f"task:{task_id}:escalated",
            incident_id=incident_id,
            actor=actor,
            payload={
                **base,
                "title": f"{svc}: incident escalated ({outcome}) - human needed",
                "summary": "SentinelOps stopped autonomous handling of this incident; it "
                "remains open for an operator.",
                "runbook": "docs/runbook.md#escalated-incidents",
            },
        )
    return None


def notify_approval_required(
    conn: Connection,
    *,
    approval_id: uuid.UUID,
    incident_id: uuid.UUID,
    task_id: uuid.UUID,
    action: str,
    target_service: str,
    risk: str,
    expires_at: datetime | None,
) -> uuid.UUID | None:
    facts = _incident(conn, incident_id)
    return enqueue(
        conn,
        event_type="approval_required",
        severity="warning",
        dedup_key=f"approval:{approval_id}:required",
        incident_id=incident_id,
        actor="policy",
        payload={
            **facts,
            "task_id": task_id,
            "title": f"{facts.get('service', '?')}: approval required for {action}",
            "approval_id": approval_id,
            "proposed_action": action,
            "target_service": target_service,
            "risk": risk,
            "expires_at": expires_at,
            "instructions": (
                f"Review with GET /v1/approvals/{approval_id} using your operator token. "
                f"Decide with POST /v1/approvals/{approval_id}/approve or /reject, sending the "
                "action_fingerprint from that response, with an approver token. This "
                "notification is NOT an approval; silence expires and fails closed."
            ),
        },
    )


def notify_report_ready(
    conn: Connection,
    *,
    report_id: uuid.UUID,
    job_id: uuid.UUID,
    incident_id: uuid.UUID,
    version: int,
    mode: str,
    fallback_reason: str | None,
) -> uuid.UUID | None:
    facts = _incident(conn, incident_id)
    return enqueue(
        conn,
        event_type="report_ready",
        severity="info",
        dedup_key=f"report_job:{job_id}:ready",
        incident_id=incident_id,
        actor="reporter",
        payload={
            **facts,
            "title": f"{facts.get('service', '?')}: incident report v{version} ready ({mode})",
            "report_id": report_id,
            "report_version": version,
            "report_job_id": job_id,
            "generation_mode": mode,
            "fallback_reason": fallback_reason,
            "instructions": f"GET /v1/incidents/{incident_id}/report (read token).",
        },
    )


def notify_report_failed(
    conn: Connection, job_id: uuid.UUID, incident_id: uuid.UUID, reason: str
) -> uuid.UUID | None:
    facts = _incident(conn, incident_id)
    return enqueue(
        conn,
        event_type="report_failed",
        severity="warning",
        dedup_key=f"report_job:{job_id}:failed",
        incident_id=incident_id,
        actor="reporter",
        payload={
            **facts,
            "title": f"{facts.get('service', '?')}: incident report could not be generated",
            "report_job_id": job_id,
            "summary": f"Report job failed ({reason}). The incident history in PostgreSQL is "
            "intact; request a new report or read /v1/incidents/<id>.",
            "runbook": "docs/runbook.md#report-failures",
        },
    )


def notify_alert(
    conn: Connection,
    *,
    alert: str,
    severity: str,
    firing: bool,
    since: datetime,
    title: str,
    details: dict[str, Any],
    ai_related: bool,
    repeat_bucket: int = 0,
) -> uuid.UUID | None:
    if firing:
        event_type = "ai_paused" if ai_related else "system_degraded"
        key = f"alert:{alert}:{since.isoformat()}:firing:{repeat_bucket}"
    else:
        event_type, severity = "alert_resolved", "info"
        key = f"alert:{alert}:{since.isoformat()}:resolved"
    return enqueue(
        conn,
        event_type=event_type,
        severity=severity,
        dedup_key=key,
        actor="alerts",
        payload={
            "title": title if firing else f"RESOLVED: {title}",
            "alert": alert,
            "since": since,
            "details": details,
            "runbook": f"docs/runbook.md#alert-{alert.replace('_', '-')}",
        },
    )
