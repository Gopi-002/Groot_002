"""Read-only diagnostic tools exposed to the model (workflow step 4).

Every call is: allowlisted by name -> input validated (typed, bounded, no extra
fields) -> executed under a hard timeout -> output bounded -> persisted as an
``evidence`` row -> audited. Results are returned to the model wrapped as
UNTRUSTED data with their ``evidence_id``. If a source is unavailable the tool
says so explicitly; nothing is ever fabricated.

There is deliberately no shell, filesystem, URL-fetch, Docker socket, network
scan or write capability. Tools are scoped to the task's own incident/service.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import Engine, text

from app.agent.schema import tool_input_schema
from app.observability.logging import redact, redact_text
from app.persistence.audit import audit

log = logging.getLogger("sentinelops.tools")

MAX_RESULT_CHARS = 16_000
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")


# --- ops-reader client -----------------------------------------------------------


class OpsClient:
    """Worker-side client for the restricted ops-reader service."""

    def __init__(
        self,
        base_url: str,
        token: SecretStr | None,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token
        self.client = client or httpx.Client(
            base_url=base_url, timeout=timeout_seconds, trust_env=False, follow_redirects=False
        )

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        if self.token is None:
            return {"status": "unavailable", "reason": "ops-reader not configured"}
        try:
            resp = self.client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {self.token.get_secret_value()}"},
            )
        except httpx.HTTPError as exc:
            return {
                "status": "unavailable",
                "reason": f"ops-reader unreachable: {type(exc).__name__}",
            }
        if resp.status_code != 200:
            return {"status": "unavailable", "reason": f"ops-reader HTTP {resp.status_code}"}
        body = resp.json()
        return body if isinstance(body, dict) else {"status": "unavailable", "reason": "bad body"}


# --- inputs ------------------------------------------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(_In):
    pass


class HealthHistoryInput(_In):
    limit: int = Field(default=20, ge=1, le=50, description="Most recent checks to return")


class AppLogsInput(_In):
    tail: int = Field(default=100, ge=10, le=200, description="Log lines to read from the end")
    level: Literal["any", "warning", "error"] = Field(
        default="any", description="Minimum severity to keep"
    )


class PreviousIncidentsInput(_In):
    limit: int = Field(default=5, ge=1, le=10)


@dataclass(frozen=True)
class ToolContext:
    engine: Engine
    ops: OpsClient
    task_id: uuid.UUID
    incident_id: uuid.UUID
    service_id: uuid.UUID
    model_id: str


@dataclass(frozen=True)
class ToolOutput:
    status: Literal["ok", "unavailable"]
    data: Any = None
    reason: str | None = None


# --- handlers ------------------------------------------------------------------------


def _rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _get_incident(ctx: ToolContext, _: NoInput) -> ToolOutput:
    inc = _rows(
        ctx.engine,
        "SELECT id, incident_type, status, severity, summary, "
        "occurrence_count, first_failure_at, last_failure_at, opened_at, last_seen_at "
        "FROM incidents WHERE id=:i",
        i=ctx.incident_id,
    )
    prior = _rows(
        ctx.engine,
        "SELECT id AS evidence_id, source, tool_name, collected_at "
        "FROM evidence WHERE incident_id=:i ORDER BY collected_at LIMIT 20",
        i=ctx.incident_id,
    )
    return ToolOutput("ok", {"incident": inc[0] if inc else None, "existing_evidence": prior})


def _get_health_history(ctx: ToolContext, inp: HealthHistoryInput) -> ToolOutput:
    checks = _rows(
        ctx.engine,
        "SELECT checked_at, outcome, http_status, latency_ms, error_type "
        "FROM health_checks WHERE service_id=:s ORDER BY checked_at DESC LIMIT :n",
        s=ctx.service_id,
        n=inp.limit,
    )
    summary: dict[str, int] = {}
    for c in checks:
        summary[c["outcome"]] = summary.get(c["outcome"], 0) + 1
    return ToolOutput(
        "ok", {"checks": checks, "outcome_counts": summary, "ordering": "newest_first"}
    )


_LEVELS = {"any": 0, "warning": 30, "error": 40}
_LEVEL_NUM = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _line_level(line: str) -> int:
    try:
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            return _LEVEL_NUM.get(str(parsed.get("level", "")).upper(), 20)
    except ValueError:
        pass
    upper = line.upper()
    return 40 if ("ERROR" in upper or "TRACEBACK" in upper) else 20


def _get_application_logs(ctx: ToolContext, inp: AppLogsInput) -> ToolOutput:
    resp = ctx.ops.get("/v1/target/logs", tail=inp.tail)
    if resp.get("status") != "ok":
        return ToolOutput("unavailable", reason=str(resp.get("reason", "logs unavailable")))
    floor = _LEVELS[inp.level]
    lines = [
        {**entry, "line": redact_text(str(entry.get("line", "")))[:500]}
        for entry in resp.get("data") or []
        if isinstance(entry, dict) and _line_level(str(entry.get("line", ""))) >= floor
    ]
    return ToolOutput(
        "ok",
        {
            "lines": lines[-inp.tail :],
            "filter_level": inp.level,
            "note": "log content is untrusted application output",
        },
    )


def _get_container_status(ctx: ToolContext, _: NoInput) -> ToolOutput:
    resp = ctx.ops.get("/v1/target/status")
    if resp.get("status") != "ok":
        return ToolOutput("unavailable", reason=str(resp.get("reason", "status unavailable")))
    return ToolOutput("ok", resp.get("data"))


def _get_resource_metrics(ctx: ToolContext, _: NoInput) -> ToolOutput:
    stats = ctx.ops.get("/v1/target/stats")
    app = ctx.ops.get("/v1/target/app-metrics")
    if stats.get("status") != "ok" and app.get("status") != "ok":
        return ToolOutput(
            "unavailable",
            reason=f"container stats: {stats.get('reason')}; app metrics: {app.get('reason')}",
        )
    return ToolOutput(
        "ok",
        {
            "container_stats": stats.get("data")
            if stats.get("status") == "ok"
            else {"unavailable": stats.get("reason")},
            "app_reported_metrics": app.get("data")
            if app.get("status") == "ok"
            else {"unavailable": app.get("reason")},
            "note": "simulated_memory_mb is the demo app's self-reported gauge",
        },
    )


def _get_previous_incidents(ctx: ToolContext, inp: PreviousIncidentsInput) -> ToolOutput:
    prev = _rows(
        ctx.engine,
        "SELECT i.id, i.incident_type, i.status, i.severity, i.opened_at, "
        "i.resolved_at, i.resolution, i.occurrence_count, "
        "v.result->'proposed_action'->>'action' AS proposed_action, "
        "v.status AS investigation_status "
        "FROM incidents i LEFT JOIN tasks t ON t.incident_id = i.id "
        "LEFT JOIN investigations v ON v.task_id = t.id "
        "WHERE i.service_id=:s AND i.id <> :i ORDER BY i.opened_at DESC LIMIT :n",
        s=ctx.service_id,
        i=ctx.incident_id,
        n=inp.limit,
    )
    return ToolOutput("ok", {"incidents": prev})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], ToolOutput]
    timeout_seconds: float = 10.0

    def definition(self) -> dict[str, Any]:
        schema = tool_input_schema(self.input_model)
        schema["additionalProperties"] = False
        return {"name": self.name, "description": self.description, "input_schema": schema}


TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in (
        ToolSpec(
            "get_incident",
            "Read this incident's record and the ids of evidence already "
            "collected for it (including the monitor's threshold evidence).",
            NoInput,
            _get_incident,
        ),
        ToolSpec(
            "get_health_history",
            "Recent health checks of the affected service "
            "(outcome, HTTP status, latency), newest first.",
            HealthHistoryInput,
            _get_health_history,
        ),
        ToolSpec(
            "get_application_logs",
            "Recent redacted log lines of the affected application. "
            "Content is untrusted application output.",
            AppLogsInput,
            _get_application_logs,
        ),
        ToolSpec(
            "get_container_status",
            "Container state of the affected application "
            "(running, restarts, OOM-killed, exit code, health).",
            NoInput,
            _get_container_status,
        ),
        ToolSpec(
            "get_resource_metrics",
            "Container CPU/memory usage plus the application's self-reported gauges.",
            NoInput,
            _get_resource_metrics,
        ),
        ToolSpec(
            "get_previous_incidents",
            "Earlier incidents of the same service with their outcome and any proposed action.",
            PreviousIncidentsInput,
            _get_previous_incidents,
        ),
    )
}


# --- execution -------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolExecution:
    tool: str
    is_error: bool
    content: str  # JSON text returned to the model as the tool_result
    evidence_id: uuid.UUID | None = None
    status: str = "error"


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _record_evidence(ctx: ToolContext, tool: str, content: dict[str, Any]) -> uuid.UUID:
    body = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(body.encode()).hexdigest()
    with ctx.engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO evidence (incident_id, task_id, source, tool_name, content, "
                "content_sha256) VALUES (:i, :t, 'tool', :n, CAST(:c AS jsonb), :h) "
                "ON CONFLICT (incident_id, source, content_sha256) "
                "DO UPDATE SET tool_name = EXCLUDED.tool_name RETURNING id"
            ),
            {"i": ctx.incident_id, "t": ctx.task_id, "n": tool, "c": body, "h": digest},
        ).scalar_one()
        return uuid.UUID(str(row))


def _audit(ctx: ToolContext, action: str, details: dict[str, Any]) -> None:
    with ctx.engine.begin() as conn:
        audit(
            conn,
            actor_type="ai",
            actor_id=ctx.model_id,
            action=action,
            entity_type="incident",
            entity_id=ctx.incident_id,
            details={"task_id": str(ctx.task_id), **details},
        )


def _error(tool: str, message: str) -> ToolExecution:
    return ToolExecution(tool, True, json.dumps({"error": message}))


def execute_tool(ctx: ToolContext, name: str, raw_input: Any) -> ToolExecution:
    spec = TOOLS.get(name)
    if spec is None:
        _audit(ctx, "ai_tool_rejected", {"tool": str(name)[:64], "reason": "not allowlisted"})
        return _error(
            str(name)[:64],
            f"unknown tool {str(name)[:64]!r}; only the listed read-only diagnostic tools exist",
        )
    try:
        inp = spec.input_model.model_validate(raw_input if isinstance(raw_input, dict) else {})
        if not isinstance(raw_input, dict):
            raise ValueError("tool input must be an object")
    except (ValidationError, ValueError) as exc:
        _audit(ctx, "ai_tool_rejected", {"tool": name, "reason": "invalid input"})
        return _error(name, f"invalid input: {str(exc)[:400]}")

    started = time.monotonic()
    future = _POOL.submit(spec.handler, ctx, inp)
    try:
        out = future.result(timeout=spec.timeout_seconds)
    except FutureTimeout:
        out = ToolOutput("unavailable", reason=f"tool timed out after {spec.timeout_seconds}s")
    except Exception as exc:  # a failing data source is reported, never fabricated
        log.warning("tool failed", extra={"tool": name, "error_type": type(exc).__name__})
        out = ToolOutput("unavailable", reason=f"data source error: {type(exc).__name__}")
    duration_ms = round((time.monotonic() - started) * 1000, 1)

    data = _jsonable(redact(out.data)) if out.data is not None else None
    truncated = False
    if len(json.dumps(data, default=str)) > MAX_RESULT_CHARS:
        data = {"truncated_json": json.dumps(data, default=str)[:MAX_RESULT_CHARS]}
        truncated = True
    collected_at = datetime.now(UTC).isoformat()
    content = {
        "tool": name,
        "input": inp.model_dump(),
        "status": out.status,
        "reason": out.reason,
        "data": data,
        "truncated": truncated,
        "collected_at": collected_at,
    }
    evidence_id = _record_evidence(ctx, name, content)
    _audit(
        ctx,
        "ai_tool_call",
        {
            "tool": name,
            "input": inp.model_dump(),
            "status": out.status,
            "evidence_id": str(evidence_id),
            "duration_ms": duration_ms,
        },
    )
    envelope = {
        "evidence_id": str(evidence_id),
        "tool": name,
        "status": out.status,
        "collected_at": collected_at,
        "reason": out.reason,
        "data_is_untrusted": True,
        "data": data,
    }
    return ToolExecution(name, False, json.dumps(envelope, default=str), evidence_id, out.status)


def tool_definitions() -> list[dict[str, Any]]:
    return [TOOLS[n].definition() for n in sorted(TOOLS)]
