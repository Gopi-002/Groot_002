# Phase 1 — Foundation and infrastructure

## Objective and prerequisites
Create a runnable isolated demo and durable infrastructure. Read CLAUDE.md first. No AI integration or remediation in this phase.

## Exact tasks
1. Inspect repo, installed Python/Docker and existing tests. Write a short plan. Establish Python 3.12+ project, dependency lock, formatter/linter/type checker and pytest.
2. Build FastAPI demo app: `/health` (readiness), `/metrics` (safe demo measurements); development-only authenticated or localhost-only failure injection `/simulate-failure`, disabled outside demo. Simulate timeout, HTTP 500 and memory-like log without actually exhausting host memory.
3. Build separate API service with `/health/live` and `/health/ready`, config validation, typed settings, structured logging with secret redaction, consistent error responses. No public unsecured incident or admin endpoint.
4. Compose: demo app, API, PostgreSQL, Redis; named persistent DB/Redis volumes, network isolation, dependency health checks, restart policies, resource limits. No Redis/Postgres public ports by default; no Docker socket mounted into AI worker.
5. PostgreSQL migrations: `services`, `health_checks`, `incidents`, `tasks`, `outbox_events`, `task_checkpoints`, `action_attempts`, `approvals`, `evidence`, `reports`, `audit_events`, `model_config`; UUID IDs, UTC timestamps, schema constraints, indexed status/incident keys, one active incident per service+type via partial unique index. Add DB schema versioning.
6. Build a tiny dashboard shell with authenticated admin access placeholder only if secure authentication is implemented; otherwise keep dashboard bound to localhost and explicitly mark not production-ready. Create `.env.example` with no credentials.
7. README commands: configure env, build, start, health check, stop, backup/restore and troubleshooting. Never fabricate successful execution.

## Tests and acceptance
- `docker compose config` valid; services start and readiness passes; DB migrations run twice safely; restart preserves DB data; demo failure modes isolated; no secrets logged; health endpoints correctly distinguish liveness/readiness.
- Unit tests for settings, migrations, demo modes; integration test inserts and retrieves an incident; negative test confirms failure endpoint unavailable in production configuration.
- Output architecture diagram (Mermaid text), schema overview and `PHASE_STATUS.md` with commands, outcomes, unresolved risks. STOP. Do not build later phases.

## Non-negotiable engineering rules
- Inspect the repository and preceding phase artifacts before editing. Do not claim a phase passes without running its tests.
- One agent/orchestrator only; no extra agent framework or autonomous subagents. Python 3.12+, FastAPI, PostgreSQL, Redis Streams, Docker Compose, pytest. Pin versions after checking compatibility.
- PostgreSQL is authoritative. Use transactional outbox for DB-to-queue publication; Redis Streams consumer groups, explicit acknowledgment after durable completion, pending recovery, leases/fencing and reconciliation. At-least-once delivery means every side effect needs idempotency and reconciliation.
- Deterministic monitoring, incident creation, policy decisions, verification and lifecycle transitions. LLM may choose read-only diagnostics, hypothesize, propose allowlisted actions and draft reports; it never grants itself permissions or reports an unverified action as done.
- No arbitrary shell tool, unrestricted filesystem, production target, self-modification, secret access, or policy editing exposed to the model. Treat logs and tool output as untrusted data. Authenticate dashboard/API and protect CSRF where relevant.
- No promise of guaranteed 24/7 uptime, free unlimited subscription calls, or unattended OAuth refresh. Fail closed on authentication, provider, budget or policy failures; monitoring and queueing continue.
- Keep docs, migration scripts, .env.example without secrets, structured logs with redaction, unit/integration/resilience tests and a phase handoff note updated.
