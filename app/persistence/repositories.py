"""Minimal Phase 1 data access: services and incidents (SQLAlchemy Core)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class Incident:
    id: uuid.UUID
    service_id: uuid.UUID
    incident_type: str
    status: str
    severity: str
    summary: str
    occurrence_count: int
    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


def upsert_service(conn: Connection, name: str, base_url: str) -> uuid.UUID:
    row = conn.execute(
        text(
            "INSERT INTO services (name, base_url) VALUES (:n, :u) "
            "ON CONFLICT (name) DO UPDATE SET base_url = EXCLUDED.base_url RETURNING id"
        ),
        {"n": name, "u": base_url},
    ).scalar_one()
    return uuid.UUID(str(row))


def create_incident(
    conn: Connection,
    service_id: uuid.UUID,
    incident_type: str,
    *,
    severity: str = "medium",
    summary: str = "",
) -> uuid.UUID:
    row = conn.execute(
        text(
            "INSERT INTO incidents (service_id, incident_type, severity, summary) "
            "VALUES (:s, :t, :sev, :sum) RETURNING id"
        ),
        {"s": service_id, "t": incident_type, "sev": severity, "sum": summary},
    ).scalar_one()
    return uuid.UUID(str(row))


def get_incident(conn: Connection, incident_id: uuid.UUID) -> Incident | None:
    row = (
        conn.execute(
            text(
                "SELECT id, service_id, incident_type, status, severity, summary, "
                "occurrence_count, opened_at, last_seen_at, resolved_at "
                "FROM incidents WHERE id = :id"
            ),
            {"id": incident_id},
        )
        .mappings()
        .first()
    )
    return Incident(**row) if row else None
