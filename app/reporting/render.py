"""Deterministic report rendering.

* ``deterministic_draft`` builds a complete, validator-clean narrative purely
  from the canonical record (used by the deterministic fallback; the test/demo
  mock model also drafts with it).
* ``render`` produces the published Markdown body and structured content.
  The FACTS sections (detection, investigation, evidence, hypotheses, proposed
  action, policy, approvals, actions executed, verification, outcome, usage)
  are ALWAYS rendered from the record. Only the narrative sections (summary,
  timeline wording, observations, unresolved questions, follow-up) come from
  the validated draft - so a draft can never redefine what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.reporting.record import IncidentRecord
from app.reporting.schema import ReportDraft

GENERATION_AI = "ai"
GENERATION_FALLBACK = "deterministic_fallback"


def _ts(v: datetime | None) -> str:
    return v.isoformat() if v else "—"


def _summary(r: IncidentRecord) -> str:
    inc, inv = r.incident, r.investigation
    threshold = next((d.threshold for d in r.detection if d.threshold), None)
    # occurrence_count is 1 at opening, +1 per failing check after the threshold,
    # so the failing checks attributed to the incident are threshold + count - 1.
    total = (threshold + inc.occurrence_count - 1) if threshold else None
    parts = [
        f"The monitor opened a {inc.incident_type} incident for {inc.service}"
        + (f" after {threshold} consecutive failing checks" if threshold else "")
        + (f" ({total} failing checks attributed to it in total)." if total else ".")
    ]
    if inv is None:
        parts.append("No AI investigation result is recorded for this incident.")
    elif inv.status == "completed" and inv.proposed_action:
        kind = "a deterministic test model" if inv.is_mock else f"auth mode {inv.auth_mode}"
        parts.append(
            f"An AI investigation with model {inv.model_id} ({kind}) proposed "
            f"{inv.proposed_action.action}; its hypotheses remain unconfirmed."
        )
    else:
        parts.append(
            f"The AI investigation ended with status {inv.status} and produced no "
            "validated proposal."
        )
    if r.policy_decisions:
        last = r.policy_decisions[-1]
        parts.append(
            f"The deterministic policy's last decision ({last.phase}) was {last.decision} "
            f"with rules {', '.join(last.rule_ids)}."
        )
    for a in r.approvals:
        parts.append(f"A human approval was requested and its final status is {a.status}.")
    if r.executed_actions:
        parts.append(
            "SentinelOps executed one restart of the demo application through the "
            "restricted executor."
        )
    elif r.actions:
        parts.append(
            f"A restart action was recorded with status {r.actions[-1].status}; it is not "
            "recorded as executed."
        )
    else:
        parts.append("No remediation action was executed.")
    if r.verification_passed:
        parts.append("Recovery verification passed.")
    elif r.verification_failed:
        parts.append("Recovery verification failed; the incident remains open for a human.")
    if inc.status == "resolved" and inc.resolution == "auto_recovered":
        parts.append(
            "The monitor observed consecutive healthy checks and the incident was resolved "
            "automatically."
        )
    elif inc.status == "resolved" and inc.resolution == "remediated":
        parts.append("The incident was resolved as remediated.")
    elif inc.status == "escalated":
        parts.append("The incident is escalated and remains open.")
    else:
        parts.append(f"The incident status is {inc.status}.")
    return " ".join(parts)


def _questions(r: IncidentRecord) -> list[str]:
    out: list[str] = []
    if not r.recovery_confirmed:
        out.append("What still prevents the service from passing its health checks?")
    inv = r.investigation
    if inv:
        for h in inv.ai_hypotheses[:3]:
            out.append(f"Is this unconfirmed hypothesis correct: {h.statement[:200]}?")
        if inv.missing_evidence:
            out.append(
                "Can the evidence that was unavailable be collected: "
                + "; ".join(inv.missing_evidence)[:220]
                + "?"
            )
    return out[:10]


def _follow_up(r: IncidentRecord) -> list[str]:
    out: list[str] = []
    inc = r.incident
    if r.verification_failed:
        out.append(
            "Investigate why the single permitted restart did not restore health; no further "
            "automated action will be taken for this incident."
        )
    if inc.status == "escalated" or r.escalated:
        reason = r.escalation_reasons[-1] if r.escalation_reasons else "see the audit trail"
        out.append(f"A human operator should review the escalated incident ({reason[:120]}).")
    for a in r.approvals:
        if a.status in ("expired", "rejected"):
            out.append(f"Review why approval {a.id} ended as {a.status}.")
    if r.executed_actions and r.verification_passed:
        out.append(
            "Watch for recurrence: the restart addressed the symptom and no cause is confirmed."
        )
    if r.investigation and r.investigation.ai_hypotheses:
        out.append("Confirm or refute the unconfirmed hypotheses with additional evidence.")
    return out[:10] or ["No follow-up is required by the records."]


def deterministic_draft(r: IncidentRecord) -> ReportDraft:
    """A narrative built only from records (validator-clean by construction)."""
    inc, inv, t = r.incident, r.investigation, r.task
    observations = [
        {
            "statement": f"Diagnostic {e.tool_name} returned status {e.status or 'unknown'}.",
            "evidence_ids": [str(e.id)],
        }
        for e in r.evidence
        if e.source == "tool" and e.tool_name
    ][:10]
    observations[:0] = [
        {
            "statement": f"The monitor recorded {len(d.checks)} consecutive failing checks of "
            f"type {d.failure_type}.",
            "evidence_ids": [str(d.evidence_id)],
        }
        for d in r.detection[:2]
    ]
    hypotheses = (
        [
            {
                "statement": h.statement,
                "certainty": "hypothesis",
                "evidence_ids": [str(x) for x in h.evidence_ids if x in r.evidence_ids],
            }
            for h in inv.ai_hypotheses
        ]
        if inv
        else []
    )
    payload: dict[str, Any] = {
        "incident_id": str(inc.id),
        "service": inc.service,
        "incident_type": inc.incident_type,
        "severity": inc.severity,
        "detected_at": inc.opened_at.isoformat(),
        "summary": _summary(r)[:1500],
        "timeline": [
            {"at": e.at.isoformat(), "event": e.text[:300], "record_ids": [str(e.ref_id)]}
            for e in r.timeline[:40]
        ]
        or [
            {
                "at": inc.opened_at.isoformat(),
                "event": "incident opened",
                "record_ids": [str(inc.id)],
            }
        ],
        "observations": observations[:20],
        "hypotheses": hypotheses[:10],
        "proposed_action": {"action": inv.proposed_action.action, "investigation_id": str(inv.id)}
        if inv and inv.status == "completed" and inv.proposed_action
        else None,
        "policy_decisions": [
            {
                "decision_id": str(p.id),
                "phase": p.phase,
                "decision": p.decision,
                "rule_ids": p.rule_ids,
            }
            for p in r.policy_decisions
        ],
        "approvals": [
            {"approval_id": str(a.id), "status": a.status, "decided_by": a.decided_by}
            for a in r.approvals
        ],
        "actions_taken": [
            {"action_attempt_id": str(a.id), "action": a.action_type, "status": a.status}
            for a in r.actions
        ],
        "verifications": [
            {"verification_id": str(v.id), "status": v.status} for v in r.verifications
        ],
        "outcome": {
            "incident_status": inc.status,
            "incident_resolution": inc.resolution,
            "task_status": t.status if t else None,
            "task_outcome": t.outcome if t else None,
        },
        "investigation_model": {"model_id": inv.model_id, "auth_mode": inv.auth_mode}
        if inv
        else None,
        "unresolved_questions": _questions(r),
        "follow_up": _follow_up(r),
    }
    return ReportDraft.model_validate(payload)


@dataclass(frozen=True)
class Provenance:
    version: int
    generation_mode: str
    model_id: str | None
    auth_mode: str | None
    is_mock_model: bool
    fallback_reason: str | None
    record_sha256: str
    generated_at: datetime
    validation_attempts: int
    rejections: list[list[str]]


def _mode_line(p: Provenance) -> str:
    if p.generation_mode == GENERATION_AI:
        who = (
            f"the deterministic MOCK model `{p.model_id}` (TEST/DEMO ONLY - not Claude)"
            if p.is_mock_model
            else f"model `{p.model_id}` (auth mode `{p.auth_mode}`)"
        )
        return (
            f"Narrative drafted by {who} and accepted by SentinelOps' deterministic validator "
            f"after {p.validation_attempts} attempt(s). All facts sections are rendered "
            "directly from records."
        )
    return (
        "DETERMINISTIC FALLBACK report rendered directly from records; no AI drafted this "
        f"report (reason: {p.fallback_reason})."
    )


def render(r: IncidentRecord, draft: ReportDraft, p: Provenance) -> tuple[str, dict[str, Any]]:
    inc, inv = r.incident, r.investigation
    L: list[str] = [
        f"# Incident report {inc.id} (v{p.version})",
        "",
        f"_{_mode_line(p)}_",
        "",
        f"- Incident ID: `{inc.id}`",
        f"- Service: `{inc.service}`",
        f"- Started (first failing check): {_ts(inc.first_failure_at)}",
        f"- Detected (incident opened): {_ts(inc.opened_at)}",
        f"- Severity / classification: {inc.severity} / {inc.incident_type}",
        f"- Current status: {inc.status}" + (f" ({inc.resolution})" if inc.resolution else ""),
        f"- Record SHA-256: `{p.record_sha256}`; generated {p.generated_at.isoformat()}",
        "",
        "## Summary",
        draft.summary,
        "",
        "## Detection evidence (observed facts)",
    ]
    for d in r.detection:
        L.append(
            f"- Evidence `{d.evidence_id}`: {len(d.checks)} consecutive `{d.failure_type}` "
            f"checks (threshold {d.threshold}), collected {_ts(d.collected_at)}"
        )
        for c in d.checks:
            L.append(
                f"  - {_ts(c.checked_at)} outcome={c.outcome} http={c.http_status} "
                f"latency_ms={c.latency_ms}"
            )
    m = r.monitoring
    L.append(
        f"- Monitoring window {_ts(m.window_start)} to {_ts(m.window_end)}: "
        + (", ".join(f"{k}={v}" for k, v in m.outcome_counts.items()) or "no checks")
    )
    L += ["", "## Investigation (observed facts)"]
    if inv is None:
        L.append("- No AI investigation is recorded.")
    else:
        L += [
            f"- Investigation `{inv.id}`: status {inv.status}"
            + (f" ({inv.failure_reason})" if inv.failure_reason else ""),
            f"- Model: `{inv.model_id}`; auth mode `{inv.auth_mode}`"
            + ("; DETERMINISTIC MOCK (TEST/DEMO ONLY - not Claude)" if inv.is_mock else ""),
            f"- Diagnostic tool calls: {inv.tool_calls}; model calls: {inv.model_calls}; "
            f"reasoning attempts: {inv.reasoning_attempts}",
        ]
    L += ["", "## Evidence reviewed"]
    L += [
        f"- `{e.id}` {e.source}"
        + (f"/{e.tool_name}" if e.tool_name else "")
        + (f" ({e.status})" if e.status else "")
        + f" at {_ts(e.collected_at)}"
        for e in r.evidence
    ] or ["- none"]
    if draft.observations:
        L += ["", "### Observations (report narrative; each cites evidence)"]
        L += [
            f"- {o.statement} [{', '.join(f'`{x}`' for x in o.evidence_ids)}]"
            for o in draft.observations
        ]
    L += ["", "## Hypotheses (AI investigation; UNCONFIRMED, not root causes)"]
    L += [
        f"- {h.statement} (confidence {h.confidence})" for h in (inv.ai_hypotheses if inv else [])
    ] or ["- none recorded"]
    L += ["", "## Proposed action (what the AI recommended)"]
    pa = inv.proposed_action if inv else None
    L.append(
        f"- `{pa.action}` target `{pa.target_service}` citing {len(pa.evidence_ids)} evidence "
        "record(s)"
        if pa
        else "- none"
    )
    L += ["", "## Policy decisions (deterministic policy engine)"]
    L += [
        f"- {p_.evaluated_at.isoformat()} {p_.phase}: **{p_.decision}** rules "
        f"{', '.join(p_.rule_ids)} (policy {p_.policy_version}) `{p_.id}`"
        for p_ in r.policy_decisions
    ] or ["- none (no policy evaluation is recorded)"]
    L += ["", "## Approval history"]
    L += [
        f"- `{a.id}`: {a.status}; requested {_ts(a.requested_at)}, expires {_ts(a.expires_at)}"
        + (f", decided by `{a.decided_by}` at {_ts(a.decided_at)}" if a.decided_by else "")
        for a in r.approvals
    ] or ["- no approval was requested"]
    L += ["", "## Actions actually executed (executor records)"]
    L += [
        f"- `{a.id}` {a.action_type}: status **{a.status}** "
        + ("(EXECUTED)" if a.executed else "(NOT executed)")
        + f"; started {_ts(a.started_at)}, completed {_ts(a.completed_at)}"
        + (f"; recorded via {a.recorded_via}" if a.recorded_via else "")
        + (f"; error: {a.error}" if a.error else "")
        for a in r.actions
    ] or ["- no action was executed"]
    L += ["", "## Recovery verification (deterministic verifier)"]
    L += [
        f"- `{v.id}`: **{v.status}** - {v.reason} ({v.healthy_probes}/{v.probes} healthy "
        f"probes, {v.critical_lines} critical log lines)"
        for v in r.verifications
    ] or ["- no verification ran (no action was executed)"]
    t = r.task
    L += [
        "",
        "## Outcome (durable state)",
        f"- Incident: {inc.status}"
        + (f" / {inc.resolution}" if inc.resolution else "")
        + (" - OPEN, owned by a human" if inc.status == "escalated" else ""),
        f"- Task: {t.status} / {t.outcome}" if t else "- Task: none",
    ]
    if r.escalation_reasons:
        L.append(f"- Escalation reason(s): {'; '.join(r.escalation_reasons)}")
    L += ["", "## Timeline"]
    L += [f"- {e.at.isoformat()} {e.event}" for e in draft.timeline]
    u = r.usage
    L += [
        "",
        "## AI usage and cost",
        f"- Investigation: {u.investigation_model_calls} model calls, "
        f"{u.investigation_input_tokens} input / {u.investigation_output_tokens} output tokens",
        f"- Report drafting: {u.report_model_calls} model calls, {u.report_input_tokens} input / "
        f"{u.report_output_tokens} output tokens",
        "- Cost: "
        + (
            f"{u.cost_usd_estimate:.6f} USD ({u.cost_basis})"
            if u.cost_usd_estimate is not None
            else u.cost_basis
        ),
        "",
        "## Unresolved questions",
    ]
    L += [f"- {q}" for q in draft.unresolved_questions] or ["- none"]
    L += ["", "## Follow-up items"]
    L += [f"- {f}" for f in draft.follow_up] or ["- none"]
    body = "\n".join(L) + "\n"
    content = {
        "schema_version": 1,
        "generation_mode": p.generation_mode,
        "provenance": {
            "version": p.version,
            "model_id": p.model_id,
            "auth_mode": p.auth_mode,
            "is_mock_model": p.is_mock_model,
            "fallback_reason": p.fallback_reason,
            "record_sha256": p.record_sha256,
            "generated_at": p.generated_at.isoformat(),
            "validation_attempts": p.validation_attempts,
            "rejections": p.rejections,
        },
        "narrative": draft.model_dump(mode="json"),
        "record": r.model_dump(mode="json"),
    }
    return body, content
