"""Deterministic rendering: facts come from the record, the generation mode is
labelled honestly, and fallback reports carry every essential section."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.agent.gateway import InvokeRequest
from app.agent.mock_gateway import DeterministicMockGateway
from app.reporting.render import (
    GENERATION_AI,
    GENERATION_FALLBACK,
    Provenance,
    deterministic_draft,
    render,
)
from app.reporting.stage import REPORT_TOOL, report_tool_definition
from app.reporting.validator import validate_draft
from tests.support.records import make_record, no_foreign

SECTIONS = [
    "## Summary",
    "## Detection evidence (observed facts)",
    "## Investigation (observed facts)",
    "## Evidence reviewed",
    "## Hypotheses (AI investigation; UNCONFIRMED, not root causes)",
    "## Proposed action (what the AI recommended)",
    "## Policy decisions (deterministic policy engine)",
    "## Approval history",
    "## Actions actually executed (executor records)",
    "## Recovery verification (deterministic verifier)",
    "## Outcome (durable state)",
    "## Timeline",
    "## AI usage and cost",
    "## Unresolved questions",
    "## Follow-up items",
]


def prov(mode=GENERATION_FALLBACK, model=None, auth=None, reason="ai_not_configured"):
    return Provenance(
        version=1,
        generation_mode=mode,
        model_id=model,
        auth_mode=auth,
        is_mock_model=bool(model and model.startswith("mock-")),
        fallback_reason=reason if mode == GENERATION_FALLBACK else None,
        record_sha256="f" * 64,
        generated_at=datetime.now(UTC),
        validation_attempts=0 if mode == GENERATION_FALLBACK else 1,
        rejections=[],
    )


@pytest.mark.parametrize(
    "scenario", ["resolved", "recovery_failed", "denied", "auto_recovered", "no_investigation"]
)
def test_fallback_report_has_every_essential_section(scenario):
    r = make_record(scenario)
    body, content = render(r, deterministic_draft(r), prov())
    for s in SECTIONS:
        assert s in body, s
    assert "DETERMINISTIC FALLBACK" in body and "no AI drafted this report" in body
    assert content["generation_mode"] == "deterministic_fallback"
    assert content["provenance"]["model_id"] is None
    assert content["record"]["incident"]["id"] == str(r.incident.id)


def test_escalated_report_never_claims_recovery():
    r = make_record("recovery_failed")
    body, _ = render(r, deterministic_draft(r), prov())
    assert "**failed**" in body and "OPEN, owned by a human" in body
    assert "recovered" not in body.lower().replace("auto_recovered", "")
    assert "resolved as remediated" not in body


def test_denied_report_marks_no_action_executed():
    r = make_record("denied")
    body, _ = render(r, deterministic_draft(r), prov())
    assert "- no action was executed" in body
    assert "**DENY** rules ENV-2" in body


def test_executed_action_is_labelled_from_the_record():
    r = make_record("resolved")
    body, _ = render(r, deterministic_draft(r), prov())
    assert "status **succeeded** (EXECUTED)" in body
    assert "**passed**" in body


def test_mock_ai_report_is_labelled_not_claude():
    r = make_record("resolved")
    body, content = render(
        r, deterministic_draft(r), prov(GENERATION_AI, "mock-investigator-v1", "mock")
    )
    assert "MOCK model `mock-investigator-v1` (TEST/DEMO ONLY - not Claude)" in body
    assert content["provenance"]["is_mock_model"] is True


def test_costs_are_rendered_only_from_records():
    r = make_record("resolved")
    body, _ = render(r, deterministic_draft(r), prov())
    assert "unavailable: no operator-configured prices" in body
    assert "$" not in body


def test_hypotheses_are_rendered_as_unconfirmed():
    r = make_record("resolved")
    body, _ = render(r, deterministic_draft(r), prov())
    assert "UNCONFIRMED, not root causes" in body
    assert "Memory exhaustion in the application process (unconfirmed)" in body


def test_record_hash_is_deterministic():
    r = make_record("resolved")
    assert r.sha256 == r.model_validate_json(r.canonical_json()).sha256


def test_mock_model_drafts_valid_report_from_record_only():
    r = make_record("recovery_failed")
    gw = DeterministicMockGateway()
    turn = gw.invoke(
        InvokeRequest(
            model_id="mock-investigator-v1",
            system="x",
            messages=[
                {
                    "role": "user",
                    "content": f"<incident_record>\n{r.canonical_json()}\n</incident_record>",
                }
            ],
            tools=[report_tool_definition()],
            max_tokens=1000,
            timeout_seconds=5,
        )
    )
    (call,) = turn.tool_calls
    assert call.name == REPORT_TOOL
    assert validate_draft(call.input, r, no_foreign).ok
    assert json.loads(json.dumps(call.input))["outcome"]["incident_status"] == "escalated"


def test_summary_counts_failing_checks_not_occurrences():
    """Phase 6 defect: occurrence_count (1 + bumps) was reported as the number of
    failing checks. Record: threshold 3, occurrence_count 5 -> 7 failing checks."""
    r = make_record("resolved")
    summary = deterministic_draft(r).summary
    assert "after 3 consecutive failing checks (7 failing checks attributed to it in total)" in (
        summary
    )
    assert "(5 failing checks" not in summary
