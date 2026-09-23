import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.monitoring.detector import Action, Thresholds, TypeState, reset_stale, step
from app.monitoring.probe import CheckResult

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
TH = Thresholds(failure_threshold=3, latency_threshold_count=3, rearm_healthy_checks=3)


def chk(outcome: str, i: int) -> CheckResult:
    status = {"healthy": 200, "degraded": 200, "unhealthy": 500}.get(outcome)
    latency = 2500.0 if outcome == "degraded" else 12.0
    return CheckResult(T0 + timedelta(seconds=30 * i), outcome, status, latency, None)


def run(outcomes: list[str], states=None):
    states = states or {}
    log = []
    for i, o in enumerate(outcomes):
        states, decisions = step(states, chk(o, i), uuid.uuid4(), TH)
        log.append([(d.action, d.failure_type) for d in decisions if d.action != Action.RECOVERED])
    return states, log


def opens(log):
    return [e for step_ in log for e in step_ if e[0] is Action.OPEN]


def test_healthy_checks_create_no_incident():
    _, log = run(["healthy"] * 10)
    assert opens(log) == []


@pytest.mark.parametrize(
    ("outcome", "ftype"),
    [("unhealthy", "http_error"), ("timeout", "unavailable"), ("error", "unavailable")],
)
def test_three_failures_open_exactly_one(outcome, ftype):
    _, log = run([outcome] * 3)
    assert log[0] == [] and log[1] == []
    assert log[2] == [(Action.OPEN, ftype)]


def test_latency_threshold_opens_high_latency():
    _, log = run(["degraded"] * 3)
    assert opens(log) == [(Action.OPEN, "high_latency")]


def test_prolonged_failure_never_duplicates():
    _, log = run(["unhealthy"] * 20)
    assert opens(log) == [(Action.OPEN, "http_error")]
    assert all(s == [(Action.BUMP, "http_error")] for s in log[3:])


def test_non_consecutive_failures_do_not_open():
    _, log = run(["unhealthy", "unhealthy", "healthy", "unhealthy", "unhealthy", "healthy"])
    assert opens(log) == []


def test_mixed_failure_types_counted_per_type():
    _, log = run(["unhealthy", "timeout", "unhealthy", "timeout"])
    assert opens(log) == []


def test_rearm_requires_healthy_streak_then_allows_new_incident():
    states, log = run(["unhealthy"] * 3 + ["healthy", "healthy", "unhealthy"])
    assert states["http_error"].armed is False  # 2 healthy is not enough
    assert opens(log) == [(Action.OPEN, "http_error")]
    states, log = run(["healthy"] * 3, states)
    assert states["http_error"].armed is True
    assert (Action.REARM, "http_error") in log[2]
    states, log = run(["unhealthy"] * 3, states)
    assert opens(log) == [(Action.OPEN, "http_error")]  # a later, separate incident


def test_degraded_checks_do_not_count_as_healthy_for_rearm():
    states, _ = run(["timeout"] * 3 + ["healthy", "degraded", "healthy", "healthy"])
    assert states["unavailable"].armed is False
    states, _ = run(["healthy"], states)
    assert states["unavailable"].armed is True


def test_recovered_emitted_once_streak_met():
    _, decisions = step({}, chk("healthy", 0), uuid.uuid4(), TH)
    assert decisions == []
    states, _ = run(["healthy"] * 2)
    _, decisions = step(states, chk("healthy", 3), uuid.uuid4(), TH)
    assert {d.failure_type for d in decisions if d.action is Action.RECOVERED} == {
        "unavailable",
        "http_error",
        "high_latency",
    }


def test_threshold_evidence_window_and_first_last_failure():
    states = {}
    ids = []
    for i in range(5):
        cid = uuid.uuid4()
        ids.append(cid)
        states, decisions = step(states, chk("unhealthy" if i >= 2 else "healthy", i), cid, TH)
    st = states["http_error"]
    assert st.first_failure_at == T0 + timedelta(seconds=60)
    assert st.last_failure_at == T0 + timedelta(seconds=120)
    assert decisions[0].evidence_check_ids == tuple(ids[2:])


def test_evidence_window_bounded_by_threshold():
    states, _ = run(["unhealthy"] * 10)
    assert len(states["http_error"].streak_check_ids) == 3


def test_reset_stale_keeps_disarmed_state():
    states, _ = run(["unhealthy"] * 3)
    st = reset_stale(states["http_error"])
    assert st.armed is False and st.consecutive_count == 0 and st.first_failure_at is not None
    armed = reset_stale(TypeState("unavailable", consecutive_count=2, first_failure_at=T0))
    assert armed.consecutive_count == 0 and armed.first_failure_at is None


def test_independent_thresholds():
    th = Thresholds(failure_threshold=2, latency_threshold_count=4, rearm_healthy_checks=1)
    states = {}
    events = []
    for i, o in enumerate(["degraded"] * 3 + ["unhealthy"] * 2):
        states, ds = step(states, chk(o, i), uuid.uuid4(), th)
        events += [(d.action, d.failure_type) for d in ds if d.action is Action.OPEN]
    assert events == [(Action.OPEN, "http_error")]
