"""Human approvals bound to one exact action, and operator identities.

* An approval row carries the action id, action fingerprint, trusted target and
  policy version it authorizes. It can authorize nothing else.
* Decisions are made by an authenticated operator with the ``approver`` role,
  through a single conditional UPDATE (``status='pending' AND expires_at > now()
  AND fingerprint matches``): replays, late decisions and concurrent decisions
  all fail with a conflict; exactly one decision can win.
* The API signs each decision with HMAC-SHA256 (``approval_signing_key``); the
  worker verifies the signature before acting, so a row inserted or edited
  directly in the database ("forged approval") does not authorize anything.
* Silence is never approval: an expired pending approval is marked ``expired``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import SecretStr
from sqlalchemy import Connection, text

from app.persistence.audit import audit
from app.safety.policy import ApprovalState


class DecideOutcome(StrEnum):
    DECIDED = "decided"
    NOT_FOUND = "not_found"
    ALREADY_DECIDED = "already_decided"
    EXPIRED = "expired"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"


@dataclass(frozen=True)
class Operator:
    id: uuid.UUID
    name: str
    role: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_operator(conn: Connection, name: str, role: str, created_by: str) -> str:
    """Create an operator; returns the plaintext token ONCE (only its hash is stored)."""
    token = "sop_" + secrets.token_urlsafe(32)
    row = conn.execute(
        text("INSERT INTO operators (name, role, token_sha256) VALUES (:n, :r, :h) RETURNING id"),
        {"n": name, "r": role, "h": hash_token(token)},
    ).scalar_one()
    audit(
        conn,
        actor_type="human",
        actor_id=created_by,
        action="operator_created",
        entity_type="operator",
        entity_id=uuid.UUID(str(row)),
        details={"name": name, "role": role},
    )
    return token


def disable_operator(conn: Connection, name: str, by: str) -> bool:
    n = conn.execute(
        text("UPDATE operators SET disabled_at = now() WHERE name=:n AND disabled_at IS NULL"),
        {"n": name},
    ).rowcount
    if n:
        audit(
            conn,
            actor_type="human",
            actor_id=by,
            action="operator_disabled",
            entity_type="operator",
            entity_id=None,
            details={"name": name},
        )
    return bool(n)


def authenticate_operator(conn: Connection, token: str) -> Operator | None:
    row = conn.execute(
        text("SELECT id, name, role FROM operators WHERE token_sha256=:h AND disabled_at IS NULL"),
        {"h": hash_token(token)},
    ).first()
    return Operator(uuid.UUID(str(row.id)), row.name, row.role) if row else None


def _signing_payload(
    approval_id: uuid.UUID, fingerprint: str, decision: str, decided_by: str, decided_at: datetime
) -> bytes:
    return "|".join(
        [str(approval_id), fingerprint, decision, decided_by, decided_at.isoformat()]
    ).encode()


def sign_decision(
    key: SecretStr,
    approval_id: uuid.UUID,
    fingerprint: str,
    decision: str,
    decided_by: str,
    decided_at: datetime,
) -> str:
    return hmac.new(
        key.get_secret_value().encode(),
        _signing_payload(approval_id, fingerprint, decision, decided_by, decided_at),
        hashlib.sha256,
    ).hexdigest()


def verify_decision(
    key: SecretStr | None,
    approval_id: uuid.UUID,
    fingerprint: str | None,
    decision: str,
    decided_by: str | None,
    decided_at: datetime | None,
    signature: str | None,
) -> bool:
    if key is None or not (fingerprint and decided_by and decided_at and signature):
        return False
    expected = sign_decision(key, approval_id, fingerprint, decision, decided_by, decided_at)
    return hmac.compare_digest(expected, signature)


def create_approval(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    incident_id: uuid.UUID,
    action_id: uuid.UUID,
    action: str,
    target_service: str,
    fingerprint: str,
    policy_version: str,
    risk: str,
    policy_decision_id: uuid.UUID,
    ttl_seconds: float,
) -> uuid.UUID:
    """Idempotent: an existing pending approval for this action is reused."""
    existing = conn.execute(
        text("SELECT id FROM approvals WHERE action_id=:a AND status='pending'"),
        {"a": action_id},
    ).scalar_one_or_none()
    if existing is not None:
        return uuid.UUID(str(existing))
    row = conn.execute(
        text(
            "INSERT INTO approvals (task_id, incident_id, action_id, proposed_action, "
            "target_service, action_fingerprint, policy_version, risk, policy_decision_id, "
            "expires_at) VALUES (:t, :i, :a, :pa, :ts, :fp, :pv, :r, :pd, "
            "now() + make_interval(secs => :ttl)) RETURNING id"
        ),
        {
            "t": task_id,
            "i": incident_id,
            "a": action_id,
            "pa": action,
            "ts": target_service,
            "fp": fingerprint,
            "pv": policy_version,
            "r": risk,
            "pd": policy_decision_id,
            "ttl": ttl_seconds,
        },
    ).scalar_one()
    approval_id = uuid.UUID(str(row))
    audit(
        conn,
        actor_type="system",
        actor_id="policy",
        action="approval_requested",
        entity_type="approval",
        entity_id=approval_id,
        details={
            "task_id": str(task_id),
            "incident_id": str(incident_id),
            "action": action,
            "target_service": target_service,
            "action_fingerprint": fingerprint,
            "risk": risk,
        },
    )
    return approval_id


def decide(
    conn: Connection,
    *,
    approval_id: uuid.UUID,
    operator: Operator,
    approve: bool,
    fingerprint: str,
    reason: str | None,
    key: SecretStr,
) -> DecideOutcome:
    """One conditional UPDATE decides; everything else is a conflict. On success
    the paused task is scheduled to resume through the normal outbox path."""
    decision = "approved" if approve else "rejected"
    row = conn.execute(
        text(
            "UPDATE approvals SET status=:d, decided_by=:by, decided_at=now(), reason=:r "
            "WHERE id=:id AND status='pending' AND expires_at > now() "
            "AND action_fingerprint = :fp "
            "RETURNING task_id, decided_at, action_fingerprint"
        ),
        {
            "d": decision,
            "by": operator.name,
            "r": (reason or "")[:500],
            "id": approval_id,
            "fp": fingerprint,
        },
    ).first()
    if row is None:
        cur = conn.execute(
            text(
                "SELECT status, expires_at > now() AS live, action_fingerprint "
                "FROM approvals WHERE id=:id"
            ),
            {"id": approval_id},
        ).first()
        if cur is None:
            return DecideOutcome.NOT_FOUND
        if cur.status != "pending":
            return DecideOutcome.ALREADY_DECIDED
        if not cur.live:
            return DecideOutcome.EXPIRED
        return DecideOutcome.FINGERPRINT_MISMATCH
    sig = sign_decision(
        key, approval_id, row.action_fingerprint, decision, operator.name, row.decided_at
    )
    conn.execute(
        text("UPDATE approvals SET decision_signature=:s WHERE id=:id"),
        {"s": sig, "id": approval_id},
    )
    task_id = uuid.UUID(str(row.task_id))
    conn.execute(
        text("UPDATE tasks SET next_attempt_at = now() WHERE id=:t AND status='waiting_approval'"),
        {"t": task_id},
    )
    conn.execute(
        text(
            "INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, dedup_key, "
            "payload) VALUES ('task', :t, 'task.dispatch', :k, "
            "jsonb_build_object('task_id', CAST(:t AS text), 'reason', 'approval_decided')) "
            "ON CONFLICT (dedup_key) DO NOTHING"
        ),
        {"t": task_id, "k": f"task:{task_id}:approval:{approval_id}"},
    )
    audit(
        conn,
        actor_type="human",
        actor_id=operator.name,
        action=f"approval_{decision}",
        entity_type="approval",
        entity_id=approval_id,
        details={
            "task_id": str(task_id),
            "operator_id": str(operator.id),
            "action_fingerprint": row.action_fingerprint,
            "reason": reason,
        },
    )
    return DecideOutcome.DECIDED


def expire_if_due(conn: Connection, approval_id: uuid.UUID) -> bool:
    n = conn.execute(
        text(
            "UPDATE approvals SET status='expired' WHERE id=:id AND status='pending' "
            "AND expires_at <= now()"
        ),
        {"id": approval_id},
    ).rowcount
    if n:
        audit(
            conn,
            actor_type="system",
            actor_id="policy",
            action="approval_expired",
            entity_type="approval",
            entity_id=approval_id,
            details={},
        )
    return bool(n)


def load_state(
    conn: Connection, action_id: uuid.UUID, key: SecretStr | None
) -> ApprovalState | None:
    """Latest approval for this action, with its signature and decider verified."""
    row = conn.execute(
        text(
            "SELECT a.id, a.status, a.action_fingerprint, a.expires_at, a.decided_at, "
            "a.decided_by, a.decision_signature, "
            "EXISTS (SELECT 1 FROM operators o WHERE o.name = a.decided_by "
            "        AND o.role = 'approver' AND o.disabled_at IS NULL) AS approver "
            "FROM approvals a WHERE a.action_id=:a ORDER BY a.requested_at DESC LIMIT 1"
        ),
        {"a": action_id},
    ).first()
    if row is None:
        return None
    aid = uuid.UUID(str(row.id))
    sig_ok = row.status in ("approved", "rejected") and verify_decision(
        key,
        aid,
        row.action_fingerprint,
        row.status,
        row.decided_by,
        row.decided_at,
        row.decision_signature,
    )
    return ApprovalState(
        id=aid,
        status=row.status,
        action_fingerprint=row.action_fingerprint,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        signature_valid=sig_ok,
        decider_is_approver=bool(row.approver),
    )
