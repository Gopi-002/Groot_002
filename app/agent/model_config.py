"""Selected auth MODE + model ID (never credentials) and gateway construction."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text

from app.agent.anthropic_gateway import AnthropicGateway
from app.agent.gateway import CredentialsMissing, ModelGateway
from app.agent.mock_gateway import DeterministicMockGateway
from app.auth.decision import SUBSCRIPTION
from app.auth.secrets import CredentialError, read_api_key
from app.config import Settings
from app.persistence.audit import audit


@dataclass(frozen=True)
class ModelSelection:
    auth_mode: str
    model_id: str


def active_selection(engine: Engine) -> ModelSelection | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT auth_mode, model_id FROM model_config WHERE is_active")
        ).first()
    return ModelSelection(row.auth_mode, row.model_id) if row else None


def save_selection(conn: Connection, selection: ModelSelection, selected_by: str) -> None:
    """Replace the active selection (auth mode + model id only). Tasks already
    pinned keep their model; only new investigations use the new one."""
    if selection.auth_mode == "subscription" and not SUBSCRIPTION.supported:
        raise ValueError(SUBSCRIPTION.reason)
    previous = conn.execute(
        text("SELECT auth_mode, model_id FROM model_config WHERE is_active FOR UPDATE")
    ).first()
    conn.execute(text("UPDATE model_config SET is_active = false WHERE is_active"))
    conn.execute(
        text(
            "INSERT INTO model_config (auth_mode, model_id, is_active, selected_by) "
            "VALUES (:a, :m, true, :b)"
        ),
        {"a": selection.auth_mode, "m": selection.model_id, "b": selected_by},
    )
    audit(
        conn,
        actor_type="human",
        actor_id=selected_by,
        action="model_selected",
        entity_type="model_config",
        entity_id=None,
        details={
            "auth_mode": selection.auth_mode,
            "model_id": selection.model_id,
            "previous": dict(previous._mapping) if previous else None,
        },
    )


def make_gateway(settings: Settings, auth_mode: str) -> ModelGateway:
    """Build the gateway for a pinned auth mode. Never substitutes one provider
    for another: a mismatch is an explicit error that pauses AI work."""
    if auth_mode == "subscription":
        raise CredentialsMissing(SUBSCRIPTION.reason)
    if auth_mode == "mock":
        if settings.ai_gateway != "mock":
            raise CredentialsMissing(
                "selection uses the mock gateway but this process is "
                "not configured for it (SENTINEL_AI_GATEWAY)"
            )
        return DeterministicMockGateway()
    if auth_mode != "api_key":
        raise CredentialsMissing(f"unknown auth mode {auth_mode!r}")
    if settings.ai_gateway != "anthropic":
        raise CredentialsMissing(
            "selection uses the Anthropic API but this process runs the "
            "mock gateway; refusing to substitute"
        )
    try:
        key = read_api_key(settings.anthropic_api_key_file)
    except CredentialError as exc:
        raise CredentialsMissing(str(exc)) from exc
    return AnthropicGateway(
        key,
        base_url=settings.anthropic_base_url,
        timeout_seconds=settings.ai_request_timeout_seconds,
        max_retries=settings.ai_sdk_max_retries,
    )
