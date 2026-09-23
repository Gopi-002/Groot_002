"""Phase 5 pure units: lifecycle state machine, jittered backoff, AI cost
estimation, redaction and correlation, and Phase 5 settings validation."""

from __future__ import annotations

import io
import json
import logging
import random
from datetime import UTC, datetime

import pytest

from app.agent.gateway import Usage
from app.agent.lifecycle import (
    TRANSITIONS,
    DurableView,
    IllegalTransition,
    Lifecycle,
    check_transition,
    derive,
)
from app.agent.usage import estimate_cost, seconds_until_next_utc_day
from app.backoff import backoff_seconds, jittered_backoff
from app.observability.logging import JsonFormatter, log_context, redact_text

L = Lifecycle


# --- lifecycle ------------------------------------------------------------------------------


def test_contract_states_all_defined_and_terminals_are_final():
    assert {s.value for s in L} == {
        "DETECTED",
        "QUEUED",
        "INVESTIGATING",
        "ACTION_PROPOSED",
        "POLICY_CHECK",
        "WAITING_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "REPORTING",
        "RESOLVED",
        "RETRY_SCHEDULED",
        "ESCALATED",
        "FAILED",
    }
    for terminal in (L.RESOLVED, L.ESCALATED, L.FAILED):
        assert TRANSITIONS[terminal] == frozenset()
    for state, nxt in TRANSITIONS.items():
        assert state in TRANSITIONS and nxt <= set(L)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (L.DETECTED, L.QUEUED),
        (L.QUEUED, L.INVESTIGATING),
        (L.INVESTIGATING, L.ACTION_PROPOSED),
        (L.ACTION_PROPOSED, L.POLICY_CHECK),
        (L.POLICY_CHECK, L.WAITING_APPROVAL),
        (L.WAITING_APPROVAL, L.POLICY_CHECK),
        (L.POLICY_CHECK, L.EXECUTING),
        (L.EXECUTING, L.VERIFYING),
        (L.VERIFYING, L.REPORTING),
        (L.REPORTING, L.RESOLVED),
        (L.REPORTING, L.ESCALATED),
        (L.INVESTIGATING, L.RETRY_SCHEDULED),
        (L.RETRY_SCHEDULED, L.INVESTIGATING),
    ],
)
def test_legal_edges(a, b):
    check_transition(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (L.RESOLVED, L.QUEUED),
        (L.ESCALATED, L.EXECUTING),
        (L.FAILED, L.REPORTING),
        (L.DETECTED, L.EXECUTING),
        (L.INVESTIGATING, L.EXECUTING),
        (L.ACTION_PROPOSED, L.EXECUTING),
        (L.WAITING_APPROVAL, L.EXECUTING),
        (L.QUEUED, L.RESOLVED),
        (L.VERIFYING, L.RESOLVED),
    ],
)
def test_illegal_edges_rejected(a, b):
    # no skipping policy before execution, no resolution without reporting, terminals final
    with pytest.raises(IllegalTransition):
        check_transition(a, b)


@pytest.mark.parametrize(
    ("view", "state"),
    [
        (DurableView(None), L.DETECTED),
        (DurableView("queued"), L.QUEUED),
        (DurableView("awaiting_investigation"), L.QUEUED),
        (DurableView("running"), L.INVESTIGATING),
        (DurableView("awaiting_policy", investigation_completed=True), L.ACTION_PROPOSED),
        (DurableView("running", investigation_completed=True), L.POLICY_CHECK),
        (DurableView("waiting_approval", investigation_completed=True), L.WAITING_APPROVAL),
        (
            DurableView("running", investigation_completed=True, attempt_status="executing"),
            L.EXECUTING,
        ),
        (
            DurableView("running", investigation_completed=True, attempt_status="succeeded"),
            L.VERIFYING,
        ),
        (DurableView("retry_scheduled"), L.RETRY_SCHEDULED),
        (DurableView("resolved", report_job_status="generating"), L.REPORTING),
        (DurableView("resolved", report_job_status="validated"), L.RESOLVED),
        (DurableView("escalated", report_job_status="fallback"), L.ESCALATED),
        (DurableView("dead_lettered"), L.ESCALATED),
        (DurableView("failed"), L.FAILED),
    ],
)
def test_lifecycle_derived_from_durable_state(view, state):
    assert derive(view) is state


# --- backoff with jitter --------------------------------------------------------------------


def test_jitter_is_bounded_by_the_deterministic_schedule():
    rng = random.Random(7)
    for attempt in range(1, 12):
        base = backoff_seconds(attempt, 5, 600)
        for _ in range(50):
            d = jittered_backoff(attempt, 5, 600, rng)
            assert 0.8 * base <= d <= base
    assert len({jittered_backoff(3, 5, 600, rng) for _ in range(20)}) > 1


# --- cost estimation --------------------------------------------------------------------------


def test_cost_is_none_without_operator_prices(base_env):
    from app.config import Settings

    assert estimate_cost(Settings(), Usage(1000, 1000)) is None


def test_cost_estimate_is_conservative_for_cache_tokens(base_env):
    from app.config import Settings

    s = Settings(ai_input_usd_per_mtok=3.0, ai_output_usd_per_mtok=15.0)
    # cache reads/writes charged at the full input price (never under-estimates)
    assert estimate_cost(s, Usage(1_000_000, 100_000, 500_000, 0)) == pytest.approx(4.5 + 1.5)


def test_next_utc_day():
    assert seconds_until_next_utc_day(datetime(2026, 9, 23, 23, 59, 0, tzinfo=UTC)) == 60


# --- redaction and correlation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "secret_text",
    [
        "redis://:s3cr3tRedisPw@redis:6379/0",
        "postgresql+psycopg://sentinelops:dbPw123456@postgres/sentinelops",
        "Authorization: Bearer sop_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        "operator token sop_AbCdEfGhIjKlMnOpQrStUvWxYz012345 used",
        "X-SentinelOps-Signature: sha256=" + "ab" * 32,
        "webhook_secret=whsecret-value-123456",
        "executor_token=exec-token-value-xyz",
        "signing_key: action-signing-key-value",
        "key sk-ant-api03-verysecretvalue",
    ],
)
def test_secret_patterns_are_redacted(secret_text):
    out = redact_text(secret_text)
    for fragment in (
        "s3cr3tRedisPw",
        "dbPw123456",
        "sop_AbCd",
        "ab" * 32,
        "whsecret-value",
        "exec-token-value",
        "action-signing-key-value",
        "verysecretvalue",
    ):
        assert fragment not in out


def test_structured_secret_fields_and_correlation_ids(caplog):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test"))
    lg = logging.getLogger("phase5.test")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        with log_context(incident_id="i-1", task_id="t-1"), log_context(report_job_id="r-1"):
            lg.info(
                "x",
                extra={
                    "notify_webhook_secret": "abc",
                    "decision_signature": "f" * 64,
                    "approval_signing_key": "k",
                },
            )
        lg.info("outside")
    finally:
        lg.removeHandler(handler)
    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert (first["incident_id"], first["task_id"], first["report_job_id"]) == ("i-1", "t-1", "r-1")
    assert first["notify_webhook_secret"] == first["decision_signature"] == "[REDACTED]"
    assert first["approval_signing_key"] == "[REDACTED]"
    assert "incident_id" not in second


# --- settings ---------------------------------------------------------------------------------


def test_webhook_settings_are_validated(base_env, monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("SENTINEL_NOTIFY_CHANNELS", "log,webhook")
    monkeypatch.setenv("SENTINEL_NOTIFY_WEBHOOK_URL", "http://notify-sink:8004/hook")
    monkeypatch.setenv("SENTINEL_NOTIFY_WEBHOOK_ALLOWED_HOSTS", "notify-sink")
    s = Settings()
    assert s.notify_channels == ("log", "webhook")
    monkeypatch.setenv("SENTINEL_NOTIFY_WEBHOOK_ALLOWED_HOSTS", "other")
    with pytest.raises(ValueError, match="allowlist"):
        Settings()


def test_production_requires_https_signed_webhook(base_env):
    from app.config import Settings

    common = {
        "environment": "production",
        "db_password": "p" * 20,
        "redis_password": "r" * 20,
        "notify_channels": ("log", "webhook"),
        "notify_webhook_allowed_hosts": ("hooks.example.com",),
    }
    with pytest.raises(ValueError, match="https"):
        Settings(**common, notify_webhook_url="http://hooks.example.com/x")
    with pytest.raises(ValueError, match="signing secret"):
        Settings(**common, notify_webhook_url="https://hooks.example.com/x")
    Settings(
        **common, notify_webhook_url="https://hooks.example.com/x", notify_webhook_secret="s" * 32
    )


def test_cost_ceilings_require_prices_and_test_hooks_forbidden_in_production(base_env):
    from app.config import Settings

    with pytest.raises(ValueError, match="require"):
        Settings(ai_daily_max_cost_usd=5)
    with pytest.raises(ValueError, match="test hooks"):
        Settings(
            environment="production",
            db_password="p" * 20,
            redis_password="r" * 20,
            test_report_delay_seconds=5,
        )


def test_empty_optional_env_values_are_unset(base_env, monkeypatch):
    from app.config import Settings

    for k in (
        "SENTINEL_AI_DAILY_MAX_TOKENS",
        "SENTINEL_NOTIFY_WEBHOOK_SECRET",
        "SENTINEL_AI_INPUT_USD_PER_MTOK",
        "SENTINEL_BACKUP_MAX_AGE_HOURS",
    ):
        monkeypatch.setenv(k, "")
    s = Settings()
    assert s.ai_daily_max_tokens is None and s.notify_webhook_secret is None
    assert s.ai_input_usd_per_mtok is None and s.backup_max_age_hours is None
