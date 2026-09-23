"""Outbox dispatcher process: python -m app.agent.dispatcher

Runs publish -> retry scheduling -> reconciliation in a loop. Several instances
are safe (FOR UPDATE SKIP LOCKED, dedup keys), though one is enough."""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import UTC, datetime

import redis
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.observability.logging import configure_logging
from app.persistence.db import make_engine
from app.persistence.heartbeats import record_heartbeat
from app.persistence.outbox import (
    publish_pending,
    reconcile_stuck_tasks,
    schedule_due_retries,
    schedule_parked_investigations,
    schedule_policy_tasks,
)
from app.persistence.streams import StreamNames, ensure_group, make_redis
from app.reporting.jobs import schedule_report_jobs
from app.runtime import StopFlag, beat

log = logging.getLogger("sentinelops.dispatcher")

RECONCILE_EVERY_SECONDS = 15.0


def run(settings: Settings, engine: Engine, client: redis.Redis, stop: StopFlag) -> None:
    names = StreamNames.from_prefix(settings.stream_prefix)
    group_ready = False
    last_reconcile = 0.0
    instance, started = f"{socket.gethostname()}-{os.getpid()}", datetime.now(UTC)
    while not stop.is_set():
        try:
            if not group_ready:
                ensure_group(client, names)
                group_ready = True
        except redis.RedisError as exc:
            log.warning(
                "redis unavailable; outbox rows retained in PostgreSQL",
                extra={"error_type": type(exc).__name__},
            )
        try:
            schedule_due_retries(engine)
            schedule_parked_investigations(engine)
            schedule_policy_tasks(engine)
            if time.monotonic() - last_reconcile >= RECONCILE_EVERY_SECONDS:
                reconcile_stuck_tasks(engine, settings.redispatch_after_seconds)
                # report jobs whose message was lost or whose worker died
                schedule_report_jobs(engine, settings.redispatch_after_seconds)
                record_heartbeat(
                    engine,
                    "dispatcher",
                    instance,
                    started,
                    details={"redis_group_ready": group_ready},
                )
                last_reconcile = time.monotonic()
            res = publish_pending(
                engine,
                client,
                names,
                batch_size=settings.dispatcher_batch_size,
                maxlen=settings.stream_maxlen,
                retry_base=settings.publish_retry_base_seconds,
                retry_max=settings.publish_retry_max_seconds,
            )
            if res.published:
                log.info("outbox published", extra={"count": res.published})
        except SQLAlchemyError as exc:
            log.warning("database unavailable", extra={"error_type": type(exc).__name__})
        beat("dispatcher")
        stop.wait(settings.dispatcher_poll_seconds)


def main() -> None:
    settings = get_settings()
    configure_logging("sentinel-dispatcher", settings.log_level)
    stop = StopFlag()
    stop.install_signal_handlers()
    engine = make_engine(settings.database_url)
    client = make_redis(settings)
    try:
        run(settings, engine, client, stop)
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
