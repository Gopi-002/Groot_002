"""Drives the REAL anthropic SDK (installed version) through an in-process
fake transport: request shape, response parsing and error classification are
verified without network access or cost."""

import json

import httpx2
import pytest
from pydantic import SecretStr

from app.agent import gateway as g
from app.agent.anthropic_gateway import AnthropicGateway
from app.auth.secrets import CredentialError

KEY = "sk-ant-api03-FAKE-for-tests-0123456789abcdef"
MODEL_A = {
    "type": "model",
    "id": "model-a",
    "display_name": "Model A",
    "created_at": "2026-01-01T00:00:00Z",
    "max_input_tokens": 200000,
    "max_tokens": 64000,
}
MODEL_B = {**MODEL_A, "id": "model-b", "display_name": "Model B"}


def message(content, stop="tool_use"):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "model-a",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 0,
        },
    }


class Recorder:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        for (method, path), resp in self.routes.items():
            if request.method == method and request.url.path.startswith(path):
                return resp(request) if callable(resp) else resp
        return httpx2.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "no route"}}
        )


def gw(routes):
    rec = Recorder(routes)
    client = httpx2.Client(transport=httpx2.MockTransport(rec))
    return AnthropicGateway(
        SecretStr(KEY),
        base_url="http://fake-anthropic",
        max_retries=0,
        http_client=client,
        check_environment=False,
    ), rec


def err(status, etype, msg="boom", headers=None):
    return httpx2.Response(
        status,
        headers=headers or {},
        json={"type": "error", "error": {"type": etype, "message": msg}},
    )


def req():
    return g.InvokeRequest(
        model_id="model-a",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "name": "get_incident",
                "description": "d",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        max_tokens=1000,
        timeout_seconds=5,
    )


def test_authenticate_is_free_model_listing_and_sends_explicit_key():
    gateway, rec = gw(
        {
            ("GET", "/v1/models"): httpx2.Response(
                200,
                json={
                    "data": [MODEL_A],
                    "has_more": False,
                    "first_id": "model-a",
                    "last_id": "model-a",
                },
            )
        }
    )
    check = gateway.authenticate()
    assert check.auth_mode == "api_key" and check.credential_fingerprint.startswith("sha256:")
    r = rec.requests[0]
    assert r.method == "GET" and r.url.path == "/v1/models"
    assert r.headers.get("x-api-key") == KEY
    assert "authorization" not in {k.lower() for k in r.headers}


def test_explicit_key_ignores_ambient_env_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token-should-never-be-sent")
    gateway, rec = gw(
        {
            ("GET", "/v1/models"): httpx2.Response(
                200, json={"data": [], "has_more": False, "first_id": None, "last_id": None}
            )
        }
    )
    gateway.authenticate()
    assert "ambient-token" not in json.dumps(dict(rec.requests[0].headers))


def test_environment_guard_on_by_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")
    with pytest.raises(CredentialError, match="ANTHROPIC_API_KEY"):
        AnthropicGateway(SecretStr(KEY), base_url="http://fake-anthropic")


def test_list_models_follows_pagination_and_maps_fields():
    pages = iter(
        [
            {"data": [MODEL_A], "has_more": True, "first_id": "model-a", "last_id": "model-a"},
            {"data": [MODEL_B], "has_more": False, "first_id": "model-b", "last_id": "model-b"},
        ]
    )
    gateway, _ = gw({("GET", "/v1/models"): lambda r: httpx2.Response(200, json=next(pages))})
    models = gateway.list_models()
    assert [m.id for m in models] == ["model-a", "model-b"]
    assert models[0].max_input_tokens == 200000 and models[0].max_output_tokens == 64000


def test_get_model_unknown_is_model_unavailable():
    gateway, _ = gw({("GET", "/v1/models/nope"): err(404, "not_found_error", "model: nope")})
    with pytest.raises(g.ModelUnavailable):
        gateway.get_model("nope")


def test_invoke_request_shape_and_parsing():
    body = message(
        [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_incident", "input": {}},
        ]
    )
    gateway, rec = gw({("POST", "/v1/messages"): httpx2.Response(200, json=body)})
    turn = gateway.invoke(req())
    sent = json.loads(rec.requests[0].content)
    assert sent["model"] == "model-a" and sent["tool_choice"] == {"type": "auto"}
    assert sent["system"] == "sys" and sent["max_tokens"] == 1000
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "fallbacks"):
        assert forbidden not in sent
    assert turn.tool_calls[0].name == "get_incident" and turn.text == "checking"
    assert turn.usage == g.Usage(120, 30, 10, 0)
    assert len(turn.raw_content) == 2


def test_refusal_is_typed_and_never_silently_falls_back():
    gateway, _ = gw(
        {("POST", "/v1/messages"): httpx2.Response(200, json=message([], stop="refusal"))}
    )
    with pytest.raises(g.ModelRefused):
        gateway.invoke(req())


@pytest.mark.parametrize(
    ("status", "etype", "msg", "cls", "pause", "retry"),
    [
        (401, "authentication_error", "invalid x-api-key", g.AuthenticationFailed, True, False),
        (403, "permission_error", "no", g.PermissionDenied, True, False),
        (404, "not_found_error", "model: x", g.ModelUnavailable, False, False),
        (402, "billing_error", "pay", g.QuotaExceeded, True, False),
        (
            400,
            "invalid_request_error",
            "Your credit balance is too low",
            g.QuotaExceeded,
            True,
            False,
        ),
        (400, "invalid_request_error", "bad field", g.InvalidRequest, False, False),
        (429, "rate_limit_error", "slow down", g.RateLimited, True, False),
        (500, "api_error", "oops", g.ProviderUnavailable, False, True),
        (529, "overloaded_error", "busy", g.ProviderUnavailable, False, True),
    ],
)
def test_error_classification(status, etype, msg, cls, pause, retry):
    headers = {"retry-after": "42"} if status == 429 else None
    gateway, _ = gw({("POST", "/v1/messages"): err(status, etype, msg, headers)})
    with pytest.raises(cls) as exc:
        gateway.invoke(req())
    assert exc.value.pause_ai is pause and exc.value.retryable is retry
    assert KEY not in str(exc.value)
    if status == 429:
        assert exc.value.retry_after == 42


def test_timeout_and_connection_errors():
    def timeout(request):
        raise httpx2.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx2.ConnectError("refused", request=request)

    for handler, cls in ((timeout, g.RequestTimeout), (refused, g.ProviderUnavailable)):
        gateway, _ = gw({("POST", "/v1/messages"): handler})
        with pytest.raises(cls) as exc:
            gateway.invoke(req())
        assert exc.value.retryable
