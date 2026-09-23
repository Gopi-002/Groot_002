"""Phase 6 fault-matrix gap tests (real PostgreSQL, real executor app over a
simulated Docker API): crash during policy evaluation, a stale worker trying to
overwrite a verification, and a database error during report generation."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.agent import tasks
from app.reporting import jobs
from app.reporting.consumer import ReportConsumer
from app.reporting.stage import ReportStage
from app.safety import remediation as remediation_mod
from tests.integration.helpers import expire_lease, one, rows
from tests.integration.test_remediation import (  # noqa: F401 - fixtures
    Crash,
    investigated,
    isolate,
    lease_for,
    ops,
    s,
    stage,
    state,
    world,
)
from tests.integration.test_reporting import job_of, mock_factory, remediated

pytestmark = pytest.mark.integration


def test_crash_during_policy_evaluation_leaves_no_side_effect_and_recovers(
    engine, s, world, monkeypatch
):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)
    real = remediation_mod.evaluate
    calls = {"n": 0}

    def dies_once(inp, cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Crash("worker died inside the policy engine")
        return real(inp, cfg)

    monkeypatch.setattr(remediation_mod, "evaluate", dies_once)
    with pytest.raises(Crash):
        stage(s, client).run(engine, lease_for(engine, tid, "a"))
    # nothing durable happened: no decision, no reservation, no restart
    assert one(engine, "SELECT count(*) FROM policy_decisions WHERE task_id=:t", t=tid) == 0
    assert state(engine, tid, iid)["attempt"] is None and docker.restarts == 0
    expire_lease(engine, tid)
    res = stage(s, client).run(engine, tasks.claim(engine, tid, "b", 60))
    assert res.outcome == "recovery_verified" and docker.restarts == 1


def test_stale_worker_cannot_overwrite_verification(engine, s, world):
    docker, client, _ = world
    iid, tid, _ = investigated(engine, s)
    stale = lease_for(engine, tid, "old")
    expire_lease(engine, tid)
    current = tasks.claim(engine, tid, "new", 60)
    assert stage(s, client).run(engine, current).outcome == "recovery_verified"
    before = rows(
        engine, "SELECT id, status, reason FROM verifications WHERE incident_id=:i", i=iid
    )
    # the stale holder re-runs verification with a FAILING probe: it may observe, but
    # nothing it writes can land (fenced checkpoint in the same transaction)
    with pytest.raises(tasks.LeaseLost):
        stage(s, client, ops(probe_ok=False)).run(engine, stale)
    after = rows(engine, "SELECT id, status, reason FROM verifications WHERE incident_id=:i", i=iid)
    assert after == before and before[0]["status"] == "passed"
    assert docker.restarts == 1
    assert one(engine, "SELECT status FROM incidents WHERE id=:i", i=iid) == "resolved"


def test_database_error_during_reporting_retries_to_one_report(engine, s, world, rclient, settings):
    iid, _ = remediated(engine, s, world)
    cfg = settings.model_copy(
        update={"ai_gateway": "mock", "task_retry_base_seconds": 1, "task_retry_max_seconds": 1}
    )

    class DbBlip(ReportStage):
        failed = False

        def run(self, engine_, lease):
            if not DbBlip.failed:
                DbBlip.failed = True
                raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
            return super().run(engine_, lease)

    consumer = ReportConsumer(cfg, engine, rclient, DbBlip(cfg, mock_factory), "c")
    job_id = job_of(engine, iid)
    assert consumer.handle("1-0", {"task_id": str(job_id)})  # retried, message ACKed
    job = rows(engine, "SELECT status, attempt, last_error FROM report_jobs WHERE id=:j", j=job_id)[
        0
    ]
    assert job["status"] == "pending" and "OperationalError" in job["last_error"]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE report_jobs SET next_attempt_at=now() WHERE id=:j"), {"j": job_id}
        )
    assert consumer.handle("2-0", {"task_id": str(job_id)})
    assert one(engine, "SELECT status FROM report_jobs WHERE id=:j", j=job_id) == "validated"
    assert one(engine, "SELECT count(*) FROM reports WHERE incident_id=:i", i=iid) == 1
    assert jobs.claim(engine, job_id, "late", 60) is None  # done: duplicates are refused
