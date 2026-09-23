"""Reports-stream consumer, run inside the worker process.

Same delivery contract as tasks: read (plus XAUTOCLAIM of stale pending
messages) -> claim a DB lease (fencing token++) -> run -> commit -> XACK.
A non-claimable job (already reported, not due, leased by a live worker) is a
duplicate: ACK. A lost lease: do NOT ACK. A heartbeat renews the lease.
"""

from __future__ import annotations

import logging
import random
import threading
import uuid
from collections.abc import Callable
from typing import Any, cast

import redis
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.backoff import jittered_backoff
from app.config import Settings
from app.observability.logging import log_context
from app.persistence.streams import StreamNames
from app.reporting import jobs
from app.reporting.stage import AiBusy, ReportStage, ReportTransient

log = logging.getLogger("sentinelops.reporting.consumer")
Hook = Callable[[str], None]


def _no_hook(_point: str) -> None:
    return None


class _Heartbeat(threading.Thread):
    def __init__(self, engine: Engine, lease: jobs.JobLease, ttl: float, every: float) -> None:
        super().__init__(daemon=True, name=f"report-lease-{lease.job_id}")
        self.engine, self.lease, self.ttl, self.every = engine, lease, ttl, every
        self.done = threading.Event()

    def run(self) -> None:
        while not self.done.wait(self.every):
            try:
                if not jobs.renew(self.engine, self.lease, self.ttl):
                    log.warning(
                        "report lease lost", extra={"report_job_id": str(self.lease.job_id)}
                    )
                    return
            except SQLAlchemyError as exc:
                log.warning("report lease renewal failed", extra={"error_type": type(exc).__name__})


class ReportConsumer:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        client: redis.Redis,
        stage: ReportStage,
        consumer: str,
        *,
        hook: Hook = _no_hook,
        rng: random.Random | None = None,
    ) -> None:
        self.s = settings
        self.engine = engine
        self.client = client
        self.stage = stage
        self.consumer = consumer
        self.names = StreamNames.from_prefix(settings.stream_prefix)
        self.hook = hook
        self.rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self.counters = {
            "processed": 0,
            "duplicates": 0,
            "retries": 0,
            "failed": 0,
            "lease_lost": 0,
        }

    def handle(self, msg_id: str, fields: dict[str, Any] | None) -> bool:
        raw = (fields or {}).get("task_id") or (fields or {}).get("report_job_id")
        try:
            job_id = uuid.UUID(str(raw))
        except ValueError:
            log.error("malformed report message; acking", extra={"msg_id": msg_id})
            return self._ack(msg_id)
        lease = jobs.claim(self.engine, job_id, self.consumer, self.s.lease_ttl_seconds)
        if lease is None:
            if jobs.fail_if_exhausted(self.engine, job_id, self.consumer):
                self.counters["failed"] += 1
            else:
                self.counters["duplicates"] += 1
            return self._ack(msg_id)
        hb = _Heartbeat(self.engine, lease, self.s.lease_ttl_seconds, self.s.heartbeat_seconds)
        hb.start()
        try:
            with log_context(report_job_id=str(job_id), incident_id=str(lease.incident_id)):
                status = self.stage.run(self.engine, lease)
                self.counters["processed"] += 1
                log.info("report job complete", extra={"status": status})
        except jobs.JobLeaseLost:
            self.counters["lease_lost"] += 1
            log.warning("report job fenced out; not acknowledging", extra={"msg_id": msg_id})
            return False
        except AiBusy:
            if not self._retry(lease, 15.0, "ai_concurrency_limit", refund=True):
                return False
        except Exception as exc:  # transient provider trouble or a DB hiccup: bounded retry
            kind = "ReportTransient" if isinstance(exc, ReportTransient) else type(exc).__name__
            delay = jittered_backoff(
                lease.attempt,
                self.s.task_retry_base_seconds,
                self.s.task_retry_max_seconds,
                self.rng,
            )
            log.warning(
                "report job error; retrying", extra={"error_type": kind, "retry_in_s": delay}
            )
            if not self._retry(lease, delay, f"{kind}: {str(exc)[:200]}", refund=False):
                return False
        finally:
            hb.done.set()
            hb.join(timeout=5)
        self.hook("before_ack")
        return self._ack(msg_id)

    def _retry(self, lease: jobs.JobLease, delay: float, error: str, *, refund: bool) -> bool:
        try:
            jobs.retry_later(self.engine, lease, delay, error, refund_attempt=refund)
        except jobs.JobLeaseLost:
            self.counters["lease_lost"] += 1
            return False
        self.counters["retries"] += 1
        return True

    def _ack(self, msg_id: str) -> bool:
        self.client.xack(self.names.reports, self.names.reports_group, msg_id)
        return True

    def poll_once(self, block_ms: int = 100) -> int:
        _next, claimed, *_ = self.client.xautoclaim(
            self.names.reports,
            self.names.reports_group,
            self.consumer,
            min_idle_time=int(self.s.pending_idle_seconds * 1000),
            start_id="0-0",
            count=5,
        )
        n = 0
        for msg_id, fields in claimed:
            self.handle(msg_id, fields)
            n += 1
        resp = self.client.xreadgroup(
            self.names.reports_group,
            self.consumer,
            {self.names.reports: ">"},
            count=1,
            block=block_ms,
        )
        streams = cast(list[tuple[str, list[tuple[str, dict[str, Any]]]]], resp or [])
        for _stream, messages in streams:
            for msg_id, fields in messages:
                self.handle(msg_id, fields)
                n += 1
        return n
