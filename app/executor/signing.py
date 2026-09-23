"""Short-lived, action-bound execution authorization (HMAC-SHA256).

Shared by the worker (signs) and the executor (verifies). Binds the action id,
the worker's fencing token, the action fingerprint and an expiry, so a captured
request cannot be replayed later or re-targeted to another action."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime

from pydantic import SecretStr

ACTION = "restart_demo_app"


def payload(
    action_id: uuid.UUID, fencing_token: int, fingerprint: str, not_after: datetime
) -> bytes:
    return "|".join(
        [ACTION, str(action_id), str(fencing_token), fingerprint, not_after.isoformat()]
    ).encode()


def sign(
    key: SecretStr, action_id: uuid.UUID, fencing_token: int, fingerprint: str, not_after: datetime
) -> str:
    return hmac.new(
        key.get_secret_value().encode(),
        payload(action_id, fencing_token, fingerprint, not_after),
        hashlib.sha256,
    ).hexdigest()


def verify(
    key: SecretStr,
    action_id: uuid.UUID,
    fencing_token: int,
    fingerprint: str,
    not_after: datetime,
    signature: str,
) -> bool:
    return hmac.compare_digest(
        sign(key, action_id, fencing_token, fingerprint, not_after), signature
    )
