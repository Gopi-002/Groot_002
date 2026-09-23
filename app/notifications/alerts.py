"""Deterministic in-app alerting (05 task 3), evaluated by the notifier from
PostgreSQL. Each rule is a pure SQL predicate; ``alert_state`` makes firing
edge-triggered (notify once on ok->firing, re-notify every
``alert_repeat_seconds`` while firing, and once on resolve).

Host-down cannot be detected from inside the host: that requires the EXTERNAL
uptime check documented in docs/deployment.md (plus the Prometheus rules in
deploy/observability/, which also cover this notifier being down).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, text

from app.config import Settings
from app.notifications.events import notify_alert
from app.persistence.audit import audit

log = logging.getLogger("sentinelops.alerts")

AUTH_OUTCOMES = (
    "ai_paused_authentication_failed",
    "ai_paused_credentials_missing",
    "ai_paused_permission_denied",
)
Evaluator = Callable[[Connection, Settings], tuple[bool, dict[str, Any]]]


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str
    title: str
    ai_related: bool
    evaluate: Evaluator


def _scalar(conn: Connection, sql: str, **p: Any) -> Any:
    return conn.execute(text(sql), p).scalar()


def _monitor_silent(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    age = _scalar(conn, "SELECT extract(epoch FROM now() - max(checked_at)) FROM health_checks")
    if age is None:  # fresh install: nothing recorded yet
        return False, {}
    return float(age) > s.alert_monitor_silence_seconds, {
        "age_seconds": round(float(age), 1),
        "threshold_seconds": s.alert_monitor_silence_seconds,
    }


def _queue_backlog(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    outbox = float(
        _scalar(
            conn,
            "SELECT COALESCE(extract(epoch FROM now() - min(next_attempt_at)), 0) "
            "FROM outbox_events WHERE published_at IS NULL AND next_attempt_at <= now()",
        )
        or 0
    )
    queued = float(
        _scalar(
            conn,
            "SELECT COALESCE(extract(epoch FROM now() - min(updated_at)), 0) FROM tasks "
            "WHERE status = 'queued'",
        )
        or 0
    )
    oldest = max(outbox, queued)
    return oldest > s.alert_queue_backlog_seconds, {
        "oldest_age_seconds": round(oldest, 1),
        "threshold_seconds": s.alert_queue_backlog_seconds,
    }


def _ai_auth(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    parked = int(
        _scalar(
            conn,
            "SELECT count(*) FROM tasks WHERE status='awaiting_investigation' "
            "AND outcome = ANY(CAST(:o AS text[]))",
            o=list(AUTH_OUTCOMES),
        )
        or 0
    )
    recent = conn.execute(
        text(
            "SELECT outcome FROM ai_usage WHERE occurred_at > now() - interval '15 minutes' "
            "ORDER BY occurred_at DESC LIMIT 1"
        )
    ).scalar()
    failing = recent in ("authentication_failed", "credentials_missing", "permission_denied")
    return parked > 0 or failing, {
        "paused_tasks": parked,
        "last_status": recent,
        "reasons": ["Rotate or re-enter the API key: docker compose run --rm onboard set-key"],
    }


def _ai_paused(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT outcome, count(*) FROM tasks WHERE status='awaiting_investigation' "
            "AND outcome LIKE 'ai\\_paused\\_%' GROUP BY 1 ORDER BY 1"
        )
    ).all()
    total = sum(int(n) for _, n in rows)
    return total > 0, {"paused_tasks": total, "reasons": [str(o) for o, _ in rows]}


def _ai_budget(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    if s.ai_daily_max_tokens is None:
        return False, {}
    used = int(
        _scalar(
            conn,
            "SELECT COALESCE(sum(input_tokens + output_tokens + cache_read_tokens + "
            "cache_write_tokens), 0) FROM ai_usage WHERE occurred_at >= "
            "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
        )
        or 0
    )
    return used >= s.ai_daily_max_tokens, {
        "daily_usage": used,
        "daily_limit": s.ai_daily_max_tokens,
    }


def _stalled_incident(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    n = int(
        _scalar(
            conn,
            "SELECT count(*) FROM incidents i WHERE i.status IN "
            "('open','investigating','remediating') "
            "AND i.opened_at < now() - make_interval(secs => :t) AND NOT EXISTS ("
            "  SELECT 1 FROM tasks k WHERE k.incident_id = i.id "
            "  AND k.updated_at > now() - make_interval(secs => :t))",
            t=s.alert_stalled_incident_seconds,
        )
        or 0
    )
    return n > 0, {"count": n, "threshold_seconds": s.alert_stalled_incident_seconds}


def _backup_failed(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    last = conn.execute(
        text("SELECT status, started_at FROM backup_runs ORDER BY started_at DESC LIMIT 1")
    ).first()
    if last is not None and last.status == "failed":
        return True, {"last_status": "failed"}
    if s.backup_max_age_hours is None:
        return False, {"last_status": last.status if last else None}
    age = _scalar(
        conn,
        "SELECT extract(epoch FROM now() - max(completed_at)) FROM backup_runs "
        "WHERE status='succeeded'",
    )
    limit = s.backup_max_age_hours * 3600
    return age is None or float(age) > limit, {
        "age_seconds": round(float(age), 1) if age is not None else None,
        "threshold_seconds": limit,
    }


def _notifications_failing(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    dead = int(
        _scalar(
            conn,
            "SELECT count(*) FROM notification_deliveries WHERE status='dead_lettered' "
            "AND updated_at > now() - interval '24 hours'",
        )
        or 0
    )
    oldest = float(
        _scalar(
            conn,
            "SELECT COALESCE(extract(epoch FROM now() - min(created_at)), 0) "
            "FROM notification_deliveries WHERE status IN ('pending','sending')",
        )
        or 0
    )
    return dead > 0 or oldest > s.alert_notification_backlog_seconds, {
        "count": dead,
        "oldest_age_seconds": round(oldest, 1),
        "threshold_seconds": s.alert_notification_backlog_seconds,
    }


def _report_failures(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    n = int(
        _scalar(
            conn,
            "SELECT count(*) FROM report_jobs WHERE status='failed' "
            "AND updated_at > now() - interval '24 hours'",
        )
        or 0
    )
    return n > 0, {"count": n}


def _service_down(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT service, extract(epoch FROM now() - max(last_seen_at)) AS age "
            "FROM service_heartbeats WHERE service IN ('worker','dispatcher') GROUP BY 1"
        )
    ).all()
    down = sorted(r.service for r in rows if float(r.age) > s.alert_service_silence_seconds)
    return bool(down), {"services": down, "threshold_seconds": s.alert_service_silence_seconds}


def _executor_unreachable(conn: Connection, s: Settings) -> tuple[bool, dict[str, Any]]:
    status = conn.execute(
        text(
            "SELECT details->>'executor' FROM service_heartbeats WHERE service='worker' "
            "AND last_seen_at > now() - interval '5 minutes' ORDER BY last_seen_at DESC LIMIT 1"
        )
    ).scalar()
    return status == "unreachable", {"status": status}


RULES: tuple[AlertRule, ...] = (
    AlertRule(
        "monitor_silent",
        "critical",
        "Monitor silent: no health checks recorded",
        False,
        _monitor_silent,
    ),
    AlertRule(
        "queue_backlog",
        "critical",
        "Queue backlog: work is not being dispatched",
        False,
        _queue_backlog,
    ),
    AlertRule(
        "ai_auth_failed",
        "critical",
        "AI authentication failed or expired - AI paused",
        True,
        _ai_auth,
    ),
    AlertRule(
        "ai_paused", "warning", "AI processing paused; monitoring continues", True, _ai_paused
    ),
    AlertRule(
        "ai_budget_exhausted",
        "warning",
        "Daily AI budget spent - AI paused until next UTC day",
        True,
        _ai_budget,
    ),
    AlertRule(
        "stalled_incident", "warning", "Incident without task progress", False, _stalled_incident
    ),
    AlertRule("backup_failed", "critical", "Backup failed or overdue", False, _backup_failed),
    AlertRule(
        "notifications_failing",
        "warning",
        "Notification delivery failing",
        False,
        _notifications_failing,
    ),
    AlertRule(
        "report_failures", "warning", "Incident report generation failed", False, _report_failures
    ),
    AlertRule(
        "service_down", "critical", "SentinelOps service heartbeat stale", False, _service_down
    ),
    AlertRule(
        "executor_unreachable",
        "critical",
        "Remediation executor unreachable from worker",
        False,
        _executor_unreachable,
    ),
)


def evaluate(
    engine: Engine, settings: Settings, rules: tuple[AlertRule, ...] = RULES
) -> dict[str, bool]:
    """Evaluate every rule; update ``alert_state``; enqueue edge notifications."""
    out: dict[str, bool] = {}
    now = datetime.now(UTC)
    for rule in rules:
        try:
            with engine.begin() as conn:
                firing, details = rule.evaluate(conn, settings)
                _apply(conn, rule, firing, details, now, settings.alert_repeat_seconds)
            out[rule.name] = firing
        except Exception as exc:  # one broken rule must not stop the others
            log.warning(
                "alert rule failed", extra={"alert": rule.name, "error_type": type(exc).__name__}
            )
    return out


def _apply(
    conn: Connection,
    rule: AlertRule,
    firing: bool,
    details: dict[str, Any],
    now: datetime,
    repeat_seconds: float,
) -> None:
    import json

    cur = conn.execute(
        text("SELECT status, since, last_notified_at FROM alert_state WHERE name=:n FOR UPDATE"),
        {"n": rule.name},
    ).first()
    status = "firing" if firing else "ok"
    if cur is None or cur.status != status:
        since = now
        conn.execute(
            text(
                "INSERT INTO alert_state (name, status, severity, since, details, updated_at) "
                "VALUES (:n, :s, :sev, :since, CAST(:d AS jsonb), now()) ON CONFLICT (name) DO "
                "UPDATE SET status=EXCLUDED.status, severity=EXCLUDED.severity, "
                "since=EXCLUDED.since, details=EXCLUDED.details, updated_at=now(), "
                "last_notified_at=CASE WHEN EXCLUDED.status='ok' THEN alert_state.last_notified_at "
                "ELSE NULL END"
            ),
            {
                "n": rule.name,
                "s": status,
                "sev": rule.severity,
                "since": since,
                "d": json.dumps(details, default=str),
            },
        )
        if firing or (cur is not None and cur.status == "firing"):
            origin = since if firing else cur.since  # type: ignore[union-attr]
            notify_alert(
                conn,
                alert=rule.name,
                severity=rule.severity,
                firing=firing,
                since=origin,
                title=rule.title,
                details=details,
                ai_related=rule.ai_related,
            )
            conn.execute(
                text("UPDATE alert_state SET last_notified_at=now() WHERE name=:n"),
                {"n": rule.name},
            )
            audit(
                conn,
                actor_type="system",
                actor_id="alerts",
                action="alert_firing" if firing else "alert_resolved",
                entity_type="alert",
                entity_id=None,
                details={"alert": rule.name, "severity": rule.severity},
            )
        return
    conn.execute(
        text("UPDATE alert_state SET details=CAST(:d AS jsonb), updated_at=now() WHERE name=:n"),
        {"n": rule.name, "d": json.dumps(details, default=str)},
    )
    if firing and cur.last_notified_at is not None:
        elapsed = (now - cur.since).total_seconds()
        bucket = int(elapsed // repeat_seconds) if repeat_seconds > 0 else 0
        last_bucket = (
            int((cur.last_notified_at - cur.since).total_seconds() // repeat_seconds)
            if repeat_seconds > 0
            else 0
        )
        if bucket > last_bucket:
            notify_alert(
                conn,
                alert=rule.name,
                severity=rule.severity,
                firing=True,
                since=cur.since,
                title=rule.title + " (still firing)",
                details=details,
                ai_related=rule.ai_related,
                repeat_bucket=bucket,
            )
            conn.execute(
                text("UPDATE alert_state SET last_notified_at=now() WHERE name=:n"),
                {"n": rule.name},
            )
