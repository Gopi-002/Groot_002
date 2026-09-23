import os
import stat

import pytest
from pydantic import SecretStr, ValidationError

from app.auth import decision
from app.auth.secrets import (
    FORBIDDEN_ENV,
    CredentialError,
    assert_clean_environment,
    delete_api_key,
    fingerprint,
    read_api_key,
    validate_key_format,
    write_api_key,
)
from app.config import Settings

KEY = "sk-ant-api03-TESTKEY-0123456789abcdefghijklmnop"


def test_subscription_marked_unsupported_with_reason_and_sources():
    assert decision.SUBSCRIPTION.supported is False
    assert "unavailable for this application" in decision.SUBSCRIPTION.reason
    assert decision.API_KEY.supported is True
    assert "separate" in decision.API_KEY.reason  # billing separation stated
    assert any("agent-sdk" in s for s in decision.SOURCES)


@pytest.mark.parametrize("name", FORBIDDEN_ENV)
def test_ambient_anthropic_credentials_refused(name):
    with pytest.raises(CredentialError, match=name):
        assert_clean_environment({name: "anything"})


def test_clean_environment_passes():
    assert_clean_environment({"PATH": "/usr/bin"})


@pytest.mark.parametrize("bad", ["", "short", "has space in-the-middle-0123456789", "x" * 500])
def test_key_format_validation(bad):
    with pytest.raises(CredentialError):
        validate_key_format(bad)


def test_key_file_roundtrip_is_0600_and_atomic(tmp_path):
    path = tmp_path / "secrets" / "anthropic_api_key"
    write_api_key(path, SecretStr(KEY + "\n"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert read_api_key(path).get_secret_value() == KEY
    assert not (path.parent / "anthropic_api_key.tmp").exists()
    assert delete_api_key(path) and not delete_api_key(path)


def test_missing_key_is_typed_error(tmp_path):
    with pytest.raises(CredentialError, match="no API key configured"):
        read_api_key(tmp_path / "absent")


def test_fingerprint_never_reveals_key():
    fp = fingerprint(SecretStr(KEY))
    assert fp.startswith("sha256:") and KEY not in fp and "TESTKEY" not in fp


def test_mock_gateway_forbidden_in_production(base_env, monkeypatch):
    monkeypatch.setenv("SENTINEL_ENVIRONMENT", "production")
    monkeypatch.setenv("SENTINEL_DB_PASSWORD", "a-very-long-db-password-123")
    monkeypatch.setenv("SENTINEL_REDIS_PASSWORD", "a-very-long-redis-password-456")
    monkeypatch.setenv("SENTINEL_AI_GATEWAY", "mock")
    with pytest.raises(ValidationError, match="mock AI gateway"):
        Settings()


def test_cost_cap_requires_operator_prices(base_env, monkeypatch):
    monkeypatch.setenv("SENTINEL_AI_MAX_COST_USD", "1.0")
    with pytest.raises(ValidationError, match="ai_input_usd_per_mtok"):
        Settings()


def test_ai_budget_defaults_match_contract(base_env):
    s = Settings()
    assert (s.ai_max_tool_calls, s.ai_max_reasoning_attempts) == (6, 3)
    assert s.ai_gateway == "anthropic" and s.ai_max_cost_usd is None
