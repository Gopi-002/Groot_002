"""Append-only audit trail helper."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import Connection, text


def audit(
    conn: Connection,
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_events (actor_type, actor_id, action, entity_type, entity_id, "
            "details) VALUES (:at, :ai, :a, :et, :eid, CAST(:d AS jsonb))"
        ),
        {
            "at": actor_type,
            "ai": actor_id,
            "a": action,
            "et": entity_type,
            "eid": entity_id,
            "d": json.dumps(details or {}, default=str),
        },
    )
