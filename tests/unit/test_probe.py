import itertools
from datetime import UTC, datetime

import httpx
import pytest

from app.monitoring.probe import HealthProbe

URL = "http://demo-app:8001/health"


def fake_clock(*values: float):
    it = iter(values)
    return lambda: next(it)


def probe(handler, clock_values=(0.0, 0.05)):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HealthProbe(
        client, URL, timeout_seconds=5, latency_threshold_seconds=2, clock=fake_clock(*clock_values)
    )


def test_healthy_fast_response():
    r = probe(lambda req: httpx.Response(200, json={"status": "ok"})).check()
    assert (r.outcome, r.http_status, r.latency_ms, r.failure_type) == ("healthy", 200, 50.0, None)
    assert r.checked_at.tzinfo is UTC


def test_slow_2xx_is_degraded_high_latency():
    r = probe(lambda req: httpx.Response(200), (10.0, 12.5)).check()
    assert (r.outcome, r.latency_ms, r.failure_type) == ("degraded", 2500.0, "high_latency")


def test_exactly_threshold_is_healthy():
    assert probe(lambda req: httpx.Response(200), (0.0, 2.0)).check().outcome == "healthy"


@pytest.mark.parametrize("code", [500, 503, 404, 302])
def test_non_2xx_is_http_error(code):
    r = probe(lambda req: httpx.Response(code)).check()
    assert (r.outcome, r.http_status, r.failure_type) == ("unhealthy", code, "http_error")


def test_timeout_is_unavailable():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    r = probe(handler, (0.0, 5.0)).check()
    assert (r.outcome, r.failure_type, r.error_type, r.http_status) == (
        "timeout",
        "unavailable",
        "ReadTimeout",
        None,
    )


def test_connection_error_is_unavailable():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    r = probe(handler).check()
    assert (r.outcome, r.failure_type, r.error_type) == ("error", "unavailable", "ConnectError")


def test_latency_uses_monotonic_clock_not_wall_clock():
    # wall clock jumps backwards; latency must still come from the monotonic clock
    walls = itertools.cycle([datetime(2030, 1, 1, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC)])
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    p = HealthProbe(
        client,
        URL,
        timeout_seconds=5,
        latency_threshold_seconds=2,
        clock=fake_clock(100.0, 100.1),
        wall_clock=lambda: next(walls),
    )
    assert p.check().latency_ms == pytest.approx(100.0)


def test_probe_sends_timeout():
    seen = {}

    def handler(req):
        seen.update(req.extensions.get("timeout", {}))
        return httpx.Response(200)

    probe(handler).check()
    assert seen["read"] == 5


def test_probe_deadline_bounds_hung_name_resolution_and_slow_io():
    """Phase 6 defect: a stopped container made DNS resolution hang ~10 s, far past
    probe_timeout_seconds (httpx timeouts do not cover getaddrinfo). The probe
    must return within its configured deadline regardless of where it hangs."""
    import time as _time

    def hangs(_req):
        _time.sleep(2.0)  # stands in for a stalled resolver / connect / body
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(hangs))
    p = HealthProbe(client, URL, timeout_seconds=0.3, latency_threshold_seconds=0.2)
    started = _time.monotonic()
    r = p.check()
    elapsed = _time.monotonic() - started
    assert elapsed < 1.0, elapsed
    assert (r.outcome, r.error_type, r.failure_type) == (
        "timeout",
        "ProbeDeadlineExceeded",
        "unavailable",
    )
    assert 250 <= r.latency_ms < 1000
    _time.sleep(2.0)  # let the abandoned request finish while log capture is still open
