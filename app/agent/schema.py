"""Typed investigation result (workflow step 5) and its JSON schema.

The model may observe, hypothesize and PROPOSE. Fields for policy verdicts,
execution status or resolution status do not exist (``extra="forbid"``), so any
attempt to supply them fails validation. Hypotheses are pinned to
``certainty="hypothesis"``: a root cause can never be declared proven here.
"""

from __future__ import annotations

import copy
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProposedActionType(StrEnum):
    # The allowlist. Only restart_demo_app is a remediation, and Phase 4's
    # deterministic policy decides whether it may run - the AI never executes.
    RESTART_DEMO_APP = "restart_demo_app"
    NO_ACTION = "no_action"
    ESCALATE_TO_HUMAN = "escalate_to_human"


class NextStep(StrEnum):
    PROPOSE_REMEDIATION = "propose_remediation"
    REQUEST_HUMAN_REVIEW = "request_human_review"
    CONTINUE_MONITORING = "continue_monitoring"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Text = Field(min_length=3, max_length=600)


class Observation(_Strict):
    statement: str = Text
    evidence_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)


class Hypothesis(_Strict):
    statement: str = Text
    certainty: Literal["hypothesis"] = "hypothesis"
    confidence: Literal["low", "medium", "high"]
    supporting_evidence_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)


class ProposedAction(_Strict):
    action: ProposedActionType
    target_service: Literal["demo-app"] | None = None
    rationale: str = Text
    evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class InvestigationResult(_Strict):
    incident_id: uuid.UUID
    observations: list[Observation] = Field(min_length=1, max_length=20)
    evidence_ids: list[uuid.UUID] = Field(min_length=1, max_length=40)
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=5)
    missing_evidence: list[str] = Field(default_factory=list, max_length=10)
    proposed_action: ProposedAction
    risks: list[str] = Field(default_factory=list, max_length=10)
    verification_plan: list[str] = Field(min_length=1, max_length=10)
    next_step: NextStep

    def cited_ids(self) -> set[uuid.UUID]:
        ids: set[uuid.UUID] = set()
        for o in self.observations:
            ids.update(o.evidence_ids)
        for h in self.hypotheses:
            ids.update(h.supporting_evidence_ids)
        ids.update(self.proposed_action.evidence_ids)
        return ids


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local $refs so the tool schema is self-contained."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)  # type: ignore[no-any-return]


def tool_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    return _inline_refs(model.model_json_schema())
