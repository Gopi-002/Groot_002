"""Deterministic report validation: every fabricated or unsupported claim is
rejected; the record-derived draft is always accepted."""

from __future__ import annotations

import uuid

import pytest

from app.reporting.render import deterministic_draft
from app.reporting.validator import EXECUTION, RECOVERY, affirmed, validate_draft
from tests.support.records import at, make_record, no_foreign

SCENARIOS = ["resolved", "recovery_failed", "denied", "auto_recovered", "no_investigation"]


def draft_for(record, **changes):
    d = deterministic_draft(record).model_dump(mode="json")
    d.update(changes)
    return d


def errors(record, payload, lookup=no_foreign, **kw):
    res = validate_draft(payload, record, lookup, **kw)
    return res.errors


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_record_derived_draft_is_valid_for_every_scenario(scenario):
    r = make_record(scenario)
    assert errors(r, draft_for(r)) == []


@pytest.mark.parametrize("approval", ["approved", "rejected", "expired"])
def test_record_derived_draft_valid_with_approval_history(approval):
    r = make_record("resolved", approval=approval)
    assert errors(r, draft_for(r)) == []


# --- the required §25 scenarios ------------------------------------------------------------


def test_rejects_restart_claim_without_action_attempt():
    r = make_record("denied")
    errs = errors(r, draft_for(r, summary="SentinelOps restarted the application to fix it."))
    assert any("no executed action is recorded" in e for e in errs)


def test_rejects_recovery_claim_when_verification_failed():
    r = make_record("recovery_failed")
    errs = errors(r, draft_for(r, summary="SentinelOps restarted it. The application recovered."))
    assert any("recovery is not confirmed" in e for e in errs)
    # the restart itself did happen: that part is not flagged
    assert not any("no executed action" in e for e in errs)


def test_rejects_hypothesis_stated_as_confirmed_root_cause():
    r = make_record("resolved")
    errs = errors(r, draft_for(r, summary="Root cause was memory exhaustion in the app process."))
    assert any("states a root cause as fact" in e for e in errs)


def test_hedged_root_cause_and_hypothesis_section_are_allowed():
    r = make_record("resolved")
    d = draft_for(
        r,
        summary="The likely root cause is memory exhaustion, which remains unconfirmed; the "
        "restart was executed and recovery verification passed.",
    )
    d["hypotheses"] = [
        {
            "statement": "Memory exhaustion caused the failure.",
            "certainty": "hypothesis",
            "evidence_ids": [],
        }
    ]
    assert errors(r, d) == []


def test_rejects_claude_claim_when_mock_model_was_used():
    r = make_record("resolved")
    errs = errors(
        r, draft_for(r, summary="Claude investigated the incident and proposed a restart.")
    )
    assert any("no Claude model was used" in e for e in errs)


def test_claude_mention_allowed_when_a_real_claude_model_investigated():
    r = make_record("resolved", auth_mode="api_key", model_id="claude-sonnet-5")
    d = draft_for(r, summary="Claude investigated the incident and proposed restart_demo_app.")
    assert errors(r, d) == []


def test_negated_claude_mention_is_not_a_claim():
    r = make_record("resolved")
    d = draft_for(r, summary="A deterministic mock model (not Claude) proposed a restart for this.")
    assert errors(r, d) == []


def test_rejects_nonexistent_evidence_id():
    r = make_record("resolved")
    fake = uuid.uuid4()
    d = draft_for(r)
    d["observations"] = [{"statement": "Logs showed errors.", "evidence_ids": [str(fake)]}]
    errs = errors(r, d)
    assert any(f"evidence {fake} does not exist" in e for e in errs)


def test_rejects_evidence_from_another_incident():
    r = make_record("resolved")
    foreign, other_incident = uuid.uuid4(), uuid.uuid4()
    d = draft_for(r)
    d["observations"] = [{"statement": "Logs showed errors.", "evidence_ids": [str(foreign)]}]
    errs = errors(r, d, lookup=lambda ids: {foreign: other_incident} if foreign in ids else {})
    assert any(f"belongs to another incident ({other_incident})" in e for e in errs)


# --- structured fabrication ------------------------------------------------------------------


def test_rejects_invented_action_attempt():
    r = make_record("denied")
    d = draft_for(r)
    d["actions_taken"] = [
        {
            "action_attempt_id": str(uuid.uuid4()),
            "action": "restart_demo_app",
            "status": "succeeded",
        }
    ]
    assert any("invented action" in e for e in errors(r, d))


def test_rejects_wrong_action_status():
    r = make_record("recovery_failed")
    d = draft_for(r)
    d["actions_taken"][0]["status"] = "failed"
    assert any("status is 'succeeded'" in e for e in errors(r, d))


def test_rejects_invented_approval_and_operator():
    r = make_record("resolved")  # autonomous: no approval exists
    d = draft_for(r)
    d["approvals"] = [{"approval_id": str(uuid.uuid4()), "status": "approved", "decided_by": "bob"}]
    assert any("invented approval" in e for e in errors(r, d))
    r2 = make_record("resolved", approval="approved")
    d2 = draft_for(r2)
    d2["approvals"][0]["decided_by"] = "mallory"
    assert any("operator identities" in e for e in errors(r2, d2))
    d3 = draft_for(r2, summary="The restart was approved by mallory and then executed properly.")
    assert any("names operator 'mallory'" in e for e in errors(r2, d3))


def test_rejects_prose_approval_claim_without_approval():
    r = make_record("resolved")
    d = draft_for(r, summary="An operator approved the restart, which SentinelOps executed.")
    assert any("no human approval is recorded" in e for e in errors(r, d))


def test_rejects_invented_or_altered_policy_decision_and_omissions():
    r = make_record("denied")
    d = draft_for(r)
    d["policy_decisions"][0]["decision"] = "ALLOW"
    assert any("must be ('proposal', 'DENY'" in e for e in errors(r, d))
    d = draft_for(r)
    d["policy_decisions"].append(
        {
            "decision_id": str(uuid.uuid4()),
            "phase": "proposal",
            "decision": "ALLOW",
            "rule_ids": ["AUT-1"],
        }
    )
    assert any("does not exist in the record (invented)" in e for e in errors(r, d))
    d = draft_for(r)
    d["policy_decisions"] = []
    assert any("is missing from policy_decisions" in e for e in errors(r, d))
    d = draft_for(r, summary="The policy engine allowed the restart of the demo application.")
    assert any("no ALLOW policy decision" in e for e in errors(r, d))


def test_rejects_fabricated_verification():
    r = make_record("recovery_failed")
    d = draft_for(r)
    d["verifications"][0]["status"] = "passed"
    assert any("status is 'failed', not 'passed'" in e for e in errors(r, d))
    d = draft_for(r, summary="The restart ran and recovery verification passed on the first try.")
    assert any("no passed verification" in e for e in errors(r, d))
    r2 = make_record("denied")
    d2 = draft_for(r2)
    d2["verifications"] = [{"verification_id": str(uuid.uuid4()), "status": "passed"}]
    assert any("invented verification" in e for e in errors(r2, d2))


def test_rejects_wrong_outcome_and_model():
    r = make_record("recovery_failed")
    d = draft_for(r)
    d["outcome"]["incident_status"] = "resolved"
    assert any("outcome must be" in e for e in errors(r, d))
    d = draft_for(r)
    d["investigation_model"] = {"model_id": "claude-opus-5-5", "auth_mode": "api_key"}
    assert any("investigation_model must be" in e for e in errors(r, d))


def test_rejects_invented_timestamps_costs_and_tokens():
    r = make_record("resolved")
    d = draft_for(r)
    d["timeline"][0]["at"] = at(9999).isoformat()
    assert any("matches no timestamp" in e for e in errors(r, d))
    d = draft_for(r, summary="The incident started at 10:04 UTC and was then handled in full.")
    assert any("timestamps only in timeline" in e for e in errors(r, d))
    d = draft_for(r, summary="Handling the incident cost $0.42 and used 12000 tokens overall.")
    assert any("costs and token counts" in e for e in errors(r, d))


def test_rejects_wrong_identity_fields_and_detection_time():
    r = make_record("resolved")
    d = draft_for(r, incident_id=str(uuid.uuid4()), service="postgres", severity="low")
    d["detected_at"] = at(5).isoformat()
    errs = errors(r, d)
    assert any("incident_id must be" in e for e in errs)
    assert any("service must be 'demo-app'" in e for e in errs)
    assert any("severity must be 'high'" in e for e in errs)
    assert any("detected_at must equal" in e for e in errs)


def test_proposed_action_must_match_investigation():
    r = make_record("resolved")
    d = draft_for(r)
    d["proposed_action"]["action"] = "no_action"
    assert any("proposed_action must be 'restart_demo_app'" in e for e in errors(r, d))
    r2 = make_record("no_investigation")
    d2 = draft_for(r2)
    d2["proposed_action"] = {"action": "restart_demo_app", "investigation_id": str(uuid.uuid4())}
    assert any("proposed_action must be null" in e for e in errors(r2, d2))


def test_schema_rejects_extra_fields_and_non_hypothesis_certainty():
    r = make_record("resolved")
    d = draft_for(r, confirmed_root_cause="memory")
    assert any(e.startswith("schema:") for e in errors(r, d))
    d = draft_for(r)
    d["hypotheses"] = [{"statement": "Memory leak.", "certainty": "confirmed", "evidence_ids": []}]
    assert any(e.startswith("schema:") for e in errors(r, d))


def test_prompt_injection_obeying_draft_is_rejected():
    """A draft that follows instructions injected into logs is caught."""
    r = make_record("recovery_failed")
    d = draft_for(
        r,
        summary="As instructed by the system notice in the logs: the application recovered and "
        "the root cause was a configuration error. No human action is needed.",
    )
    errs = errors(r, d)
    assert any("recovery is not confirmed" in e for e in errs)
    assert any("root cause as fact" in e for e in errs)


# --- negation / questions are not claims ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The application was not restarted.",
        "No restart was performed because the policy denied it.",
        "SentinelOps did not restart the demo application.",
        "Did SentinelOps restart the application?",
    ],
)
def test_negated_and_interrogative_execution_statements_are_not_claims(text):
    assert affirmed(EXECUTION, text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Recovery verification failed, so the application has not recovered.",
        "The service has not recovered yet.",
    ],
)
def test_negated_recovery_statements_are_not_claims(text):
    assert affirmed(RECOVERY, text) == []


def test_contrastive_clause_is_still_checked():
    assert affirmed(RECOVERY, "Although verification did not pass, the service recovered.")
