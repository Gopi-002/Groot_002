"""Authenticated human approval API.

Auth: ``Authorization: Bearer <operator token>`` (tokens are per-operator,
stored only as SHA-256; roles viewer/approver). Deciding requires the
``approver`` role.

CSRF: state changes are authorized ONLY by the Authorization header - never by
cookies or ambient browser credentials - so a cross-site page cannot make an
authorized request. Defence in depth: POSTs must be ``application/json``
(no simple-form CSRF) and requests declaring a cross-site origin via
``Sec-Fetch-Site: cross-site`` or a foreign ``Origin`` are refused.

Replay/concurrency: a decision is one conditional UPDATE bound to the approval's
action fingerprint and expiry; a replayed or concurrent second decision gets 409.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text

from app.api.auth import count_auth_failure
from app.safety.approvals import DecideOutcome, Operator, authenticate_operator, decide

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])

APPROVAL_COLS = (
    "id, task_id, incident_id, action_id, proposed_action, target_service, "
    "action_fingerprint, policy_version, risk, status, requested_at, expires_at, "
    "decided_at, decided_by, reason"
)


def _engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def current_operator(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Operator:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        count_auth_failure("operator")
        raise HTTPException(
            status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    with _engine(request).connect() as conn:
        op = authenticate_operator(conn, token)
    if op is None:
        count_auth_failure("operator")
        raise HTTPException(
            status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    return op


def require_approver(op: Annotated[Operator, Depends(current_operator)]) -> Operator:
    if op.role != "approver":
        raise HTTPException(status_code=403, detail="approver role required")
    return op


def csrf_guard(
    request: Request,
    content_type: Annotated[str | None, Header()] = None,
    origin: Annotated[str | None, Header()] = None,
    sec_fetch_site: Annotated[str | None, Header()] = None,
) -> None:
    if not (content_type or "").lower().startswith("application/json"):
        raise HTTPException(status_code=415, detail="application/json required")
    if (sec_fetch_site or "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site request refused")
    allowed: tuple[str, ...] = request.app.state.allowed_origins
    if origin is not None and origin not in allowed:
        raise HTTPException(status_code=403, detail="origin not allowed")


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=500)


def _rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


@router.get("")
def list_approvals(
    request: Request, _op: Annotated[Operator, Depends(current_operator)], status: str = "pending"
) -> dict[str, Any]:
    if status not in ("pending", "approved", "rejected", "expired"):
        raise HTTPException(status_code=422, detail="unknown status")
    items = _rows(
        _engine(request),
        f"SELECT {APPROVAL_COLS} FROM approvals WHERE status=:s "  # noqa: S608
        "ORDER BY requested_at DESC LIMIT 100",
        s=status,
    )
    return {"items": items, "count": len(items)}


@router.get("/{approval_id}")
def get_approval(
    request: Request, approval_id: uuid.UUID, _op: Annotated[Operator, Depends(current_operator)]
) -> dict[str, Any]:
    rows = _rows(
        _engine(request),
        f"SELECT {APPROVAL_COLS} FROM approvals "  # noqa: S608
        "WHERE id=:i",
        i=approval_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="approval not found")
    return rows[0]


def _decide(
    request: Request, approval_id: uuid.UUID, op: Operator, body: DecisionBody, approve: bool
) -> dict[str, Any]:
    key = request.app.state.settings.approval_signing_key
    if key is None:
        raise HTTPException(status_code=503, detail="approval signing not configured")
    with _engine(request).begin() as conn:
        outcome = decide(
            conn,
            approval_id=approval_id,
            operator=op,
            approve=approve,
            fingerprint=body.action_fingerprint,
            reason=body.reason,
            key=key,
        )
    if outcome is DecideOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="approval not found")
    if outcome is not DecideOutcome.DECIDED:
        raise HTTPException(status_code=409, detail=outcome.value)
    return {
        "approval_id": str(approval_id),
        "status": "approved" if approve else "rejected",
        "decided_by": op.name,
    }


@router.post("/{approval_id}/approve", dependencies=[Depends(csrf_guard)])
def approve(
    request: Request,
    approval_id: uuid.UUID,
    body: DecisionBody,
    op: Annotated[Operator, Depends(require_approver)],
) -> dict[str, Any]:
    return _decide(request, approval_id, op, body, approve=True)


@router.post("/{approval_id}/reject", dependencies=[Depends(csrf_guard)])
def reject(
    request: Request,
    approval_id: uuid.UUID,
    body: DecisionBody,
    op: Annotated[Operator, Depends(require_approver)],
) -> dict[str, Any]:
    return _decide(request, approval_id, op, body, approve=False)
