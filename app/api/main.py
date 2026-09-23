"""SentinelOps API service.

Health probes, the dev-only read-only dashboard shell, the bearer-protected
read-only ``/v1`` status API (incidents, reports, notifications, system status,
metrics) and the authenticated approval API. No remediation endpoint exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import redis
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, text

from app.api import approvals, errors, v1
from app.api.health import ReadinessChecks, router
from app.config import Environment, Settings, get_settings
from app.observability.logging import configure_logging
from app.persistence.db import current_revision, head_revision, make_engine

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def make_api_redis(settings: Settings) -> redis.Redis:
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password.get_secret_value(),
        socket_timeout=settings.readiness_timeout_seconds,
        socket_connect_timeout=settings.readiness_timeout_seconds,
        decode_responses=True,
    )


def default_checks(settings: Settings, engine: Engine, rclient: redis.Redis) -> ReadinessChecks:
    expected_head = head_revision()

    def database() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def schema() -> None:
        if current_revision(engine) != expected_head:
            raise RuntimeError("schema not at migration head")

    def redis_ping() -> None:
        if not rclient.ping():
            raise RuntimeError("redis ping failed")

    return ReadinessChecks(
        {"database": database, "schema": schema, "redis": redis_ping},
        timeout_seconds=settings.readiness_timeout_seconds + 1.0,
    )


def create_app(
    settings: Settings | None = None,
    readiness: ReadinessChecks | None = None,
    *,
    engine: Engine | None = None,
    redis_client: redis.Redis | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("sentinel-api", settings.log_level)
    prod = settings.environment is Environment.PRODUCTION
    app = FastAPI(
        title="SentinelOps API",
        docs_url=None if prod else "/docs",
        redoc_url=None,
        openapi_url=None if prod else "/openapi.json",
    )
    # Engines connect lazily; constructing them does not touch the network.
    engine = engine or make_engine(
        settings.database_url, timeout_seconds=settings.readiness_timeout_seconds
    )
    redis_client = redis_client or make_api_redis(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.redis = redis_client
    app.state.readiness = (
        readiness if readiness is not None else default_checks(settings, engine, redis_client)
    )
    errors.install(app)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/v1"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(router)
    app.include_router(v1.router)
    app.include_router(approvals.router)
    app.state.allowed_origins = ("http://127.0.0.1:8000", "http://localhost:8000")
    # Dashboard shell: static, holds no data, NOT production-ready. Never served
    # in production; in dev the API port is published on 127.0.0.1 only.
    if not prod and DASHBOARD_DIR.is_dir():
        app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
    return app


def app_factory() -> FastAPI:
    return create_app()
