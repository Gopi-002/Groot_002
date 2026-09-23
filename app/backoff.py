"""Bounded exponential backoff: a deterministic base schedule (predictable for
tests and audits) plus a bounded-jitter variant used by retry loops."""

from __future__ import annotations

import random


def backoff_seconds(attempt: int, base: float, maximum: float) -> float:
    """attempt is 1-based: 1 -> base, 2 -> 2*base, ... capped at maximum."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return float(min(maximum, base * (2 ** min(attempt - 1, 32))))


def jittered_backoff(attempt: int, base: float, maximum: float, rng: random.Random) -> float:
    """Bounded exponential backoff with jitter: uniformly within [80%, 100%] of
    ``backoff_seconds`` - de-synchronises retry storms while never exceeding the
    documented bound and never retrying sooner than 80% of the base schedule."""
    delay = backoff_seconds(attempt, base, maximum)
    return round(delay * (0.8 + 0.2 * rng.random()), 3)
