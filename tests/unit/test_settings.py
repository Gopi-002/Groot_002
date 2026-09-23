import pytest
from pydantic import ValidationError

from app.config import Environment, Settings


def test_valid_settings_from_env(base_env):
    s = Settings()
    assert s.environment is Environment.DEVELOPMENT
    assert s.db_password.get_secret_value() == base_env["SENTINEL_DB_PASSWORD"]


def test_missing_password_rejected(base_env, monkeypatch):
    monkeypatch.delenv("SENTINEL_DB_PASSWORD")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["", "change-me", "password", "postgres"])
def test_placeholder_password_rejected(base_env, monkeypatch, value):
    monkeypatch.setenv("SENTINEL_REDIS_PASSWORD", value)
    with pytest.raises(ValidationError, match="placeholder"):
        Settings()


def test_production_requires_strong_secrets(base_env, monkeypatch):
    monkeypatch.setenv("SENTINEL_ENVIRONMENT", "production")
    monkeypatch.setenv("SENTINEL_DB_PASSWORD", "short-pw")
    with pytest.raises(ValidationError, match="16 characters"):
        Settings()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SENTINEL_ENVIRONMENT", "staging"),
        ("SENTINEL_DB_PORT", "70000"),
        ("SENTINEL_LOG_LEVEL", "LOUD"),
        ("SENTINEL_DEMO_APP_URL", "ftp://x"),
    ],
)
def test_invalid_values_rejected(base_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_not_in_repr_and_url_is_quoted(base_env, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PASSWORD", "p@ss/w:rd#long-enough")
    s = Settings()
    assert "p@ss" not in repr(s) and "p@ss" not in str(s.model_dump())
    assert "p%40ss%2Fw%3Ard%23long-enough@postgres:5432/sentinelops" in s.database_url


def test_settings_are_immutable(base_env):
    s = Settings()
    with pytest.raises(ValidationError):
        s.db_host = "elsewhere"  # type: ignore[misc]
