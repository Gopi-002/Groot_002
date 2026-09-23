"""HTTP health probe (workflow step 1). Latency is measured with a monotonic
clock; the result timestamp is wall-clock UTC.

The configured timeout is a HARD deadline for the whole probe. httpx's
per-phase timeouts do not cover name resolution (``getaddrinfo``), and Docker's
embedded DNS can stall ~10 s for a stopped container - which made probes overrun
the interval and hid an outage (Phase 6 finding). The request therefore runs on a
small bounded pool and the probe stops waiting at the deadline."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

Clock = Callable[[], float]
WallClock = Callable[[], datetime]


@dataclass(frozen=True)
class CheckResult:
    checked_at: datetime
    outcome: str  # healthy | degraded | unhealthy | timeout | error
    http_status: int | None
    latency_ms: float | None
    error_type: str | None

    @property
    def failure_type(self) -> str | None:
        """Maps a check to the incident type it counts toward (None = healthy)."""
        return {
            "timeout": "unavailable",
            "error": "unavailable",
            "unhealthy": "http_error",
            "degraded": "high_latency",
        }.get(self.outcome)


# Bounded: a stalled resolver can occupy at most these threads; later probes queue
# and still time out at their own deadline (queued work is cancelled).
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HealthProbe:
    def __init__(
        self,
        client: httpx.Client,
        url: str,
        *,
        timeout_seconds: float,
        latency_threshold_seconds: float,
        clock: Clock | None = None,
        wall_clock: WallClock = _utcnow,
    ) -> None:
        import time

        self.client = client
        self.url = url
        self.timeout = httpx.Timeout(timeout_seconds)
        self.deadline_seconds = timeout_seconds
        self.latency_threshold_ms = latency_threshold_seconds * 1000
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock

    def check(self) -> CheckResult:
        at = self.wall_clock()
        start = self.clock()
        future = _POOL.submit(self.client.get, self.url, timeout=self.timeout)
        try:
            resp = future.result(timeout=self.deadline_seconds)
        except FutureTimeout:
            future.cancel()
            ms = (self.clock() - start) * 1000
            return CheckResult(at, "timeout", None, round(ms, 3), "ProbeDeadlineExceeded")
        except httpx.TimeoutException as exc:
            ms = (self.clock() - start) * 1000
            return CheckResult(at, "timeout", None, round(ms, 3), type(exc).__name__)
        except httpx.HTTPError as exc:
            ms = (self.clock() - start) * 1000
            return CheckResult(at, "error", None, round(ms, 3), type(exc).__name__)
        ms = round((self.clock() - start) * 1000, 3)
        if not 200 <= resp.status_code < 300:
            return CheckResult(at, "unhealthy", resp.status_code, ms, None)
        if ms > self.latency_threshold_ms:
            return CheckResult(at, "degraded", resp.status_code, ms, None)
        return CheckResult(at, "healthy", resp.status_code, ms, None)
