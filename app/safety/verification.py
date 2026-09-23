"""Deterministic post-action recovery verification (workflow step 8).

Recovery is established ONLY by fresh evidence, never by a model's opinion:
  1. within ``deadline`` the target must answer ``consecutive`` fresh health
     probes in a row, each successful and faster than ``latency_max``;
  2. no new log lines at a critical level since the restarted process started.
Probes run on a bounded interval until the condition holds or the deadline
passes (no fixed sleeps). Missing evidence fails closed.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import Settings


@dataclass(frozen=True)
class Criteria:
    readiness_deadline_seconds: float
    consecutive_successes: int
    latency_max_seconds: float
    probe_interval_seconds: float
    error_levels: tuple[str, ...]

    @classmethod
    def from_settings(cls, s: Settings) -> Criteria:
        return cls(
            s.verify_readiness_deadline_seconds,
            s.verify_consecutive_successes,
            s.verify_latency_max_seconds,
            s.verify_probe_interval_seconds,
            tuple(level.upper() for level in s.verify_error_levels),
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Outcome:
    passed: bool
    reason: str
    observations: list[dict[str, Any]] = field(default_factory=list)


ProbeFn = Callable[[], dict[str, Any] | None]
LogsFn = Callable[[], list[dict[str, Any]] | None]


def parse_docker_time(value: object) -> datetime | None:
    """Docker timestamps carry nanoseconds ('...10.331038746Z'); Python accepts
    at most microseconds. Returns an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$", value)
    if m is None:
        return None
    frac = (m.group(2) or "")[:7]  # '.' + up to 6 digits
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(m.group(1) + frac + tz).astimezone(UTC)


def line_level(line: str) -> str:
    try:
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            return str(parsed.get("level", "INFO")).upper()
    except ValueError:
        pass
    upper = line.upper()
    if "CRITICAL" in upper:
        return "CRITICAL"
    return "ERROR" if ("ERROR" in upper or "TRACEBACK" in upper) else "INFO"


def verify(
    probe: ProbeFn,
    new_logs: LogsFn,
    criteria: Criteria,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> Outcome:
    deadline = clock() + criteria.readiness_deadline_seconds
    streak = 0
    obs: list[dict[str, Any]] = []
    while True:
        p = probe()
        ok = bool(
            p
            and p.get("ok")
            and p.get("latency_ms") is not None
            and float(p["latency_ms"]) < criteria.latency_max_seconds * 1000
        )
        streak = streak + 1 if ok else 0
        obs.append({"type": "probe", "ok": ok, "probe": p, "streak": streak})
        if streak >= criteria.consecutive_successes:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            return Outcome(
                False,
                f"readiness deadline {criteria.readiness_deadline_seconds}s "
                f"exceeded without {criteria.consecutive_successes} "
                "consecutive healthy, fast probes",
                obs,
            )
        wait(min(criteria.probe_interval_seconds, remaining))
    lines = new_logs()
    if lines is None:
        return Outcome(False, "error evidence unavailable; cannot confirm recovery", obs)
    critical = [ln for ln in lines if line_level(str(ln.get("line", ""))) in criteria.error_levels]
    obs.append({"type": "log_scan", "lines_scanned": len(lines), "critical_lines": critical[:20]})
    if critical:
        return Outcome(False, f"{len(critical)} new critical error line(s) after restart", obs)
    return Outcome(
        True,
        f"{criteria.consecutive_successes} consecutive healthy probes under "
        f"{criteria.latency_max_seconds}s and no new critical errors",
        obs,
    )
