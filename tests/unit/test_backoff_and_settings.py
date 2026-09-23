import pytest
from pydantic import ValidationError

from app.backoff import backoff_seconds
from app.config import Settings


def test_backoff_exponential_and_capped():
    assert [backoff_seconds(a, 10, 300) for a in range(1, 8)] == [10, 20, 40, 80, 160, 300, 300]
    assert backoff_seconds(1000, 1, 60) == 60
    with pytest.raises(ValueError):
        backoff_seconds(0, 1, 2)


def test_phase2_defaults(base_env):
    s = Settings()
    assert (s.monitor_interval_seconds, s.probe_timeout_seconds) == (30, 5)
    assert (s.failure_threshold, s.latency_threshold_seconds, s.latency_threshold_count) == (
        3,
        2,
        3,
    )
    assert s.api_read_token is None


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"SENTINEL_PROBE_TIMEOUT_SECONDS": "30"}, "probe_timeout"),
        ({"SENTINEL_LATENCY_THRESHOLD_SECONDS": "6"}, "latency_threshold"),
        ({"SENTINEL_HEARTBEAT_SECONDS": "40"}, "heartbeat"),
        ({"SENTINEL_PENDING_IDLE_SECONDS": "30"}, "pending_idle"),
        ({"SENTINEL_TASK_RETRY_BASE_SECONDS": "900"}, "task_retry_base"),
        ({"SENTINEL_API_READ_TOKEN": "too-short"}, "api_read_token"),
    ],
)
def test_timing_and_token_invariants(base_env, monkeypatch, env, match):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match=match):
        Settings()
