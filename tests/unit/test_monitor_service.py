import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.monitoring import service as svc_mod
from app.monitoring.probe import CheckResult
from app.monitoring.recorder import RecordOutcome
from app.monitoring.service import Leadership, MonitorService
from app.runtime import StopFlag

T0 = datetime(2026, 9, 22, tzinfo=UTC)


def result(i: int, outcome: str = "unhealthy") -> CheckResult:
    return CheckResult(T0 + timedelta(seconds=30 * i), outcome, 500, 5.0, None)


def make(recorder) -> MonitorService:
    s = MonitorService(
        Settings(),
        engine=None,
        probe=None,
        stop=StopFlag(),  # type: ignore[arg-type]
        recorder=recorder,
    )
    s.leadership = Leadership.LEADER
    s._service_id = uuid.uuid4()
    return s


def test_buffer_is_bounded_and_drops_oldest(base_env, monkeypatch):
    monkeypatch.setattr(svc_mod, "BUFFER_MAX", 3)
    s = make(lambda *a, **k: RecordOutcome(uuid.uuid4()))
    for i in range(5):
        s.enqueue(result(i))
    assert [r.checked_at for r in s.buffer] == [result(i).checked_at for i in (2, 3, 4)]
    assert s.counters["dropped"] == 2


def test_drain_keeps_order_and_retains_checks_on_db_failure(base_env):
    recorded: list[datetime] = []
    down = {"on": True}

    def recorder(engine, sid, res, th, **kw):
        if down["on"]:
            raise OperationalError("SELECT 1", {}, Exception("db down"))
        recorded.append(res.checked_at)
        return RecordOutcome(uuid.uuid4())

    s = make(recorder)
    for i in range(3):
        s.enqueue(result(i))
    assert s.drain() == 0 and len(s.buffer) == 3  # nothing lost while DB is down
    assert s.counters["record_failures"] == 1
    down["on"] = False
    assert s.drain() == 3 and not s.buffer
    assert recorded == [result(i).checked_at for i in range(3)]  # original order and timestamps


def test_non_leader_does_not_record(base_env):
    calls = []
    s = make(lambda *a, **k: calls.append(1) or RecordOutcome(uuid.uuid4()))
    s.leadership = Leadership.PRESUMED
    s.enqueue(result(0))
    assert s.drain() == 0 and calls == [] and len(s.buffer) == 1
