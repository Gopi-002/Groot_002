"""Remediation stage (workflow steps 6-8), run by the existing worker under the
task's fenced lease. Resumable from durable state at every point:

  policy (step 6) -> [approval wait] -> reserve -> intent -> execute (step 7)
  -> record -> verify (step 8) -> finalize

Idempotency / crash matrix (action id = uuid5(incident), unique per incident):
  * crash before reservation      -> nothing recorded; re-evaluated from scratch
  * crash after reservation       -> 'pending' attempt; policy re-checked, then run
  * crash during execution        -> 'executing': reconcile with the executor's
                                     ledger + container StartedAt; NEVER blind re-issue
  * crash after execution         -> ledger 'completed' -> recorded, then verified
  * crash after recording/verify  -> finalize is idempotent (unique verification)
  * stale worker                  -> fenced DB writes + executor fencing refuse it
Policy is evaluated immediately before any side effect, from current DB state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, text

from app.agent import tasks
from app.agent.stages import StageResult, TransientError
from app.agent.tools import OpsClient
from app.config import Settings
from app.notifications.events import notify_approval_required
from app.persistence.audit import audit
from app.safety import approvals, verification
from app.safety.executor_client import ExecutorClient, ExecutorRefused, ExecutorUnavailable
from app.safety.policy import (
    RESTART,
    Decision,
    EvidenceRef,
    PolicyConfig,
    PolicyInput,
    PolicyResult,
    action_fingerprint,
    action_id_for,
    evaluate,
)

log = logging.getLogger("sentinelops.remediation")

ACTIVE = ("open", "investigating", "remediating", "waiting_approval", "escalated")


@dataclass(frozen=True)
class Ctx:
    task_id: uuid.UUID
    incident_id: uuid.UUID
    incident_status: str
    service_id: uuid.UUID
    service_name: str
    service_environment: str
    investigation_id: uuid.UUID | None
    investigation_status: str | None
    investigation_task_id: uuid.UUID | None
    investigation_incident_id: uuid.UUID | None
    investigation_cost: float | None
    result: dict[str, Any] | None
    action_id: uuid.UUID


def _uuid(v: Any) -> uuid.UUID | None:
    return uuid.UUID(str(v)) if v is not None else None


class RemediationStage:
    def __init__(
        self,
        settings: Settings,
        executor: ExecutorClient,
        ops: OpsClient,
        *,
        clock: Callable[[], float] | None = None,
        wait: Callable[[float], None] | None = None,
    ) -> None:
        self.s = settings
        self.cfg = PolicyConfig.from_settings(settings)
        self.executor = executor
        self.ops = ops
        self.clock = clock
        self.wait = wait

    # --- loading ----------------------------------------------------------------------
    def _ctx(self, conn: Connection, lease: tasks.Lease) -> Ctx:
        row = (
            conn.execute(
                text(
                    "SELECT i.status, i.service_id, s.name, s.environment, v.id AS inv_id, "
                    "v.status AS inv_status, v.task_id AS inv_task, v.incident_id AS inv_incident, "
                    "v.cost_usd, v.result FROM incidents i JOIN services s ON s.id = i.service_id "
                    "LEFT JOIN investigations v ON v.task_id = :t "
                    "WHERE i.id = :i FOR SHARE OF i"
                ),
                {"t": lease.task_id, "i": lease.incident_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise TransientError("incident not found")
        return Ctx(
            lease.task_id,
            lease.incident_id,
            row["status"],
            row["service_id"],
            row["name"],
            row["environment"],
            _uuid(row["inv_id"]),
            row["inv_status"],
            _uuid(row["inv_task"]),
            _uuid(row["inv_incident"]),
            float(row["cost_usd"]) if row["cost_usd"] is not None else None,
            row["result"],
            action_id_for(lease.incident_id),
        )

    def _fingerprint(self, ctx: Ctx) -> str:
        action = (ctx.result or {}).get("proposed_action", {}).get("action")
        return action_fingerprint(
            incident_id=ctx.incident_id,
            task_id=ctx.task_id,
            investigation_id=ctx.investigation_id,
            action=action,
            target_service=self.cfg.target_service,
            action_id=ctx.action_id,
            policy_version=self.cfg.version,
        )

    def _policy_input(
        self, conn: Connection, ctx: Ctx, lease: tasks.Lease, phase: str
    ) -> PolicyInput:
        proposal = (ctx.result or {}).get("proposed_action") or {}
        cited = tuple(uuid.UUID(e) for e in proposal.get("evidence_ids") or [])
        ev = {
            uuid.UUID(str(r.id)): EvidenceRef(uuid.UUID(str(r.incident_id)), r.collected_at)
            for r in conn.execute(
                text(
                    "SELECT id, incident_id, collected_at FROM evidence "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(c) for c in cited]},
            )
        }
        latest = conn.execute(
            text(
                "SELECT checked_at, outcome FROM health_checks WHERE service_id=:s "
                "ORDER BY checked_at DESC LIMIT 1"
            ),
            {"s": ctx.service_id},
        ).first()
        counts = (
            conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE incident_id=:i AND action_id <> :a "
                    "  AND status <> 'pending') AS per_incident, "
                    "count(*) FILTER (WHERE started_at > now() - interval '1 hour' "
                    "  AND action_id <> :a) AS last_hour, "
                    "max(status) FILTER (WHERE action_id = :a) AS this_status "
                    "FROM action_attempts WHERE action_type=:t"
                ),
                {"i": ctx.incident_id, "a": ctx.action_id, "t": RESTART},
            )
            .mappings()
            .one()
        )
        lease_ok = bool(
            conn.execute(
                text(
                    "SELECT 1 FROM tasks WHERE id=:t AND status='running' AND lease_owner=:o "
                    "AND fencing_token=:k AND lease_expires_at > now()"
                ),
                {"t": lease.task_id, "o": lease.owner, "k": lease.token},
            ).first()
        )
        return PolicyInput(
            phase="pre_execution" if phase == "pre_execution" else "proposal",
            now=datetime.now(UTC),
            task_id=ctx.task_id,
            incident_id=ctx.incident_id,
            incident_status=ctx.incident_status,
            service_name=ctx.service_name,
            service_environment=ctx.service_environment,
            investigation_id=ctx.investigation_id,
            investigation_status=ctx.investigation_status,
            investigation_task_id=ctx.investigation_task_id,
            investigation_incident_id=ctx.investigation_incident_id,
            proposed_action=proposal.get("action"),
            proposed_target=proposal.get("target_service"),
            cited_evidence_ids=cited,
            evidence=ev,
            latest_health=(latest.checked_at, latest.outcome) if latest else None,
            restarts_for_incident=int(counts["per_incident"] or 0),
            restarts_last_hour=int(counts["last_hour"] or 0),
            this_action_status=counts["this_status"],
            lease_valid=lease_ok,
            investigation_cost_usd=ctx.investigation_cost,
            approval=approvals.load_state(conn, ctx.action_id, self.s.approval_signing_key),
            fingerprint=self._fingerprint(ctx),
            executor_configured=self.executor.configured,
        )

    def _record_decision(
        self, conn: Connection, lease: tasks.Lease, ctx: Ctx, inp: PolicyInput, res: PolicyResult
    ) -> uuid.UUID:
        inputs = {
            "incident_status": inp.incident_status,
            "service": inp.service_name,
            "proposed_action": inp.proposed_action,
            "proposed_target": inp.proposed_target,
            "cited_evidence_ids": [str(e) for e in inp.cited_evidence_ids],
            "latest_health": [inp.latest_health[0].isoformat(), inp.latest_health[1]]
            if inp.latest_health
            else None,
            "restarts_for_incident": inp.restarts_for_incident,
            "restarts_last_hour": inp.restarts_last_hour,
            "this_action_status": inp.this_action_status,
            "lease_valid": inp.lease_valid,
            "approval_id": str(inp.approval.id) if inp.approval else None,
            "approval_status": inp.approval.status if inp.approval else None,
            "action_id": str(ctx.action_id),
            "autonomy": self.cfg.auto_enabled,
            "environment": self.cfg.environment,
            "remediation_environment": self.cfg.remediation_environment,
        }
        row = conn.execute(
            text(
                "INSERT INTO policy_decisions (task_id, incident_id, investigation_id, phase, "
                "proposed_action, decision, rule_ids, reasons, inputs, policy_version, "
                "action_fingerprint, fencing_token) VALUES (:t, :i, :v, :ph, :pa, :d, "
                "CAST(:r AS text[]), CAST(:re AS jsonb), CAST(:in AS jsonb), :pv, :fp, :ft) "
                "ON CONFLICT (task_id, phase, fencing_token) DO NOTHING RETURNING id"
            ),
            {
                "t": ctx.task_id,
                "i": ctx.incident_id,
                "v": ctx.investigation_id,
                "ph": inp.phase,
                "pa": inp.proposed_action,
                "d": res.decision.value,
                "r": list(res.rule_ids),
                "re": json.dumps(list(res.reasons)),
                "in": json.dumps(inputs),
                "pv": self.cfg.version,
                "fp": inp.fingerprint,
                "ft": lease.token,
            },
        ).scalar_one_or_none()
        if row is None:
            row = conn.execute(
                text(
                    "SELECT id FROM policy_decisions WHERE task_id=:t AND phase=:ph "
                    "AND fencing_token=:ft"
                ),
                {"t": ctx.task_id, "ph": inp.phase, "ft": lease.token},
            ).scalar_one()
        decision_id = uuid.UUID(str(row))
        tasks.checkpoint(
            conn,
            lease,
            6,
            f"policy_{res.decision.value.lower()}",
            {
                "decision_id": str(decision_id),
                "rule_ids": list(res.rule_ids),
                "phase": inp.phase,
                "policy_version": self.cfg.version,
            },
        )
        audit(
            conn,
            actor_type="system",
            actor_id="policy",
            action="policy_decision",
            entity_type="incident",
            entity_id=ctx.incident_id,
            details={
                "decision": res.decision.value,
                "rule_ids": list(res.rule_ids),
                "reasons": list(res.reasons),
                "phase": inp.phase,
                "task_id": str(ctx.task_id),
                "decision_id": str(decision_id),
            },
        )
        return decision_id

    # --- terminal helpers -----------------------------------------------------------------
    def _end(
        self,
        conn: Connection,
        lease: tasks.Lease,
        status: str,
        outcome: str,
        error: str | None = None,
    ) -> StageResult:
        tasks.transition(conn, lease, status, outcome=outcome, error=error)
        log.info(
            "remediation stage finished",
            extra={"task_id": str(lease.task_id), "status": status, "outcome": outcome},
        )
        return StageResult(status, outcome)

    # --- stage entry ------------------------------------------------------------------------
    def run(self, engine: Engine, lease: tasks.Lease) -> StageResult:
        with engine.begin() as conn:
            ctx = self._ctx(conn, lease)
            attempt = (
                conn.execute(
                    text(
                        "SELECT id, status, pre_state, post_state, started_at FROM action_attempts "
                        "WHERE action_id=:a"
                    ),
                    {"a": ctx.action_id},
                )
                .mappings()
                .first()
            )
        if attempt is not None and attempt["status"] == "executing":
            return self._reconcile(engine, lease, ctx, dict(attempt))
        if attempt is not None and attempt["status"] in ("succeeded", "reconciled"):
            return self._verify_and_finish(
                engine, lease, ctx, uuid.UUID(str(attempt["id"])), attempt["post_state"] or {}
            )
        if attempt is not None and attempt["status"] in ("failed", "unknown"):
            with engine.begin() as conn:
                return self._end(conn, lease, "escalated", f"restart_{attempt['status']}")
        return self._decide(engine, lease, ctx)

    def _decide(self, engine: Engine, lease: tasks.Lease, ctx: Ctx) -> StageResult:
        with engine.begin() as conn:
            if ctx.incident_status not in ACTIVE:
                audit(
                    conn,
                    actor_type="system",
                    actor_id="policy",
                    action="action_skipped",
                    entity_type="incident",
                    entity_id=ctx.incident_id,
                    details={"reason": f"incident {ctx.incident_status}"},
                )
                return self._end(conn, lease, "resolved", "incident_no_longer_active")
            # 'proposal' here; the last-moment re-check right before the side effect
            # is recorded separately as 'pre_execution' (both are persisted).
            phase = "proposal"
            inp = self._policy_input(conn, ctx, lease, phase)
            res = evaluate(inp, self.cfg)
            decision_id = self._record_decision(conn, lease, ctx, inp, res)

            if res.decision is Decision.DENY:
                return self._denied(conn, lease, ctx, res)
            if res.decision is Decision.REQUIRE_APPROVAL:
                return self._await_approval(conn, lease, ctx, inp, res, decision_id)
            if res.effect == "escalate":
                return self._end(conn, lease, "escalated", "escalated_by_proposal")
            if res.effect == "none":
                if inp.latest_health and inp.latest_health[1] == "healthy":
                    return self._end(conn, lease, "resolved", "no_action_service_healthy")
                return self._end(conn, lease, "escalated", "no_action_but_service_failing")
            # ALLOW restart: reserve the unique action (idempotent).
            conn.execute(
                text(
                    "INSERT INTO action_attempts (action_id, task_id, incident_id, "
                    "target_service_id, action_type, status, fencing_token, action_fingerprint, "
                    "policy_decision_id, approval_id) VALUES (:a, :t, :i, :s, :at, 'pending', "
                    ":ft, :fp, :pd, :ap) ON CONFLICT (action_id) DO NOTHING"
                ),
                {
                    "a": ctx.action_id,
                    "t": ctx.task_id,
                    "i": ctx.incident_id,
                    "s": ctx.service_id,
                    "at": RESTART,
                    "ft": lease.token,
                    "fp": inp.fingerprint,
                    "pd": decision_id,
                    "ap": inp.approval.id if inp.approval else None,
                },
            )
            tasks.checkpoint(
                conn,
                lease,
                7,
                "action_reserved",
                {"action_id": str(ctx.action_id), "decision_id": str(decision_id)},
            )
        return self._execute(engine, lease, ctx, inp.fingerprint)

    def _denied(
        self, conn: Connection, lease: tasks.Lease, ctx: Ctx, res: PolicyResult
    ) -> StageResult:
        rules = set(res.rule_ids)
        audit(
            conn,
            actor_type="system",
            actor_id="policy",
            action="action_skipped",
            entity_type="incident",
            entity_id=ctx.incident_id,
            details={"rule_ids": sorted(rules), "reasons": list(res.reasons)},
        )
        if rules <= {"HLT-2"}:
            # The app recovered before remediation: do not restart a healthy app.
            # Hand the incident back to the monitor, whose healthy-check hysteresis
            # (not this stage) decides whether it is really recovered.
            conn.execute(
                text(
                    "UPDATE incidents SET status='investigating' WHERE id=:i "
                    "AND status IN ('waiting_approval','remediating')"
                ),
                {"i": ctx.incident_id},
            )
            return self._end(conn, lease, "resolved", "service_recovered_before_action")
        if "APR-3" in rules:
            a = approvals.load_state(conn, ctx.action_id, None)
            if a is not None:
                approvals.expire_if_due(conn, a.id)
            return self._end(conn, lease, "escalated", "approval_expired")
        if "APR-7" in rules:
            return self._end(conn, lease, "escalated", "approval_rejected")
        return self._end(conn, lease, "escalated", "policy_denied", error=", ".join(sorted(rules)))

    def _await_approval(
        self,
        conn: Connection,
        lease: tasks.Lease,
        ctx: Ctx,
        inp: PolicyInput,
        res: PolicyResult,
        decision_id: uuid.UUID,
    ) -> StageResult:
        risk = (
            "Restarts the demo-app container: brief unavailability of the demo "
            "application; no data loss expected."
        )
        approval_id = approvals.create_approval(
            conn,
            task_id=ctx.task_id,
            incident_id=ctx.incident_id,
            action_id=ctx.action_id,
            action=RESTART,
            target_service=self.cfg.target_service,
            fingerprint=inp.fingerprint,
            policy_version=self.cfg.version,
            risk=risk,
            policy_decision_id=decision_id,
            ttl_seconds=self.s.approval_ttl_seconds,
        )
        row = conn.execute(
            text(
                "SELECT extract(epoch FROM expires_at - now()) AS left, expires_at "
                "FROM approvals WHERE id=:a"
            ),
            {"a": approval_id},
        ).one()
        expires = row.left
        # Durable operator notification (outbox; delivered by the notifier). It carries
        # no fingerprint or token and is never itself an approval.
        notify_approval_required(
            conn,
            approval_id=approval_id,
            incident_id=ctx.incident_id,
            task_id=ctx.task_id,
            action=RESTART,
            target_service=self.cfg.target_service,
            risk=risk,
            expires_at=row.expires_at,
        )
        conn.execute(
            text(
                "UPDATE incidents SET status='waiting_approval' WHERE id=:i "
                "AND status IN ('open','investigating')"
            ),
            {"i": ctx.incident_id},
        )
        # Park WITHOUT holding the worker; re-dispatched at expiry (fail closed) or
        # immediately after an operator decision. Waiting is not a failed attempt.
        tasks.transition(
            conn,
            lease,
            "waiting_approval",
            outcome="approval_required",
            next_attempt_in=max(float(expires or 0), 0.0) + 1.0,
            refund_attempt=True,
        )
        log.warning(
            "APPROVAL REQUIRED (notification)",
            extra={
                "approval_id": str(approval_id),
                "incident_id": str(ctx.incident_id),
                "action": RESTART,
                "target": self.cfg.target_service,
                "action_fingerprint": inp.fingerprint,
            },
        )
        return StageResult("waiting_approval", "approval_required")

    # --- execution ----------------------------------------------------------------------------
    def _mark_executing(
        self, engine: Engine, lease: tasks.Lease, ctx: Ctx, pre: dict[str, Any]
    ) -> uuid.UUID:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "UPDATE action_attempts SET status='executing', started_at=now(), "
                    "fencing_token=:ft, pre_state=CAST(:pre AS jsonb) "
                    "WHERE action_id=:a AND status='pending' AND EXISTS ("
                    "  SELECT 1 FROM tasks WHERE id=:t AND status='running' AND lease_owner=:o "
                    "  AND fencing_token=:ft AND lease_expires_at > now()) RETURNING id"
                ),
                {
                    "ft": lease.token,
                    "pre": json.dumps(pre),
                    "a": ctx.action_id,
                    "t": ctx.task_id,
                    "o": lease.owner,
                },
            ).scalar_one_or_none()
            if row is None:
                raise tasks.LeaseLost(f"cannot record execution intent for {ctx.action_id}")
            conn.execute(
                text(
                    "UPDATE incidents SET status='remediating' WHERE id=:i "
                    "AND status IN ('open','investigating','waiting_approval')"
                ),
                {"i": ctx.incident_id},
            )
            tasks.checkpoint(
                conn,
                lease,
                7,
                "action_executing",
                {"action_id": str(ctx.action_id), "pre_state": pre},
            )
            audit(
                conn,
                actor_type="system",
                actor_id=lease.owner,
                action="action_execution_intent",
                entity_type="action_attempt",
                entity_id=uuid.UUID(str(row)),
                details={
                    "action": RESTART,
                    "action_id": str(ctx.action_id),
                    "target": self.cfg.target_service,
                    "fencing_token": lease.token,
                },
            )
            return uuid.UUID(str(row))

    def _record(
        self,
        engine: Engine,
        lease: tasks.Lease,
        ctx: Ctx,
        status: str,
        post: dict[str, Any] | None,
        error: str | None,
        how: str,
    ) -> uuid.UUID:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "UPDATE action_attempts SET status=:st, completed_at=now(), "
                    "post_state=CAST(:post AS jsonb), error=:e, result=CAST(:r AS jsonb) "
                    "WHERE action_id=:a AND status IN ('executing','pending') RETURNING id"
                ),
                {
                    "st": status,
                    "post": json.dumps(post) if post else None,
                    "e": error,
                    "r": json.dumps({"recorded_via": how}),
                    "a": ctx.action_id,
                },
            ).scalar_one()
            tasks.checkpoint(
                conn,
                lease,
                7,
                f"action_{status}",
                {
                    "action_id": str(ctx.action_id),
                    "post_state": post,
                    "error": error,
                    "recorded_via": how,
                },
            )
            audit(
                conn,
                actor_type="system",
                actor_id="executor",
                action=f"action_{status}",
                entity_type="action_attempt",
                entity_id=uuid.UUID(str(row)),
                details={
                    "action_id": str(ctx.action_id),
                    "post_state": post,
                    "error": error,
                    "recorded_via": how,
                },
            )
            return uuid.UUID(str(row))

    def _execute(
        self, engine: Engine, lease: tasks.Lease, ctx: Ctx, fingerprint: str
    ) -> StageResult:
        # Last-moment recheck from CURRENT state (race: app recovered, incident closed,
        # approval expired, lease lost...). Recorded as the pre_execution decision.
        with engine.begin() as conn:
            ctx = self._ctx(conn, lease)
            inp = self._policy_input(conn, ctx, lease, "pre_execution")
            res = evaluate(inp, self.cfg)
            self._record_decision(conn, lease, ctx, inp, res)
            if res.decision is not Decision.ALLOW or res.effect != "restart":
                conn.execute(
                    text("DELETE FROM action_attempts WHERE action_id=:a AND status='pending'"),
                    {"a": ctx.action_id},
                )
                return self._denied(conn, lease, ctx, res)
            fingerprint = inp.fingerprint
        try:
            pre = self.executor.state()
        except ExecutorUnavailable as exc:
            raise TransientError(f"executor unavailable before execution: {exc}") from exc
        self._mark_executing(engine, lease, ctx, pre)
        try:
            out = self.executor.restart(ctx.action_id, lease.token, fingerprint)
        except ExecutorUnavailable as exc:
            # Outcome unknown: leave 'executing'; the next attempt reconciles.
            raise TransientError(f"restart outcome unknown: {exc}") from exc
        except ExecutorRefused as exc:
            if exc.reason == "stale":
                raise tasks.LeaseLost(f"executor refused stale fencing token: {exc}") from exc
            if exc.reason == "rate_limited":
                self._record(engine, lease, ctx, "failed", None, "executor rate limit", "refused")
                with engine.begin() as conn:
                    return self._end(conn, lease, "escalated", "restart_rate_limited")
            raise TransientError(f"executor refused ({exc.reason}); will reconcile") from exc
        return self._after_executor(engine, lease, ctx, out, "executor_response")

    def _after_executor(
        self, engine: Engine, lease: tasks.Lease, ctx: Ctx, out: dict[str, Any], how: str
    ) -> StageResult:
        if out.get("status") == "completed":
            attempt_id = self._record(
                engine, lease, ctx, "succeeded", out.get("post_state"), None, how
            )
            return self._verify_and_finish(
                engine, lease, ctx, attempt_id, out.get("post_state") or {}
            )
        self._record(
            engine,
            lease,
            ctx,
            "failed",
            out.get("post_state"),
            str(out.get("error") or "restart failed"),
            how,
        )
        with engine.begin() as conn:
            return self._end(
                conn, lease, "escalated", "restart_failed", error=str(out.get("error"))[:300]
            )

    def _reconcile(
        self, engine: Engine, lease: tasks.Lease, ctx: Ctx, attempt: dict[str, Any]
    ) -> StageResult:
        """A previous executor of this task crashed after recording intent. Establish
        what actually happened before doing anything; never blindly re-issue."""
        try:
            ledger = self.executor.get_action(ctx.action_id)
        except ExecutorUnavailable as exc:
            raise TransientError(f"cannot reconcile, executor unavailable: {exc}") from exc
        with engine.begin() as conn:
            audit(
                conn,
                actor_type="system",
                actor_id=lease.owner,
                action="action_reconciling",
                entity_type="action_attempt",
                entity_id=uuid.UUID(str(attempt["id"])),
                details={"ledger_status": ledger.get("status") if ledger else None},
            )
        if ledger is not None and ledger.get("status") in ("completed", "failed"):
            return self._after_executor(engine, lease, ctx, ledger, "reconciled_from_ledger")
        pre = attempt.get("pre_state") or {}
        if ledger is None:
            # The executor never received the request: nothing ran. Return the
            # reservation to 'pending' (fenced) and go through policy again.
            with engine.begin() as conn:
                n = conn.execute(
                    text(
                        "UPDATE action_attempts SET status='pending', started_at=NULL "
                        "WHERE action_id=:a AND status='executing' AND EXISTS ("
                        "  SELECT 1 FROM tasks WHERE id=:t AND status='running' AND lease_owner=:o "
                        "  AND fencing_token=:ft AND lease_expires_at > now())"
                    ),
                    {"a": ctx.action_id, "t": ctx.task_id, "o": lease.owner, "ft": lease.token},
                ).rowcount
                if n != 1:
                    raise tasks.LeaseLost("reconcile lost ownership")
            return self._execute(engine, lease, ctx, self._fingerprint(ctx))
        # Ledger says 'started': the executor itself was interrupted mid-action.
        try:
            now_state = self.executor.state()
        except ExecutorUnavailable as exc:
            raise TransientError(f"cannot inspect target: {exc}") from exc
        if now_state.get("started_at") and now_state.get("started_at") != pre.get("started_at"):
            attempt_id = self._record(
                engine, lease, ctx, "reconciled", now_state, None, "reconciled_from_container_state"
            )
            return self._verify_and_finish(engine, lease, ctx, attempt_id, now_state)
        self._record(
            engine,
            lease,
            ctx,
            "unknown",
            now_state,
            "executor interrupted; restart not observed",
            "reconcile_unknown",
        )
        with engine.begin() as conn:
            return self._end(conn, lease, "escalated", "action_outcome_unknown")

    # --- verification ---------------------------------------------------------------------------
    def _probe(self) -> dict[str, Any] | None:
        r = self.ops.get("/v1/target/probe")
        data = r.get("data")
        return dict(data) if r.get("status") == "ok" and isinstance(data, dict) else None

    def _new_logs(self, since: datetime | None) -> list[dict[str, Any]] | None:
        params: dict[str, Any] = {"tail": 200}
        if since is not None:
            params["since"] = int(since.timestamp()) + 1
        r = self.ops.get("/v1/target/logs", **params)
        data = r.get("data")
        return list(data) if r.get("status") == "ok" and isinstance(data, list) else None

    def _verify_and_finish(
        self,
        engine: Engine,
        lease: tasks.Lease,
        ctx: Ctx,
        attempt_id: uuid.UUID,
        post: dict[str, Any],
    ) -> StageResult:
        with engine.connect() as conn:
            done = conn.execute(
                text("SELECT status, reason FROM verifications WHERE action_attempt_id=:a"),
                {"a": attempt_id},
            ).first()
        if done is None:
            criteria = verification.Criteria.from_settings(self.s)
            started = datetime.now(UTC)
            since = verification.parse_docker_time(post.get("started_at")) or started
            kwargs: dict[str, Any] = {}
            if self.clock:
                kwargs["clock"] = self.clock
            if self.wait:
                kwargs["wait"] = self.wait
            outcome = verification.verify(
                self._probe, lambda: self._new_logs(since), criteria, **kwargs
            )
            body = json.dumps(
                {
                    "criteria": criteria.to_json(),
                    "passed": outcome.passed,
                    "reason": outcome.reason,
                    "observations": outcome.observations,
                },
                sort_keys=True,
                default=str,
            )
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO verifications (action_attempt_id, task_id, incident_id, "
                        "status, reason, criteria, observations, started_at) "
                        "VALUES (:a, :t, :i, :st, :r, "
                        "CAST(:c AS jsonb), CAST(:o AS jsonb), :sa) "
                        "ON CONFLICT (action_attempt_id) DO NOTHING"
                    ),
                    {
                        "a": attempt_id,
                        "t": ctx.task_id,
                        "i": ctx.incident_id,
                        "st": "passed" if outcome.passed else "failed",
                        "r": outcome.reason,
                        "c": json.dumps(criteria.to_json()),
                        "o": json.dumps(outcome.observations, default=str),
                        "sa": started,
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO evidence (incident_id, task_id, source, tool_name, content, "
                        "content_sha256) VALUES (:i, :t, 'verification', 'recovery_verification', "
                        "CAST(:c AS jsonb), :h) ON CONFLICT (incident_id, source, content_sha256) "
                        "DO NOTHING"
                    ),
                    {
                        "i": ctx.incident_id,
                        "t": ctx.task_id,
                        "c": body,
                        "h": hashlib.sha256(body.encode()).hexdigest(),
                    },
                )
                tasks.checkpoint(
                    conn,
                    lease,
                    8,
                    "verified" if outcome.passed else "verification_failed",
                    {"reason": outcome.reason},
                )
            passed, reason = outcome.passed, outcome.reason
        else:
            passed, reason = done.status == "passed", done.reason
        with engine.begin() as conn:
            if passed:
                conn.execute(
                    text(
                        "UPDATE incidents SET status='resolved', resolved_at=now(), "
                        "resolution='remediated' WHERE id=:i AND status IN "
                        "('open','investigating','remediating','waiting_approval')"
                    ),
                    {"i": ctx.incident_id},
                )
                audit(
                    conn,
                    actor_type="system",
                    actor_id="verifier",
                    action="recovery_verified",
                    entity_type="incident",
                    entity_id=ctx.incident_id,
                    details={"action_attempt_id": str(attempt_id), "reason": reason},
                )
                return self._end(conn, lease, "resolved", "recovery_verified")
            audit(
                conn,
                actor_type="system",
                actor_id="verifier",
                action="recovery_failed",
                entity_type="incident",
                entity_id=ctx.incident_id,
                details={"action_attempt_id": str(attempt_id), "reason": reason},
            )
            # One restart max: no further automated action; a human takes over and
            # the incident stays open (escalated).
            return self._end(conn, lease, "escalated", "recovery_failed", error=reason[:300])
