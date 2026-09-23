"""ModelGateway backed by the official Anthropic Python SDK (verified against
anthropic 1.8.0). API-key auth only; see docs/auth-decision.md.

Credential isolation: the key is always passed explicitly (``api_key=``). Per the
installed SDK, an explicit credential disables environment/profile/federation
credential discovery, and ``assert_clean_environment`` additionally refuses to
start if ambient ANTHROPIC_* credentials or headers exist. ``base_url`` is passed
explicitly too, so ANTHROPIC_BASE_URL cannot redirect traffic.

Requests use only parameters every current model accepts: ``tool_choice`` auto,
no sampling parameters, no assistant prefill, provider-default thinking. The
server-side refusal ``fallbacks`` feature is deliberately NOT enabled: the
contract forbids silent model fallback, so a refusal becomes ``ModelRefused``.
"""

from __future__ import annotations

from typing import Any, cast

import anthropic
import httpx2
from anthropic.types import MessageParam, ToolChoiceAutoParam, ToolParam
from pydantic import SecretStr

from app.agent.gateway import (
    AuthCheck,
    AuthenticationFailed,
    GatewayError,
    InvalidRequest,
    InvokeRequest,
    ModelDescriptor,
    ModelRefused,
    ModelTurn,
    ModelUnavailable,
    PermissionDenied,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
    RequestTimeout,
    ToolCall,
    Usage,
)
from app.auth.secrets import assert_clean_environment, fingerprint

MAX_LISTED_MODELS = 200


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    try:
        return float(exc.response.headers.get("retry-after", ""))
    except ValueError:
        return None


def _error_type(exc: anthropic.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error")
    return str(err.get("type", "")) if isinstance(err, dict) else ""


def classify(exc: Exception) -> GatewayError:
    """Map SDK exceptions to typed gateway errors. Messages carry no secrets
    (the SDK never echoes the key) and are truncated."""
    rid = getattr(exc, "request_id", None)
    msg = f"{type(exc).__name__}: {str(exc)[:300]}"
    if isinstance(exc, anthropic.APITimeoutError):
        return RequestTimeout(msg, request_id=rid)
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailable(msg, request_id=rid)
    if isinstance(exc, anthropic.APIStatusError):
        et = _error_type(exc)
        if exc.status_code == 402 or et == "billing_error":
            return QuotaExceeded(msg, request_id=rid)
        if isinstance(exc, anthropic.AuthenticationError):
            return AuthenticationFailed(msg, request_id=rid)
        if isinstance(exc, anthropic.PermissionDeniedError):
            return PermissionDenied(msg, request_id=rid)
        if isinstance(exc, anthropic.NotFoundError):
            return ModelUnavailable(msg, request_id=rid)
        if isinstance(exc, anthropic.RateLimitError):
            return RateLimited(msg, retry_after=_retry_after(exc), request_id=rid)
        if exc.status_code >= 500:
            return ProviderUnavailable(msg, retry_after=_retry_after(exc), request_id=rid)
        if exc.status_code == 400 and "credit balance" in str(exc).lower():
            return QuotaExceeded(msg, request_id=rid)
        return InvalidRequest(msg, request_id=rid)
    return ProviderUnavailable(msg, request_id=rid)


def _descriptor(m: Any) -> ModelDescriptor:
    caps = m.capabilities.model_dump(exclude_none=True) if m.capabilities is not None else {}
    return ModelDescriptor(
        id=m.id,
        display_name=m.display_name,
        max_input_tokens=m.max_input_tokens,
        max_output_tokens=m.max_tokens,
        capabilities=caps,
    )


class AnthropicGateway:
    provider = "anthropic"
    auth_mode = "api_key"

    def __init__(
        self,
        api_key: SecretStr,
        *,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        http_client: httpx2.Client | None = None,
        check_environment: bool = True,
    ) -> None:
        if check_environment:
            assert_clean_environment()
        self._fingerprint = fingerprint(api_key)
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )

    def authenticate(self) -> AuthCheck:
        """Minimal, free provider call (model listing consumes no tokens)."""
        try:
            self._client.models.list(limit=1)
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        return AuthCheck(self.provider, self.auth_mode, self._fingerprint)

    def list_models(self) -> list[ModelDescriptor]:
        out: list[ModelDescriptor] = []
        try:
            for m in self._client.models.list(limit=100):
                out.append(_descriptor(m))
                if len(out) >= MAX_LISTED_MODELS:
                    break
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        return out

    def get_model(self, model_id: str) -> ModelDescriptor:
        try:
            return _descriptor(self._client.models.retrieve(model_id))
        except anthropic.APIError as exc:
            raise classify(exc) from exc

    def invoke(self, request: InvokeRequest) -> ModelTurn:
        try:
            resp = self._client.with_options(timeout=request.timeout_seconds).messages.create(
                model=request.model_id,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=cast(list[MessageParam], request.messages),
                tools=cast(list[ToolParam], request.tools),
                tool_choice=ToolChoiceAutoParam(type="auto"),
            )
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        if resp.stop_reason == "refusal":
            raise ModelRefused("the model declined the request", request_id=resp._request_id)
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = tuple(
            ToolCall(id=b.id, name=b.name, input=dict(b.input) if isinstance(b.input, dict) else {})
            for b in resp.content
            if b.type == "tool_use"
        )
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
        )
        return ModelTurn(
            stop_reason=resp.stop_reason,
            text=text,
            tool_calls=calls,
            raw_content=tuple(resp.content),
            usage=usage,
            model=resp.model,
            request_id=resp._request_id,
        )
