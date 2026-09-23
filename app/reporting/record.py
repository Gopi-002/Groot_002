"""Canonical, deterministic ``IncidentRecord``: the ONLY input to reporting.

Built exclusively from authoritative PostgreSQL rows by explicit, bounded
queries - never by forwarding arbitrary rows to a model. Every list is capped,
every free-text field is redacted and truncated, secrets never appear (no
credential columns exist; audit details are whitelisted). Content that
originated outside SentinelOps' deterministic components (application log
lines, the investigation model's own statements) is kept in fields whose names
say so, and the report prompt declares it untrusted data.

``sha256`` is a hash of the canonical JSON; it is stored with every report as
provenance of exactly which facts the report was produced from.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, Engine, text

from app.observability.logging import redact, redact_text

SCHEMA_VERSION = 1
MAX_EVIDENCE = 100
MAX_CHECKS = 10
MAX_LOG_LINES = 12
MAX_AUDIT = 80
MAX_TIMELINE = 60
AUDIT_DETAIL_KEYS = (
    "reason",
    "decision",
    "rule_ids",
    "phase",
    "outcome",
    "status",
    "failure_type",
    "action",
    "error",
    "recorded_via",
    "ledger_status",
)


def _clip(value: object, n: int = 300) -> str | None:
    if value is None:
        return None
    return redact_text(str(value))[:n]


class _Rec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecIncident(_Rec):
    id: uuid.UUID
    service: str
    incident_type: str
    severity: str
    status: str
    resolution: str | None
    summary: str
    occurrence_count: int
    first_failure_at: datetime | None
    last_failure_at: datetime | None
    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


class RecTask(_Rec):
    id: uuid.UUID
    status: str
    outcome: str | None
    attempt: int
    max_attempts: int
    model_id: str | None
    last_error: str | None
    created_at: datetime
    completed_at: datetime | None


class RecCheck(_Rec):
    id: uuid.UUID | None
    checked_at: datetime
    outcome: str
    http_status: int | None
    latency_ms: float | None


class RecDetection(_Rec):
    evidence_id: uuid.UUID
    collected_at: datetime
    failure_type: str | None
    threshold: int | None
    checks: list[RecCheck]


class RecMonitoring(_Rec):
    window_start: datetime | None
    window_end: datetime | None
    outcome_counts: dict[str, int]
    last_check: RecCheck | None


class RecEvidence(_Rec):
    id: uuid.UUID
    source: str
    tool_name: str | None
    status: str | None
    collected_at: datetime
    sha256: str


class RecLogExcerpt(_Rec):
    evidence_id: uuid.UUID
    untrusted_lines: list[str]


class RecStatement(_Rec):
    statement: str
    evidence_ids: list[uuid.UUID]


class RecHypothesis(_Rec):
    statement: str
    certainty: str
    confidence: str | None
    evidence_ids: list[uuid.UUID]


class RecProposal(_Rec):
    action: str
    target_service: str | None
    evidence_ids: list[uuid.UUID]


class RecInvestigation(_Rec):
    id: uuid.UUID
    task_id: uuid.UUID
    status: str
    model_id: str
    auth_mode: str
    is_mock: bool
    failure_reason: str | None
    tool_calls: int
    model_calls: int
    reasoning_attempts: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    started_at: datetime
    completed_at: datetime
    # The investigation model's own (validated) statements: AI-authored content.
    ai_observations: list[RecStatement]
    ai_hypotheses: list[RecHypothesis]
    proposed_action: RecProposal | None
    missing_evidence: list[str]
    next_step: str | None


class RecPolicyDecision(_Rec):
    id: uuid.UUID
    phase: str
    decision: str
    rule_ids: list[str]
    reasons: list[str]
    policy_version: str
    evaluated_at: datetime


class RecApproval(_Rec):
    id: uuid.UUID
    status: str
    proposed_action: str | None
    risk: str | None
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by: str | None


class RecAction(_Rec):
    id: uuid.UUID
    action_id: uuid.UUID
    action_type: str
    status: str
    executed: bool
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    recorded_via: str | None
    error: str | None


class RecVerification(_Rec):
    id: uuid.UUID
    action_attempt_id: uuid.UUID
    status: str
    reason: str
    probes: int
    healthy_probes: int
    critical_lines: int
    started_at: datetime
    completed_at: datetime


class RecUsage(_Rec):
    investigation_model_calls: int
    investigation_input_tokens: int
    investigation_output_tokens: int
    report_model_calls: int
    report_input_tokens: int
    report_output_tokens: int
    cost_usd_estimate: float | None
    cost_basis: str


class RecAudit(_Rec):
    id: uuid.UUID
    occurred_at: datetime
    actor_type: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    details: dict[str, Any]


class RecTimeline(_Rec):
    at: datetime
    kind: str
    ref_id: uuid.UUID
    text: str


class IncidentRecord(_Rec):
    schema_version: int
    incident: RecIncident
    task: RecTask | None
    tasks: list[RecTask]
    detection: list[RecDetection]
    monitoring: RecMonitoring
    investigation: RecInvestigation | None
    evidence: list[RecEvidence]
    untrusted_log_excerpts: list[RecLogExcerpt]
    policy_decisions: list[RecPolicyDecision]
    approvals: list[RecApproval]
    actions: list[RecAction]
    verifications: list[RecVerification]
    escalated: bool
    escalation_reasons: list[str]
    usage: RecUsage
    audit: list[RecAudit]
    timeline: list[RecTimeline]

    # --- derived facts (deterministic) ---------------------------------------------------
    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), default=str
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @property
    def executed_actions(self) -> list[RecAction]:
        return [a for a in self.actions if a.executed]

    @property
    def verification_passed(self) -> bool:
        return any(v.status == "passed" for v in self.verifications)

    @property
    def verification_failed(self) -> bool:
        return any(v.status == "failed" for v in self.verifications)

    @property
    def recovery_confirmed(self) -> bool:
        """Recovery is a fact only if the deterministic verifier passed, or the
        monitor's healthy-check hysteresis auto-resolved the incident."""
        inc = self.incident
        return self.verification_passed or (
            inc.status in ("resolved", "closed")
            and inc.resolution in ("auto_recovered", "remediated")
        )

    @property
    def human_approved(self) -> bool:
        return any(a.status == "approved" for a in self.approvals)

    @property
    def deciders(self) -> set[str]:
        return {a.decided_by for a in self.approvals if a.decided_by}

    @property
    def evidence_ids(self) -> set[uuid.UUID]:
        return {e.id for e in self.evidence}

    def id_index(self) -> dict[uuid.UUID, list[datetime]]:
        """Every record id the report may cite, with the timestamps it carries."""
        idx: dict[uuid.UUID, list[datetime]] = {}

        def add(i: uuid.UUID | None, *ts: datetime | None) -> None:
            if i is not None:
                idx.setdefault(i, []).extend(t for t in ts if t is not None)

        inc = self.incident
        add(inc.id, inc.opened_at, inc.first_failure_at, inc.last_failure_at, inc.resolved_at)
        for t in self.tasks:
            add(t.id, t.created_at, t.completed_at)
        for e in self.evidence:
            add(e.id, e.collected_at)
        for d in self.detection:
            add(d.evidence_id, d.collected_at)
            for c in d.checks:
                add(c.id, c.checked_at)
        if self.investigation:
            add(
                self.investigation.id,
                self.investigation.started_at,
                self.investigation.completed_at,
            )
        for p in self.policy_decisions:
            add(p.id, p.evaluated_at)
        for a in self.approvals:
            add(a.id, a.requested_at, a.decided_at, a.expires_at)
        for x in self.actions:
            add(x.id, x.requested_at, x.started_at, x.completed_at)
        for v in self.verifications:
            add(v.id, v.started_at, v.completed_at)
        for ev in self.audit:
            add(ev.id, ev.occurred_at)
        for tl in self.timeline:
            add(tl.ref_id, tl.at)
        return idx

    @property
    def claude_used(self) -> bool:
        """True only if a real (non-mock) Anthropic model did AI work here."""
        inv = self.investigation
        return bool(inv and inv.auth_mode == "api_key" and not inv.is_mock)


# --- builder -------------------------------------------------------------------------------


def _rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _uuid(v: Any) -> uuid.UUID | None:
    return uuid.UUID(str(v)) if v is not None else None


def _task(r: dict[str, Any]) -> RecTask:
    return RecTask(
        id=r["id"],
        status=r["status"],
        outcome=r["outcome"],
        attempt=r["attempt"],
        max_attempts=r["max_attempts"],
        model_id=r["model_id"],
        last_error=_clip(r["last_error"], 200),
        created_at=r["created_at"],
        completed_at=r["completed_at"],
    )


def _check(c: dict[str, Any]) -> RecCheck | None:
    try:
        return RecCheck(
            id=_uuid(c.get("id")),
            checked_at=c["checked_at"],
            outcome=str(c["outcome"]),
            http_status=c.get("http_status"),
            latency_ms=c.get("latency_ms"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _investigation(r: dict[str, Any]) -> RecInvestigation:
    res = r["result"] or {}
    pa = res.get("proposed_action")
    return RecInvestigation(
        id=r["id"],
        task_id=r["task_id"],
        status=r["status"],
        model_id=r["model_id"],
        auth_mode=r["auth_mode"],
        is_mock=r["auth_mode"] == "mock" or str(r["model_id"]).startswith("mock-"),
        failure_reason=_clip(r["failure_reason"]),
        tool_calls=r["tool_calls"],
        model_calls=r["model_calls"],
        reasoning_attempts=r["reasoning_attempts"],
        input_tokens=int(r["input_tokens"]),
        output_tokens=int(r["output_tokens"]),
        cost_usd=float(r["cost_usd"]) if r["cost_usd"] is not None else None,
        started_at=r["started_at"],
        completed_at=r["completed_at"],
        ai_observations=[
            RecStatement(statement=_clip(o["statement"], 600) or "", evidence_ids=o["evidence_ids"])
            for o in (res.get("observations") or [])[:20]
        ],
        ai_hypotheses=[
            RecHypothesis(
                statement=_clip(h["statement"], 600) or "",
                certainty=str(h.get("certainty", "hypothesis")),
                confidence=h.get("confidence"),
                evidence_ids=h.get("supporting_evidence_ids") or [],
            )
            for h in (res.get("hypotheses") or [])[:5]
        ],
        proposed_action=RecProposal(
            action=str(pa.get("action")),
            target_service=pa.get("target_service"),
            evidence_ids=pa.get("evidence_ids") or [],
        )
        if isinstance(pa, dict) and pa.get("action")
        else None,
        missing_evidence=[_clip(m, 200) or "" for m in (res.get("missing_evidence") or [])[:10]],
        next_step=res.get("next_step"),
    )


def _audit_details(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in AUDIT_DETAIL_KEYS:
        if k in d and d[k] is not None:
            v = d[k]
            out[k] = [str(x)[:40] for x in v][:10] if isinstance(v, list) else _clip(v, 200)
    return out


def build_record(
    engine: Engine, incident_id: uuid.UUID, task_id: uuid.UUID | None
) -> IncidentRecord:
    with engine.connect() as conn:
        return build_record_conn(conn, incident_id, task_id)


def build_record_conn(
    conn: Connection, incident_id: uuid.UUID, task_id: uuid.UUID | None
) -> IncidentRecord:
    inc_rows = _rows(
        conn,
        "SELECT i.id, s.name AS service, i.service_id, i.incident_type, i.severity, i.status, "
        "i.resolution, i.summary, i.occurrence_count, i.first_failure_at, i.last_failure_at, "
        "i.opened_at, i.last_seen_at, i.resolved_at FROM incidents i "
        "JOIN services s ON s.id = i.service_id WHERE i.id = :i",
        i=incident_id,
    )
    if not inc_rows:
        raise LookupError(f"incident {incident_id} not found")
    ir = inc_rows[0]
    incident = RecIncident(
        **{k: ir[k] for k in RecIncident.model_fields if k != "summary"},
        summary=_clip(ir["summary"], 300) or "",
    )
    task_rows = _rows(
        conn,
        "SELECT id, status, outcome, attempt, max_attempts, model_id, last_error, created_at, "
        "completed_at FROM tasks WHERE incident_id=:i ORDER BY created_at LIMIT 10",
        i=incident_id,
    )
    tasks = [_task(t) for t in task_rows]
    task = next((t for t in tasks if t.id == task_id), tasks[-1] if tasks else None)

    ev_rows = _rows(
        conn,
        "SELECT id, source, tool_name, content, content_sha256, collected_at FROM evidence "
        "WHERE incident_id=:i ORDER BY collected_at, id LIMIT :n",
        i=incident_id,
        n=MAX_EVIDENCE,
    )
    evidence: list[RecEvidence] = []
    detection: list[RecDetection] = []
    logs: list[RecLogExcerpt] = []
    for e in ev_rows:
        content = e["content"] if isinstance(e["content"], dict) else {}
        evidence.append(
            RecEvidence(
                id=e["id"],
                source=e["source"],
                tool_name=e["tool_name"],
                status=str(content.get("status")) if content.get("status") else None,
                collected_at=e["collected_at"],
                sha256=e["content_sha256"],
            )
        )
        if e["source"] == "health_check":
            rule = content.get("rule") or {}
            checks = [c for c in (_check(x) for x in (content.get("checks") or [])) if c]
            detection.append(
                RecDetection(
                    evidence_id=e["id"],
                    collected_at=e["collected_at"],
                    failure_type=rule.get("failure_type"),
                    threshold=rule.get("threshold"),
                    checks=checks[:MAX_CHECKS],
                )
            )
        if e["tool_name"] == "get_application_logs" and content.get("status") == "ok":
            lines = ((content.get("data") or {}).get("lines")) or []
            logs.append(
                RecLogExcerpt(
                    evidence_id=e["id"],
                    untrusted_lines=[
                        _clip(ln.get("line"), 300) or ""
                        for ln in lines[-MAX_LOG_LINES:]
                        if isinstance(ln, dict)
                    ],
                )
            )

    window_start = (incident.first_failure_at or incident.opened_at) - timedelta(minutes=5)
    window_end = (
        task.completed_at if task and task.completed_at else None
    ) or incident.last_seen_at
    counts = {
        str(k): int(v)
        for k, v in conn.execute(
            text(
                "SELECT outcome, count(*) FROM health_checks WHERE service_id=:s "
                "AND checked_at BETWEEN :a AND :b GROUP BY 1 ORDER BY 1"
            ),
            {"s": ir["service_id"], "a": window_start, "b": window_end},
        ).all()
    }
    last = _rows(
        conn,
        "SELECT id, checked_at, outcome, http_status, latency_ms FROM health_checks "
        "WHERE service_id=:s AND checked_at <= :b ORDER BY checked_at DESC LIMIT 1",
        s=ir["service_id"],
        b=window_end,
    )
    monitoring = RecMonitoring(
        window_start=window_start,
        window_end=window_end,
        outcome_counts=counts,
        last_check=_check(last[0]) if last else None,
    )

    inv_rows = _rows(
        conn,
        "SELECT id, task_id, status, model_id, auth_mode, failure_reason, result, tool_calls, "
        "model_calls, reasoning_attempts, input_tokens, output_tokens, cost_usd, started_at, "
        "completed_at FROM investigations WHERE incident_id=:i "
        "ORDER BY (task_id = :t) DESC, completed_at DESC LIMIT 1",
        i=incident_id,
        t=task.id if task else None,
    )
    investigation = _investigation(inv_rows[0]) if inv_rows else None

    policy = [
        RecPolicyDecision(
            id=r["id"],
            phase=r["phase"],
            decision=r["decision"],
            rule_ids=list(r["rule_ids"]),
            reasons=[_clip(x, 200) or "" for x in (r["reasons"] or [])][:10],
            policy_version=r["policy_version"],
            evaluated_at=r["evaluated_at"],
        )
        for r in _rows(
            conn,
            "SELECT id, phase, decision, rule_ids, reasons, policy_version, evaluated_at "
            "FROM policy_decisions WHERE incident_id=:i ORDER BY evaluated_at, id LIMIT 20",
            i=incident_id,
        )
    ]
    approvals = [
        RecApproval(
            id=r["id"],
            status=r["status"],
            proposed_action=r["proposed_action"],
            risk=_clip(r["risk"], 300),
            requested_at=r["requested_at"],
            expires_at=r["expires_at"],
            decided_at=r["decided_at"],
            decided_by=r["decided_by"],
        )
        for r in _rows(
            conn,
            "SELECT id, status, proposed_action, risk, requested_at, expires_at, decided_at, "
            "decided_by FROM approvals WHERE incident_id=:i ORDER BY requested_at LIMIT 10",
            i=incident_id,
        )
    ]
    actions = [
        RecAction(
            id=r["id"],
            action_id=r["action_id"],
            action_type=r["action_type"],
            status=r["status"],
            executed=r["status"] in ("succeeded", "reconciled"),
            requested_at=r["requested_at"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
            recorded_via=(r["result"] or {}).get("recorded_via"),
            error=_clip(r["error"], 200),
        )
        for r in _rows(
            conn,
            "SELECT id, action_id, action_type, status, requested_at, started_at, completed_at, "
            "result, error FROM action_attempts WHERE incident_id=:i ORDER BY requested_at "
            "LIMIT 5",
            i=incident_id,
        )
    ]
    verifications = []
    for r in _rows(
        conn,
        "SELECT id, action_attempt_id, status, reason, observations, started_at, completed_at "
        "FROM verifications WHERE incident_id=:i ORDER BY completed_at LIMIT 5",
        i=incident_id,
    ):
        obs = r["observations"] if isinstance(r["observations"], list) else []
        probes = [o for o in obs if isinstance(o, dict) and o.get("type") == "probe"]
        scans = [o for o in obs if isinstance(o, dict) and o.get("type") == "log_scan"]
        verifications.append(
            RecVerification(
                id=r["id"],
                action_attempt_id=r["action_attempt_id"],
                status=r["status"],
                reason=_clip(r["reason"], 300) or "",
                probes=len(probes),
                healthy_probes=sum(1 for p in probes if p.get("ok")),
                critical_lines=sum(len(s.get("critical_lines") or []) for s in scans),
                started_at=r["started_at"],
                completed_at=r["completed_at"],
            )
        )

    entity_ids = [incident_id, *(t.id for t in tasks), *(a.id for a in approvals)]
    entity_ids += [a.id for a in actions]
    audit_rows = _rows(
        conn,
        "SELECT id, occurred_at, actor_type, actor_id, action, entity_type, entity_id, details "
        "FROM audit_events WHERE entity_id = ANY(CAST(:ids AS uuid[])) "
        "AND action NOT IN ('task_claimed','ai_tool_call') "
        "ORDER BY occurred_at, id LIMIT :n",
        ids=[str(i) for i in entity_ids],
        n=MAX_AUDIT,
    )
    audit_events = [
        RecAudit(
            id=r["id"],
            occurred_at=r["occurred_at"],
            actor_type=r["actor_type"],
            actor_id=_clip(r["actor_id"], 80) or "",
            action=r["action"],
            entity_type=r["entity_type"],
            entity_id=r["entity_id"],
            details=_audit_details(redact(r["details"] or {})),
        )
        for r in audit_rows
    ]
    escalation_reasons = [
        str(a.details.get("reason"))
        for a in audit_events
        if a.action == "incident_escalated" and a.details.get("reason")
    ]
    if task and task.status in ("escalated", "dead_lettered", "failed") and task.outcome:
        escalation_reasons.append(f"task {task.status}: {task.outcome}")

    usage_row = conn.execute(
        text(
            "SELECT count(*) FILTER (WHERE stage='report') AS rc, "
            "COALESCE(sum(input_tokens + cache_read_tokens + cache_write_tokens) "
            "  FILTER (WHERE stage='report'), 0) AS ri, "
            "COALESCE(sum(output_tokens) FILTER (WHERE stage='report'), 0) AS ro, "
            "sum(cost_usd_estimate) AS cost, count(cost_usd_estimate) AS priced, "
            "count(*) AS calls FROM ai_usage WHERE incident_id=:i"
        ),
        {"i": incident_id},
    ).one()
    cost: float | None = None
    basis = "unavailable: no operator-configured prices"
    if usage_row.calls and usage_row.priced == usage_row.calls:
        cost = round(float(usage_row.cost or 0), 6)
        basis = "estimate from operator-configured prices"
    elif usage_row.calls == 0 and investigation and investigation.cost_usd is not None:
        cost, basis = investigation.cost_usd, "estimate from operator-configured prices"
    usage = RecUsage(
        investigation_model_calls=investigation.model_calls if investigation else 0,
        investigation_input_tokens=investigation.input_tokens if investigation else 0,
        investigation_output_tokens=investigation.output_tokens if investigation else 0,
        report_model_calls=int(usage_row.rc or 0),
        report_input_tokens=int(usage_row.ri or 0),
        report_output_tokens=int(usage_row.ro or 0),
        cost_usd_estimate=cost,
        cost_basis=basis,
    )

    return IncidentRecord(
        schema_version=SCHEMA_VERSION,
        incident=incident,
        task=task,
        tasks=tasks,
        detection=detection,
        monitoring=monitoring,
        investigation=investigation,
        evidence=evidence,
        untrusted_log_excerpts=logs[:3],
        policy_decisions=policy,
        approvals=approvals,
        actions=actions,
        verifications=verifications,
        escalated=bool(escalation_reasons) or incident.status == "escalated",
        escalation_reasons=escalation_reasons[:10],
        usage=usage,
        audit=audit_events,
        timeline=_timeline(
            incident,
            tasks,
            evidence,
            investigation,
            policy,
            approvals,
            actions,
            verifications,
            audit_events,
        ),
    )


def _timeline(
    inc: RecIncident,
    tasks: list[RecTask],
    evidence: list[RecEvidence],
    inv: RecInvestigation | None,
    policy: list[RecPolicyDecision],
    approvals: list[RecApproval],
    actions: list[RecAction],
    verifications: list[RecVerification],
    audit_events: list[RecAudit],
) -> list[RecTimeline]:
    t: list[RecTimeline] = []

    def add(at: datetime | None, kind: str, ref: uuid.UUID, msg: str) -> None:
        if at is not None:
            t.append(RecTimeline(at=at, kind=kind, ref_id=ref, text=msg))

    add(inc.first_failure_at, "first_failure", inc.id, f"first failing {inc.incident_type} check")
    add(inc.opened_at, "incident_opened", inc.id, f"incident opened ({inc.incident_type})")
    for e in evidence:
        if e.source == "tool":
            add(e.collected_at, "tool_call", e.id, f"diagnostic {e.tool_name} ({e.status})")
    if inv:
        add(
            inv.started_at,
            "investigation_started",
            inv.id,
            f"investigation started ({inv.model_id})",
        )
        add(inv.completed_at, "investigation_finished", inv.id, f"investigation {inv.status}")
    for p in policy:
        add(
            p.evaluated_at,
            "policy_decision",
            p.id,
            f"policy {p.phase}: {p.decision} {','.join(p.rule_ids)}",
        )
    for a in approvals:
        add(a.requested_at, "approval_requested", a.id, "human approval requested")
        add(a.decided_at, "approval_decided", a.id, f"approval {a.status}")
    for x in actions:
        add(x.started_at, "action_started", x.id, f"{x.action_type} execution started")
        add(x.completed_at, "action_finished", x.id, f"{x.action_type} {x.status}")
    for v in verifications:
        add(v.completed_at, "verification", v.id, f"recovery verification {v.status}")
    for ev in audit_events:
        if ev.action == "incident_escalated":
            add(ev.occurred_at, "incident_escalated", ev.id, "incident escalated to a human")
    for tk in tasks:
        add(tk.completed_at, "task_finished", tk.id, f"task {tk.status} ({tk.outcome})")
    if inc.resolved_at:
        add(
            inc.resolved_at,
            "incident_resolved",
            inc.id,
            f"incident {inc.status} ({inc.resolution})",
        )
    t.sort(key=lambda x: (x.at, x.kind))
    return t[:MAX_TIMELINE]
