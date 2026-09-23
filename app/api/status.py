"""Degraded-mode reporting (read-only, bearer token): which parts of SentinelOps
are healthy, degraded or unavailable. Distinct from ``/health/live`` (process up)
and ``/health/ready`` (core dependencies: DB, schema, Redis) - an optional
provider (AI, notification webhook) being down degrades, never kills, the
service: monitoring and durable queueing continue.

Modes: CORE_HEALTHY | AI_DEGRADED | NOTIFICATIONS_DEGRADED | EXECUTOR_DEGRADED |
MONITOR_DEGRADED | REPORTING_DEGRADED | BACKUPS_DEGRADED | DATABASE_UNAVAILABLE |
REDIS_UNAVAILABLE.
"""

from __future__ import annotations

from typing import Any

import redis
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings


def _q(engine: Engine, sql: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def system_status(engine: Engine, rclient: redis.Redis | None, s: Settings) -> dict[str, Any]:
    comps: dict[str, dict[str, Any]] = {}
    try:
        _q(engine, "SELECT 1")
        comps["database"] = {"status": "ok"}
    except SQLAlchemyError:
        return {
            "overall": "unavailable",
            "modes": ["DATABASE_UNAVAILABLE"],
            "components": {"database": {"status": "unavailable"}},
        }
    try:
        comps["redis"] = {
            "status": "ok" if rclient is not None and rclient.ping() else "unavailable"
        }
    except redis.RedisError:
        comps["redis"] = {"status": "unavailable"}

    age = _q(engine, "SELECT extract(epoch FROM now() - max(checked_at)) FROM health_checks")
    comps["monitor"] = {
        "status": "unknown"
        if age is None
        else ("ok" if float(age) <= s.alert_monitor_silence_seconds else "degraded"),
        "last_check_age_seconds": None if age is None else round(float(age), 1),
    }
    firing = {
        str(r[0]) for r in _rows(engine, "SELECT name FROM alert_state WHERE status='firing'")
    }
    paused = int(
        _q(
            engine,
            "SELECT count(*) FROM tasks WHERE status='awaiting_investigation' "
            "AND outcome LIKE 'ai\\_paused\\_%'",
        )
        or 0
    )
    selected = _q(engine, "SELECT auth_mode || ':' || model_id FROM model_config WHERE is_active")
    ai_bad = paused > 0 or bool(firing & {"ai_auth_failed", "ai_paused", "ai_budget_exhausted"})
    comps["ai"] = {
        # paused work or a firing AI alert is degradation even with no model selected
        "status": "degraded" if ai_bad else ("not_configured" if selected is None else "ok"),
        "selected": selected,
        "paused_tasks": paused,
        "gateway": s.ai_gateway,
    }
    pend_age = _q(
        engine,
        "SELECT extract(epoch FROM now() - min(created_at)) FROM notification_deliveries "
        "WHERE status IN ('pending','sending')",
    )
    dead = int(
        _q(
            engine,
            "SELECT count(*) FROM notification_deliveries WHERE status='dead_lettered' "
            "AND updated_at > now() - interval '24 hours'",
        )
        or 0
    )
    notif_bad = dead > 0 or (
        pend_age is not None and float(pend_age) > s.alert_notification_backlog_seconds
    )
    comps["notifications"] = {
        "status": "degraded" if notif_bad or "notifications_failing" in firing else "ok",
        "oldest_pending_age_seconds": None if pend_age is None else round(float(pend_age), 1),
        "dead_lettered_24h": dead,
    }
    ex = _q(
        engine,
        "SELECT details->>'executor' FROM service_heartbeats WHERE service='worker' "
        "AND last_seen_at > now() - interval '5 minutes' ORDER BY last_seen_at DESC LIMIT 1",
    )
    comps["executor"] = {"status": {"ok": "ok", "unreachable": "degraded"}.get(ex or "", "unknown")}
    failed_reports = int(
        _q(
            engine,
            "SELECT count(*) FROM report_jobs WHERE status='failed' "
            "AND updated_at > now() - interval '24 hours'",
        )
        or 0
    )
    comps["reporting"] = {
        "status": "degraded" if failed_reports else "ok",
        "failed_jobs_24h": failed_reports,
    }
    comps["backups"] = {"status": "degraded" if "backup_failed" in firing else "ok"}
    beats = {
        str(r[0]): round(float(r[1]), 1)
        for r in _rows(
            engine,
            "SELECT service, extract(epoch FROM now() - max(last_seen_at)) FROM service_heartbeats "
            "GROUP BY 1",
        )
    }
    comps["services"] = {"status": "ok", "heartbeat_age_seconds": beats}

    modes: list[str] = []
    if comps["redis"]["status"] != "ok":
        modes.append("REDIS_UNAVAILABLE")
    for name, mode in (
        ("ai", "AI_DEGRADED"),
        ("notifications", "NOTIFICATIONS_DEGRADED"),
        ("executor", "EXECUTOR_DEGRADED"),
        ("monitor", "MONITOR_DEGRADED"),
        ("reporting", "REPORTING_DEGRADED"),
        ("backups", "BACKUPS_DEGRADED"),
    ):
        if comps[name]["status"] == "degraded":
            modes.append(mode)
    # Redis down delays dispatch but PostgreSQL keeps every task: degraded, not dead.
    overall = "degraded" if modes else "healthy"
    if not modes:
        modes = ["CORE_HEALTHY"]
    return {
        "overall": overall,
        "modes": modes,
        "alerts_firing": sorted(firing),
        "components": comps,
    }


def _rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql)).all())
