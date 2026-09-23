"""Pipeline stages executed by the worker under a fenced lease.

Phase 2 implements only the deterministic intake stage (end of workflow
step 3). With no investigator stage yet, an accepted task rests in
``awaiting_investigation`` (still an *active* task, incident stays open).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Engine, text

from app.agent.tasks import Lease, checkpoint, transition

ACTIVE_INCIDENT = ("open", "investigating", "remediating", "waiting_approval", "escalated")


class TransientError(Exception):
    """Retry with backoff."""


class PermanentError(Exception):
    """Do not retry: task FAILED, incident escalated."""


@dataclass(frozen=True)
class StageResult:
    status: str
    outcome: str


class Stage(Protocol):
    def run(self, engine: Engine, lease: Lease) -> StageResult: ...


class IntakeStage:
    """Validate the incident, checkpoint step 3, hand off to the next stage."""

    def run(self, engine: Engine, lease: Lease) -> StageResult:
        with engine.begin() as conn:
            # Lock order incident -> task (same as the monitor's auto-resolve).
            inc = conn.execute(
                text("SELECT status FROM incidents WHERE id=:i FOR SHARE"),
                {"i": lease.incident_id},
            ).scalar_one_or_none()
            if inc is None:
                raise PermanentError("incident not found")
            if inc not in ACTIVE_INCIDENT:
                result = StageResult("resolved", "incident_no_longer_active")
                transition(conn, lease, result.status, outcome=result.outcome)
                return result
            evidence = conn.execute(
                text("SELECT count(*) FROM evidence WHERE incident_id=:i"),
                {"i": lease.incident_id},
            ).scalar_one()
            checkpoint(
                conn,
                lease,
                3,
                "intake_complete",
                {"incident_status": inc, "evidence_records": evidence, "attempt": lease.attempt},
            )
            result = StageResult("awaiting_investigation", "intake_complete")
            transition(conn, lease, result.status, outcome=result.outcome)
        return result
