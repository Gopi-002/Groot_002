"""Structured JSON logging with secret redaction.

Redaction is applied to both structured fields (by key name) and to free-text
messages (by pattern), because log content may include untrusted input.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|credential|dsn|private[_-]?key"
    r"|signature|signing[_-]?key)",
    re.IGNORECASE,
)
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL userinfo: scheme://user:pass@host
    (re.compile(r"(?P<p>[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+(?=@)", re.I), rf"\g<p>{REDACTED}"),
    # URL with an empty user and a password: redis://:pass@host
    (re.compile(r"(?P<p>[a-z][a-z0-9+.-]*://:)[^@\s]+(?=@)", re.I), rf"\g<p>{REDACTED}"),
    # Anthropic-style keys, SentinelOps operator tokens, webhook signatures
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), REDACTED),
    (re.compile(r"\bsop_[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bsha256=[0-9a-f]{64}\b"), REDACTED),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]+"), rf"\g<1>{REDACTED}"),
    # key=value / key: value for sensitive key names
    (
        re.compile(
            r"(?i)\b((?:[a-z_]*)(?:password|secret|token|api[_-]?key|signing[_-]?key|signature)"
            r"[a-z_]*)"
            r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
        ),
        rf"\g<1>\g<2>{REDACTED}",
    ),
)

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

# Correlation ids (incident_id, task_id, report_job_id, notification_id, request_id,
# ...) attached to every log line emitted inside a ``log_context`` block.
_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "sentinel_log_context", default=None
)


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    merged = {**(_CONTEXT.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_CONTEXT.get() or {})


def redact_text(text: str) -> str:
    for pattern, repl in _TEXT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    if hasattr(value, "get_secret_value"):
        return REDACTED
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_CONTEXT.get() or {})
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
