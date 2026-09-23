"""Notifier process: python -m app.notifications.service

Least privilege: PostgreSQL only (no Redis, no Docker, no AI key, no executor
or signing keys); the webhook signing secret is the only secret it holds, and
the ONLY outbound destination is the configured, allowlisted webhook URL.

Loop: fan out new events -> deliver due deliveries -> evaluate alerts
(every ``alert_eval_seconds``) -> retention prune (hourly) -> heartbeat.
Every step is independent; a failing provider or a DB outage only delays work.
"""

from __future__ import annotations

import logging
import os
import random
import socket
import time
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.notifications import alerts
from app.notifications.channels import LogChannel, NotificationChannel, WebhookChannel
from app.notifications.delivery import deliver_due, fan_out, prune
from app.observability.logging import configure_logging
from app.persistence.db import make_engine
from app.persistence.heartbeats import record_heartbeat
from app.runtime import StopFlag, beat

log = logging.getLogger("sentinelops.notifier")
BATCH = 10


def build_channels(settings: Settings) -> dict[str, NotificationChannel]:
    channels: dict[str, NotificationChannel] = {}
    if "log" in settings.notify_channels:
        channels["log"] = LogChannel()
    if "webhook" in settings.notify_channels and settings.notify_webhook_url:
        channels["webhook"] = WebhookChannel(
            settings.notify_webhook_url,
            settings.notify_webhook_secret,
            timeout_seconds=settings.notify_timeout_seconds,
            max_response_bytes=settings.notify_max_response_bytes,
        )
    return channels


class Notifier:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        channels: dict[str, NotificationChannel] | None = None,
        *,
        instance: str | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.s = settings
        self.engine = engine
        self.channels = channels if channels is not None else build_channels(settings)
        self.instance = instance or f"{socket.gethostname()}-{os.getpid()}"
        self.rng = rng or random.Random()  # noqa: S311 - retry jitter, not security
        self.started_at = datetime.now(UTC)
        self.lease_ttl = BATCH * (settings.notify_timeout_seconds + 2) + 30
        self._last_alerts = 0.0
        self._last_prune = 0.0
        self.totals: dict[str, int] = {}

    def cycle(self, *, force_alerts: bool = False) -> dict[str, int]:
        fan_out(self.engine, list(self.channels) or ["log"], self.s.notify_max_attempts)
        counts = deliver_due(
            self.engine,
            self.channels,
            owner=self.instance,
            lease_ttl=self.lease_ttl,
            retry_base=self.s.notify_retry_base_seconds,
            retry_max=self.s.notify_retry_max_seconds,
            rng=self.rng,
            batch=BATCH,
        )
        for k, v in counts.items():
            self.totals[k] = self.totals.get(k, 0) + v
        now = time.monotonic()
        if force_alerts or now - self._last_alerts >= self.s.alert_eval_seconds:
            alerts.evaluate(self.engine, self.s)
            self._last_alerts = now
        if now - self._last_prune >= 3600:
            prune(self.engine, self.s.notify_retention_days)
            self._last_prune = now
        record_heartbeat(
            self.engine,
            "notifier",
            self.instance,
            self.started_at,
            details={"channels": sorted(self.channels), "totals": self.totals},
        )
        return counts

    def run(self, stop: StopFlag) -> None:
        failures = 0
        while not stop.is_set():
            beat("notifier")
            try:
                self.cycle()
                failures = 0
            except SQLAlchemyError as exc:
                failures += 1
                log.warning(
                    "database unavailable; notifications retained",
                    extra={"error_type": type(exc).__name__},
                )
            stop.wait(self.s.notify_poll_seconds if failures == 0 else min(30.0, 2.0 * failures))


def main() -> None:
    settings = get_settings()
    configure_logging("sentinel-notifier", settings.log_level)
    stop = StopFlag()
    stop.install_signal_handlers()
    engine = make_engine(settings.database_url)
    notifier = Notifier(settings, engine)
    log.info(
        "notifier starting",
        extra={
            "channels": sorted(notifier.channels),
            "webhook_host": (settings.notify_webhook_url or "").split("/")[2:3],
        },
    )
    try:
        notifier.run(stop)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
