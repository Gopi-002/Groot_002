"""Monitor process (workflow step 1).

Behaviour:
* Probing is decoupled from recording. The probe loop never touches the
  database; results go into a bounded in-memory buffer drained in order by a
  recorder thread. A PostgreSQL outage therefore never stalls probing, and
  checks taken during the outage are recorded (with their original UTC
  timestamps) once the database returns, so detection still sees them.
  If the buffer fills (``BUFFER_MAX`` checks, ~24 h at 30 s) the oldest are
  dropped and counted.
* Single active monitor: a session-level PostgreSQL advisory lock on a
  dedicated connection, managed by the recorder thread. If that connection
  breaks, the monitor keeps probing as *presumed leader* and re-acquires when
  the DB returns; only if another instance holds the lock does it drop its
  buffer and go to standby. A standby instance does not probe.
* Startup: probing starts once leadership has been acquired; detection state is
  loaded from PostgreSQL, so a restart continues existing streaks unless the
  gap exceeds 3 intervals (then streaks reset, armed/disarmed kept).
* No overlapping checks: one sequential probe loop on a monotonic grid; missed
  ticks are skipped (logged), never run concurrently.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
import zlib
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.backoff import backoff_seconds
from app.config import Settings
from app.monitoring.detector import Thresholds
from app.monitoring.probe import CheckResult, HealthProbe
from app.monitoring.recorder import (
    RecordOutcome,
    prune_health_checks,
    record_check,
    upsert_service,
)
from app.runtime import StopFlag, beat

log = logging.getLogger("sentinelops.monitor")

PRUNE_EVERY_SECONDS = 3600.0
BUFFER_MAX = 2880

Recorder = Callable[..., RecordOutcome]


class Leadership(StrEnum):
    UNKNOWN = "unknown"  # never acquired: do not probe yet
    LEADER = "leader"  # lock held on a live connection
    PRESUMED = "presumed"  # held it, connection lost, DB unreachable: keep probing
    STANDBY = "standby"  # another instance holds the lock


def leader_lock_key(service_name: str) -> int:
    return 7_420_000_000 + zlib.crc32(service_name.encode())


class MonitorService:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        probe: HealthProbe,
        stop: StopFlag,
        clock: Callable[[], float] = time.monotonic,
        recorder: Recorder = record_check,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.probe = probe
        self.stop = stop
        self.clock = clock
        self.recorder = recorder
        self.thresholds = Thresholds(
            failure_threshold=settings.failure_threshold,
            latency_threshold_count=settings.latency_threshold_count,
            rearm_healthy_checks=settings.rearm_healthy_checks,
        )
        self.stale_after = timedelta(seconds=3 * settings.monitor_interval_seconds)
        self.lock_key = leader_lock_key(settings.demo_service_name)
        self.leadership = Leadership.UNKNOWN
        self.buffer: deque[CheckResult] = deque()
        self._buf_lock = threading.Lock()
        self._wake = threading.Event()
        self._lock_conn: Connection | None = None
        self._service_id: uuid.UUID | None = None
        self._last_prune = -math.inf
        self.counters = {
            "checks": 0,
            "recorded": 0,
            "record_failures": 0,
            "dropped": 0,
            "incidents_opened": 0,
            "skipped_ticks": 0,
        }

    # --- buffer ---------------------------------------------------------------
    def enqueue(self, result: CheckResult) -> None:
        with self._buf_lock:
            if len(self.buffer) >= BUFFER_MAX:
                self.buffer.popleft()
                self.counters["dropped"] += 1
                log.error("check buffer full; oldest check dropped")
            self.buffer.append(result)
        self._wake.set()

    def _peek(self) -> CheckResult | None:
        with self._buf_lock:
            return self.buffer[0] if self.buffer else None

    def _pop(self) -> None:
        with self._buf_lock:
            self.buffer.popleft()

    # --- leadership (recorder thread only) --------------------------------------
    def _release(self) -> None:
        """Unlock, then invalidate so the physical connection is closed rather than
        returned to the pool (a pooled session would keep holding the lock)."""
        if self._lock_conn is not None:
            try:
                self._lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.lock_key})
            except SQLAlchemyError:
                pass
            try:
                self._lock_conn.invalidate()
                self._lock_conn.close()
            except SQLAlchemyError:
                pass
            self._lock_conn = None

    def _still_holds_lock(self) -> bool:
        """Ask PostgreSQL whether *this session* holds the lock. A plain ``SELECT 1``
        is not enough: SQLAlchemy may transparently reconnect an invalidated
        connection, yielding a new session that does not hold the lock."""
        assert self._lock_conn is not None
        held = self._lock_conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND granted "
                "AND classid = :hi AND objid = :lo AND objsubid = 1)"
            ),
            {"hi": (self.lock_key >> 32) & 0xFFFFFFFF, "lo": self.lock_key & 0xFFFFFFFF},
        ).scalar()
        self._lock_conn.commit()
        return bool(held)

    def refresh_leadership(self) -> Leadership:
        if self._lock_conn is not None:
            try:
                if self._still_holds_lock():
                    self.leadership = Leadership.LEADER
                    return self.leadership
                log.warning("leader lock no longer held by this session")
            except SQLAlchemyError:
                pass
            self._release()
            if self.leadership is Leadership.LEADER:
                log.warning("leader connection lost; continuing as presumed leader")
                self.leadership = Leadership.PRESUMED
        try:
            conn = self.engine.connect()
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": self.lock_key}
            ).scalar()
            conn.commit()
        except SQLAlchemyError as exc:
            log.warning("leader lock unavailable", extra={"error_type": type(exc).__name__})
            return self.leadership  # UNKNOWN stays UNKNOWN; LEADER/PRESUMED -> PRESUMED
        if got:
            self._lock_conn = conn
            if self.leadership is not Leadership.LEADER:
                log.info("monitor leadership acquired")
            self.leadership = Leadership.LEADER
        else:
            conn.close()
            if self.leadership is not Leadership.STANDBY:
                log.info("another monitor holds leadership; standby")
            self.leadership = Leadership.STANDBY
            with self._buf_lock:
                self.buffer.clear()
        return self.leadership

    # --- recording (recorder thread) ----------------------------------------------
    def drain(self) -> int:
        """Record buffered checks in order. Stops at the first DB failure (the
        check stays buffered). Returns the number recorded."""
        n = 0
        while (result := self._peek()) is not None:
            if self.leadership is not Leadership.LEADER:
                return n
            try:
                if self._service_id is None:
                    self._service_id = upsert_service(
                        self.engine, self.settings.demo_service_name, self.settings.demo_app_url
                    )
                out = self.recorder(
                    self.engine,
                    self._service_id,
                    result,
                    self.thresholds,
                    stale_after=self.stale_after,
                    max_attempts=self.settings.task_max_attempts,
                )
            except SQLAlchemyError as exc:
                self.counters["record_failures"] += 1
                log.error(
                    "check not recorded (database unavailable); buffered for retry",
                    extra={
                        "outcome": result.outcome,
                        "checked_at": result.checked_at.isoformat(),
                        "buffered": len(self.buffer),
                        "error_type": type(exc).__name__,
                    },
                )
                return n
            self._pop()
            n += 1
            self.counters["recorded"] += 1
            self.counters["incidents_opened"] += len(out.opened_incidents)
            log.info(
                "check recorded",
                extra={
                    "outcome": result.outcome,
                    "latency_ms": result.latency_ms,
                    "http_status": result.http_status,
                    "checked_at": result.checked_at.isoformat(),
                    "decisions": [
                        f"{d.action.value}:{d.failure_type}"
                        for d in out.decisions
                        if d.action.value != "recovered"
                    ],
                    "opened": [str(i) for i in out.opened_incidents],
                    "resolved": [str(i) for i in out.resolved_incidents],
                },
            )
        return n

    def _maybe_prune(self) -> None:
        if self.clock() - self._last_prune < PRUNE_EVERY_SECONDS:
            return
        try:
            deleted = prune_health_checks(self.engine, self.settings.health_check_retention_days)
            self._last_prune = self.clock()
            log.info("health check retention applied", extra={"deleted": deleted})
        except SQLAlchemyError as exc:
            log.warning("retention prune failed", extra={"error_type": type(exc).__name__})

    def recorder_loop(self) -> None:
        failures = 0
        while not self.stop.is_set():
            self.refresh_leadership()
            if self.leadership is Leadership.LEADER:
                self._maybe_prune()
                self.drain()
            failures = (
                failures + 1 if self.buffer and self.leadership is not Leadership.LEADER else 0
            )
            wait = backoff_seconds(failures, 1.0, 15.0) if failures else 5.0
            self._wake.wait(wait)
            self._wake.clear()
        self.drain()
        self._release()

    # --- probing (main thread) ---------------------------------------------------
    def probe_once(self) -> CheckResult:
        result = self.probe.check()
        self.counters["checks"] += 1
        self.enqueue(result)
        return result

    def run(self) -> None:
        interval = self.settings.monitor_interval_seconds
        recorder = threading.Thread(target=self.recorder_loop, name="recorder", daemon=True)
        recorder.start()
        next_at = self.clock()
        while not self.stop.is_set():
            beat("monitor")
            if self.leadership in (Leadership.UNKNOWN, Leadership.STANDBY):
                self._wake.set()  # ask the recorder thread to (re)try leadership
                self.stop.wait(min(interval, 2.0))
                next_at = self.clock()
                continue
            self.probe_once()
            next_at += interval
            now = self.clock()
            if next_at <= now:
                skipped = math.ceil((now - next_at) / interval)
                self.counters["skipped_ticks"] += skipped
                next_at += skipped * interval
                log.warning("monitor overran interval; ticks skipped", extra={"skipped": skipped})
            self.stop.wait(next_at - now)
        self._wake.set()
        recorder.join(timeout=30)
        log.info("monitor stopped", extra=self.counters)
