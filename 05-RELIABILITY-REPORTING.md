# Phase 5 — Reporting, resilience, security and observability

## Objective
Implement steps 9–10 and finish cross-cutting 12 factors. Read master and previous phase statuses.

## Exact tasks
1. Report builder loads immutable incident timeline, evidence IDs, model proposals, policy verdicts, action attempts and deterministic verification. Selected model drafts structured summary, timeline, observations vs hypotheses, actions actually completed, outcome and follow-up. Validator cross-checks referenced IDs, timestamps, action status and recovery; no invented root cause or claim of recovery. Store versioned report and provenance; deterministic fallback report when AI unavailable.
2. Implement lifecycle state machine with guarded transitions: DETECTED, QUEUED, INVESTIGATING, ACTION_PROPOSED, POLICY_CHECK, WAITING_APPROVAL, EXECUTING, VERIFYING, REPORTING, RESOLVED, RETRY_SCHEDULED, ESCALATED, FAILED. Escalated task may be terminal while incident remains open. Reconciliation jobs for stuck tasks/outbox/action attempts; bounded exponential backoff + jitter and dead-letter.
3. Observability: structured redacted logs, trace/correlation IDs, Prometheus metrics for monitor success, incident age, queue lag, AI latency/usage, policy denies, action counts, recovery rate and auth errors. Grafana optional but provide dashboard config; alerts for monitor silence, queue backlog, expired auth, stalled incident, failed backup and host down. External uptime check required for host failure.
4. Security: service-level least privilege, protected secrets, auth for API/dashboard, audit append-only protections, TLS deployment guidance, backup/restore drill, retention and redaction for potentially sensitive logs. Prompt injection and malicious log tests.
5. Cost: configurable daily and per-incident token/currency ceilings, max concurrent AI jobs, no idle AI calls, accurate provider usage if available, conservative estimates otherwise; budget exhausted => pause AI, continue deterministic monitoring and notify.
6. Deployment guide: always-on VM, persistent storage, restart policies, backups, secure ingress, external monitoring, maintenance and key rotation. Explicitly document single-VM SPOF and subscription expiry/quota risks; no guaranteed 24/7 SLA.

## Tests and acceptance
- AI fabricated action/report rejected; deterministic fallback works without provider; repeated report generation idempotent; retries bounded; monitor remains alive on AI outage; secrets redacted; metrics/alerts fire in test; backup restore works; budget stops calls; auth expiry yields actionable alert.
- Produce operations runbook, 12-factor traceability draft, status and STOP.

## Non-negotiable engineering rules
- Inspect the repository and preceding phase artifacts before editing. Do not claim a phase passes without running its tests.
- One agent/orchestrator only; no extra agent framework or autonomous subagents. Python 3.12+, FastAPI, PostgreSQL, Redis Streams, Docker Compose, pytest. Pin versions after checking compatibility.
- PostgreSQL is authoritative. Use transactional outbox for DB-to-queue publication; Redis Streams consumer groups, explicit acknowledgment after durable completion, pending recovery, leases/fencing and reconciliation. At-least-once delivery means every side effect needs idempotency and reconciliation.
- Deterministic monitoring, incident creation, policy decisions, verification and lifecycle transitions. LLM may choose read-only diagnostics, hypothesize, propose allowlisted actions and draft reports; it never grants itself permissions or reports an unverified action as done.
- No arbitrary shell tool, unrestricted filesystem, production target, self-modification, secret access, or policy editing exposed to the model. Treat logs and tool output as untrusted data. Authenticate dashboard/API and protect CSRF where relevant.
- No promise of guaranteed 24/7 uptime, free unlimited subscription calls, or unattended OAuth refresh. Fail closed on authentication, provider, budget or policy failures; monitoring and queueing continue.
- Keep docs, migration scripts, .env.example without secrets, structured logs with redaction, unit/integration/resilience tests and a phase handoff note updated.
