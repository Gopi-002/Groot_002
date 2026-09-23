"""Onboarding / model-selection CLI against real PostgreSQL (mock provider)."""

from __future__ import annotations

import json
import os
import stat

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.agent.gateway import AuthCheck, AuthenticationFailed, ModelDescriptor
from app.agent.model_config import active_selection
from app.cli import IO, Cli
from app.config import Settings

pytestmark = pytest.mark.integration

KEY = "sk-ant-api03-ONBOARDING-TEST-KEY-0123456789abcdef"
MODELS = [
    ModelDescriptor("model-alpha", "Alpha", 200_000, 64_000),
    ModelDescriptor("model-beta", "Beta", 1_000_000, 128_000),
]


class FakeProvider:
    provider, auth_mode = "anthropic", "api_key"

    def __init__(self, key, valid=True):
        self.key, self.valid, self.calls = key, valid, []

    def authenticate(self):
        self.calls.append("authenticate")
        if not self.valid:
            raise AuthenticationFailed("invalid x-api-key")
        return AuthCheck("anthropic", "api_key", "sha256:abc")

    def list_models(self):
        self.calls.append("list_models")
        return MODELS

    def get_model(self, model_id):
        self.calls.append(f"get_model:{model_id}")
        return next(m for m in MODELS if m.id == model_id)

    def invoke(self, request):  # onboarding must never spend tokens
        raise AssertionError("onboarding made a billable model call")


def make_cli(engine, tmp_path, answers, secrets=(), valid=True, mock=False):
    out: list[str] = []
    answers, secrets = list(answers), list(secrets)
    built: list[FakeProvider] = []

    def builder(settings, key):
        p = FakeProvider(key, valid)
        built.append(p)
        return p

    s = Settings(
        anthropic_api_key_file=tmp_path / "anthropic_api_key",
        ai_gateway="mock" if mock else "anthropic",
    )
    io = IO(ask=lambda _p: answers.pop(0), ask_secret=lambda _p: secrets.pop(0), say=out.append)
    return Cli(s, engine, io, builder), out, built, s


@pytest.fixture(autouse=True)
def clean_selection(engine, base_env):
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_config SET is_active=false"))


def test_subscription_option_is_disabled_and_explained(engine, tmp_path):
    cli, out, built, _ = make_cli(engine, tmp_path, ["1", "3"])
    assert cli.onboard() == 0
    text_out = "\n".join(out)
    assert "Welcome to SentinelOps" in text_out and "Choose authentication:" in text_out
    assert "1. Claude Subscription  [unavailable]" in text_out
    assert "Subscription integration unavailable for this application" in text_out
    assert built == [] and active_selection(engine) is None


def test_api_key_onboarding_selects_and_persists_mode_and_model_only(engine, tmp_path):
    cli, out, built, s = make_cli(engine, tmp_path, ["2", "2"], secrets=[KEY])
    assert cli.onboard() == 0
    sel = active_selection(engine)
    assert (sel.auth_mode, sel.model_id) == ("api_key", "model-beta")
    assert built[0].calls == ["authenticate", "list_models", "get_model:model-beta"]
    assert s.anthropic_api_key_file.read_text() == KEY
    assert stat.S_IMODE(os.stat(s.anthropic_api_key_file).st_mode) == 0o600
    joined = "\n".join(out)
    assert KEY not in joined and "separate" in joined and "Rotation" in joined
    assert "model-alpha" in joined and "model-beta" in joined


def test_key_never_stored_in_database(engine, tmp_path):
    cli, _, _, _ = make_cli(engine, tmp_path, ["2", "1"], secrets=[KEY])
    cli.onboard()
    with engine.connect() as conn:
        tables = (
            conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
            .scalars()
            .all()
        )
        for t in tables:
            dump = conn.execute(text(f'SELECT to_jsonb(x)::text FROM "{t}" x')).scalars().all()
            assert not any(KEY in d or "ONBOARDING-TEST-KEY" in d for d in dump), t


def test_invalid_key_is_not_saved(engine, tmp_path):
    cli, out, _, s = make_cli(engine, tmp_path, ["2"], secrets=[KEY, KEY, KEY], valid=False)
    assert cli.onboard() == 1
    assert not s.anthropic_api_key_file.exists() and active_selection(engine) is None
    assert any("authentication_failed" in line for line in out)


def test_malformed_key_rejected_before_any_provider_call(engine, tmp_path):
    cli, _, built, _ = make_cli(engine, tmp_path, ["2"], secrets=["bad key", "x", "y"])
    assert cli.onboard() == 1 and built == []


def test_change_model_and_audit(engine, tmp_path):
    cli, _, _, s = make_cli(engine, tmp_path, ["2", "1"], secrets=[KEY])
    cli.onboard()
    cli2, out, _, _ = make_cli(engine, tmp_path, ["2"])
    cli2.s = s
    assert cli2.change_model() == 0
    assert active_selection(engine).model_id == "model-beta"
    assert any("keep the model they were pinned to" in line for line in out)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT details FROM audit_events WHERE "
                    "action='model_selected' ORDER BY occurred_at DESC LIMIT 1"
                )
            )
            .scalars()
            .all()
        )
    assert rows[0]["previous"]["model_id"] == "model-alpha"
    assert KEY not in json.dumps(rows)


def test_status_shows_fingerprint_not_key(engine, tmp_path):
    cli, _, _, s = make_cli(engine, tmp_path, ["2", "1"], secrets=[KEY])
    cli.onboard()
    cli2, out, _, _ = make_cli(engine, tmp_path, [])
    cli2.s = s
    cli2.status()
    joined = "\n".join(out)
    assert "API key: configured (sha256:" in joined and KEY not in joined


def test_remove_key_pauses_ai(engine, tmp_path):
    cli, _, _, s = make_cli(engine, tmp_path, ["2", "1"], secrets=[KEY])
    cli.onboard()
    assert cli.remove_key() == 0 and not s.anthropic_api_key_file.exists()


def test_mock_mode_is_labelled_and_needs_no_key(engine, tmp_path):
    cli, out, _, _ = make_cli(engine, tmp_path, ["2", "1"], mock=True)
    cli.builder = lambda settings, key: __import__(
        "app.agent.mock_gateway", fromlist=["x"]
    ).DeterministicMockGateway()
    assert cli.onboard() == 0
    assert any("MOCK AI GATEWAY ACTIVE" in line for line in out)
    sel = active_selection(engine)
    assert (sel.auth_mode, sel.model_id) == ("mock", "mock-investigator-v1")


def test_secretstr_never_leaks_via_repr():
    assert KEY not in repr(SecretStr(KEY))
