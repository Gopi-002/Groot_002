# Phase 6 — End-to-end validation, deployment and handoff

## Objective
Prove—not assume—that the integrated system satisfies 10 workflow steps and 12 operational factors. Read all preceding files/status and inspect entire repo.

## Exact tasks
1. Build a traceability matrix: each of 10 workflow steps and 12 factors maps to code path, test name, observed result and remaining risk. Mark untested as NOT VERIFIED, never PASS.
2. Run unit, integration and resilience tests with exact recorded commands, versions and results. Scenario A: healthy baseline; B: 3 timeouts create exactly one incident; C: model picks read-only tools, evidence-backed hypothesis and allowlisted proposal; D: policy allows demo restart, verification passes, report saved; E: policy denies unauthorized action; F: recovery fails and escalates without closing incident.
3. Inject failures: DB transaction/outbox crash, Redis outage/duplicate publish, worker crash before ACK and during action, concurrent workers, provider timeout/429/quota/expired auth, model invalid JSON and prompt injection, dashboard unauthorized request, report hallucination, VM/monitor outage detection, restart and persistent data restoration. Measure recovery and prove no unauthorized/duplicate side effects in tested scenarios.
4. Test both authentication paths as supported: subscription only if official documentation and live behavior permit this app; otherwise record UNSUPPORTED, not failure of core agent. API path with explicit consent and limited spend. Verify model selection, per-task pinning and all three AI stages use chosen model. Test AI unavailable: monitoring/queue persist, no fake analysis, alerts fire, tasks resume after auth restoration.
5. Deploy to an always-on VM only with explicit user authorization and credentials. TLS, secure ingress, volumes, backup schedule, external monitor, alerts, least privilege and documented RTO/RPO. Single VM remains SPOF. Do not claim actual deployment if not performed.
6. Execute 72-hour soak ONLY if environment/time actually available; record start/end, incident injection schedule, downtime, model failures, task loss, duplicates, policy violations, usage and measured availability. If not run, state PENDING and provide exact operator instructions.
7. Deliver README quickstart, architecture, operator runbook, threat model, auth-decision, sample redacted incident report, test evidence, final traceability, limitations and next steps. Include commands for `docker compose up -d --build`, `docker compose ps`, `docker compose logs`, `docker compose down` (verify project commands first).

## Final acceptance
- No lost tasks, unauthorized actions or unintended duplicate remediation in defined tests; evidence-backed reports; all 10 workflow steps demonstrable; each of 12 factors tested or explicitly marked incomplete. No unsupported promise of subscription-based 24/7 AI. Human approval required only where policy says so; routine preauthorized demo recovery works unattended.
- Update `PHASE_STATUS.md` with exact truth, list remaining risks, then STOP. Do not silently claim production-ready if soak, security review, or deployment is incomplete.

## Non-negotiable engineering rules
- Inspect the repository and preceding phase artifacts before editing. Do not claim a phase passes without running its tests.
- One agent/orchestrator only; no extra agent framework or autonomous subagents. Python 3.12+, FastAPI, PostgreSQL, Redis Streams, Docker Compose, pytest. Pin versions after checking compatibility.
- PostgreSQL is authoritative. Use transactional outbox for DB-to-queue publication; Redis Streams consumer groups, explicit acknowledgment after durable completion, pending recovery, leases/fencing and reconciliation. At-least-once delivery means every side effect needs idempotency and reconciliation.
- Deterministic monitoring, incident creation, policy decisions, verification and lifecycle transitions. LLM may choose read-only diagnostics, hypothesize, propose allowlisted actions and draft reports; it never grants itself permissions or reports an unverified action as done.
- No arbitrary shell tool, unrestricted filesystem, production target, self-modification, secret access, or policy editing exposed to the model. Treat logs and tool output as untrusted data. Authenticate dashboard/API and protect CSRF where relevant.
- No promise of guaranteed 24/7 uptime, free unlimited subscription calls, or unattended OAuth refresh. Fail closed on authentication, provider, budget or policy failures; monitoring and queueing continue.
- Keep docs, migration scripts, .env.example without secrets, structured logs with redaction, unit/integration/resilience tests and a phase handoff note updated.
