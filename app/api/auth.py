"""Bearer-token guard for the read-only status API.

Fails closed: if no token is configured the endpoints return 503, never data.
Tokens are compared in constant time and never logged. GET-only endpoints with
header (not cookie) auth are not CSRF-exposed.
"""

from __future__ import annotations

import hmac
import threading
from typing import Annotated

from fastapi import Header, HTTPException, Request

from app.config import Settings

# Per-process counter of rejected API credentials (exported by /v1/metrics).
AUTH_FAILURES: dict[str, int] = {"reader": 0, "operator": 0}
_LOCK = threading.Lock()


def count_auth_failure(kind: str) -> None:
    with _LOCK:
        AUTH_FAILURES[kind] = AUTH_FAILURES.get(kind, 0) + 1


def require_reader(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    settings: Settings = request.app.state.settings
    if settings.api_read_token is None:
        raise HTTPException(status_code=503, detail="status API authentication not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    expected = settings.api_read_token.get_secret_value()
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), expected.encode()):
        count_auth_failure("reader")
        raise HTTPException(
            status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
