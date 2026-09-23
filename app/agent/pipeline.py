"""Routes a claimed task to the right stage from durable state (no in-memory
workflow): no completed investigation yet -> investigation (steps 4-5);
otherwise -> remediation (steps 6-8)."""

from __future__ import annotations

from sqlalchemy import Engine, text

from app.agent.stages import Stage, StageResult
from app.agent.tasks import Lease


class TaskPipeline:
    def __init__(self, investigation: Stage, remediation: Stage) -> None:
        self.investigation = investigation
        self.remediation = remediation

    def run(self, engine: Engine, lease: Lease) -> StageResult:
        with engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM investigations WHERE task_id=:t"),
                {"t": lease.task_id},
            ).scalar_one_or_none()
        stage = self.remediation if status == "completed" else self.investigation
        return stage.run(engine, lease)
