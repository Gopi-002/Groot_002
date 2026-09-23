"""Notification channels behind one small interface (no vendor coupling).

* ``LogChannel``      - structured, redacted WARNING line (always available).
* ``WebhookChannel``  - generic signed JSON webhook to ONE operator-configured
  URL. The destination comes only from trusted configuration (never from a
  model or a payload), is validated against an explicit host allowlist, must be
  HTTPS outside development/test/demo, may not carry credentials, and cloud
  metadata / link-local addresses are always refused. Requests have a bounded
  timeout, no redirects, no proxy-from-environment, a bounded response read, an
  ``Idempotency-Key`` header (receivers can de-duplicate at-least-once delivery)
  and an optional HMAC-SHA256 signature over ``<timestamp>.<body>``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.observability.logging import redact, redact_text

log = logging.getLogger("sentinelops.notify")

LOCAL_ENVIRONMENTS = frozenset({"development", "test", "demo"})
ALWAYS_BLOCKED = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. cloud metadata
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("0.0.0.0/8"),
)


@dataclass(frozen=True)
class Message:
    event_id: uuid.UUID
    event_type: str
    severity: str
    created_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    retryable: bool
    http_status: int | None = None
    error: str | None = None


class NotificationChannel(Protocol):
    name: str

    def send(self, message: Message) -> DeliveryResult: ...


class InvalidWebhookUrl(ValueError):
    pass


def validate_webhook_url(url: str, allowed_hosts: Iterable[str], environment: str) -> str:
    """Return the URL if it is an acceptable, trusted destination, else raise."""
    parts = urlsplit(url)
    allowed = {h.strip().lower() for h in allowed_hosts if h.strip()}
    if parts.scheme not in ("https", "http"):
        raise InvalidWebhookUrl("webhook URL must use https (or http in development/test)")
    if parts.scheme == "http" and environment not in LOCAL_ENVIRONMENTS:
        raise InvalidWebhookUrl("webhook URL must use https outside development/test/demo")
    if parts.username is not None or parts.password is not None:
        raise InvalidWebhookUrl("webhook URL must not embed credentials")
    if parts.fragment:
        raise InvalidWebhookUrl("webhook URL must not have a fragment")
    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidWebhookUrl("webhook URL has no host")
    if host not in allowed:
        raise InvalidWebhookUrl(f"webhook host {host!r} is not in the configured allowlist")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if any(ip in net for net in ALWAYS_BLOCKED):
            raise InvalidWebhookUrl("link-local / metadata addresses are never allowed")
        if environment not in LOCAL_ENVIRONMENTS and (ip.is_loopback or ip.is_private):
            raise InvalidWebhookUrl("private or loopback webhook addresses are not allowed here")
    return url


def classify(status_code: int | None, exc: BaseException | None) -> DeliveryResult:
    """Retry only what can succeed later; never retry a request the receiver
    rejected as invalid (4xx) or a redirect (refused: destinations are fixed)."""
    if exc is not None:
        retryable = isinstance(exc, httpx.TimeoutException | httpx.TransportError)
        return DeliveryResult(False, retryable, None, type(exc).__name__)
    assert status_code is not None
    if 200 <= status_code < 300:
        return DeliveryResult(True, False, status_code)
    if 300 <= status_code < 400:
        return DeliveryResult(False, False, status_code, "redirect_refused")
    if status_code in (408, 425, 429) or status_code >= 500:
        return DeliveryResult(False, True, status_code, f"http_{status_code}")
    return DeliveryResult(False, False, status_code, f"http_{status_code}")


def signature(secret: SecretStr, timestamp: str, body: bytes) -> str:
    mac = hmac.new(
        secret.get_secret_value().encode(), timestamp.encode() + b"." + body, hashlib.sha256
    )
    return "sha256=" + mac.hexdigest()


def envelope(message: Message) -> dict[str, Any]:
    return {
        "id": str(message.event_id),
        "idempotency_key": str(message.event_id),
        "type": message.event_type,
        "severity": message.severity,
        "created_at": message.created_at.isoformat(),
        "source": "sentinelops",
        "payload": message.payload,
    }


class LogChannel:
    name = "log"

    def send(self, message: Message) -> DeliveryResult:
        log.warning(
            "NOTIFICATION %s: %s",
            message.event_type,
            redact_text(str(message.payload.get("title", "")))[:200],
            extra={"notification": redact(envelope(message))},
        )
        return DeliveryResult(True, False)


class WebhookChannel:
    name = "webhook"

    def __init__(
        self,
        url: str,
        secret: SecretStr | None,
        *,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 65_536,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.secret = secret
        self.max_response_bytes = max_response_bytes
        self.client = client or httpx.Client(
            timeout=timeout_seconds, follow_redirects=False, trust_env=False
        )

    def send(self, message: Message) -> DeliveryResult:
        body = json.dumps(envelope(message), sort_keys=True, default=str).encode()
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "SentinelOps-Notifier/1",
            "Idempotency-Key": str(message.event_id),
            "X-SentinelOps-Event": message.event_type,
            "X-SentinelOps-Timestamp": ts,
        }
        if self.secret is not None:
            headers["X-SentinelOps-Signature"] = signature(self.secret, ts, body)
        try:
            with self.client.stream("POST", self.url, content=body, headers=headers) as resp:
                read = 0
                for chunk in resp.iter_bytes():
                    read += len(chunk)
                    if read >= self.max_response_bytes:
                        break  # bounded: we never need the body
                return classify(resp.status_code, None)
        except httpx.HTTPError as exc:
            return classify(None, exc)
