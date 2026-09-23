"""Notification payloads, webhook destination validation, retry classification
and the webhook channel's transport behaviour (no network)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.notifications.channels import (
    InvalidWebhookUrl,
    LogChannel,
    Message,
    WebhookChannel,
    classify,
    validate_webhook_url,
)
from app.notifications.events import sanitize

SECRET = "w" * 40


def msg(**payload):
    return Message(uuid.uuid4(), "approval_required", "warning", datetime.now(UTC), payload)


# --- payload construction ------------------------------------------------------------------


def test_payload_whitelist_drops_secrets_and_unknown_keys():
    out = sanitize(
        {
            "title": "approval needed",
            "approval_id": uuid.uuid4(),
            "action_fingerprint": "a" * 64,
            "operator_token": "sop_abcdefghijklmnopqrstuvwxyz",
            "api_key": "sk-ant-api03-SECRET",
            "raw_logs": ["password=hunter2"],
            "risk": "restart; Authorization: Bearer abc.def.ghi",
            "details": {"count": 3, "db_password": "x", "reasons": ["ok"]},
        }
    )
    assert set(out) == {"title", "approval_id", "risk", "details"}
    assert "abc.def.ghi" not in out["risk"] and "[REDACTED]" in out["risk"]
    assert out["details"] == {"count": 3, "reasons": ["ok"]}
    text = json.dumps(out)
    for leak in ("hunter2", "sk-ant", "sop_", "a" * 64):
        assert leak not in text


def test_payload_values_are_bounded_and_serialisable():
    out = sanitize({"summary": "x" * 5000, "details": {"reasons": list(range(100))}})
    assert len(out["summary"]) == 600 and len(out["details"]["reasons"]) == 20
    json.dumps(out)


# --- destination validation (SSRF) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "env"),
    [
        ("https://hooks.example.com/x", "production"),
        ("http://notify-sink:8004/hook", "development"),
        ("https://hooks.example.com:8443/a?b=c", "production"),
    ],
)
def test_valid_webhook_destinations(url, env):
    hosts = ["hooks.example.com", "notify-sink"]
    assert validate_webhook_url(url, hosts, env) == url


@pytest.mark.parametrize(
    ("url", "env", "why"),
    [
        ("http://hooks.example.com/x", "production", "https"),
        ("https://evil.example.net/x", "production", "allowlist"),
        ("https://user:pw@hooks.example.com/x", "production", "credentials"),
        ("ftp://hooks.example.com/x", "development", "https"),
        ("https://169.254.169.254/latest", "development", "link-local"),
        ("https://10.0.0.5/hook", "production", "private"),
        ("https://127.0.0.1/hook", "production", "private"),
        ("https://hooks.example.com/x#frag", "production", "fragment"),
        ("https:///nohost", "production", "host"),
    ],
)
def test_untrusted_webhook_destinations_rejected(url, env, why):
    hosts = ["hooks.example.com", "169.254.169.254", "10.0.0.5", "127.0.0.1"]
    with pytest.raises(InvalidWebhookUrl, match=why):
        validate_webhook_url(url, hosts, env)


# --- retry classification ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "ok", "retry"),
    [
        (200, True, False),
        (204, True, False),
        (500, False, True),
        (503, False, True),
        (429, False, True),
        (408, False, True),
        (400, False, False),
        (401, False, False),
        (404, False, False),
        (302, False, False),
    ],
)
def test_http_classification(code, ok, retry):
    r = classify(code, None)
    assert (r.ok, r.retryable) == (ok, retry)


def test_transport_errors_are_retryable():
    assert classify(None, httpx.ConnectError("x")).retryable
    assert classify(None, httpx.ReadTimeout("x")).retryable
    assert not classify(None, ValueError("x")).retryable


# --- webhook channel ------------------------------------------------------------------------


def test_webhook_sends_signed_idempotent_request_without_following_redirects():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    ch = WebhookChannel(
        "https://hooks.example.com/x",
        SecretStr(SECRET),
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )
    m = msg(title="t")
    res = ch.send(m)
    assert (res.ok, res.retryable, res.error) == (False, False, "redirect_refused")
    assert len(seen) == 1  # the redirect was NOT followed
    req = seen[0]
    assert req.headers["Idempotency-Key"] == str(m.event_id)
    ts = req.headers["X-SentinelOps-Timestamp"]
    expected = hmac.new(SECRET.encode(), ts.encode() + b"." + req.content, hashlib.sha256)
    assert req.headers["X-SentinelOps-Signature"] == "sha256=" + expected.hexdigest()
    body = json.loads(req.content)
    assert body["idempotency_key"] == str(m.event_id) and body["type"] == "approval_required"


def test_webhook_response_read_is_bounded():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5_000_000)

    ch = WebhookChannel(
        "https://hooks.example.com/x",
        None,
        max_response_bytes=1024,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert ch.send(msg()).ok


def test_webhook_network_failure_is_retryable():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    ch = WebhookChannel(
        "https://hooks.example.com/x",
        None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    r = ch.send(msg())
    assert (r.ok, r.retryable, r.error) == (False, True, "ConnectError")


def test_log_channel_always_delivers_and_redacts(caplog):
    caplog.set_level("WARNING")
    assert LogChannel().send(msg(title="Bearer sop_verysecrettokenvalue1234")).ok
    assert "sop_verysecrettokenvalue1234" not in caplog.text
