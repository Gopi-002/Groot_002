# Phase 2 — Monitoring, detection and durable dispatch

## Objective
Implement workflow steps 1–3, fully deterministic. Read master and phase 1 status first.

## Exact tasks
1. Separate monitor service polls demo `/health` every 30 seconds, 5-second timeout, monotonic latency measurement, UTC result timestamps; configurable thresholds (3 consecutive failures, or 3 latency measurements >2 s). Avoid overlapping checks; startup/restart behavior must be defined.
2. Persist every check or bounded aggregate. Detection state per service+failure type; atomic incident upsert using unique active-incident constraint. Record threshold evidence and first/last failure. Do not open duplicates during prolonged failure; define healthy hysteresis/rearm rules.
3. In ONE PostgreSQL transaction insert incident, task and outbox event. Outbox dispatcher publishes to Redis Streams with durable config; record published state and reconcile crash windows (duplicate delivery permitted, task processing idempotent).
4. Worker consumer group claims tasks, DB-backed lease with expiry/fencing token, checkpoints, bounded retry/backoff, pending message recovery and dead-letter/FAILED status. ACK only after committed outcome; ensure reclaim never creates competing valid executors.
5. Build read-only incident/task status API, access controlled. Instrument counters for checks, detected incidents, queue lag, retry and dead-letter.

## Tests and acceptance
- Healthy checks create no incident; 3 failures create exactly one; latency threshold creates incident; subsequent checks do not duplicate; healthy rearm allows a later separate incident.
- Crash after DB commit before publish: dispatcher eventually publishes. Crash after Redis publish before marking outbox: duplicate event cannot duplicate work. Worker crash before ACK: task recovered. Two workers racing: only current lease/fencing holder may advance task. Redis outage: DB retains pending work; retry later.
- Record actual commands/results in status; STOP. No LLM calls or remediation yet.

## Non-negotiable engineering rules
- Inspect the repository and preceding phase artifacts before editing. Do not claim a phase passes without running its tests.
- One agent/orchestrator only; no extra agent framework or autonomous subagents. Python 3.12+, FastAPI, PostgreSQL, Redis Streams, Docker Compose, pytest. Pin versions after checking compatibility.
- PostgreSQL is authoritative. Use transactional outbox for DB-to-queue publication; Redis Streams consumer groups, explicit acknowledgment after durable completion, pending recovery, leases/fencing and reconciliation. At-least-once delivery means every side effect needs idempotency and reconciliation.
- Deterministic monitoring, incident creation, policy decisions, verification and lifecycle transitions. LLM may choose read-only diagnostics, hypothesize, propose allowlisted actions and draft reports; it never grants itself permissions or reports an unverified action as done.
- No arbitrary shell tool, unrestricted filesystem, production target, self-modification, secret access, or policy editing exposed to the model. Treat logs and tool output as untrusted data. Authenticate dashboard/API and protect CSRF where relevant.
- No promise of guaranteed 24/7 uptime, free unlimited subscription calls, or unattended OAuth refresh. Fail closed on authentication, provider, budget or policy failures; monitoring and queueing continue.
- Keep docs, migration scripts, .env.example without secrets, structured logs with redaction, unit/integration/resilience tests and a phase handoff note updated.
