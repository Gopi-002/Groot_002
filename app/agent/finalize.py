"""Workflow step 10 hooks, run INSIDE the transaction that makes a task terminal:
request the incident report (step 9, durable job) and enqueue the outcome
notification. Both are plain inserts into PostgreSQL (outbox pattern), so they
commit atomically with the transition and can never block or roll it back."""

from __future__ import annotations

import uuid

from sqlalchemy import Connection

from app.notifications.events import notify_task_terminal
from app.reporting.jobs import request_report


def on_task_terminal(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    incident_id: uuid.UUID,
    status: str,
    outcome: str | None,
    actor: str,
) -> None:
    request_report(
        conn,
        incident_id=incident_id,
        task_id=task_id,
        reason=f"task_{status}",
        actor=actor,
    )
    notify_task_terminal(
        conn, task_id=task_id, incident_id=incident_id, status=status, outcome=outcome, actor=actor
    )
