"""Deterministic remediation policy (workflow step 6).

A pure function of a typed ``PolicyInput`` (built from authoritative DB state
and trusted configuration) and a ``PolicyConfig``. No LLM, no I/O, no model
self-assessment: the model's prose is never an input, only its validated,
allowlisted ``proposed_action`` and cited evidence ids.

Decisions: ALLOW | REQUIRE_APPROVAL | DENY, each with the rule ids that
produced it. DEFAULT DENY: missing/invalid input or any evaluation error denies.
The model cannot edit policy: ``PolicyConfig`` comes only from settings, and
its hash (``version``) is bound into every approval and action fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from app.config import Environment, Settings

ALLOWLIST = ("restart_demo_app", "no_action", "escalate_to_human")
RESTART = "restart_demo_app"
ACTIVE_FOR_RESTART = ("open", "investigating", "remediating", "waiting_approval")
ACTION_NAMESPACE = uuid.UUID("5e7f1c9e-9d2b-4c52-9a3e-6b4f0d2a7c11")


class Decision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


Effect = Literal["restart", "none", "escalate"]


@dataclass(frozen=True)
class PolicyConfig:
    environment: str
    auto_enabled: bool
    approval_enabled: bool
    remediation_environment: str | None
    target_service: str
    max_restarts_per_incident: int
    max_restarts_per_hour: int
    health_freshness_seconds: float
    evidence_max_age_seconds: float
    max_cost_usd: float | None

    @classmethod
    def from_settings(cls, s: Settings) -> PolicyConfig:
        return cls(
            environment=s.environment.value,
            auto_enabled=s.remediation_auto_enabled,
            approval_enabled=s.remediation_approval_enabled,
            remediation_environment=s.remediation_environment,
            target_service=s.remediation_target_service,
            max_restarts_per_incident=s.remediation_max_restarts_per_incident,
            max_restarts_per_hour=s.remediation_max_restarts_per_hour,
            health_freshness_seconds=s.policy_health_freshness_seconds,
            evidence_max_age_seconds=s.ai_evidence_max_age_seconds,
            max_cost_usd=s.ai_max_cost_usd,
        )

    @property
    def version(self) -> str:
        body = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:16]


def action_id_for(incident_id: uuid.UUID, action: str = RESTART) -> uuid.UUID:
    """Deterministic: one restart action per incident, identical across retries,
    workers and duplicate deliveries."""
    return uuid.uuid5(ACTION_NAMESPACE, f"{incident_id}:{action}:1")


def action_fingerprint(
    *,
    incident_id: uuid.UUID,
    task_id: uuid.UUID,
    investigation_id: uuid.UUID | None,
    action: str | None,
    target_service: str,
    action_id: uuid.UUID,
    policy_version: str,
) -> str:
    """Binds an authorization to one exact action. Any change to the incident,
    investigation, action, trusted target or policy produces a new fingerprint,
    so an approval can never authorize something else."""
    body = json.dumps(
        {
            "incident_id": str(incident_id),
            "task_id": str(task_id),
            "investigation_id": str(investigation_id),
            "action": action,
            "target_service": target_service,
            "action_id": str(action_id),
            "policy_version": policy_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class EvidenceRef:
    incident_id: uuid.UUID
    collected_at: datetime


@dataclass(frozen=True)
class ApprovalState:
    id: uuid.UUID
    status: str  # pending | approved | rejected | expired
    action_fingerprint: str | None
    expires_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    signature_valid: bool
    decider_is_approver: bool


@dataclass(frozen=True)
class PolicyInput:
    phase: Literal["proposal", "pre_execution"]
    now: datetime
    task_id: uuid.UUID
    incident_id: uuid.UUID
    incident_status: str | None
    service_name: str | None
    service_environment: str | None
    investigation_id: uuid.UUID | None
    investigation_status: str | None
    investigation_task_id: uuid.UUID | None
    investigation_incident_id: uuid.UUID | None
    proposed_action: str | None
    proposed_target: str | None
    cited_evidence_ids: tuple[uuid.UUID, ...]
    evidence: dict[uuid.UUID, EvidenceRef]
    latest_health: tuple[datetime, str] | None  # (checked_at, outcome)
    restarts_for_incident: int  # executed/in-flight restarts, excluding this action
    restarts_last_hour: int
    this_action_status: str | None  # status of action_id_for(incident), if any
    lease_valid: bool
    investigation_cost_usd: float | None
    approval: ApprovalState | None
    fingerprint: str
    executor_configured: bool = False


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    effect: Effect
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        return f"{self.decision.value} [{', '.join(self.rule_ids)}]"


def _deny(rules: list[tuple[str, str]]) -> PolicyResult:
    return PolicyResult(
        Decision.DENY, "escalate", tuple(r for r, _ in rules), tuple(m for _, m in rules)
    )


def evaluate(inp: PolicyInput, cfg: PolicyConfig) -> PolicyResult:
    try:
        return _evaluate(inp, cfg)
    except Exception as exc:  # default deny on any evaluation fault
        return PolicyResult(
            Decision.DENY,
            "escalate",
            ("SYS-1",),
            (f"policy evaluation error: {type(exc).__name__}",),
        )


def _evaluate(inp: PolicyInput, cfg: PolicyConfig) -> PolicyResult:
    # Investigation must be a completed, validated result for THIS task/incident.
    if (
        inp.investigation_id is None
        or inp.investigation_status != "completed"
        or inp.investigation_task_id != inp.task_id
        or inp.investigation_incident_id != inp.incident_id
    ):
        return _deny([("INV-1", "no completed, validated investigation for this task")])
    action = inp.proposed_action
    if action not in ALLOWLIST:
        return _deny([("ACT-1", f"action {action!r} is not allowlisted")])
    if action == "escalate_to_human":
        return PolicyResult(
            Decision.ALLOW, "escalate", ("ESC-1",), ("escalation to a human is always permitted",)
        )
    if action == "no_action":
        return PolicyResult(Decision.ALLOW, "none", ("NOA-1",), ("no side effect requested",))

    # --- restart_demo_app: every condition must hold ---------------------------------
    failed: list[tuple[str, str]] = []
    if cfg.environment == Environment.PRODUCTION.value:
        failed.append(("ENV-1", "remediation is never authorized in production"))
    if cfg.remediation_environment != "isolated-demo":
        failed.append(("ENV-2", "remediation environment is not declared 'isolated-demo'"))
    if inp.proposed_target != cfg.target_service:
        failed.append(
            (
                "TGT-1",
                f"proposed target {inp.proposed_target!r} is not the trusted "
                f"target {cfg.target_service!r}",
            )
        )
    if inp.service_name != cfg.target_service or inp.service_environment != "demo":
        failed.append(("TGT-2", "incident service is not the trusted isolated demo target"))
    if inp.incident_status not in ACTIVE_FOR_RESTART:
        failed.append(
            ("INC-1", f"incident status {inp.incident_status!r} does not permit remediation")
        )
    if not inp.cited_evidence_ids:
        failed.append(("EVD-1", "proposal cites no evidence"))
    for eid in inp.cited_evidence_ids:
        ref = inp.evidence.get(eid)
        if ref is None or ref.incident_id != inp.incident_id:
            failed.append(("EVD-1", f"evidence {eid} missing or belongs to another incident"))
        elif inp.now - ref.collected_at > timedelta(seconds=cfg.evidence_max_age_seconds):
            failed.append(("EVD-2", f"evidence {eid} is stale"))
    if inp.latest_health is None:
        failed.append(("HLT-1", "no health check available to confirm the failure"))
    else:
        checked_at, outcome = inp.latest_health
        if inp.now - checked_at > timedelta(seconds=cfg.health_freshness_seconds):
            failed.append(("HLT-1", "latest health check is too old to confirm the failure"))
        elif outcome == "healthy":
            failed.append(("HLT-2", "service is currently healthy; not restarting it"))
    if inp.restarts_for_incident >= cfg.max_restarts_per_incident:
        failed.append(
            ("LIM-1", f"restart limit per incident ({cfg.max_restarts_per_incident}) reached")
        )
    if inp.restarts_last_hour >= cfg.max_restarts_per_hour:
        failed.append(("LIM-2", f"restart limit per hour ({cfg.max_restarts_per_hour}) reached"))
    if inp.this_action_status not in (None, "pending"):
        failed.append(("DUP-1", f"this action already has status {inp.this_action_status!r}"))
    if inp.phase == "pre_execution" and not inp.lease_valid:
        failed.append(("LSE-1", "executor does not hold the current task lease"))
    if not inp.executor_configured:
        failed.append(("EXE-1", "restricted executor is not configured"))
    if (
        cfg.max_cost_usd is not None
        and inp.investigation_cost_usd is not None
        and inp.investigation_cost_usd > cfg.max_cost_usd
    ):
        failed.append(("CST-1", "investigation exceeded the cost budget"))
    if failed:
        return _deny(failed)

    # --- authorization: preauthorized autonomy, or a valid bound human approval ----------
    if cfg.auto_enabled:
        return PolicyResult(
            Decision.ALLOW,
            "restart",
            ("AUT-1",),
            ("preauthorized autonomous demo restart; all conditions hold",),
        )
    if not cfg.approval_enabled:
        return _deny([("AUT-2", "autonomy disabled and approvals disabled")])
    a = inp.approval
    if a is None:
        return PolicyResult(
            Decision.REQUIRE_APPROVAL,
            "none",
            ("APR-0",),
            ("autonomy disabled: human approval required",),
        )
    if a.action_fingerprint != inp.fingerprint:
        return _deny([("APR-4", "approval is bound to a different action fingerprint")])
    if a.status == "pending" and inp.now < a.expires_at:
        return PolicyResult(
            Decision.REQUIRE_APPROVAL, "none", ("APR-2",), ("waiting for approval",)
        )
    if a.status == "approved":
        if not a.signature_valid:
            return _deny([("APR-5", "approval signature invalid (forged or tampered)")])
        if not a.decider_is_approver:
            return _deny([("APR-6", "approval was not decided by an active approver")])
        if a.decided_at is None or a.decided_at > a.expires_at:
            return _deny([("APR-3", "approval decided after expiry")])
        return PolicyResult(Decision.ALLOW, "restart", ("APR-1",), (f"approved by {a.decided_by}",))
    if a.status == "rejected":
        return _deny([("APR-7", f"approval rejected by {a.decided_by}")])
    return _deny([("APR-3", "approval expired without a decision")])
