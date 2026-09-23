"""Shared helpers for Phase 2 integration tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text

from app.monitoring.detector import Thresholds
from app.monitoring.probe import CheckResult
from app.monitoring.recorder import RecordOutcome, record_check, upsert_service

TH = Thresholds(failure_threshold=3, latency_threshold_count=3, rearm_healthy_checks=3)
STALE = timedelta(seconds=90)


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC) - timedelta(hours=1)

    def next(self) -> datetime:
        self.t += timedelta(seconds=30)
        return self.t


def new_service(engine: Engine) -> uuid.UUID:
    return upsert_service(engine, f"svc-{uuid.uuid4().hex[:8]}", "http://demo-app:8001")


def check(outcome: str, at: datetime) -> CheckResult:
    code = {"healthy": 200, "degraded": 200, "unhealthy": 500}.get(outcome)
    return CheckResult(at, outcome, code, 2500.0 if outcome == "degraded" else 10.0, None)


def feed(
    engine: Engine, service_id: uuid.UUID, outcomes: list[str], clock: Clock, max_attempts: int = 3
) -> list[RecordOutcome]:
    return [
        record_check(
            engine,
            service_id,
            check(o, clock.next()),
            TH,
            stale_after=STALE,
            max_attempts=max_attempts,
        )
        for o in outcomes
    ]


def one(engine: Engine, sql: str, **params: object) -> object:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(engine: Engine, sql: str, **params: object) -> list[dict[str, object]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def open_task(engine: Engine, max_attempts: int = 3) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a real incident + task via the monitor path. Returns (incident, task)."""
    sid = new_service(engine)
    outs = feed(engine, sid, ["unhealthy"] * 3, Clock(), max_attempts)
    return outs[-1].opened_incidents[0], outs[-1].created_tasks[0]


def expire_lease(engine: Engine, task_id: uuid.UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET lease_expires_at = now() - interval '1 second' WHERE id=:t"),
            {"t": task_id},
        )


def make_due(engine: Engine, task_id: uuid.UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET next_attempt_at = now() - interval '1 second' WHERE id=:t"),
            {"t": task_id},
        )
