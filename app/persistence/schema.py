"""Schema constants shared by code and tests (kept in sync with migrations)."""

from __future__ import annotations

REQUIRED_TABLES: tuple[str, ...] = (
    "services",
    "health_checks",
    "incidents",
    "tasks",
    "outbox_events",
    "task_checkpoints",
    "action_attempts",
    "approvals",
    "evidence",
    "reports",
    "audit_events",
    "model_config",
    "detection_state",
    "investigations",
    "policy_decisions",
    "operators",
    "verifications",
    "report_jobs",
    "notification_events",
    "notification_deliveries",
    "ai_usage",
    "ai_job_slots",
    "alert_state",
    "service_heartbeats",
    "backup_runs",
)

INCIDENT_TYPES = ("unavailable", "http_error", "high_latency", "memory_pressure")
INCIDENT_ACTIVE_STATUSES = frozenset(
    {"open", "investigating", "remediating", "waiting_approval", "escalated"}
)
INCIDENT_TERMINAL_STATUSES = frozenset({"resolved", "closed"})
TASK_ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "running",
        "retry_scheduled",
        "waiting_approval",
        "awaiting_investigation",
        "awaiting_policy",
    }
)
TASK_TERMINAL_STATUSES = frozenset({"escalated", "failed", "resolved", "dead_lettered"})
AUTH_MODES = frozenset({"api_key", "subscription", "mock"})
INVESTIGATION_STATUSES = frozenset({"completed", "insufficient_evidence", "failed"})
POLICY_DECISIONS = frozenset({"ALLOW", "REQUIRE_APPROVAL", "DENY"})
OPERATOR_ROLES = frozenset({"viewer", "approver"})
REPORT_JOB_STATUSES = frozenset({"pending", "generating", "validated", "fallback", "failed"})
REPORT_GENERATION_MODES = frozenset({"ai", "deterministic_fallback"})
NOTIFICATION_DELIVERY_STATUSES = frozenset({"pending", "sending", "delivered", "dead_lettered"})
