# SentinelOps — master constitution and execution guide

## Mission
Build one bounded 24/7 incident-response agent for exactly one isolated FastAPI demo application. It continuously monitors, detects, durably queues, uses Claude to investigate and propose actions, enforces deterministic policy, performs one preauthorized demo-only restart, verifies recovery, reports, and resumes monitoring. It must demonstrate all 12 operational factors, not promise perfect availability.

## Read order and stop gates
1. Read this entire file, inspect repository, then execute `01-FOUNDATION.md`; run acceptance tests, write `PHASE_STATUS.md`, STOP.
2. Execute `02-MONITORING-QUEUE.md`; tests/status, STOP.
3. Execute `03-CLAUDE-BRAIN.md`; tests/status, STOP.
4. Execute `04-SAFETY-AUTONOMY.md`; tests/status, STOP.
5. Execute `05-RELIABILITY-REPORTING.md`; tests/status, STOP.
6. Execute `06-END-TO-END-VALIDATION.md`; tests/status, STOP.
Do not silently skip steps or reinterpret an earlier locked decision. Ask only for truly missing credentials/authorization or irreconcilable requirements. Never print secrets. At each phase: inspect existing code, write brief plan, implement smallest vertical slice, run tests and lint/type checks, report commands/results and open risks, then stop for review. Do not mark a future phase complete.

## Critical authentication and model contract
User wants Claude Code-like onboarding: choose **Subscription sign-in** or **Anthropic API key**, then list models and select one. Do NOT assume Claude Code's interactive subscription credentials may legally or technically power this standalone 24/7 app. Before coding authentication, inspect current official Anthropic Agent SDK documentation, terms, auth and model-list support; record URL, access date, SDK version, supported method and test evidence in `docs/auth-decision.md`. Official guidance as of Sep 22 2026: June 15 Agent SDK subscription billing changes were paused, but third-party usage is conditional and production/shared automation should use API auth. Sources: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan and https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account and https://platform.claude.com/docs/en/manage-claude/authentication . Verify again at build time.
- Only implement subscription mode if an officially documented, permitted integration applies to THIS custom app and unattended use; use only the provider's supported auth flow, no token scraping, copied Claude Code credential files, masquerading as Claude Code, proxying through `claude -p` to bypass restrictions, or fabricated OAuth flow. If not verified, display `Subscription integration unavailable for this application` and offer API-key mode; do not present a fake sign-in button.
- API mode: provider-supported SDK/API with secure key input, masked UI, secrets manager or protected runtime env, no DB plaintext key, no logs or source commits. Check authentication with minimal provider call; no implicit charges without clear consent. API billing is separate from a Claude subscription.
- Model selection: use official model listing when available for the authenticated method; otherwise obtain documented supported models from official source and validate availability with provider. Never hardcode a fabricated model list. Persist selected model ID and auth mode (not credentials); show capabilities, availability errors and actual provider limits. All AI calls—investigation, reasoning, report—must use the selected model via one `ModelGateway`; no silent provider fallback. A changed model applies to new tasks; in-flight tasks pin model ID.
- If subscription mode is permitted but cannot be safely refreshed unattended, mark it unsuitable for guaranteed 24/7 autonomous AI; pause AI tasks on expiry/limits and notify, while deterministic monitoring continues. No claims of unlimited usage.

## 10-step workflow contract
1 monitor HTTP health/latency; 2 threshold + deduplicated incident; 3 transactionally create task/outbox and dispatch; 4 AI selects bounded read-only tools; 5 AI returns evidence-linked hypothesis and allowlisted proposal; 6 deterministic policy ALLOW/APPROVAL/DENY; 7 execute only permitted demo action with unique action ID and reconciliation; 8 deterministic recovery checks; 9 AI report with record-backed verification; 10 durable final state, metrics, notification and continued monitoring. Branches: RETRY_SCHEDULED, WAITING_APPROVAL, ESCALATED, FAILED, RESOLVED. Incident remains open when autonomous task escalates.

## All 12 quality factors and required evidence
Availability: restart/health/external checks; Trigger: event + scheduled monitor; State: PostgreSQL checkpoints; Recovery: retries/backoff/dead letter/reconciliation; Decision boundaries: allowlist and approvals; Security: least privilege/auth/secrets; Cost: per-incident model/tool budgets; Concurrency: dedup, leases and idempotency; Observability: logs/metrics/traces/alerts; Integrations: timeout/rate-limit/auth expiry; Quality: evidence/schema/policy tests; Human handoff: authenticated approval, expiry and escalation. Every factor must have a test and be listed in final traceability matrix.

## Deployment and scope
Local Docker Compose is development, not guaranteed always-on. First unattended deployment: always-on VM with persistent volumes, backups, TLS/auth, external uptime monitoring, restart policies and alerts; single VM is a single point of failure. No production remediation. 72-hour soak is a target to actually execute, not claim in advance. Document measured availability, failure injection, RTO/RPO and remaining risks.

## Repository map
`app/{api,agent,monitoring,safety,persistence,observability,auth}/`, `demo_application/`, `dashboard/`, `tests/{unit,integration,resilience}/`, `migrations/`, `docs/`, `docker-compose.yml`, `.env.example`, `PHASE_STATUS.md`.

## Claude Code start prompt
Read `CLAUDE.md` fully, inspect the entire repository and current official Anthropic authentication/Agent SDK documentation. Execute ONLY the earliest incomplete numbered phase. Create a concise plan, implement and test it, update `PHASE_STATUS.md` with exact commands/results, then STOP. Do not assume subscription access or bypass official auth.
