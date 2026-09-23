"""executor: performs exactly ONE state-changing operation - restarting the
single trusted demo-app container - for requests that carry a valid,
unexpired, action-bound authorization.

Independent guards (none trust the request body beyond the signed fields):
* bearer token (worker only; reachable only on the internal ``exec_net``);
* HMAC signature over (action, action_id, fencing token, fingerprint, expiry);
* the target comes from this service's own configuration (compose labels),
  never from the request - there is no container parameter at all;
* durable ledger: at most one execution per action id; stale fencing tokens and
  fingerprint changes are refused; an in-progress/unknown action is not redone;
* hourly restart cap.

Security note: Docker socket group access is root-equivalent on the host. The
narrow HTTP API, hardening and lack of egress reduce - they do NOT eliminate -
that risk. See docs/architecture.md.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.api import errors
from app.executor import signing
from app.executor.ledger import Ledger
from app.observability.logging import configure_logging
from app.ops_reader.docker import DockerReader, DockerUnavailable

log = logging.getLogger("sentinelops.executor")


class ExecSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXEC_", extra="ignore", frozen=True)

    token: SecretStr = Field(min_length=32)
    signing_key: SecretStr = Field(min_length=32)
    environment: str = "development"
    docker_socket: str = "/var/run/docker.sock"
    target_project: str = "sentinelops"
    target_service: str = "demo-app"
    ledger_path: Path = Path("/var/lib/sentinel-executor/ledger.sqlite3")
    max_restarts_per_hour: int = Field(default=3, ge=0, le=20)
    stop_timeout_seconds: int = Field(default=5, ge=0, le=60)
    ready_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_authorization_ttl_seconds: float = Field(default=600.0, gt=0, le=3600)
    # TEST/DEMO ONLY: widens the crash window between ledger reservation and the
    # Docker call so resilience tests can kill the worker mid-execution.
    test_pre_restart_delay_seconds: float = Field(default=0.0, ge=0, le=60)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _guard(self) -> ExecSettings:
        if self.environment == "production" and self.test_pre_restart_delay_seconds:
            raise ValueError("test hooks are forbidden in production")
        return self


class RestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: uuid.UUID
    fencing_token: int = Field(ge=1)
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    not_after: datetime
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class DockerController(DockerReader):
    """Reads come from DockerReader; the ONE write is restart()."""

    def state(self) -> dict[str, Any]:
        s = self.status()
        return {
            k: s.get(k)
            for k in ("state", "running", "started_at", "restart_count", "health_status")
        }

    def restart(self, stop_timeout: int, ready_timeout: float) -> dict[str, Any]:
        cid = self.container_id()
        try:
            resp = self.client.post(
                f"/containers/{cid}/restart", params={"t": stop_timeout}, timeout=stop_timeout + 30
            )
        except httpx.HTTPError as exc:
            raise DockerUnavailable(f"restart call failed: {type(exc).__name__}") from exc
        if resp.status_code != 204:
            raise DockerUnavailable(f"restart returned HTTP {resp.status_code}")
        deadline = time.monotonic() + ready_timeout
        while True:
            st = self.state()
            if st.get("running"):
                return st
            if time.monotonic() >= deadline:
                raise DockerUnavailable("container not running after restart deadline")
            time.sleep(0.5)


def create_executor_app(
    settings: ExecSettings | None = None,
    docker_client: httpx.Client | None = None,
    ledger: Ledger | None = None,
) -> FastAPI:
    settings = settings or ExecSettings()
    configure_logging("executor", settings.log_level)
    app = FastAPI(title="SentinelOps executor", docs_url=None, redoc_url=None, openapi_url=None)
    errors.install(app)
    docker_client = docker_client or httpx.Client(
        transport=httpx.HTTPTransport(uds=settings.docker_socket),
        base_url="http://docker",
        timeout=30,
    )
    docker = DockerController(
        docker_client, project=settings.target_project, service=settings.target_service
    )
    ledger = ledger or Ledger(settings.ledger_path)
    exec_lock = threading.Lock()  # one restart at a time

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.encode(), settings.token.get_secret_value().encode()
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/v1/target/state", dependencies=[Depends(auth)])
    def target_state() -> dict[str, Any]:
        try:
            return {"status": "ok", "data": docker.state()}
        except DockerUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @app.get("/v1/actions/{action_id}", dependencies=[Depends(auth)])
    def get_action(action_id: uuid.UUID) -> dict[str, Any]:
        entry = ledger.get(str(action_id))
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown action")
        return entry.to_json()

    @app.post("/v1/actions/restart-demo-app", dependencies=[Depends(auth)])
    def restart(req: RestartRequest) -> JSONResponse:
        now = datetime.now(UTC)
        if not signing.verify(
            settings.signing_key,
            req.action_id,
            req.fencing_token,
            req.action_fingerprint,
            req.not_after,
            req.signature,
        ):
            log.warning(
                "rejected: bad authorization signature", extra={"action_id": str(req.action_id)}
            )
            raise HTTPException(status_code=403, detail="invalid authorization")
        if now > req.not_after:
            raise HTTPException(status_code=403, detail="authorization expired")
        if req.not_after - now > timedelta(seconds=settings.max_authorization_ttl_seconds):
            raise HTTPException(status_code=403, detail="authorization lifetime too long")
        with exec_lock:
            try:
                pre = docker.state()
            except DockerUnavailable as exc:
                return JSONResponse({"status": "unavailable", "reason": str(exc)}, 503)
            verdict, existing = ledger.reserve(
                str(req.action_id),
                req.fencing_token,
                req.action_fingerprint,
                pre,
                settings.max_restarts_per_hour,
            )
            if verdict == "replay" and existing is not None:
                log.info(
                    "duplicate request: returning recorded result",
                    extra={"action_id": str(req.action_id)},
                )
                return JSONResponse({**existing.to_json(), "replayed": True}, 200)
            if verdict != "execute":
                status = {
                    "stale": 409,
                    "in_progress": 409,
                    "fingerprint_mismatch": 409,
                    "rate_limited": 429,
                }[verdict]
                body: dict[str, Any] = {"status": "refused", "reason": verdict}
                if existing is not None:
                    body["ledger"] = existing.to_json()
                log.warning(
                    "restart refused", extra={"action_id": str(req.action_id), "reason": verdict}
                )
                return JSONResponse(body, status)
            log.warning(
                "EXECUTING restart",
                extra={
                    "action_id": str(req.action_id),
                    "fencing_token": req.fencing_token,
                    "target": settings.target_service,
                },
            )
            if settings.test_pre_restart_delay_seconds:
                time.sleep(settings.test_pre_restart_delay_seconds)
            try:
                post = docker.restart(settings.stop_timeout_seconds, settings.ready_timeout_seconds)
                entry = ledger.finish(str(req.action_id), "completed", post, None)
            except DockerUnavailable as exc:
                entry = ledger.finish(str(req.action_id), "failed", None, str(exc))
                log.error(
                    "restart failed", extra={"action_id": str(req.action_id), "error": str(exc)}
                )
        return JSONResponse({**entry.to_json(), "replayed": False}, 200)

    return app


def app_factory() -> FastAPI:
    return create_executor_app()
