import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.safety.policy import (
    ApprovalState,
    Decision,
    EvidenceRef,
    PolicyConfig,
    PolicyInput,
    action_fingerprint,
    action_id_for,
    evaluate,
)

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
TASK, INC, INV = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
E1 = uuid.uuid4()

CFG = PolicyConfig(
    environment="demo",
    auto_enabled=True,
    approval_enabled=True,
    remediation_environment="isolated-demo",
    target_service="demo-app",
    max_restarts_per_incident=1,
    max_restarts_per_hour=3,
    health_freshness_seconds=120,
    evidence_max_age_seconds=3600,
    max_cost_usd=None,
)
FP = action_fingerprint(
    incident_id=INC,
    task_id=TASK,
    investigation_id=INV,
    action="restart_demo_app",
    target_service="demo-app",
    action_id=action_id_for(INC),
    policy_version=CFG.version,
)


def inp(**over):
    base = PolicyInput(
        phase="pre_execution",
        now=NOW,
        task_id=TASK,
        incident_id=INC,
        incident_status="investigating",
        service_name="demo-app",
        service_environment="demo",
        investigation_id=INV,
        investigation_status="completed",
        investigation_task_id=TASK,
        investigation_incident_id=INC,
        proposed_action="restart_demo_app",
        proposed_target="demo-app",
        cited_evidence_ids=(E1,),
        evidence={E1: EvidenceRef(INC, NOW - timedelta(minutes=5))},
        latest_health=(NOW - timedelta(seconds=10), "unhealthy"),
        restarts_for_incident=0,
        restarts_last_hour=0,
        this_action_status=None,
        lease_valid=True,
        investigation_cost_usd=None,
        approval=None,
        fingerprint=FP,
        executor_configured=True,
    )
    return dataclasses.replace(base, **over)


def approval(**over):
    base = ApprovalState(
        id=uuid.uuid4(),
        status="approved",
        action_fingerprint=FP,
        expires_at=NOW + timedelta(minutes=10),
        decided_at=NOW - timedelta(minutes=1),
        decided_by="alice",
        signature_valid=True,
        decider_is_approver=True,
    )
    return dataclasses.replace(base, **over)


def cfg(**over):
    return dataclasses.replace(CFG, **over)


def test_allow_preauthorized_autonomous_restart():
    r = evaluate(inp(), CFG)
    assert (r.decision, r.effect, r.rule_ids) == (Decision.ALLOW, "restart", ("AUT-1",))


@pytest.mark.parametrize(
    ("over", "config", "rule"),
    [
        ({"investigation_status": "failed"}, {}, "INV-1"),
        ({"investigation_id": None}, {}, "INV-1"),
        ({"investigation_task_id": uuid.uuid4()}, {}, "INV-1"),
        ({"proposed_action": "run_shell"}, {}, "ACT-1"),
        ({"proposed_action": None}, {}, "ACT-1"),
        ({}, {"environment": "production"}, "ENV-1"),
        ({}, {"remediation_environment": None}, "ENV-2"),
        ({"proposed_target": "postgres"}, {}, "TGT-1"),
        ({"proposed_target": None}, {}, "TGT-1"),
        ({"service_name": "other-app"}, {}, "TGT-2"),
        ({"service_environment": "prod"}, {}, "TGT-2"),
        ({"incident_status": "resolved"}, {}, "INC-1"),
        ({"incident_status": "escalated"}, {}, "INC-1"),
        ({"cited_evidence_ids": ()}, {}, "EVD-1"),
        ({"evidence": {}}, {}, "EVD-1"),
        ({"evidence": {E1: EvidenceRef(uuid.uuid4(), NOW)}}, {}, "EVD-1"),
        ({"evidence": {E1: EvidenceRef(INC, NOW - timedelta(hours=3))}}, {}, "EVD-2"),
        ({"latest_health": None}, {}, "HLT-1"),
        ({"latest_health": (NOW - timedelta(minutes=10), "unhealthy")}, {}, "HLT-1"),
        ({"latest_health": (NOW, "healthy")}, {}, "HLT-2"),
        ({"restarts_for_incident": 1}, {}, "LIM-1"),
        ({}, {"max_restarts_per_incident": 0}, "LIM-1"),
        ({"restarts_last_hour": 3}, {}, "LIM-2"),
        ({"this_action_status": "succeeded"}, {}, "DUP-1"),
        ({"this_action_status": "executing"}, {}, "DUP-1"),
        ({"lease_valid": False}, {}, "LSE-1"),
        ({"executor_configured": False}, {}, "EXE-1"),
        ({"investigation_cost_usd": 5.0}, {"max_cost_usd": 1.0}, "CST-1"),
    ],
)
def test_each_condition_denies(over, config, rule):
    r = evaluate(inp(**over), cfg(**config))
    assert r.decision is Decision.DENY and rule in r.rule_ids, r
    assert r.effect == "escalate"


def test_lease_only_checked_before_side_effects():
    assert evaluate(inp(phase="proposal", lease_valid=False), CFG).decision is Decision.ALLOW


def test_multiple_failures_all_reported():
    r = evaluate(inp(proposed_target="x", restarts_for_incident=1), CFG)
    assert {"TGT-1", "LIM-1"} <= set(r.rule_ids)


def test_no_action_and_escalation_never_restart():
    assert evaluate(inp(proposed_action="no_action"), CFG).effect == "none"
    esc = evaluate(inp(proposed_action="escalate_to_human"), CFG)
    assert (esc.decision, esc.effect) == (Decision.ALLOW, "escalate")


def test_disabled_autonomy_requires_approval():
    r = evaluate(inp(), cfg(auto_enabled=False))
    assert (r.decision, r.rule_ids) == (Decision.REQUIRE_APPROVAL, ("APR-0",))


def test_disabled_autonomy_and_approvals_denies():
    r = evaluate(inp(), cfg(auto_enabled=False, approval_enabled=False))
    assert (r.decision, r.rule_ids) == (Decision.DENY, ("AUT-2",))


@pytest.mark.parametrize(
    ("appr", "decision", "rule"),
    [
        ({}, Decision.ALLOW, "APR-1"),
        (
            {"status": "pending", "decided_at": None, "decided_by": None},
            Decision.REQUIRE_APPROVAL,
            "APR-2",
        ),
        (
            {"status": "pending", "decided_at": None, "expires_at": NOW - timedelta(seconds=1)},
            Decision.DENY,
            "APR-3",
        ),
        ({"status": "expired", "decided_at": None}, Decision.DENY, "APR-3"),
        ({"decided_at": NOW + timedelta(hours=1)}, Decision.DENY, "APR-3"),
        ({"status": "rejected"}, Decision.DENY, "APR-7"),
        ({"action_fingerprint": "0" * 64}, Decision.DENY, "APR-4"),
        ({"signature_valid": False}, Decision.DENY, "APR-5"),
        ({"decider_is_approver": False}, Decision.DENY, "APR-6"),
    ],
)
def test_approval_rules(appr, decision, rule):
    r = evaluate(inp(approval=approval(**appr)), cfg(auto_enabled=False))
    assert r.decision is decision and rule in r.rule_ids, r


def test_approval_cannot_override_hard_conditions():
    r = evaluate(inp(approval=approval(), proposed_target="postgres"), cfg(auto_enabled=False))
    assert r.decision is Decision.DENY and "TGT-1" in r.rule_ids


def test_evaluation_error_defaults_to_deny():
    broken = inp(evidence=None)  # type: ignore[arg-type]
    r = evaluate(broken, CFG)
    assert (r.decision, r.rule_ids) == (Decision.DENY, ("SYS-1",))


def test_policy_version_changes_with_config_and_binds_fingerprint():
    other = cfg(max_restarts_per_hour=5)
    assert other.version != CFG.version
    fp2 = action_fingerprint(
        incident_id=INC,
        task_id=TASK,
        investigation_id=INV,
        action="restart_demo_app",
        target_service="demo-app",
        action_id=action_id_for(INC),
        policy_version=other.version,
    )
    assert fp2 != FP


def test_action_id_deterministic_per_incident():
    assert action_id_for(INC) == action_id_for(INC) != action_id_for(uuid.uuid4())


def test_model_prose_is_not_an_input():
    fields = {f.name for f in dataclasses.fields(PolicyInput)}
    assert not fields & {
        "rationale",
        "hypotheses",
        "observations",
        "risks",
        "statement",
        "confidence",
    }


def test_settings_defaults_are_safe(base_env):
    s = Settings()
    c = PolicyConfig.from_settings(s)
    assert (c.auto_enabled, c.remediation_environment, c.max_restarts_per_incident) == (
        False,
        None,
        1,
    )


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"SENTINEL_REMEDIATION_AUTO_ENABLED": "true"}, "isolated-demo"),
        ({"SENTINEL_REMEDIATION_MAX_RESTARTS_PER_INCIDENT": "2"}, "less than or equal"),
        ({"SENTINEL_REMEDIATION_ENVIRONMENT": "production"}, "isolated-demo"),
        ({"SENTINEL_EXECUTOR_TOKEN": "short"}, "executor_token"),
    ],
)
def test_settings_reject_unsafe_config(base_env, monkeypatch, env, match):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match=match):
        Settings()


def test_autonomy_forbidden_in_production(base_env, monkeypatch):
    for k, v in {
        "SENTINEL_ENVIRONMENT": "production",
        "SENTINEL_DB_PASSWORD": "a-very-long-db-password-123",
        "SENTINEL_REDIS_PASSWORD": "a-very-long-redis-password-456",
        "SENTINEL_REMEDIATION_AUTO_ENABLED": "true",
        "SENTINEL_REMEDIATION_ENVIRONMENT": "isolated-demo",
    }.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match="production"):
        Settings()


def test_empty_environment_means_undeclared(base_env, monkeypatch):
    monkeypatch.setenv("SENTINEL_REMEDIATION_ENVIRONMENT", "")
    assert Settings().remediation_environment is None
