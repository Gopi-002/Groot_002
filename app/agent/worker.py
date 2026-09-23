"""Task worker: python -m app.agent.worker

Delivery contract (at-least-once, idempotent):
1. Read from the consumer group (new messages, plus XAUTOCLAIM of messages idle
   longer than ``pending_idle_seconds`` from crashed consumers).
2. Claim a DB lease (fencing token++). If the task is not claimable - terminal,
   not yet due, or leased by a live executor - the message is a duplicate: ACK.
3. Run the stage; every write is fenced. A heartbeat thread renews the lease.
4. XACK only AFTER the outcome is committed. If the lease was lost, do not ACK:
   the current holder (or later recovery) owns the task.
Retries use bounded exponential backoff; the final failure dead-letters the task
(DB status + dead-letter stream) and escalates the incident to a human.
"""

from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import redis
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.agent import tasks
from app.agent.investigation import InvestigationStage
from app.agent.model_config import make_gateway
from app.agent.pipeline import TaskPipeline
from app.agent.stages import IntakeStage, PermanentError, Stage
from app.agent.tools import OpsClient
from app.backoff import backoff_seconds, jittered_backoff
from app.config import Settings, get_settings
from app.observability.logging import configure_logging, log_context
from app.persistence.db import make_engine
from app.persistence.heartbeats import record_heartbeat
from app.persistence.streams import StreamNames, ensure_group, make_redis
from app.reporting.consumer import ReportConsumer
from app.reporting.stage import ReportStage
from app.runtime import StopFlag, beat
from app.safety.executor_client import ExecutorClient, ExecutorUnavailable
from app.safety.remediation import RemediationStage

log = logging.getLogger("sentinelops.worker")

Hook = Callable[[str], None]


def _no_hook(_point: str) -> None:
    return None


class _Heartbeat(threading.Thread):
    def __init__(self, engine: Engine, lease: tasks.Lease, ttl: float, every: float) -> None:
        super().__init__(daemon=True, name=f"lease-{lease.task_id}")
        self.engine, self.lease, self.ttl, self.every = engine, lease, ttl, every
        self.done = threading.Event()
        self.lost = threading.Event()

    def run(self) -> None:
        while not self.done.wait(self.every):
            try:
                if not tasks.renew(self.engine, self.lease, self.ttl):
                    self.lost.set()
                    log.warning("lease lost", extra={"task_id": str(self.lease.task_id)})
                    return
            except SQLAlchemyError as exc:
                log.warning("lease renewal failed", extra={"error_type": type(exc).__name__})


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        client: redis.Redis,
        *,
        stage: Stage | None = None,
        consumer: str | None = None,
        hook: Hook = _no_hook,
        reports: ReportConsumer | None = None,
        executor: ExecutorClient | None = None,
    ) -> None:
        self.s = settings
        self.engine = engine
        self.client = client
        self.names = StreamNames.from_prefix(settings.stream_prefix)
        self.stage = stage or IntakeStage()
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.hook = hook
        self.reports = reports
        self.executor = executor
        self.rng = random.Random()  # noqa: S311 - retry jitter, not security
        self.started_at = datetime.now(UTC)
        self._last_heartbeat = 0.0
        self.counters = {
            "processed": 0,
            "duplicates": 0,
            "retries": 0,
            "dead_lettered": 0,
            "failed": 0,
            "lease_lost": 0,
            "reclaimed": 0,
        }

    # --- single message -------------------------------------------------------
    def handle(self, msg_id: str, fields: dict[str, Any] | None) -> bool:
        """Process one message. Returns True if it was ACKed."""
        try:
            task_id = uuid.UUID(str((fields or {})["task_id"]))
        except (KeyError, ValueError):
            log.error("malformed message; dead-lettering", extra={"msg_id": msg_id})
            self._to_dead_letter_stream(msg_id, None, "malformed_message")
            return self._ack(msg_id)

        lease = tasks.claim(self.engine, task_id, self.consumer, self.s.lease_ttl_seconds)
        if lease is None:
            if tasks.dead_letter_if_exhausted(self.engine, task_id, self.consumer):
                self.counters["dead_lettered"] += 1
                self._to_dead_letter_stream(msg_id, task_id, "attempts_exhausted")
            else:
                self.counters["duplicates"] += 1
                log.info(
                    "message not claimable (duplicate/terminal/not due/leased)",
                    extra={"task_id": str(task_id), "msg_id": msg_id},
                )
            return self._ack(msg_id)

        with log_context(task_id=str(task_id), incident_id=str(lease.incident_id)):
            return self._process(msg_id, task_id, lease)

    def _process(self, msg_id: str, task_id: uuid.UUID, lease: tasks.Lease) -> bool:
        self.hook("after_claim")
        hb = _Heartbeat(self.engine, lease, self.s.lease_ttl_seconds, self.s.heartbeat_seconds)
        hb.start()
        try:
            result = self.stage.run(self.engine, lease)
            self.counters["processed"] += 1
            log.info(
                "task stage complete",
                extra={
                    "task_id": str(task_id),
                    "status": result.status,
                    "fencing_token": lease.token,
                },
            )
        except tasks.LeaseLost:
            self.counters["lease_lost"] += 1
            log.warning(
                "fenced write rejected; not acknowledging",
                extra={"task_id": str(task_id), "fencing_token": lease.token},
            )
            return False
        except PermanentError as exc:
            if not self._fenced(lease, "failed", "permanent_error", exc):
                return False
            self.counters["failed"] += 1
        except Exception as exc:
            if not self._retry_or_dead_letter(lease, exc):
                return False
            if lease.attempt >= lease.max_attempts:
                self._to_dead_letter_stream(msg_id, task_id, "attempts_exhausted")
        finally:
            hb.done.set()
            hb.join(timeout=5)
        self.hook("before_ack")
        return self._ack(msg_id)

    def _retry_or_dead_letter(self, lease: tasks.Lease, exc: Exception) -> bool:
        if lease.attempt >= lease.max_attempts:
            ok = self._fenced(lease, "dead_lettered", "attempts_exhausted", exc)
            if ok:
                self.counters["dead_lettered"] += 1
            return ok
        delay = jittered_backoff(
            lease.attempt, self.s.task_retry_base_seconds, self.s.task_retry_max_seconds, self.rng
        )
        ok = self._fenced(lease, "retry_scheduled", "transient_error", exc, next_in=delay)
        if ok:
            self.counters["retries"] += 1
        return ok

    def _fenced(
        self,
        lease: tasks.Lease,
        status: str,
        outcome: str,
        exc: Exception,
        next_in: float | None = None,
    ) -> bool:
        error = f"{type(exc).__name__}"
        try:
            with self.engine.begin() as conn:
                tasks.transition(
                    conn, lease, status, outcome=outcome, error=error, next_attempt_in=next_in
                )
        except tasks.LeaseLost:
            self.counters["lease_lost"] += 1
            return False
        log.warning(
            "task stage error",
            extra={
                "task_id": str(lease.task_id),
                "status": status,
                "attempt": lease.attempt,
                "error_type": error,
                "retry_in_s": next_in,
            },
        )
        return True

    def _ack(self, msg_id: str) -> bool:
        self.client.xack(self.names.tasks, self.names.group, msg_id)
        return True

    def _to_dead_letter_stream(self, msg_id: str, task_id: uuid.UUID | None, reason: str) -> None:
        try:
            self.client.xadd(
                self.names.dead_letter,
                {
                    "source_msg_id": msg_id,
                    "task_id": str(task_id or ""),
                    "reason": reason,
                    "at": str(time.time()),
                },
                maxlen=self.s.stream_maxlen,
                approximate=True,
            )
        except redis.RedisError as exc:  # DB status is authoritative; stream is informational
            log.warning("dead-letter stream write failed", extra={"error_type": type(exc).__name__})

    # --- polling --------------------------------------------------------------
    def recover_pending(self) -> int:
        """Take over messages whose consumer died (idle > pending_idle_seconds)."""
        _next, claimed, *_ = self.client.xautoclaim(
            self.names.tasks,
            self.names.group,
            self.consumer,
            min_idle_time=int(self.s.pending_idle_seconds * 1000),
            start_id="0-0",
            count=10,
        )
        for msg_id, fields in claimed:
            self.counters["reclaimed"] += 1
            log.warning("reclaimed pending message", extra={"msg_id": msg_id})
            self.handle(msg_id, fields)
        return len(claimed)

    def poll_once(self, block_ms: int | None = None) -> int:
        self.recover_pending()
        resp = self.client.xreadgroup(
            self.names.group,
            self.consumer,
            {self.names.tasks: ">"},
            count=1,
            block=int(self.s.worker_block_seconds * 1000) if block_ms is None else block_ms,
        )
        n = 0
        streams = cast(list[tuple[str, list[tuple[str, dict[str, Any]]]]], resp or [])
        for _stream, messages in streams:
            for msg_id, fields in messages:
                self.handle(msg_id, fields)
                n += 1
        return n

    def heartbeat(self) -> None:
        """Component status for /v1/system/status and the service_down /
        executor_unreachable alerts (at most every 15 s)."""
        if time.monotonic() - self._last_heartbeat < 15:
            return
        self._last_heartbeat = time.monotonic()
        details: dict[str, Any] = {"ai_gateway": self.s.ai_gateway, "counters": self.counters}
        status = "ok"
        if self.executor is not None and self.executor.configured:
            try:
                self.executor.state()
                details["executor"] = "ok"
            except ExecutorUnavailable:
                details["executor"], status = "unreachable", "degraded"
        try:
            record_heartbeat(
                self.engine,
                "worker",
                self.consumer,
                self.started_at,
                status=status,
                details=details,
            )
        except SQLAlchemyError as exc:
            log.warning("heartbeat write failed", extra={"error_type": type(exc).__name__})

    def run(self, stop: StopFlag) -> None:
        group_ready = False
        failures = 0
        while not stop.is_set():
            beat("worker")
            try:
                if not group_ready:
                    ensure_group(self.client, self.names)
                    group_ready = True
                self.heartbeat()
                self.poll_once()
                if self.reports is not None:
                    self.reports.poll_once(block_ms=50)
                failures = 0
            except (redis.RedisError, SQLAlchemyError) as exc:
                failures += 1
                delay = backoff_seconds(failures, 1.0, 30.0)
                log.warning(
                    "worker dependency error; backing off",
                    extra={"error_type": type(exc).__name__, "retry_in_s": delay},
                )
                stop.wait(delay)
        log.info("worker stopped", extra=self.counters)


def main() -> None:
    settings = get_settings()
    configure_logging("sentinel-worker", settings.log_level)
    stop = StopFlag()
    stop.install_signal_handlers()
    engine = make_engine(settings.database_url)
    client = make_redis(settings)
    ops = OpsClient(
        settings.ops_reader_url, settings.ops_reader_token, settings.ops_reader_timeout_seconds
    )
    executor = ExecutorClient(
        settings.executor_url,
        settings.executor_token,
        settings.action_signing_key,
        settings.executor_timeout_seconds,
        settings.action_authorization_ttl_seconds,
    )
    stage = TaskPipeline(
        InvestigationStage(settings, make_gateway, ops),
        RemediationStage(settings, executor, ops),
    )
    consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    delay = settings.test_report_delay_seconds
    reports = ReportConsumer(
        settings,
        engine,
        client,
        ReportStage(
            settings,
            make_gateway,
            # TEST/DEMO ONLY (rejected in production): widen the reporting crash window.
            hook=(lambda p: time.sleep(delay) if p == "before_model_call" else None)
            if delay
            else (lambda _p: None),
        ),
        consumer,
    )
    log.info(
        "worker starting",
        extra={
            "ai_gateway": settings.ai_gateway,
            "stage": "intake+investigation+policy+remediation+reporting",
            "autonomous_remediation": settings.remediation_auto_enabled,
            "remediation_environment": settings.remediation_environment,
        },
    )
    if settings.ai_gateway == "mock":
        log.warning(
            "MOCK AI GATEWAY ACTIVE: investigations use a deterministic test model, "
            "not Claude (test/demo only)"
        )
    try:
        Worker(
            settings,
            engine,
            client,
            stage=stage,
            consumer=consumer,
            reports=reports,
            executor=executor,
        ).run(stop)
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
