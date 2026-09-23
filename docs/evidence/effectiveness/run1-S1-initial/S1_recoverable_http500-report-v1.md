# Incident report 7e346c6c-0c26-4ab9-a535-b70f5a6b9747 (v1)

_Narrative drafted by the deterministic MOCK model `mock-investigator-v1` (TEST/DEMO ONLY - not Claude) and accepted by SentinelOps' deterministic validator after 1 attempt(s). All facts sections are rendered directly from records._

- Incident ID: `7e346c6c-0c26-4ab9-a535-b70f5a6b9747`
- Service: `demo-app`
- Started (first failing check): 2026-09-23T13:29:27.844145+00:00
- Detected (incident opened): 2026-09-23T13:29:37.844188+00:00
- Severity / classification: high / http_error
- Current status: resolved (remediated)
- Record SHA-256: `49920fe422d57466e022d0582961ca7789f5d2ed093b1c19abb04836ef05c284`; generated 2026-09-23T13:29:53.879923+00:00

## Summary
The monitor opened a http_error incident for demo-app after 3 consecutive failing checks (3 failing checks attributed to it in total). An AI investigation with model mock-investigator-v1 (a deterministic test model) proposed restart_demo_app; its hypotheses remain unconfirmed. The deterministic policy's last decision (pre_execution) was ALLOW with rules AUT-1. SentinelOps executed one restart of the demo application through the restricted executor. Recovery verification passed. The incident was resolved as remediated.

## Detection evidence (observed facts)
- Evidence `0320cf9e-eb1e-4459-b1ab-16852a7dc074`: 3 consecutive `http_error` checks (threshold 3), collected 2026-09-23T13:29:37.848366+00:00
  - 2026-09-23T13:29:27.844145+00:00 outcome=unhealthy http=500 latency_ms=3.146
  - 2026-09-23T13:29:32.844172+00:00 outcome=unhealthy http=500 latency_ms=2.077
  - 2026-09-23T13:29:37.844188+00:00 outcome=unhealthy http=500 latency_ms=2.562
- Monitoring window 2026-09-23T13:24:27.844145+00:00 to 2026-09-23T13:29:48.688123+00:00: error=4, healthy=58, unhealthy=3

## Investigation (observed facts)
- Investigation `d7b837bb-4bfd-4ad2-b0bf-f38766fe7b92`: status completed
- Model: `mock-investigator-v1`; auth mode `mock`; DETERMINISTIC MOCK (TEST/DEMO ONLY - not Claude)
- Diagnostic tool calls: 3; model calls: 4; reasoning attempts: 0

## Evidence reviewed
- `0320cf9e-eb1e-4459-b1ab-16852a7dc074` health_check at 2026-09-23T13:29:37.848366+00:00
- `2ddb01a5-136a-46a5-a1e4-7734a1e066fe` tool/get_incident (ok) at 2026-09-23T13:29:39.012284+00:00
- `b1668181-1cb7-4714-b181-db61979f5543` tool/get_application_logs (ok) at 2026-09-23T13:29:39.049228+00:00
- `507f2537-817b-4fac-b735-7b49f3d155ce` tool/get_container_status (ok) at 2026-09-23T13:29:39.073534+00:00
- `3fa6f2a8-914d-409a-bd80-19707395cbe0` verification/recovery_verification at 2026-09-23T13:29:48.682680+00:00

### Observations (report narrative; each cites evidence)
- The monitor recorded 3 consecutive failing checks of type http_error. [`0320cf9e-eb1e-4459-b1ab-16852a7dc074`]
- Diagnostic get_incident returned status ok. [`2ddb01a5-136a-46a5-a1e4-7734a1e066fe`]
- Diagnostic get_application_logs returned status ok. [`b1668181-1cb7-4714-b181-db61979f5543`]
- Diagnostic get_container_status returned status ok. [`507f2537-817b-4fac-b735-7b49f3d155ce`]

## Hypotheses (AI investigation; UNCONFIRMED, not root causes)
- The application process is in a degraded state that a restart may clear (unconfirmed). (confidence medium)

## Proposed action (what the AI recommended)
- `restart_demo_app` target `demo-app` citing 3 evidence record(s)

## Policy decisions (deterministic policy engine)
- 2026-09-23T13:29:39.983465+00:00 proposal: **ALLOW** rules AUT-1 (policy 5f01b017223b3c64) `9e7b1635-31c9-429f-b336-aef79de8fb78`
- 2026-09-23T13:29:40.066769+00:00 pre_execution: **ALLOW** rules AUT-1 (policy 5f01b017223b3c64) `5a109f77-5cdd-4ae3-93f3-88cd8d4de68b`

## Approval history
- no approval was requested

## Actions actually executed (executor records)
- `aad0694c-cee8-4c5d-ba7d-9cfd4408fa24` restart_demo_app: status **succeeded** (EXECUTED); started 2026-09-23T13:29:40.091925+00:00, completed 2026-09-23T13:29:40.635053+00:00; recorded via executor_response

## Recovery verification (deterministic verifier)
- `d72ea15d-9eaa-4e5e-ae2b-40ef915b5a03`: **passed** - 3 consecutive healthy probes under 2.0s and no new critical errors (3/5 healthy probes, 0 critical log lines)

## Outcome (durable state)
- Incident: resolved / remediated
- Task: resolved / recovery_verified

## Timeline
- 2026-09-23T13:29:27.844145+00:00 first failing http_error check
- 2026-09-23T13:29:37.844188+00:00 incident opened (http_error)
- 2026-09-23T13:29:38.985269+00:00 investigation started (mock-investigator-v1)
- 2026-09-23T13:29:39.012284+00:00 diagnostic get_incident (ok)
- 2026-09-23T13:29:39.049228+00:00 diagnostic get_application_logs (ok)
- 2026-09-23T13:29:39.073534+00:00 diagnostic get_container_status (ok)
- 2026-09-23T13:29:39.085610+00:00 investigation completed
- 2026-09-23T13:29:39.983465+00:00 policy proposal: ALLOW AUT-1
- 2026-09-23T13:29:40.066769+00:00 policy pre_execution: ALLOW AUT-1
- 2026-09-23T13:29:40.091925+00:00 restart_demo_app execution started
- 2026-09-23T13:29:40.635053+00:00 restart_demo_app succeeded
- 2026-09-23T13:29:48.682680+00:00 recovery verification passed
- 2026-09-23T13:29:48.688123+00:00 incident resolved (remediated)
- 2026-09-23T13:29:48.688123+00:00 task resolved (recovery_verified)

## AI usage and cost
- Investigation: 4 model calls, 2000 input / 400 output tokens
- Report drafting: 0 model calls, 0 input / 0 output tokens
- Cost: unavailable: no operator-configured prices

## Unresolved questions
- Is this unconfirmed hypothesis correct: The application process is in a degraded state that a restart may clear (unconfirmed).?

## Follow-up items
- Watch for recurrence: the restart addressed the symptom and no cause is confirmed.
- Confirm or refute the unconfirmed hypotheses with additional evidence.
