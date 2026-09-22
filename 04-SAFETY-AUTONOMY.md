# Phase 4 — Policy, approval, execution and recovery verification

## Objective
Implement workflow steps 6–8. One isolated demo restart only. Read prior phases and master.

## Exact tasks
1. Deterministic policy engine accepts validated proposal and authoritative DB state; decisions ALLOW, REQUIRE_APPROVAL, DENY with rule IDs. Default deny. Only `RESTART_DEMO_APP` on exact isolated target; require active incident, evidence, one restart per incident, configured preauthorization, current lease and cost/action limits. Model cannot edit policy.
2. Authenticated admin approval endpoint/dashboard with CSRF protections where applicable, roles, action fingerprint, target, risk and expiry. Persist WAITING_APPROVAL without holding worker. Denial and expiry fail closed; notification failure does not approve. If no approval needed, proceed autonomously.
3. Restricted executor service with narrowly scoped control of demo app; no Docker socket in agent. Use action ID + unique DB constraint, reservation/lease, pre/post state and reconciliation before retries. If external restart outcome unknown, inspect actual state; do not blindly reissue. Audit action intent, authorization, execution, result and actor.
4. Verify after action with readiness deadline 2 min, 3 consecutive successful health checks, latency <2 s and no new matching critical errors during verification (configurable demo values). Deterministic recovery decision; AI may interpret failures but cannot mark resolved. One restart max; unresolved escalates and incident stays open.
5. Ensure policy checks happen immediately before side effects, not only when proposal generated. Secrets and log content are not sent unnecessarily to model.

## Tests and acceptance
- Preauthorized demo restart executes without interactive human approval; unauthorized target, second restart, expired approval, forged approval and changed action fingerprint denied.
- Crash before/after restart reconciles with no unintended duplicate; no privileged Docker socket available to model; failed verification cannot close incident; approvals resume paused workflow; denial/escalation leaves incident open.
- Provide demonstration of one allowed and one blocked action, update status and STOP.

## Non-negotiable engineering rules
- Inspect the repository and preceding phase artifacts before editing. Do not claim a phase passes without running its tests.
- One agent/orchestrator only; no extra agent framework or autonomous subagents. Python 3.12+, FastAPI, PostgreSQL, Redis Streams, Docker Compose, pytest. Pin versions after checking compatibility.
- PostgreSQL is authoritative. Use transactional outbox for DB-to-queue publication; Redis Streams consumer groups, explicit acknowledgment after durable completion, pending recovery, leases/fencing and reconciliation. At-least-once delivery means every side effect needs idempotency and reconciliation.
- Deterministic monitoring, incident creation, policy decisions, verification and lifecycle transitions. LLM may choose read-only diagnostics, hypothesize, propose allowlisted actions and draft reports; it never grants itself permissions or reports an unverified action as done.
- No arbitrary shell tool, unrestricted filesystem, production target, self-modification, secret access, or policy editing exposed to the model. Treat logs and tool output as untrusted data. Authenticate dashboard/API and protect CSRF where relevant.
- No promise of guaranteed 24/7 uptime, free unlimited subscription calls, or unattended OAuth refresh. Fail closed on authentication, provider, budget or policy failures; monitoring and queueing continue.
- Keep docs, migration scripts, .env.example without secrets, structured logs with redaction, unit/integration/resilience tests and a phase handoff note updated.
