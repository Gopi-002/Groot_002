import uuid

import pytest
from pydantic import ValidationError

from app.agent.investigation import submit_tool_definition, system_prompt
from app.agent.schema import InvestigationResult
from app.agent.tools import tool_definitions

E1, E2 = str(uuid.uuid4()), str(uuid.uuid4())


def valid(**over):
    base = {
        "incident_id": str(uuid.uuid4()),
        "observations": [{"statement": "Three 500 responses", "evidence_ids": [E1]}],
        "evidence_ids": [E1, E2],
        "hypotheses": [
            {
                "statement": "App in bad state",
                "certainty": "hypothesis",
                "confidence": "medium",
                "supporting_evidence_ids": [E2],
            }
        ],
        "missing_evidence": [],
        "proposed_action": {
            "action": "restart_demo_app",
            "target_service": "demo-app",
            "rationale": "restart may clear it",
            "evidence_ids": [E1],
        },
        "risks": ["brief downtime"],
        "verification_plan": ["3 healthy checks"],
        "next_step": "propose_remediation",
    }
    base.update(over)
    return base


def test_valid_result_parses_and_collects_cited_ids():
    r = InvestigationResult.model_validate(valid())
    assert {str(i) for i in r.cited_ids()} == {E1, E2}


@pytest.mark.parametrize(
    "extra",
    ["policy_decision", "executed", "resolution_status", "approved", "root_cause_confirmed"],
)
def test_policy_execution_and_resolution_fields_rejected(extra):
    with pytest.raises(ValidationError, match="Extra inputs"):
        InvestigationResult.model_validate(valid(**{extra: True}))


@pytest.mark.parametrize("action", ["run_shell", "delete_database", "restart_postgres", ""])
def test_unsupported_actions_rejected(action):
    bad = valid()
    bad["proposed_action"] = {**bad["proposed_action"], "action": action}
    with pytest.raises(ValidationError):
        InvestigationResult.model_validate(bad)


def test_hypothesis_cannot_be_declared_proven():
    bad = valid()
    bad["hypotheses"] = [{**bad["hypotheses"][0], "certainty": "proven"}]
    with pytest.raises(ValidationError):
        InvestigationResult.model_validate(bad)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observations", []),
        ("hypotheses", []),
        ("verification_plan", []),
        ("evidence_ids", ["not-a-uuid"]),
        ("next_step", "execute_now"),
    ],
)
def test_malformed_outputs_rejected(field, value):
    with pytest.raises(ValidationError):
        InvestigationResult.model_validate(valid(**{field: value}))


def test_observation_requires_evidence():
    bad = valid(observations=[{"statement": "unsupported claim", "evidence_ids": []}])
    with pytest.raises(ValidationError):
        InvestigationResult.model_validate(bad)


def test_tool_schemas_are_self_contained_and_closed():
    defs = [*tool_definitions(), submit_tool_definition()]
    names = [d["name"] for d in defs]
    assert names == [*sorted(names[:-1]), "submit_investigation"]
    assert set(names) == {
        "get_incident",
        "get_health_history",
        "get_application_logs",
        "get_container_status",
        "get_resource_metrics",
        "get_previous_incidents",
        "submit_investigation",
    }
    for d in defs:
        assert "$ref" not in str(d["input_schema"])
    for d in defs[:-1]:
        assert d["input_schema"]["additionalProperties"] is False


def test_no_dangerous_tools_exposed():
    names = {d["name"] for d in tool_definitions()}
    for word in ("bash", "shell", "exec", "file", "url", "fetch", "docker", "restart", "write"):
        assert not any(word in n for n in names)


def test_system_prompt_states_untrusted_data_and_budget():
    p = system_prompt(6)
    assert "untrusted" in p and "at most 6 diagnostic" in p and "never" in p.lower()
    assert system_prompt(6) == system_prompt(6)  # stable (cacheable, no timestamps)
