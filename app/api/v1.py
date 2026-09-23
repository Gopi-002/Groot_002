"""Read-only incident/task status API (bearer-token protected)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import Engine, text

from app.agent.lifecycle import DurableView, derive
from app.api.auth import AUTH_FAILURES, require_reader
from app.api.status import system_status
from app.observability.metrics import Metric, collect, render
from app.persistence.schema import INCIDENT_ACTIVE_STATUSES, INCIDENT_TERMINAL_STATUSES
from app.persistence.streams import StreamNames

router = APIRouter(prefix="/v1", tags=["status"], dependencies=[Depends(require_reader)])

INCIDENT_COLS = (
    "id, service_id, incident_type, status, severity, summary, occurrence_count, "
    "first_failure_at, last_failure_at, opened_at, last_seen_at, resolved_at, resolution"
)
TASK_COLS = (
    "id, incident_id, status, outcome, model_id, attempt, max_attempts, next_attempt_at, "
    "lease_expires_at, fencing_token, last_error, created_at, updated_at, completed_at"
)
ALL_INCIDENT_STATUSES = INCIDENT_ACTIVE_STATUSES | INCIDENT_TERMINAL_STATUSES


def _engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def _rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


@router.get("/incidents")
def list_incidents(
    request: Request,
    status: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    if status is not None and status not in ALL_INCIDENT_STATUSES:
        raise HTTPException(status_code=422, detail="unknown status")
    where = "WHERE status = :st" if status else ""
    items = _rows(
        _engine(request),
        f"SELECT {INCIDENT_COLS} FROM incidents {where} "  # noqa: S608 - constants
        "ORDER BY opened_at DESC LIMIT :lim",
        st=status,
        lim=limit,
    )
    return {"items": items, "count": len(items)}


@router.get("/incidents/{incident_id}")
def get_incident(request: Request, incident_id: uuid.UUID) -> dict[str, Any]:
    engine = _engine(request)
    inc = _rows(
        engine,
        f"SELECT {INCIDENT_COLS} FROM incidents WHERE id=:i",  # noqa: S608
        i=incident_id,
    )
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")
    tasks = _rows(
        engine,
        f"SELECT {TASK_COLS} FROM tasks WHERE incident_id=:i "  # noqa: S608
        "ORDER BY created_at",
        i=incident_id,
    )
    evidence = _rows(
        engine,
        "SELECT id, task_id, source, content, content_sha256, "
        "collected_at FROM evidence WHERE incident_id=:i "
        "ORDER BY collected_at",
        i=incident_id,
    )
    investigations = _rows(
        engine,
        "SELECT id, task_id, model_id, auth_mode, status, failure_reason, result, rejections, "
        "tool_calls, model_calls, reasoning_attempts, input_tokens, output_tokens, cost_usd, "
        "started_at, completed_at FROM investigations WHERE incident_id=:i "
        "ORDER BY completed_at",
        i=incident_id,
    )
    policy = _rows(
        engine,
        "SELECT id, task_id, phase, proposed_action, decision, rule_ids, reasons, "
        "policy_version, action_fingerprint, evaluated_at FROM policy_decisions "
        "WHERE incident_id=:i ORDER BY evaluated_at",
        i=incident_id,
    )
    actions = _rows(
        engine,
        "SELECT id, action_id, action_type, status, pre_state, post_state, error, "
        "requested_at, started_at, completed_at FROM action_attempts WHERE incident_id=:i",
        i=incident_id,
    )
    verifs = _rows(
        engine,
        "SELECT id, action_attempt_id, status, reason, criteria, observations, started_at, "
        "completed_at FROM verifications WHERE incident_id=:i",
        i=incident_id,
    )
    appr = _rows(
        engine,
        "SELECT id, action_id, proposed_action, target_service, action_fingerprint, status, "
        "expires_at, decided_by, decided_at FROM approvals WHERE incident_id=:i",
        i=incident_id,
    )
    report_jobs = _rows(
        engine,
        "SELECT id, task_id, reason, status, attempt, max_attempts, model_id, auth_mode, "
        "last_error, report_id, created_at, completed_at FROM report_jobs WHERE incident_id=:i "
        "ORDER BY created_at",
        i=incident_id,
    )
    reports = _rows(
        engine,
        "SELECT id, version, generation_mode, model_id, auth_mode, fallback_reason, "
        "record_sha256, created_at FROM reports WHERE incident_id=:i ORDER BY version",
        i=incident_id,
    )
    for t in tasks:
        t["lifecycle_state"] = _lifecycle(engine, t, actions, verifs, report_jobs).value
    return {
        "policy_decisions": policy,
        "action_attempts": actions,
        "verifications": verifs,
        "approvals": appr,
        "incident": inc[0],
        "tasks": tasks,
        "evidence": evidence,
        "investigations": investigations,
        "report_jobs": report_jobs,
        "reports": reports,
    }


def _lifecycle(
    engine: Engine,
    task: dict[str, Any],
    actions: list[dict[str, Any]],
    verifs: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
) -> Any:
    inv = _rows(engine, "SELECT status FROM investigations WHERE task_id=:t", t=task["id"])
    mine = [j for j in jobs if j["task_id"] == task["id"]]
    return derive(
        DurableView(
            task_status=task["status"],
            task_outcome=task["outcome"],
            investigation_completed=bool(inv and inv[0]["status"] == "completed"),
            attempt_status=actions[-1]["status"] if actions else None,
            verification_status=verifs[-1]["status"] if verifs else None,
            report_job_status=mine[-1]["status"] if mine else None,
        )
    )


REPORT_COLS = (
    "id, incident_id, task_id, job_id, version, generation_mode, model_id, auth_mode, "
    "fallback_reason, record_sha256, model_calls, input_tokens, output_tokens, cost_usd, "
    "verification AS validation, body, content, created_at"
)


@router.get("/incidents/{incident_id}/report")
def get_incident_report(
    request: Request,
    incident_id: uuid.UUID,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """Latest (or a specific) report version, with its state: ``validated`` (AI
    draft accepted by the validator), ``fallback`` (deterministic), ``pending`` /
    ``generating`` (not ready yet), ``failed`` or ``not_requested``."""
    engine = _engine(request)
    if not _rows(engine, "SELECT id FROM incidents WHERE id=:i", i=incident_id):
        raise HTTPException(status_code=404, detail="incident not found")
    where = "AND version=:v" if version is not None else ""
    rep = _rows(
        engine,
        f"SELECT {REPORT_COLS} FROM reports WHERE incident_id=:i {where} "  # noqa: S608
        "ORDER BY version DESC LIMIT 1",
        i=incident_id,
        v=version,
    )
    jobs = _rows(
        engine,
        "SELECT id, status, reason, attempt, max_attempts, last_error, report_id, created_at "
        "FROM report_jobs WHERE incident_id=:i ORDER BY created_at DESC LIMIT 10",
        i=incident_id,
    )
    if version is not None and not rep:
        raise HTTPException(status_code=404, detail="report version not found")
    active = next((j for j in jobs if j["status"] in ("pending", "generating")), None)
    if rep:
        status = "validated" if rep[0]["generation_mode"] == "ai" else "fallback"
    elif active is not None:
        status = str(active["status"])
    elif jobs:
        status = str(jobs[0]["status"])
    else:
        status = "not_requested"
    return {
        "incident_id": str(incident_id),
        "status": status,
        "newer_report_pending": bool(rep and active),
        "report": rep[0] if rep else None,
        "jobs": jobs,
    }


@router.get("/reports/{report_id}")
def get_report(request: Request, report_id: uuid.UUID) -> dict[str, Any]:
    rep = _rows(
        _engine(request),
        f"SELECT {REPORT_COLS} FROM reports WHERE id=:r",  # noqa: S608
        r=report_id,
    )
    if not rep:
        raise HTTPException(status_code=404, detail="report not found")
    return rep[0]


@router.get("/notifications")
def list_notifications(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    items = _rows(
        _engine(request),
        "SELECT e.id, e.event_type, e.severity, e.incident_id, e.payload, e.created_at, "
        "COALESCE(jsonb_agg(jsonb_build_object('channel', d.channel, 'status', d.status, "
        "'attempt', d.attempt, 'last_error', d.last_error, 'delivered_at', d.delivered_at)) "
        "FILTER (WHERE d.id IS NOT NULL), '[]'::jsonb) AS deliveries "
        "FROM notification_events e LEFT JOIN notification_deliveries d ON d.event_id = e.id "
        "GROUP BY e.id ORDER BY e.created_at DESC LIMIT :n",
        n=limit,
    )
    return {"items": items, "count": len(items)}


@router.get("/system/status")
def get_system_status(request: Request) -> dict[str, Any]:
    return system_status(_engine(request), request.app.state.redis, request.app.state.settings)


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: uuid.UUID) -> dict[str, Any]:
    engine = _engine(request)
    task = _rows(
        engine,
        f"SELECT {TASK_COLS} FROM tasks WHERE id=:t",  # noqa: S608
        t=task_id,
    )
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    checkpoints = _rows(
        engine,
        "SELECT step, state, fencing_token, data, created_at "
        "FROM task_checkpoints WHERE task_id=:t ORDER BY step",
        t=task_id,
    )
    return {"task": task[0], "checkpoints": checkpoints}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:
    settings = request.app.state.settings
    names = StreamNames.from_prefix(settings.stream_prefix)
    api_auth = Metric(
        "sentinel_api_auth_failures_total",
        "counter",
        "Rejected API credentials since this API process started, by credential kind",
        [({"kind": k}, float(v)) for k, v in sorted(AUTH_FAILURES.items())],
    )
    return render([*collect(_engine(request), request.app.state.redis, names), api_auth])
