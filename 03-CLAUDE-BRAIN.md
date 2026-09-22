# Phase 3 — Claude authentication, model selection and AI investigation

## Objective
Implement workflow steps 4–5, subscription-first only IF officially supported for this standalone app; API-key fallback must work. Read master and earlier status.

## Authentication decision gate — do this BEFORE coding
1. Inspect currently installed Claude Agent SDK and official docs/terms for SDK auth, supported subscription usage in custom third-party app, unattended operation, model discovery, refresh, limits, and deployment. Record URLs, date, version and tested behavior in `docs/auth-decision.md`. Never infer permission merely because `claude` CLI is signed in.
2. Onboarding UI/CLI: `Choose authentication: Subscription (only if supported) | Anthropic API key | Exit`. If subscription integration is not officially supported, show why and disable it; API key remains usable. Do not request Claude password, scrape browser cookies, read CLI credential stores, forge OAuth tokens, or impersonate Claude Code. For API key use official SDK and secret store/protected environment, masked input and rotation instructions. Explicitly explain subscription and API billing are separate.
3. Authenticate with officially supported provider method. Handle invalid/expired credentials, rate limits, quotas and network failures as typed errors; queue tasks and alert rather than switch providers or repeatedly retry. Verify whether any supported subscription method can truly work unattended; if not, clearly mark limitation.
4. Model selector: call provider-supported model-list endpoint where available; otherwise use documented official model IDs with provider validation. Display ID and availability, select/persist ID + auth mode, pin per task. Never invent model names or promise availability. Provide explicit change-model command; no credential persistence in DB.

## AI design
5. One `ModelGateway` abstraction with `authenticate`, `list_models` (or documented availability fallback), `invoke`, token/usage accounting and typed failures. Use selected model for ALL AI stages; mocks for CI. SDK usage must be verified against installed version, not guessed.
6. Read-only tools with typed bounded inputs/outputs: get_incident, get_health_history, get_application_logs (redacted, capped), get_container_status via restricted service, get_resource_metrics, get_previous_incidents. No Bash, filesystem, arbitrary URL, direct Docker socket, network scan or unrestricted shell exposed to LLM.
7. Orchestrator loads incident + selected model, sends explicit objective, allows max 6 diagnostic calls, 3 reasoning attempts, timeout and per-incident budget. Each tool request validated, logged and checkpointed. Untrusted logs never override system instructions. AI outputs JSON/schema with observations, evidence IDs, hypotheses (not proven root causes), missing evidence, next_step, allowlisted proposed action, risks and verification plan.
8. Validate evidence IDs exist and match incident, tool outputs are fresh enough, schema is valid, and proposal is on allowlist. On malformed output retry boundedly then `ESCALATED`/`INSUFFICIENT_EVIDENCE`. Never permit AI to output policy verdict, actual execution or resolution status.

## Tests and acceptance
- Mock model proves diagnostic branching based on tool results; invalid evidence IDs rejected; prompt injection in logs ignored; tool call/time/token budget enforced; unsupported action rejected; selected model used for investigation and proposal; model change affects only new tasks.
- Auth tests: unsupported subscription disabled, API key path works with mock provider, no secrets in logs/DB, quota/expiry pauses AI while monitor/queue continues. Live auth smoke test only with explicit user authorization and available credentials; document whether executed.
- Update status and STOP; no remediation yet.

## Non-negotiable engineering rules
- Inspect the repository and preceding phase artifacts before editing. Do not claim a phase passes without running its tests.
- One agent/orchestrator only; no extra agent framework or autonomous subagents. Python 3.12+, FastAPI, PostgreSQL, Redis Streams, Docker Compose, pytest. Pin versions after checking compatibility.
- PostgreSQL is authoritative. Use transactional outbox for DB-to-queue publication; Redis Streams consumer groups, explicit acknowledgment after durable completion, pending recovery, leases/fencing and reconciliation. At-least-once delivery means every side effect needs idempotency and reconciliation.
- Deterministic monitoring, incident creation, policy decisions, verification and lifecycle transitions. LLM may choose read-only diagnostics, hypothesize, propose allowlisted actions and draft reports; it never grants itself permissions or reports an unverified action as done.
- No arbitrary shell tool, unrestricted filesystem, production target, self-modification, secret access, or policy editing exposed to the model. Treat logs and tool output as untrusted data. Authenticate dashboard/API and protect CSRF where relevant.
- No promise of guaranteed 24/7 uptime, free unlimited subscription calls, or unattended OAuth refresh. Fail closed on authentication, provider, budget or policy failures; monitoring and queueing continue.
- Keep docs, migration scripts, .env.example without secrets, structured logs with redaction, unit/integration/resilience tests and a phase handoff note updated.
