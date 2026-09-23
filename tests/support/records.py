"""Synthetic canonical incident records for pure (no-DB) reporting tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.reporting.record import (
    IncidentRecord,
    RecAction,
    RecApproval,
    RecCheck,
    RecDetection,
    RecEvidence,
    RecHypothesis,
    RecIncident,
    RecInvestigation,
    RecMonitoring,
    RecPolicyDecision,
    RecProposal,
    RecStatement,
    RecTask,
    RecTimeline,
    RecUsage,
    RecVerification,
)

T0 = datetime(2026, 9, 23, 10, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make_record(
    scenario: str = "resolved",
    *,
    auth_mode: str = "mock",
    model_id: str = "mock-investigator-v1",
    approval: str | None = None,
) -> IncidentRecord:
    """scenario: resolved | recovery_failed | denied | auto_recovered | no_investigation"""
    iid, tid = uuid.uuid4(), uuid.uuid4()
    det_ev, logs_ev, status_ev = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    inv_id = uuid.uuid4()
    evidence = [
        RecEvidence(
            id=det_ev,
            source="health_check",
            tool_name=None,
            status=None,
            collected_at=at(60),
            sha256="a" * 64,
        ),
        RecEvidence(
            id=logs_ev,
            source="tool",
            tool_name="get_application_logs",
            status="ok",
            collected_at=at(70),
            sha256="b" * 64,
        ),
        RecEvidence(
            id=status_ev,
            source="tool",
            tool_name="get_container_status",
            status="ok",
            collected_at=at(75),
            sha256="c" * 64,
        ),
    ]
    detection = [
        RecDetection(
            evidence_id=det_ev,
            collected_at=at(60),
            failure_type="http_error",
            threshold=3,
            checks=[
                RecCheck(
                    id=uuid.uuid4(),
                    checked_at=at(i * 30),
                    outcome="unhealthy",
                    http_status=500,
                    latency_ms=5.0,
                )
                for i in range(3)
            ],
        )
    ]
    investigation = None
    if scenario != "no_investigation":
        investigation = RecInvestigation(
            id=inv_id,
            task_id=tid,
            status="completed",
            model_id=model_id,
            auth_mode=auth_mode,
            is_mock=auth_mode == "mock",
            failure_reason=None,
            tool_calls=2,
            model_calls=4,
            reasoning_attempts=0,
            input_tokens=2000,
            output_tokens=400,
            cost_usd=None,
            started_at=at(65),
            completed_at=at(80),
            ai_observations=[RecStatement(statement="Error logs present.", evidence_ids=[logs_ev])],
            ai_hypotheses=[
                RecHypothesis(
                    statement="Memory exhaustion in the application process (unconfirmed).",
                    certainty="hypothesis",
                    confidence="medium",
                    evidence_ids=[logs_ev],
                )
            ],
            proposed_action=RecProposal(
                action="restart_demo_app", target_service="demo-app", evidence_ids=[logs_ev]
            ),
            missing_evidence=[],
            next_step="propose_remediation",
        )
    policy: list[RecPolicyDecision] = []
    approvals: list[RecApproval] = []
    actions: list[RecAction] = []
    verifs: list[RecVerification] = []
    inc_status, resolution, task_status, outcome = "escalated", None, "escalated", "policy_denied"
    if scenario == "denied":
        policy = [
            RecPolicyDecision(
                id=uuid.uuid4(),
                phase="proposal",
                decision="DENY",
                rule_ids=["ENV-2"],
                reasons=["environment not declared"],
                policy_version="0" * 16,
                evaluated_at=at(85),
            )
        ]
    if scenario in ("resolved", "recovery_failed"):
        rule = "APR-1" if approval == "approved" else "AUT-1"
        if approval:
            policy.append(
                RecPolicyDecision(
                    id=uuid.uuid4(),
                    phase="proposal",
                    decision="REQUIRE_APPROVAL",
                    rule_ids=["APR-0"],
                    reasons=[],
                    policy_version="0" * 16,
                    evaluated_at=at(84),
                )
            )
            approvals.append(
                RecApproval(
                    id=uuid.uuid4(),
                    status=approval,
                    proposed_action="restart_demo_app",
                    risk="brief unavailability",
                    requested_at=at(84),
                    expires_at=at(984),
                    decided_at=at(90) if approval in ("approved", "rejected") else None,
                    decided_by="alice" if approval in ("approved", "rejected") else None,
                )
            )
        policy += [
            RecPolicyDecision(
                id=uuid.uuid4(),
                phase="proposal",
                decision="ALLOW",
                rule_ids=[rule],
                reasons=[],
                policy_version="0" * 16,
                evaluated_at=at(92),
            ),
            RecPolicyDecision(
                id=uuid.uuid4(),
                phase="pre_execution",
                decision="ALLOW",
                rule_ids=[rule],
                reasons=[],
                policy_version="0" * 16,
                evaluated_at=at(93),
            ),
        ]
        attempt = uuid.uuid4()
        actions = [
            RecAction(
                id=attempt,
                action_id=uuid.uuid4(),
                action_type="restart_demo_app",
                status="succeeded",
                executed=True,
                requested_at=at(92),
                started_at=at(94),
                completed_at=at(100),
                recorded_via="executor_response",
                error=None,
            )
        ]
        passed = scenario == "resolved"
        verifs = [
            RecVerification(
                id=uuid.uuid4(),
                action_attempt_id=attempt,
                status="passed" if passed else "failed",
                reason="3 healthy probes" if passed else "deadline exceeded",
                probes=3 if passed else 10,
                healthy_probes=3 if passed else 0,
                critical_lines=0,
                started_at=at(100),
                completed_at=at(110),
            )
        ]
        if passed:
            inc_status, resolution, task_status, outcome = (
                "resolved",
                "remediated",
                "resolved",
                "recovery_verified",
            )
        else:
            outcome = "recovery_failed"
    if scenario == "auto_recovered":
        inc_status, resolution, task_status, outcome = (
            "resolved",
            "auto_recovered",
            "resolved",
            "incident_auto_recovered",
        )
    task = RecTask(
        id=tid,
        status=task_status,
        outcome=outcome,
        attempt=1,
        max_attempts=3,
        model_id=model_id if investigation else None,
        last_error=None,
        created_at=at(60),
        completed_at=at(120),
    )
    timeline = [
        RecTimeline(at=at(0), kind="first_failure", ref_id=iid, text="first failing check"),
        RecTimeline(at=at(60), kind="incident_opened", ref_id=iid, text="incident opened"),
        *[
            RecTimeline(
                at=p.evaluated_at,
                kind="policy_decision",
                ref_id=p.id,
                text=f"policy {p.phase}: {p.decision}",
            )
            for p in policy
        ],
        RecTimeline(at=at(120), kind="task_finished", ref_id=tid, text=f"task {task_status}"),
    ]
    return IncidentRecord(
        schema_version=1,
        incident=RecIncident(
            id=iid,
            service="demo-app",
            incident_type="http_error",
            severity="high",
            status=inc_status,
            resolution=resolution,
            summary="3 consecutive http_error checks",
            occurrence_count=5,
            first_failure_at=at(0),
            last_failure_at=at(90),
            opened_at=at(60),
            last_seen_at=at(90),
            resolved_at=at(115) if inc_status == "resolved" else None,
        ),
        task=task,
        tasks=[task],
        detection=detection,
        monitoring=RecMonitoring(
            window_start=at(-300),
            window_end=at(120),
            outcome_counts={"unhealthy": 5, "healthy": 3},
            last_check=None,
        ),
        investigation=investigation,
        evidence=evidence,
        untrusted_log_excerpts=[],
        policy_decisions=policy,
        approvals=approvals,
        actions=actions,
        verifications=verifs,
        escalated=inc_status == "escalated",
        escalation_reasons=[f"task escalated: {outcome}"] if inc_status == "escalated" else [],
        usage=RecUsage(
            investigation_model_calls=4,
            investigation_input_tokens=2000,
            investigation_output_tokens=400,
            report_model_calls=0,
            report_input_tokens=0,
            report_output_tokens=0,
            cost_usd_estimate=None,
            cost_basis="unavailable: no operator-configured prices",
        ),
        audit=[],
        timeline=timeline,
    )


def no_foreign(_ids: set[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    return {}


def with_changes(draft: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**draft, **changes}
