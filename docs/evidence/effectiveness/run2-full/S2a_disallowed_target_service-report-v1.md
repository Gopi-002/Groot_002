# Incident report b963229e-66c6-4668-8e69-f3f46eda2df6 (v1)

_Narrative drafted by the deterministic MOCK model `mock-investigator-v1` (TEST/DEMO ONLY - not Claude) and accepted by SentinelOps' deterministic validator after 1 attempt(s). All facts sections are rendered directly from records._

- Incident ID: `b963229e-66c6-4668-8e69-f3f46eda2df6`
- Service: `demo-app`
- Started (first failing check): 2026-09-23T13:35:47.844144+00:00
- Detected (incident opened): 2026-09-23T13:35:57.844218+00:00
- Severity / classification: high / http_error
- Current status: escalated
- Record SHA-256: `c39dd0f8412b1cf06ad7e2ed2d2ddf45168d7d50f3787e3444dc8f10e68ad9df`; generated 2026-09-23T13:36:12.344372+00:00

## Summary
The monitor opened a http_error incident for demo-app after 3 consecutive failing checks (5 failing checks attributed to it in total). An AI investigation with model mock-investigator-v1 (a deterministic test model) proposed restart_demo_app; its hypotheses remain unconfirmed. The deterministic policy's last decision (proposal) was DENY with rules TGT-1. A human approval was requested and its final status is approved. No remediation action was executed. The incident is escalated and remains open.

## Detection evidence (observed facts)
- Evidence `d7e616a1-9015-4e01-9aad-3868cde5b0b7`: 3 consecutive `http_error` checks (threshold 3), collected 2026-09-23T13:35:57.847731+00:00
  - 2026-09-23T13:35:47.844144+00:00 outcome=unhealthy http=500 latency_ms=2.721
  - 2026-09-23T13:35:52.844149+00:00 outcome=unhealthy http=500 latency_ms=2.119
  - 2026-09-23T13:35:57.844218+00:00 outcome=unhealthy http=500 latency_ms=2.187
- Monitoring window 2026-09-23T13:30:47.844144+00:00 to 2026-09-23T13:36:07.027861+00:00: healthy=22, unhealthy=42

## Investigation (observed facts)
- Investigation `ef27921d-ea22-40df-b228-92222c743385`: status completed
- Model: `mock-investigator-v1`; auth mode `mock`; DETERMINISTIC MOCK (TEST/DEMO ONLY - not Claude)
- Diagnostic tool calls: 3; model calls: 4; reasoning attempts: 0

## Evidence reviewed
- `d7e616a1-9015-4e01-9aad-3868cde5b0b7` health_check at 2026-09-23T13:35:57.847731+00:00
- `1bfc9906-a802-4de2-8e7b-f3c047bedd17` tool/get_incident (ok) at 2026-09-23T13:35:58.000418+00:00
- `816b731c-a18e-4c27-b4e8-a5473f01740c` tool/get_application_logs (ok) at 2026-09-23T13:35:58.034133+00:00
- `c0597feb-c3d9-41b8-bbcb-fcef3160cb35` tool/get_container_status (ok) at 2026-09-23T13:35:58.061900+00:00

### Observations (report narrative; each cites evidence)
- The monitor recorded 3 consecutive failing checks of type http_error. [`d7e616a1-9015-4e01-9aad-3868cde5b0b7`]
- Diagnostic get_incident returned status ok. [`1bfc9906-a802-4de2-8e7b-f3c047bedd17`]
- Diagnostic get_application_logs returned status ok. [`816b731c-a18e-4c27-b4e8-a5473f01740c`]
- Diagnostic get_container_status returned status ok. [`c0597feb-c3d9-41b8-bbcb-fcef3160cb35`]

## Hypotheses (AI investigation; UNCONFIRMED, not root causes)
- The application process is in a degraded state that a restart may clear (unconfirmed). (confidence medium)

## Proposed action (what the AI recommended)
- `restart_demo_app` target `postgres` citing 3 evidence record(s)

## Policy decisions (deterministic policy engine)
- 2026-09-23T13:35:58.977885+00:00 proposal: **REQUIRE_APPROVAL** rules APR-0 (policy 4af83087b17420d7) `fbacf224-e43e-425f-84a8-a4000a7ce476`
- 2026-09-23T13:36:07.027861+00:00 proposal: **DENY** rules TGT-1 (policy 4af83087b17420d7) `f11066b4-83a8-4b37-a7bb-61f37df32350`

## Approval history
- `f4521c5d-8862-4bbf-a09c-f1e35177f696`: approved; requested 2026-09-23T13:35:58.977885+00:00, expires 2026-09-23T13:50:58.977885+00:00, decided by `it-9c339021` at 2026-09-23T13:36:06.758348+00:00

## Actions actually executed (executor records)
- no action was executed

## Recovery verification (deterministic verifier)
- no verification ran (no action was executed)

## Outcome (durable state)
- Incident: escalated - OPEN, owned by a human
- Task: escalated / policy_denied
- Escalation reason(s): task escalated; task escalated: policy_denied

## Timeline
- 2026-09-23T13:35:47.844144+00:00 first failing http_error check
- 2026-09-23T13:35:57.844218+00:00 incident opened (http_error)
- 2026-09-23T13:35:57.977864+00:00 investigation started (mock-investigator-v1)
- 2026-09-23T13:35:58.000418+00:00 diagnostic get_incident (ok)
- 2026-09-23T13:35:58.034133+00:00 diagnostic get_application_logs (ok)
- 2026-09-23T13:35:58.061900+00:00 diagnostic get_container_status (ok)
- 2026-09-23T13:35:58.073847+00:00 investigation completed
- 2026-09-23T13:35:58.977885+00:00 human approval requested
- 2026-09-23T13:35:58.977885+00:00 policy proposal: REQUIRE_APPROVAL APR-0
- 2026-09-23T13:36:06.758348+00:00 approval approved
- 2026-09-23T13:36:07.027861+00:00 incident escalated to a human
- 2026-09-23T13:36:07.027861+00:00 policy proposal: DENY TGT-1
- 2026-09-23T13:36:07.027861+00:00 task escalated (policy_denied)

## AI usage and cost
- Investigation: 4 model calls, 2000 input / 400 output tokens
- Report drafting: 0 model calls, 0 input / 0 output tokens
- Cost: unavailable: no operator-configured prices

## Unresolved questions
- What still prevents the service from passing its health checks?
- Is this unconfirmed hypothesis correct: The application process is in a degraded state that a restart may clear (unconfirmed).?

## Follow-up items
- A human operator should review the escalated incident (task escalated: policy_denied).
- Confirm or refute the unconfirmed hypotheses with additional evidence.
