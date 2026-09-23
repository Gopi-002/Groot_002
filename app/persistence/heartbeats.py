"""Component heartbeats in PostgreSQL (service_heartbeats): feed the degraded-mode
status API and the service_down / executor_unreachable alerts. Container health
checks (heartbeat files) stay the liveness mechanism; this is for operators."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text


def record_heartbeat(
    engine: Engine,
    service: str,
    instance: str,
    started_at: datetime,
    *,
    status: str = "ok",
    details: dict[str, Any] | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO service_heartbeats (service, instance, started_at, last_seen_at, "
                "status, details) VALUES (:s, :i, :st, now(), :status, CAST(:d AS jsonb)) "
                "ON CONFLICT (service, instance) DO UPDATE SET last_seen_at=now(), "
                "status=EXCLUDED.status, details=EXCLUDED.details"
            ),
            {
                "s": service,
                "i": instance[:120],
                "st": started_at,
                "status": status,
                "d": json.dumps(details or {}, default=str),
            },
        )
        # bounded table: forget instances not seen for a week (restarts create new ones)
        conn.execute(
            text(
                "DELETE FROM service_heartbeats WHERE service=:s "
                "AND last_seen_at < now() - interval '7 days'"
            ),
            {"s": service},
        )
