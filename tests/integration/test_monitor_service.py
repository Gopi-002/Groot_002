"""MonitorService leadership, probing cadence and DB-outage buffering."""

import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.monitoring.probe import CheckResult
from app.monitoring.recorder import record_check
from app.monitoring.service import Leadership, MonitorService
from app.runtime import StopFlag
from tests.integration.helpers import one

pytestmark = pytest.mark.integration


class FakeProbe:
    def __init__(self, outcome="unhealthy", delay=0.0):
        self.outcome, self.delay = outcome, delay
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self._lock = threading.Lock()

    def check(self):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls += 1
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        code = 500 if self.outcome == "unhealthy" else 200
        return CheckResult(datetime.now(UTC), self.outcome, code, 3.0, None)


@pytest.fixture
def fast(base_env):
    import uuid

    return Settings(
        monitor_interval_seconds=1,
        probe_timeout_seconds=0.5,
        latency_threshold_seconds=0.4,
        demo_service_name=f"mon-{uuid.uuid4().hex[:8]}",
    )


def test_single_leader_and_standby_takeover(engine, fast):
    a = MonitorService(fast, engine, FakeProbe(), StopFlag())
    b = MonitorService(fast, engine, FakeProbe(), StopFlag())
    assert a.refresh_leadership() is Leadership.LEADER
    assert b.refresh_leadership() is Leadership.STANDBY
    a._release()  # leader dies
    assert b.refresh_leadership() is Leadership.LEADER
    b._release()


def test_presumed_leader_keeps_probing_when_db_unreachable(engine, fast):
    s = MonitorService(fast, engine, FakeProbe(), StopFlag())
    assert s.refresh_leadership() is Leadership.LEADER
    s._lock_conn.invalidate()  # connection to the DB broke
    s.engine = create_engine(
        "postgresql+psycopg://x:y@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    assert s.refresh_leadership() is Leadership.PRESUMED
    s.probe_once()
    assert len(s.buffer) == 1  # still probing, buffered for later recording


def test_checks_during_db_outage_are_recorded_later_and_detected(engine, fast):
    outage = {"on": True}

    def flaky_record(*args, **kwargs):
        if outage["on"]:
            raise OperationalError("INSERT", {}, Exception("db down"))
        return record_check(*args, **kwargs)

    s = MonitorService(fast, engine, FakeProbe("unhealthy"), StopFlag(), recorder=flaky_record)
    s.refresh_leadership()
    for _ in range(3):
        s.probe_once()
    assert s.drain() == 0 and len(s.buffer) == 3
    outage["on"] = False
    assert s.drain() == 3
    sid = s._service_id
    assert one(engine, "SELECT count(*) FROM health_checks WHERE service_id=:s", s=sid) == 3
    assert one(engine, "SELECT count(*) FROM incidents WHERE service_id=:s", s=sid) == 1
    s._release()


def test_run_loop_never_overlaps_and_skips_missed_ticks(engine, fast):
    probe = FakeProbe("healthy", delay=1.6)  # longer than the 1 s interval
    stop = StopFlag()
    s = MonitorService(fast, engine, probe, stop)
    t = threading.Thread(target=s.run)
    t.start()
    time.sleep(6)
    stop.set()
    t.join(timeout=20)
    assert not t.is_alive()
    assert probe.max_active == 1 and probe.calls >= 2
    assert s.counters["skipped_ticks"] >= 1
    assert (
        one(
            engine,
            "SELECT count(*) FROM health_checks h JOIN services v "
            "ON v.id=h.service_id WHERE v.name=:n",
            n=fast.demo_service_name,
        )
        == probe.calls
    )


def test_lock_released_on_stop(engine, fast):
    stop = StopFlag()
    s = MonitorService(fast, engine, FakeProbe("healthy"), stop)
    t = threading.Thread(target=s.run)
    t.start()
    time.sleep(2)

    def held() -> int:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted "
                    "AND classid=:hi AND objid=:lo AND objsubid=1"
                ),
                {"hi": s.lock_key >> 32, "lo": s.lock_key & 0xFFFFFFFF},
            ).scalar()

    assert held() == 1  # held while running
    stop.set()
    t.join(timeout=20)
    assert held() == 0  # released (connection closed, not pooled) on stop
