"""ModelGateway: the ONE abstraction every AI call goes through.

All AI stages use the model pinned on the task; there is no silent provider or
model fallback. Failures surface as typed ``GatewayError`` subclasses carrying a
retry classification the orchestrator acts on:

* ``pause_ai``  - operator/time needed (auth, permission, quota, rate limit):
                  the task is parked, not failed; monitoring/queueing continue.
* ``retryable`` - transient provider trouble: bounded task retries.
* neither       - permanent for this task (bad request, model gone, refusal):
                  escalate to a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol


@dataclass(frozen=True)
class ModelDescriptor:
    id: str
    display_name: str
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    stop_reason: str | None
    text: str
    tool_calls: tuple[ToolCall, ...]
    # Provider content blocks, echoed back unchanged as the assistant turn
    # (required for tool use and for thinking blocks).
    raw_content: tuple[Any, ...]
    usage: Usage
    model: str
    request_id: str | None = None


@dataclass(frozen=True)
class InvokeRequest:
    model_id: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class AuthCheck:
    provider: str
    auth_mode: str
    credential_fingerprint: str | None


class GatewayError(Exception):
    kind: ClassVar[str] = "gateway_error"
    retryable: ClassVar[bool] = False
    pause_ai: ClassVar[bool] = False

    def __init__(
        self, message: str, *, retry_after: float | None = None, request_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.request_id = request_id


class CredentialsMissing(GatewayError):
    kind = "credentials_missing"
    pause_ai = True


class AuthenticationFailed(GatewayError):
    kind = "authentication_failed"  # invalid, revoked or expired key
    pause_ai = True


class PermissionDenied(GatewayError):
    kind = "permission_denied"
    pause_ai = True


class QuotaExceeded(GatewayError):
    kind = "quota_exceeded"  # billing / credit / spend limit
    pause_ai = True


class RateLimited(GatewayError):
    kind = "rate_limited"
    pause_ai = True


class ProviderUnavailable(GatewayError):
    kind = "provider_unavailable"  # 5xx / 529 overloaded / network
    retryable = True


class RequestTimeout(GatewayError):
    kind = "request_timeout"
    retryable = True


class ModelUnavailable(GatewayError):
    kind = "model_unavailable"  # pinned model unknown or not available to this org


class InvalidRequest(GatewayError):
    kind = "invalid_request"


class ModelRefused(GatewayError):
    kind = "model_refused"


class ModelGateway(Protocol):
    provider: str
    auth_mode: str

    def authenticate(self) -> AuthCheck: ...

    def list_models(self) -> list[ModelDescriptor]: ...

    def get_model(self, model_id: str) -> ModelDescriptor: ...

    def invoke(self, request: InvokeRequest) -> ModelTurn: ...
