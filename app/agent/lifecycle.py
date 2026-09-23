"""Canonical incident-task lifecycle (05-RELIABILITY-REPORTING task 2).

The durable source of truth stays the existing columns (``tasks.status``,
action attempts, verifications, report jobs). This module defines the
contract's named lifecycle states, the ONLY legal edges between them, and a
pure function that derives the current state from durable records - so the
lifecycle can never drift from what PostgreSQL says.

Guarding happens in two places:
* ``trg_tasks_guard_status`` (migration 0005) enforces the legal ``tasks.status``
  edges in the database (terminal statuses are final);
* every worker transition is fenced on (owner, fencing token) (``tasks.transition``).

An ESCALATED task is terminal while its incident remains open for a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Lifecycle(StrEnum):
    DETECTED = "DETECTED"
    QUEUED = "QUEUED"
    INVESTIGATING = "INVESTIGATING"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    POLICY_CHECK = "POLICY_CHECK"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    REPORTING = "REPORTING"
    RESOLVED = "RESOLVED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


L = Lifecycle
TERMINAL = frozenset({L.RESOLVED, L.ESCALATED, L.FAILED})
_WORK = (L.INVESTIGATING, L.POLICY_CHECK, L.EXECUTING, L.VERIFYING)

# Legal edges. Every working state may fail into RETRY_SCHEDULED (bounded retries),
# ESCALATED (dead letter / human needed) or FAILED (permanent error); RETRY_SCHEDULED
# returns to whichever stage the durable state routes to.
TRANSITIONS: dict[Lifecycle, frozenset[Lifecycle]] = {
    L.DETECTED: frozenset({L.QUEUED}),
    L.QUEUED: frozenset({L.INVESTIGATING, L.RETRY_SCHEDULED, L.REPORTING, L.FAILED}),
    L.INVESTIGATING: frozenset(
        {L.ACTION_PROPOSED, L.QUEUED, L.RETRY_SCHEDULED, L.REPORTING, L.FAILED}
    ),
    L.ACTION_PROPOSED: frozenset({L.POLICY_CHECK, L.REPORTING, L.FAILED}),
    L.POLICY_CHECK: frozenset(
        {L.WAITING_APPROVAL, L.EXECUTING, L.RETRY_SCHEDULED, L.REPORTING, L.FAILED}
    ),
    L.WAITING_APPROVAL: frozenset({L.POLICY_CHECK, L.REPORTING, L.FAILED}),
    L.EXECUTING: frozenset({L.VERIFYING, L.RETRY_SCHEDULED, L.REPORTING, L.FAILED}),
    L.VERIFYING: frozenset({L.REPORTING, L.RETRY_SCHEDULED, L.FAILED}),
    L.RETRY_SCHEDULED: frozenset({*_WORK, L.REPORTING, L.FAILED}),
    # Reporting never changes WHAT happened; it ends in the durable outcome.
    L.REPORTING: frozenset({L.RESOLVED, L.ESCALATED, L.FAILED}),
    L.RESOLVED: frozenset(),
    L.ESCALATED: frozenset(),
    L.FAILED: frozenset(),
}


class IllegalTransition(ValueError):
    pass


def check_transition(current: Lifecycle, new: Lifecycle) -> None:
    if new is current:
        return
    if new not in TRANSITIONS[current]:
        raise IllegalTransition(f"{current} -> {new} is not a legal lifecycle transition")


@dataclass(frozen=True)
class DurableView:
    """The minimal durable facts the lifecycle is derived from."""

    task_status: str | None  # None: incident exists, no task yet
    task_outcome: str | None = None
    investigation_completed: bool = False
    last_checkpoint_step: int | None = None
    attempt_status: str | None = None  # action_attempts.status
    verification_status: str | None = None
    report_job_status: str | None = None  # latest report job


_TERMINAL_TASK = {
    "resolved": L.RESOLVED,
    "escalated": L.ESCALATED,
    "dead_lettered": L.ESCALATED,
    "failed": L.FAILED,
}


def derive(v: DurableView) -> Lifecycle:
    s = v.task_status
    if s is None:
        return L.DETECTED
    if s in _TERMINAL_TASK:
        if v.report_job_status in ("pending", "generating"):
            return L.REPORTING
        return _TERMINAL_TASK[s]
    if s in ("queued", "awaiting_investigation"):
        return L.QUEUED
    if s == "retry_scheduled":
        return L.RETRY_SCHEDULED
    if s == "awaiting_policy":
        return L.ACTION_PROPOSED
    if s == "waiting_approval":
        return L.WAITING_APPROVAL
    # running: route by durable progress (same routing as the worker pipeline)
    if not v.investigation_completed:
        return L.INVESTIGATING
    if v.attempt_status in ("succeeded", "reconciled") and v.verification_status is None:
        return L.VERIFYING
    if v.attempt_status in ("pending", "executing"):
        return L.EXECUTING
    return L.POLICY_CHECK
