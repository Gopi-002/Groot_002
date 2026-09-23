"""Deterministic detection state machine (workflow step 2).

Per service + failure type:
* A check "counts" toward a type if ``CheckResult.failure_type`` equals it.
  Any other check breaks that type's consecutive streak.
* When ARMED and the streak reaches the type's threshold -> OPEN (disarm).
* While DISARMED, further matching checks -> BUMP the active incident
  (occurrence/last_seen) but never open a duplicate.
* Hysteresis: only after ``rearm_healthy_checks`` consecutive *healthy* checks
  (degraded does not count) does a disarmed type re-ARM, which also allows the
  monitor to auto-resolve an incident nobody has started working on.

This module is pure (no I/O) so every rule is unit-testable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from app.monitoring.probe import CheckResult

DETECTED_TYPES = ("unavailable", "http_error", "high_latency")


@dataclass(frozen=True)
class Thresholds:
    failure_threshold: int = 3
    latency_threshold_count: int = 3
    rearm_healthy_checks: int = 3

    def for_type(self, failure_type: str) -> int:
        return (
            self.latency_threshold_count
            if failure_type == "high_latency"
            else self.failure_threshold
        )


@dataclass(frozen=True)
class TypeState:
    failure_type: str
    armed: bool = True
    consecutive_count: int = 0
    healthy_streak: int = 0
    streak_check_ids: tuple[uuid.UUID, ...] = ()
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_check_at: datetime | None = None


class Action(StrEnum):
    OPEN = "open"
    BUMP = "bump"
    REARM = "rearm"
    RECOVERED = "recovered"  # armed and healthy streak satisfied: try auto-resolve


@dataclass(frozen=True)
class Decision:
    action: Action
    failure_type: str
    state: TypeState
    evidence_check_ids: tuple[uuid.UUID, ...] = field(default=())


def reset_stale(state: TypeState) -> TypeState:
    """After a monitoring gap, consecutive evidence is broken: clear streaks but
    keep armed/disarmed so an ongoing incident is not re-opened as a duplicate."""
    return replace(
        state,
        consecutive_count=0,
        healthy_streak=0,
        streak_check_ids=(),
        first_failure_at=None if state.armed else state.first_failure_at,
    )


def step(
    states: dict[str, TypeState],
    check: CheckResult,
    check_id: uuid.UUID,
    thresholds: Thresholds,
) -> tuple[dict[str, TypeState], list[Decision]]:
    new: dict[str, TypeState] = {}
    decisions: list[Decision] = []
    ftype = check.failure_type
    healthy = check.outcome == "healthy"
    for t in DETECTED_TYPES:
        st = replace(states.get(t, TypeState(t)), last_check_at=check.checked_at)
        if ftype == t:
            count = st.consecutive_count + 1
            limit = thresholds.for_type(t)
            st = replace(
                st,
                consecutive_count=count,
                healthy_streak=0,
                streak_check_ids=(*st.streak_check_ids, check_id)[-limit:],
                first_failure_at=check.checked_at if count == 1 else st.first_failure_at,
                last_failure_at=check.checked_at,
            )
            if st.armed and count >= limit:
                st = replace(st, armed=False)
                decisions.append(Decision(Action.OPEN, t, st, st.streak_check_ids))
            elif not st.armed:
                decisions.append(Decision(Action.BUMP, t, st))
        else:
            streak = st.healthy_streak + 1 if healthy else 0
            st = replace(st, consecutive_count=0, streak_check_ids=(), healthy_streak=streak)
            if streak >= thresholds.rearm_healthy_checks:
                if not st.armed:
                    st = replace(st, armed=True, first_failure_at=None)
                    decisions.append(Decision(Action.REARM, t, st))
                decisions.append(Decision(Action.RECOVERED, t, st))
        new[t] = st
    return new, decisions
