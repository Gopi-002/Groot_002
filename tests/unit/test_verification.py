import uuid
from datetime import UTC, datetime

from pydantic import SecretStr

from app.safety import approvals
from app.safety.verification import Criteria, line_level, parse_docker_time, verify

C = Criteria(
    readiness_deadline_seconds=10,
    consecutive_successes=3,
    latency_max_seconds=2.0,
    probe_interval_seconds=1,
    error_levels=("ERROR", "CRITICAL"),
)


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.waits = []

    def clock(self):
        return self.t

    def wait(self, s):
        self.waits.append(s)
        self.t += s


def probes(*seq):
    it = iter(seq)

    def probe():
        v = next(it, seq[-1])
        return None if v is None else {"ok": v[0], "latency_ms": v[1]}

    return probe


GOOD = (True, 50.0)


def run(probe, logs=lambda: []):
    ft = FakeTime()
    return verify(probe, logs, C, clock=ft.clock, wait=ft.wait), ft


def test_three_consecutive_fast_healthy_probes_pass():
    out, ft = run(probes(GOOD, GOOD, GOOD))
    assert out.passed and len([o for o in out.observations if o["type"] == "probe"]) == 3
    assert ft.waits == [1, 1]


def test_streak_resets_on_failure():
    out, _ = run(probes(GOOD, GOOD, (False, 10.0), GOOD, GOOD, GOOD))
    assert out.passed and [o["streak"] for o in out.observations[:6]] == [1, 2, 0, 1, 2, 3]


def test_slow_responses_do_not_count():
    out, _ = run(probes((True, 2500.0)))
    assert not out.passed and "deadline" in out.reason


def test_deadline_bounded_never_sleeps_past_it():
    out, ft = run(probes((False, None)))
    assert not out.passed and ft.t <= C.readiness_deadline_seconds + 1e-9


def test_probe_unavailable_counts_as_failure():
    out, _ = run(probes(None))
    assert not out.passed


def test_new_critical_errors_fail_verification():
    def logs():
        return [
            {"line": '{"level": "ERROR", "msg": "simulated internal error"}'},
            {"line": '{"level": "INFO", "msg": "ok"}'},
        ]

    out, _ = run(probes(GOOD), logs)
    assert not out.passed and "1 new critical" in out.reason


def test_missing_error_evidence_fails_closed():
    out, _ = run(probes(GOOD), lambda: None)
    assert not out.passed and "unavailable" in out.reason


def test_line_level_and_docker_time():
    assert line_level('{"level": "critical"}') == "CRITICAL"
    assert line_level("Traceback (most recent call last)") == "ERROR"
    assert line_level("plain info") == "INFO"
    assert parse_docker_time("2026-09-22T22:25:10.331038746Z") == datetime(
        2026, 9, 22, 22, 25, 10, 331038, tzinfo=UTC
    )
    assert parse_docker_time("0001-01-01T00:00:00Z") is None


def test_approval_signature_binds_every_field():
    key = SecretStr("s" * 40)
    aid, at = uuid.uuid4(), datetime.now(UTC)
    sig = approvals.sign_decision(key, aid, "f" * 64, "approved", "alice", at)
    assert approvals.verify_decision(key, aid, "f" * 64, "approved", "alice", at, sig)
    for args in [
        (uuid.uuid4(), "f" * 64, "approved", "alice", at),
        (aid, "e" * 64, "approved", "alice", at),
        (aid, "f" * 64, "rejected", "alice", at),
        (aid, "f" * 64, "approved", "mallory", at),
    ]:
        assert not approvals.verify_decision(key, *args, sig)
    assert not approvals.verify_decision(
        SecretStr("x" * 40), aid, "f" * 64, "approved", "alice", at, sig
    )
    assert not approvals.verify_decision(None, aid, "f" * 64, "approved", "alice", at, sig)
