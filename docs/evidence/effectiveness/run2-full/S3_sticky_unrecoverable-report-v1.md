# Incident report c0323d2c-ecf6-478b-8919-add193f505a0 (v1)

_Narrative drafted by the deterministic MOCK model `mock-investigator-v1` (TEST/DEMO ONLY - not Claude) and accepted by SentinelOps' deterministic validator after 1 attempt(s). All facts sections are rendered directly from records._

- Incident ID: `c0323d2c-ecf6-478b-8919-add193f505a0`
- Service: `demo-app`
- Started (first failing check): 2026-09-23T13:32:22.844178+00:00
- Detected (incident opened): 2026-09-23T13:32:32.844176+00:00
- Severity / classification: high / http_error
- Current status: escalated
- Record SHA-256: `78bb6182a40c3fb8bf4071bf699052cfe5ae14ccc2f57415f2be23b50208375f`; generated 2026-09-23T13:34:39.829469+00:00

## Summary
The monitor opened a http_error incident for demo-app after 3 consecutive failing checks (28 failing checks attributed to it in total). An AI investigation with model mock-investigator-v1 (a deterministic test model) proposed restart_demo_app; its hypotheses remain unconfirmed. The deterministic policy's last decision (pre_execution) was ALLOW with rules AUT-1. SentinelOps executed one restart of the demo application through the restricted executor. Recovery verification failed; the incident remains open for a human. The incident is escalated and remains open.

## Detection evidence (observed facts)
- Evidence `c3fb0c59-488d-4104-bf1e-e56c95660add`: 3 consecutive `http_error` checks (threshold 3), collected 2026-09-23T13:32:32.847587+00:00
  - 2026-09-23T13:32:22.844178+00:00 outcome=unhealthy http=500 latency_ms=2.176
  - 2026-09-23T13:32:27.844205+00:00 outcome=unhealthy http=500 latency_ms=3.008
  - 2026-09-23T13:32:32.844176+00:00 outcome=unhealthy http=500 latency_ms=2.225
- Monitoring window 2026-09-23T13:27:22.844178+00:00 to 2026-09-23T13:34:34.607651+00:00: error=4, healthy=50, unhealthy=33

## Investigation (observed facts)
- Investigation `9df906c8-1c9a-45ee-96fc-d5e31d9077ed`: status completed
- Model: `mock-investigator-v1`; auth mode `mock`; DETERMINISTIC MOCK (TEST/DEMO ONLY - not Claude)
- Diagnostic tool calls: 3; model calls: 4; reasoning attempts: 0

## Evidence reviewed
- `c3fb0c59-488d-4104-bf1e-e56c95660add` health_check at 2026-09-23T13:32:32.847587+00:00
- `3b5ac3e4-8e9b-41be-8632-9d8e45ed6ea5` tool/get_incident (ok) at 2026-09-23T13:32:33.051980+00:00
- `8a4104a0-69ce-44ef-9d16-815618c1f212` tool/get_application_logs (ok) at 2026-09-23T13:32:33.083245+00:00
- `a6c3942d-fc06-48d3-b2cb-15cf469f6a47` tool/get_container_status (ok) at 2026-09-23T13:32:33.105818+00:00
- `89138d54-4fbb-46fb-91d3-5fafb0373547` verification/recovery_verification at 2026-09-23T13:34:34.602877+00:00

### Observations (report narrative; each cites evidence)
- The monitor recorded 3 consecutive failing checks of type http_error. [`c3fb0c59-488d-4104-bf1e-e56c95660add`]
- Diagnostic get_incident returned status ok. [`3b5ac3e4-8e9b-41be-8632-9d8e45ed6ea5`]
- Diagnostic get_application_logs returned status ok. [`8a4104a0-69ce-44ef-9d16-815618c1f212`]
- Diagnostic get_container_status returned status ok. [`a6c3942d-fc06-48d3-b2cb-15cf469f6a47`]

## Hypotheses (AI investigation; UNCONFIRMED, not root causes)
- The application process is in a degraded state that a restart may clear (unconfirmed). (confidence medium)

## Proposed action (what the AI recommended)
- `restart_demo_app` target `demo-app` citing 3 evidence record(s)

## Policy decisions (deterministic policy engine)
- 2026-09-23T13:32:33.987753+00:00 proposal: **ALLOW** rules AUT-1 (policy 5f01b017223b3c64) `ac57caf2-b2c0-480c-9188-1f1d541405b0`
- 2026-09-23T13:32:33.996745+00:00 pre_execution: **ALLOW** rules AUT-1 (policy 5f01b017223b3c64) `ab31a890-9402-413f-b1b8-6f259acf540c`

## Approval history
- no approval was requested

## Actions actually executed (executor records)
- `b6653616-d93b-4d2d-8f0a-83c463cf9463` restart_demo_app: status **succeeded** (EXECUTED); started 2026-09-23T13:32:34.027536+00:00, completed 2026-09-23T13:32:34.593888+00:00; recorded via executor_response

## Recovery verification (deterministic verifier)
- `6fbedb51-08c1-4d78-bb88-a4846488d97d`: **failed** - readiness deadline 120.0s exceeded without 3 consecutive healthy, fast probes (0/61 healthy probes, 0 critical log lines)

## Outcome (durable state)
- Incident: escalated - OPEN, owned by a human
- Task: escalated / recovery_failed
- Escalation reason(s): task escalated; task escalated: recovery_failed

## Timeline
- 2026-09-23T13:32:22.844178+00:00 first failing http_error check
- 2026-09-23T13:32:32.844176+00:00 incident opened (http_error)
- 2026-09-23T13:32:33.033483+00:00 investigation started (mock-investigator-v1)
- 2026-09-23T13:32:33.051980+00:00 diagnostic get_incident (ok)
- 2026-09-23T13:32:33.083245+00:00 diagnostic get_application_logs (ok)
- 2026-09-23T13:32:33.105818+00:00 diagnostic get_container_status (ok)
- 2026-09-23T13:32:33.115608+00:00 investigation completed
- 2026-09-23T13:32:33.987753+00:00 policy proposal: ALLOW AUT-1
- 2026-09-23T13:32:33.996745+00:00 policy pre_execution: ALLOW AUT-1
- 2026-09-23T13:32:34.027536+00:00 restart_demo_app execution started
- 2026-09-23T13:32:34.593888+00:00 restart_demo_app succeeded
- 2026-09-23T13:34:34.602877+00:00 recovery verification failed
- 2026-09-23T13:34:34.607651+00:00 incident escalated to a human
- 2026-09-23T13:34:34.607651+00:00 task escalated (recovery_failed)

## AI usage and cost
- Investigation: 4 model calls, 2000 input / 400 output tokens
- Report drafting: 0 model calls, 0 input / 0 output tokens
- Cost: unavailable: no operator-configured prices

## Unresolved questions
- What still prevents the service from passing its health checks?
- Is this unconfirmed hypothesis correct: The application process is in a degraded state that a restart may clear (unconfirmed).?

## Follow-up items
- Investigate why the single permitted restart did not restore health; no further automated action will be taken for this incident.
- A human operator should review the escalated incident (task escalated: recovery_failed).
- Confirm or refute the unconfirmed hypotheses with additional evidence.
