"""Redis Streams naming and client construction. Redis is transport only;
PostgreSQL remains authoritative for every task and incident."""

from __future__ import annotations

from dataclasses import dataclass

import redis

from app.config import Settings


@dataclass(frozen=True)
class StreamNames:
    tasks: str
    dead_letter: str
    group: str
    reports: str = ""
    reports_group: str = ""

    @classmethod
    def from_prefix(cls, prefix: str) -> StreamNames:
        return cls(
            tasks=f"{prefix}:tasks",
            dead_letter=f"{prefix}:tasks:dead",
            group=f"{prefix}-workers",
            reports=f"{prefix}:reports",
            reports_group=f"{prefix}-reporters",
        )

    def stream_for(self, event_type: str) -> str:
        """Outbox routing: report jobs have their own stream and consumer group,
        so incident tasks are never queued behind report generation."""
        return self.reports if event_type == "report.generate" and self.reports else self.tasks


def make_redis(settings: Settings) -> redis.Redis:
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password.get_secret_value(),
        socket_timeout=max(settings.worker_block_seconds + 5, 10),
        socket_connect_timeout=5,
        decode_responses=True,
        health_check_interval=30,
    )


def ensure_group(client: redis.Redis, names: StreamNames) -> None:
    """Create the consumer groups (and streams) if missing. Idempotent."""
    pairs = [(names.tasks, names.group)]
    if names.reports:
        pairs.append((names.reports, names.reports_group))
    for stream, group in pairs:
        try:
            client.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
