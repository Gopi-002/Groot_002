"""Typed AI report draft (``submit_incident_report``).

The model DRAFTS: a summary, a timeline of cited records, a restatement of
observations vs hypotheses, and follow-up. Every factual field (ids, statuses,
decisions, timestamps, operators, model) is cross-checked against the
canonical record by ``validator.py``; the published facts sections are
rendered from the record itself, never from this draft. There is no field for
cost, tokens, a confirmed root cause, or anything the record does not hold.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Short = Field(min_length=3, max_length=300)
Statement = Field(min_length=3, max_length=600)
Item = Annotated[str, Field(min_length=3, max_length=300)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DraftTimelineEntry(_Strict):
    at: datetime
    event: str = Short
    record_ids: list[uuid.UUID] = Field(min_length=1, max_length=5)


class DraftObservation(_Strict):
    statement: str = Statement
    evidence_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)


class DraftHypothesis(_Strict):
    statement: str = Statement
    certainty: Literal["hypothesis"] = "hypothesis"
    evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class DraftProposedAction(_Strict):
    action: Literal["restart_demo_app", "no_action", "escalate_to_human"]
    investigation_id: uuid.UUID


class DraftPolicyDecision(_Strict):
    decision_id: uuid.UUID
    phase: Literal["proposal", "pre_execution"]
    decision: Literal["ALLOW", "REQUIRE_APPROVAL", "DENY"]
    rule_ids: list[str] = Field(min_length=1, max_length=12)


class DraftApproval(_Strict):
    approval_id: uuid.UUID
    status: Literal["pending", "approved", "rejected", "expired"]
    decided_by: str | None = Field(default=None, max_length=64)


class DraftAction(_Strict):
    action_attempt_id: uuid.UUID
    action: Literal["restart_demo_app"]
    status: Literal["pending", "executing", "succeeded", "failed", "unknown", "reconciled"]


class DraftVerification(_Strict):
    verification_id: uuid.UUID
    status: Literal["passed", "failed"]


class DraftOutcome(_Strict):
    incident_status: str = Field(max_length=32)
    incident_resolution: str | None = Field(default=None, max_length=32)
    task_status: str | None = Field(default=None, max_length=32)
    task_outcome: str | None = Field(default=None, max_length=64)


class DraftModel(_Strict):
    model_id: str = Field(min_length=1, max_length=200)
    auth_mode: Literal["api_key", "subscription", "mock"]


class ReportDraft(_Strict):
    incident_id: uuid.UUID
    service: str = Field(max_length=64)
    incident_type: str = Field(max_length=32)
    severity: str = Field(max_length=16)
    detected_at: datetime
    summary: str = Field(min_length=20, max_length=1500)
    timeline: list[DraftTimelineEntry] = Field(min_length=1, max_length=40)
    observations: list[DraftObservation] = Field(default_factory=list, max_length=20)
    hypotheses: list[DraftHypothesis] = Field(default_factory=list, max_length=10)
    proposed_action: DraftProposedAction | None = None
    policy_decisions: list[DraftPolicyDecision] = Field(default_factory=list, max_length=20)
    approvals: list[DraftApproval] = Field(default_factory=list, max_length=10)
    actions_taken: list[DraftAction] = Field(default_factory=list, max_length=5)
    verifications: list[DraftVerification] = Field(default_factory=list, max_length=5)
    outcome: DraftOutcome
    investigation_model: DraftModel | None = None
    unresolved_questions: list[Item] = Field(default_factory=list, max_length=10)
    follow_up: list[Item] = Field(default_factory=list, max_length=10)

    def free_texts(self, *, include_hypotheses: bool) -> list[tuple[str, str]]:
        """(field, text) pairs of all model-authored prose."""
        out = [("summary", self.summary)]
        out += [(f"timeline[{i}].event", t.event) for i, t in enumerate(self.timeline)]
        out += [(f"observations[{i}]", o.statement) for i, o in enumerate(self.observations)]
        if include_hypotheses:
            out += [(f"hypotheses[{i}]", h.statement) for i, h in enumerate(self.hypotheses)]
        out += [(f"unresolved_questions[{i}]", q) for i, q in enumerate(self.unresolved_questions)]
        out += [(f"follow_up[{i}]", f) for i, f in enumerate(self.follow_up)]
        return out
