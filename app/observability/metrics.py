"""Operational metrics derived from PostgreSQL (authoritative, shared by all
processes, survives restarts) and Redis stream state. Rendered in Prometheus
text exposition format."""

from __future__ import annotations

from dataclasses import dataclass, field

import redis
from sqlalchemy import Engine, text

from app.persistence.streams import StreamNames


@dataclass
class Metric:
    name: str
    kind: str  # counter | gauge
    help: str
    samples: list[tuple[dict[str, str], float]] = field(default_factory=list)


def _grouped(engine: Engine, sql: str, label: str) -> list[tuple[dict[str, str], float]]:
    with engine.connect() as conn:
        return [({label: str(k)}, float(v)) for k, v in conn.execute(text(sql)).all()]


def _scalar(engine: Engine, sql: str) -> float:
    with engine.connect() as conn:
        v = conn.execute(text(sql)).scalar()
    return float(v or 0)


def collect(engine: Engine, client: redis.Redis | None, names: StreamNames) -> list[Metric]:
    m = [
        Metric(
            "sentinel_health_checks_retained",
            "gauge",
            "Health checks stored within the retention window, by outcome",
            _grouped(engine, "SELECT outcome, count(*) FROM health_checks GROUP BY 1", "outcome"),
        ),
        Metric(
            "sentinel_last_check_age_seconds",
            "gauge",
            "Seconds since the most recent recorded health check (monitor liveness)",
            [
                (
                    {},
                    _scalar(
                        engine,
                        "SELECT COALESCE(extract(epoch FROM now() - "
                        "max(checked_at)), -1) FROM health_checks",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_incidents_detected_total",
            "counter",
            "Incidents opened, by type",
            _grouped(
                engine, "SELECT incident_type, count(*) FROM incidents GROUP BY 1", "incident_type"
            ),
        ),
        Metric(
            "sentinel_incidents_active",
            "gauge",
            "Active incidents, by status",
            _grouped(
                engine,
                "SELECT status, count(*) FROM incidents WHERE status NOT IN "
                "('resolved','closed') GROUP BY 1",
                "status",
            ),
        ),
        Metric(
            "sentinel_tasks",
            "gauge",
            "Tasks by status",
            _grouped(engine, "SELECT status, count(*) FROM tasks GROUP BY 1", "status"),
        ),
        Metric(
            "sentinel_task_retries_total",
            "counter",
            "Task retries scheduled",
            [
                (
                    {},
                    _scalar(
                        engine,
                        "SELECT count(*) FROM audit_events WHERE action='task_retry_scheduled'",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_tasks_dead_lettered_total",
            "counter",
            "Tasks dead-lettered",
            [
                (
                    {},
                    _scalar(
                        engine,
                        "SELECT count(*) FROM audit_events WHERE action='task_dead_lettered'",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_outbox_unpublished",
            "gauge",
            "Outbox events not yet published",
            [
                (
                    {},
                    _scalar(
                        engine, "SELECT count(*) FROM outbox_events WHERE published_at IS NULL"
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_outbox_oldest_unpublished_age_seconds",
            "gauge",
            "Age of the oldest unpublished outbox event (DB-to-queue lag)",
            [
                (
                    {},
                    _scalar(
                        engine,
                        "SELECT COALESCE(extract(epoch FROM now() - "
                        "min(created_at)), 0) FROM outbox_events "
                        "WHERE published_at IS NULL",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_investigations_total",
            "counter",
            "AI investigations by final status",
            _grouped(engine, "SELECT status, count(*) FROM investigations GROUP BY 1", "status"),
        ),
        Metric(
            "sentinel_ai_tokens_total",
            "counter",
            "Model tokens consumed by investigations",
            [
                (
                    {"kind": "input"},
                    _scalar(engine, "SELECT COALESCE(sum(input_tokens), 0) FROM investigations"),
                ),
                (
                    {"kind": "output"},
                    _scalar(engine, "SELECT COALESCE(sum(output_tokens), 0) FROM investigations"),
                ),
            ],
        ),
        Metric(
            "sentinel_ai_tool_calls_total",
            "counter",
            "Diagnostic tool calls executed for the AI, by tool",
            _grouped(
                engine,
                "SELECT details->>'tool', count(*) FROM audit_events "
                "WHERE action='ai_tool_call' GROUP BY 1",
                "tool",
            ),
        ),
        Metric(
            "sentinel_ai_paused_tasks",
            "gauge",
            "Tasks parked waiting for AI availability, by reason (alert when > 0)",
            _grouped(
                engine,
                "SELECT COALESCE(outcome, 'parked'), count(*) FROM tasks "
                "WHERE status='awaiting_investigation' GROUP BY 1",
                "reason",
            ),
        ),
    ]
    m += phase5_metrics(engine)
    up, length, pending, lag = 0.0, 0.0, 0.0, 0.0
    r_pending, r_lag = 0.0, 0.0
    if client is not None:
        try:
            length = float(client.xlen(names.tasks))
            for g in client.xinfo_groups(names.tasks):
                if g.get("name") == names.group:
                    pending = float(g.get("pending") or 0)
                    lag = float(g.get("lag") or 0)
            up = 1.0
            for g in client.xinfo_groups(names.reports):
                if g.get("name") == names.reports_group:
                    r_pending = float(g.get("pending") or 0)
                    r_lag = float(g.get("lag") or 0)
        except redis.ResponseError:
            up = 1.0  # stream/group not created yet
        except redis.RedisError:
            up = 0.0
    m += [
        Metric("sentinel_redis_up", "gauge", "Redis reachable from the API", [({}, up)]),
        Metric(
            "sentinel_queue_stream_length", "gauge", "Entries in the task stream", [({}, length)]
        ),
        Metric(
            "sentinel_queue_pending",
            "gauge",
            "Delivered but unacknowledged task messages",
            [({}, pending)],
        ),
        Metric(
            "sentinel_queue_lag",
            "gauge",
            "Task messages not yet delivered to the consumer group",
            [({}, lag)],
        ),
        Metric(
            "sentinel_report_queue_pending",
            "gauge",
            "Delivered but unacknowledged report-job messages",
            [({}, r_pending)],
        ),
        Metric(
            "sentinel_report_queue_lag",
            "gauge",
            "Report-job messages not yet delivered to the reporters group",
            [({}, r_lag)],
        ),
    ]
    return m


# Label values must come from these bounded sets (no ids, no free text).
BOUNDED_LABELS: dict[str, frozenset[str] | None] = {
    "outcome": None,  # constrained by DB CHECKs / fixed code paths
    "status": None,
    "incident_type": None,
    "kind": None,
    "tool": None,
    "reason": None,
    "stage": frozenset({"investigation", "report"}),
    "phase": frozenset({"proposal", "pre_execution"}),
    "decision": frozenset({"ALLOW", "REQUIRE_APPROVAL", "DENY"}),
    "rule_id": None,
    "resolution": None,
    "mode": frozenset({"ai", "deterministic_fallback"}),
    "channel": frozenset({"log", "webhook"}),
    "event_type": None,
    "alert": None,
    "service": None,
}


def _counts(engine: Engine, sql: str, *labels: str) -> list[tuple[dict[str, str], float]]:
    with engine.connect() as conn:
        out = []
        for row in conn.execute(text(sql)).all():
            *keys, value = row
            out.append(
                ({lab: str(k) for lab, k in zip(labels, keys, strict=True)}, float(value or 0))
            )
        return out


def phase5_metrics(engine: Engine) -> list[Metric]:
    one = _scalar
    return [
        # --- monitoring ----------------------------------------------------------------
        Metric(
            "sentinel_monitor_success_ratio",
            "gauge",
            "Share of health checks in the last hour that were healthy (-1 if none)",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT COALESCE(avg((outcome='healthy')::int), -1) FROM health_checks "
                        "WHERE checked_at > now() - interval '1 hour'",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_detection_latency_seconds",
            "gauge",
            "Detection latency (incident opened - first failing check), last 24 h average",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT COALESCE(avg(extract(epoch FROM opened_at - first_failure_at)), 0) "
                        "FROM incidents WHERE first_failure_at IS NOT NULL "
                        "AND opened_at > now() - interval '24 hours'",
                    ),
                )
            ],
        ),
        # --- incidents -----------------------------------------------------------------
        Metric(
            "sentinel_incidents_resolved_total",
            "counter",
            "Incidents resolved, by resolution",
            _counts(
                engine,
                "SELECT COALESCE(resolution, 'none'), count(*) FROM incidents "
                "WHERE status IN ('resolved','closed') GROUP BY 1",
                "resolution",
            ),
        ),
        Metric(
            "sentinel_incidents_escalated_total",
            "counter",
            "Incident escalations recorded",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT count(*) FROM audit_events WHERE action='incident_escalated'",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_incident_oldest_active_age_seconds",
            "gauge",
            "Age of the oldest active incident",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT COALESCE(extract(epoch FROM now() - min(opened_at)), 0) "
                        "FROM incidents WHERE status NOT IN ('resolved','closed')",
                    ),
                )
            ],
        ),
        # --- AI usage / cost -------------------------------------------------------------
        Metric(
            "sentinel_ai_calls_total",
            "counter",
            "Model calls by stage and outcome (ok or typed error kind)",
            _counts(
                engine,
                "SELECT stage, outcome, count(*) FROM ai_usage GROUP BY 1, 2",
                "stage",
                "outcome",
            ),
        ),
        Metric(
            "sentinel_ai_usage_tokens_total",
            "counter",
            "Provider-reported tokens by stage and kind",
            _counts(
                engine,
                "SELECT stage, k, sum(v) FROM ai_usage, LATERAL (VALUES ('input', input_tokens), "
                "('output', output_tokens), ('cache_read', cache_read_tokens), "
                "('cache_write', cache_write_tokens)) AS t(k, v) GROUP BY 1, 2",
                "stage",
                "kind",
            ),
        ),
        Metric(
            "sentinel_ai_cost_usd_estimate_total",
            "counter",
            "ESTIMATED cost from operator-configured prices (absent when unpriced)",
            _counts(
                engine,
                "SELECT stage, sum(cost_usd_estimate) FROM ai_usage "
                "WHERE cost_usd_estimate IS NOT NULL GROUP BY 1",
                "stage",
            ),
        ),
        Metric(
            "sentinel_ai_call_latency_seconds_sum",
            "counter",
            "Total model call latency by stage",
            _counts(
                engine, "SELECT stage, sum(latency_ms) / 1000.0 FROM ai_usage GROUP BY 1", "stage"
            ),
        ),
        Metric(
            "sentinel_ai_call_latency_seconds_count",
            "counter",
            "Model calls measured for latency by stage",
            _counts(engine, "SELECT stage, count(*) FROM ai_usage GROUP BY 1", "stage"),
        ),
        Metric(
            "sentinel_ai_auth_errors_total",
            "counter",
            "Model calls failing authentication / credentials / permission",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT count(*) FROM ai_usage WHERE outcome IN "
                        "('authentication_failed','credentials_missing','permission_denied')",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_ai_daily_tokens",
            "gauge",
            "Tokens used in the current UTC day (budget basis)",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT COALESCE(sum(input_tokens + output_tokens + cache_read_tokens + "
                        "cache_write_tokens), 0) FROM ai_usage WHERE occurred_at >= "
                        "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
                    ),
                )
            ],
        ),
        # --- policy ---------------------------------------------------------------------
        Metric(
            "sentinel_policy_decisions_total",
            "counter",
            "Deterministic policy decisions by phase and decision",
            _counts(
                engine,
                "SELECT phase, decision, count(*) FROM policy_decisions GROUP BY 1, 2",
                "phase",
                "decision",
            ),
        ),
        Metric(
            "sentinel_policy_rule_hits_total",
            "counter",
            "Policy rule ids that produced decisions (bounded rule set)",
            _counts(
                engine,
                "SELECT r, count(*) FROM policy_decisions, unnest(rule_ids) AS r GROUP BY 1",
                "rule_id",
            ),
        ),
        # --- remediation ------------------------------------------------------------------
        Metric(
            "sentinel_actions_total",
            "counter",
            "Remediation action attempts by status",
            _counts(engine, "SELECT status, count(*) FROM action_attempts GROUP BY 1", "status"),
        ),
        Metric(
            "sentinel_duplicate_actions_prevented_total",
            "counter",
            "Duplicate remediation prevented (DUP-1/LIM-1 policy denials + ledger reconciliations)",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT (SELECT count(*) FROM policy_decisions WHERE rule_ids && "
                        "ARRAY['DUP-1','LIM-1']) + (SELECT count(*) FROM audit_events "
                        "WHERE action='action_reconciling')",
                    ),
                )
            ],
        ),
        # --- verification -----------------------------------------------------------------
        Metric(
            "sentinel_verifications_total",
            "counter",
            "Recovery verifications by status",
            _counts(engine, "SELECT status, count(*) FROM verifications GROUP BY 1", "status"),
        ),
        Metric(
            "sentinel_verification_duration_seconds_sum",
            "counter",
            "Total verification duration",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT COALESCE(sum(extract(epoch FROM completed_at - started_at)), 0) "
                        "FROM verifications",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_verification_duration_seconds_count",
            "counter",
            "Verifications measured",
            [({}, one(engine, "SELECT count(*) FROM verifications"))],
        ),
        Metric(
            "sentinel_recovery_rate",
            "gauge",
            "Share of verifications that passed (-1 if none)",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT COALESCE(avg((status='passed')::int), -1) FROM verifications",
                    ),
                )
            ],
        ),
        # --- reporting --------------------------------------------------------------------
        Metric(
            "sentinel_reports_total",
            "counter",
            "Reports persisted by generation mode",
            _counts(
                engine,
                "SELECT generation_mode, count(*) FROM reports WHERE generation_mode IS NOT NULL "
                "GROUP BY 1",
                "mode",
            ),
        ),
        Metric(
            "sentinel_report_jobs",
            "gauge",
            "Report jobs by status",
            _counts(engine, "SELECT status, count(*) FROM report_jobs GROUP BY 1", "status"),
        ),
        Metric(
            "sentinel_report_validation_rejections_total",
            "counter",
            "AI report drafts rejected by the deterministic validator",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT count(*) FROM audit_events "
                        "WHERE action='report_validation_rejected'",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_report_ai_failures_total",
            "counter",
            "Fallback reports whose AI path failed (validation, provider, budget)",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT count(*) FROM reports "
                        "WHERE generation_mode='deterministic_fallback' "
                        "AND fallback_reason NOT IN ('ai_reporting_disabled','ai_not_configured')",
                    ),
                )
            ],
        ),
        # --- notifications ------------------------------------------------------------------
        Metric(
            "sentinel_notification_events_total",
            "counter",
            "Logical notification events by type",
            _counts(
                engine,
                "SELECT event_type, count(*) FROM notification_events GROUP BY 1",
                "event_type",
            ),
        ),
        Metric(
            "sentinel_notification_deliveries",
            "gauge",
            "Notification deliveries by channel and status",
            _counts(
                engine,
                "SELECT channel, status, count(*) FROM notification_deliveries GROUP BY 1, 2",
                "channel",
                "status",
            ),
        ),
        Metric(
            "sentinel_notification_retries_total",
            "counter",
            "Failed delivery attempts that were retried",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT count(*) FROM audit_events WHERE action='notification_failed'",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_notifications_dead_lettered_total",
            "counter",
            "Notification deliveries dead-lettered",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT count(*) FROM audit_events "
                        "WHERE action='notification_dead_lettered'",
                    ),
                )
            ],
        ),
        # --- alerts / backups / components ----------------------------------------------------
        Metric(
            "sentinel_alerts_firing",
            "gauge",
            "Deterministic in-app alerts currently firing (1) or ok (0)",
            _counts(engine, "SELECT name, (status='firing')::int FROM alert_state", "alert"),
        ),
        Metric(
            "sentinel_backup_last_success_age_seconds",
            "gauge",
            "Seconds since the last successful backup (-1 if none recorded)",
            [
                (
                    {},
                    one(
                        engine,
                        "SELECT COALESCE(extract(epoch FROM now() - max(completed_at)), -1) "
                        "FROM backup_runs WHERE status='succeeded'",
                    ),
                )
            ],
        ),
        Metric(
            "sentinel_service_heartbeat_age_seconds",
            "gauge",
            "Seconds since each long-running service last reported (component liveness)",
            _counts(
                engine,
                "SELECT service, extract(epoch FROM now() - max(last_seen_at)) "
                "FROM service_heartbeats GROUP BY 1",
                "service",
            ),
        ),
    ]


def render(metrics: list[Metric]) -> str:
    lines: list[str] = []
    for metric in metrics:
        lines.append(f"# HELP {metric.name} {metric.help}")
        lines.append(f"# TYPE {metric.name} {metric.kind}")
        for labels, value in metric.samples or [({}, 0.0)]:
            lab = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            lines.append(f"{metric.name}{{{lab}}} {value:g}" if lab else f"{metric.name} {value:g}")
    return "\n".join(lines) + "\n"
