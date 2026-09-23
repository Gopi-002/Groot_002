"""Isolated FastAPI demo application monitored by SentinelOps.

Failure injection is simulated in-process only:
* ``timeout``    — /health delays (async sleep; no threads/CPU burned).
* ``http_500``   — /health returns 500.
* ``memory_log`` — emits memory-pressure-like log lines and reports a
  *simulated* memory figure in /metrics. No memory is actually allocated.

``/simulate-failure`` exists only when DEMO_ENV=demo and injection is enabled;
it additionally requires a token and a loopback/private-network client.
In production configuration the route is never registered (404).
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.observability.logging import configure_logging

log = logging.getLogger("demo_application")

MAX_DURATION_SECONDS = 600
BASELINE_MEMORY_MB = 64.0
SIMULATED_PRESSURE_MB = 480.0


class DemoEnv(StrEnum):
    DEMO = "demo"
    TEST = "test"
    PRODUCTION = "production"


class DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEMO_", extra="ignore", frozen=True)

    env: DemoEnv = DemoEnv.PRODUCTION  # safe default: injection off
    failure_injection_enabled: bool = False
    injection_token: SecretStr | None = None
    timeout_delay_seconds: float = Field(default=15.0, gt=0, le=120)
    # Demo-only: where a "sticky" failure mode is persisted so that it survives a
    # container restart (to demonstrate a restart that does NOT fix the problem).
    state_dir: Path | None = None
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _guard(self) -> DemoSettings:
        if self.failure_injection_enabled:
            if self.env is DemoEnv.PRODUCTION:
                raise ValueError("failure injection cannot be enabled in production")
            tok = self.injection_token.get_secret_value() if self.injection_token else ""
            if len(tok) < 16:
                raise ValueError("DEMO_INJECTION_TOKEN (>=16 chars) required for injection")
        return self

    @property
    def injection_active(self) -> bool:
        return self.failure_injection_enabled and self.env is not DemoEnv.PRODUCTION


class FailureMode(StrEnum):
    NONE = "none"
    TIMEOUT = "timeout"
    HTTP_500 = "http_500"
    MEMORY_LOG = "memory_log"


@dataclass
class DemoState:
    mode: FailureMode = FailureMode.NONE
    expires_at: float | None = None
    started_at: float = 0.0
    requests_total: int = 0
    health_failures_total: int = 0

    def current_mode(self, now: float) -> FailureMode:
        if self.expires_at is not None and now >= self.expires_at:
            log.info("failure mode expired; reverting", extra={"mode": self.mode.value})
            self.mode, self.expires_at = FailureMode.NONE, None
        return self.mode


class FailureRequest(BaseModel):
    mode: FailureMode
    duration_seconds: int | None = Field(default=None, ge=1, le=MAX_DURATION_SECONDS)
    sticky: bool = False  # demo-only: survive restarts (requires DEMO_STATE_DIR)


STICKY_FILE = "sticky_failure_mode"


def _load_sticky(settings: DemoSettings) -> FailureMode:
    if not settings.injection_active or settings.state_dir is None:
        return FailureMode.NONE
    try:
        return FailureMode((settings.state_dir / STICKY_FILE).read_text().strip())
    except (OSError, ValueError):
        return FailureMode.NONE


def _client_allowed(host: str | None) -> bool:
    if host is None:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "testclient"  # Starlette TestClient
    return ip.is_loopback or ip.is_private


def create_demo_app(settings: DemoSettings | None = None) -> FastAPI:
    settings = settings or DemoSettings()
    configure_logging("demo-app", settings.log_level)
    app = FastAPI(
        title="SentinelOps Demo Application", docs_url=None, redoc_url=None, openapi_url=None
    )
    state = DemoState(started_at=time.monotonic(), mode=_load_sticky(settings))
    if state.mode is not FailureMode.NONE:
        log.warning("sticky failure mode restored at startup", extra={"mode": state.mode.value})
    app.state.demo = state

    @app.middleware("http")
    async def count(request, call_next):  # type: ignore[no-untyped-def]
        state.requests_total += 1
        return await call_next(request)

    @app.get("/health")
    async def health() -> JSONResponse:
        mode = state.current_mode(time.monotonic())
        if mode is FailureMode.HTTP_500:
            state.health_failures_total += 1
            log.error("simulated internal error", extra={"mode": mode.value})
            return JSONResponse({"status": "error", "detail": "simulated failure"}, status_code=500)
        if mode is FailureMode.TIMEOUT:
            state.health_failures_total += 1
            await asyncio.sleep(settings.timeout_delay_seconds)
        if mode is FailureMode.MEMORY_LOG:
            log.warning(
                "SIMULATED memory pressure: rss_mb=%.0f threshold_mb=400 (no real allocation)",
                BASELINE_MEMORY_MB + SIMULATED_PRESSURE_MB,
                extra={"mode": mode.value, "simulated": True},
            )
        return JSONResponse({"status": "ok"})

    @app.get("/metrics")
    async def metrics() -> dict[str, object]:
        now = time.monotonic()
        mode = state.current_mode(now)
        mem = BASELINE_MEMORY_MB + (SIMULATED_PRESSURE_MB if mode is FailureMode.MEMORY_LOG else 0)
        return {
            "uptime_seconds": round(now - state.started_at, 3),
            "requests_total": state.requests_total,
            "health_failures_total": state.health_failures_total,
            "failure_mode": mode.value,
            "simulated_memory_mb": mem,
            "failure_injection_available": settings.injection_active,
        }

    if settings.injection_active:
        token = settings.injection_token
        assert token is not None

        @app.post("/simulate-failure")
        async def simulate_failure(
            body: FailureRequest,
            request: Request,
            x_demo_token: Annotated[str | None, Header()] = None,
        ) -> dict[str, object]:
            client = request.client.host if request.client else None
            if not _client_allowed(client):
                raise HTTPException(status_code=403, detail="forbidden")
            if x_demo_token is None or not hmac.compare_digest(
                x_demo_token.encode(), token.get_secret_value().encode()
            ):
                raise HTTPException(status_code=401, detail="unauthorized")
            if body.sticky and settings.state_dir is None:
                raise HTTPException(status_code=422, detail="sticky mode needs DEMO_STATE_DIR")
            if settings.state_dir is not None:
                sticky_path = settings.state_dir / STICKY_FILE
                if body.sticky and body.mode is not FailureMode.NONE:
                    sticky_path.write_text(body.mode.value)
                else:
                    sticky_path.unlink(missing_ok=True)
            state.mode = body.mode
            state.expires_at = (
                time.monotonic() + body.duration_seconds
                if body.duration_seconds and body.mode is not FailureMode.NONE
                else None
            )
            log.warning(
                "failure mode set",
                extra={"mode": body.mode.value, "duration_seconds": body.duration_seconds},
            )
            return {
                "mode": body.mode.value,
                "duration_seconds": body.duration_seconds,
                "sticky": body.sticky,
            }

    return app


def app_factory() -> FastAPI:
    return create_demo_app()
