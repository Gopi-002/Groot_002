"""ops-reader: token-protected, GET-only view of the demo-app container.

Security posture: this is the only service with the Docker socket (group
access). Its own API exposes exactly four read endpoints for one allowlisted
container, it has no egress network, a read-only root FS, no capabilities, and
the AI never reaches it directly - only the worker's typed tools do.
"""

from __future__ import annotations

import hmac
import time
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.api import errors
from app.observability.logging import configure_logging
from app.ops_reader.docker import DockerReader, DockerUnavailable

APP_METRIC_FIELDS = (
    "uptime_seconds",
    "requests_total",
    "health_failures_total",
    "simulated_memory_mb",
)


class OpsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPS_", extra="ignore", frozen=True)

    token: SecretStr = Field(min_length=32)
    docker_socket: str = "/var/run/docker.sock"
    target_project: str = "sentinelops"
    target_service: str = "demo-app"
    target_metrics_url: str = Field(default="http://demo-app:8001/metrics", pattern=r"^https?://")
    target_health_url: str = Field(default="http://demo-app:8001/health", pattern=r"^https?://")
    probe_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    log_level: str = "INFO"


def create_ops_app(
    settings: OpsSettings | None = None,
    docker_client: httpx.Client | None = None,
    http_client: httpx.Client | None = None,
) -> FastAPI:
    settings = settings or OpsSettings()
    configure_logging("ops-reader", settings.log_level)
    app = FastAPI(title="SentinelOps ops-reader", docs_url=None, redoc_url=None, openapi_url=None)
    errors.install(app)
    docker_client = docker_client or httpx.Client(
        transport=httpx.HTTPTransport(uds=settings.docker_socket),
        base_url="http://docker",
        timeout=settings.timeout_seconds,
    )
    http_client = http_client or httpx.Client(
        timeout=settings.timeout_seconds, trust_env=False, follow_redirects=False
    )
    reader = DockerReader(
        docker_client, project=settings.target_project, service=settings.target_service
    )

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.encode(), settings.token.get_secret_value().encode()
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    def wrap(fn: Any, *args: Any) -> dict[str, Any]:
        try:
            return {"status": "ok", "data": fn(*args)}
        except DockerUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/v1/target/status", dependencies=[Depends(auth)])
    def status() -> dict[str, Any]:
        return wrap(reader.status)

    @app.get("/v1/target/logs", dependencies=[Depends(auth)])
    def logs(
        tail: Annotated[int, Query(ge=1, le=200)] = 100,
        since: Annotated[int | None, Query(ge=0)] = None,
    ) -> dict[str, Any]:
        return wrap(reader.logs, tail, since)

    @app.get("/v1/target/probe", dependencies=[Depends(auth)])
    def probe() -> dict[str, Any]:
        """One fresh, read-only health probe of the target (used by recovery
        verification). Latency is measured here, inside the demo network."""
        started = time.monotonic()
        checked_at = datetime.now(UTC).isoformat()
        try:
            resp = http_client.get(
                settings.target_health_url, timeout=settings.probe_timeout_seconds
            )
        except httpx.TimeoutException as exc:
            return {
                "status": "ok",
                "data": {
                    "ok": False,
                    "http_status": None,
                    "latency_ms": round((time.monotonic() - started) * 1000, 3),
                    "error": type(exc).__name__,
                    "checked_at": checked_at,
                },
            }
        except httpx.HTTPError as exc:
            return {
                "status": "ok",
                "data": {
                    "ok": False,
                    "http_status": None,
                    "latency_ms": None,
                    "error": type(exc).__name__,
                    "checked_at": checked_at,
                },
            }
        latency = round((time.monotonic() - started) * 1000, 3)
        return {
            "status": "ok",
            "data": {
                "ok": 200 <= resp.status_code < 300,
                "http_status": resp.status_code,
                "latency_ms": latency,
                "error": None,
                "checked_at": checked_at,
            },
        }

    @app.get("/v1/target/stats", dependencies=[Depends(auth)])
    def stats() -> dict[str, Any]:
        return wrap(reader.stats)

    @app.get("/v1/target/app-metrics", dependencies=[Depends(auth)])
    def app_metrics() -> dict[str, Any]:
        try:
            resp = http_client.get(settings.target_metrics_url)
            body = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "unavailable", "reason": f"app metrics: {type(exc).__name__}"}
        if not isinstance(body, dict):
            return {"status": "unavailable", "reason": f"app metrics HTTP {resp.status_code}"}
        # Pass through operational gauges only; test-injection fields are not
        # observable in a real system and are withheld.
        return {"status": "ok", "data": {k: body.get(k) for k in APP_METRIC_FIELDS}}

    return app


def app_factory() -> FastAPI:
    return create_ops_app()
