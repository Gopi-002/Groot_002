# Incident report 05ff17e2-41af-4e70-9c95-21fc3fef3ac2 (v1)

_Narrative drafted by the deterministic MOCK model `mock-investigator-v1` (TEST/DEMO ONLY - not Claude) and accepted by SentinelOps' deterministic validator after 1 attempt(s). All facts sections are rendered directly from records._

- Incident ID: `05ff17e2-41af-4e70-9c95-21fc3fef3ac2`
- Service: `demo-app`
- Started (first failing check): 2026-09-23T13:31:17.844212+00:00
- Detected (incident opened): 2026-09-23T13:31:27.844235+00:00
- Severity / classification: high / http_error
- Current status: resolved (remediated)
- Record SHA-256: `d9f3e784610ad5e2f04a728971f478f218ca826fbfeedd5ef056d213fd5cc76f`; generated 2026-09-23T13:31:41.577495+00:00

## Summary
The monitor opened a http_error incident for demo-app after 3 consecutive failing checks (3 failing checks attributed to it in total). An AI investigation with model mock-investigator-v1 (a deterministic test model) proposed restart_demo_app; its hypotheses remain unconfirmed. The deterministic policy's last decision (pre_execution) was ALLOW with rules AUT-1. SentinelOps executed one restart of the demo application through the restricted executor. Recovery verification passed. The incident was resolved as remediated.

## Detection evidence (observed facts)
- Evidence `ad9b07a7-f27d-4c69-861b-8e75c2fd6863`: 3 consecutive `http_error` checks (threshold 3), collected 2026-09-23T13:31:27.848818+00:00
  - 2026-09-23T13:31:17.844212+00:00 outcome=unhealthy http=500 latency_ms=2.147
  - 2026-09-23T13:31:22.844156+00:00 outcome=unhealthy http=500 latency_ms=2.314
  - 2026-09-23T13:31:27.844235+00:00 outcome=unhealthy http=500 latency_ms=2.825
- Monitoring window 2026-09-23T13:26:17.844212+00:00 to 2026-09-23T13:31:36.381391+00:00: error=4, healthy=53, unhealthy=6

## Investigation (observed facts)
- Investigation `d998611f-b85d-4469-80f4-0b8bfd228cec`: status completed
- Model: `mock-investigator-v1`; auth mode `mock`; DETERMINISTIC MOCK (TEST/DEMO ONLY - not Claude)
- Diagnostic tool calls: 3; model calls: 4; reasoning attempts: 0

## Evidence reviewed
- `ad9b07a7-f27d-4c69-861b-8e75c2fd6863` health_check at 2026-09-23T13:31:27.848818+00:00
- `0b307a70-3315-413b-8028-c89e9be5b9d2` tool/get_incident (ok) at 2026-09-23T13:31:28.620103+00:00
- `b429c503-8e87-4450-b639-f16024dec0c6` tool/get_application_logs (ok) at 2026-09-23T13:31:28.656793+00:00
- `fe5983c1-7848-45b5-8137-a1919c5665c3` tool/get_container_status (ok) at 2026-09-23T13:31:28.683496+00:00
- `b93fcc85-eccc-43e7-9667-89a7b413f6e2` verification/recovery_verification at 2026-09-23T13:31:36.378025+00:00

### Observations (report narrative; each cites evidence)
- The monitor recorded 3 consecutive failing checks of type http_error. [`ad9b07a7-f27d-4c69-861b-8e75c2fd6863`]
- Diagnostic get_incident returned status ok. [`0b307a70-3315-413b-8028-c89e9be5b9d2`]
- Diagnostic get_application_logs returned status ok. [`b429c503-8e87-4450-b639-f16024dec0c6`]
- Diagnostic get_container_status returned status ok. [`fe5983c1-7848-45b5-8137-a1919c5665c3`]

## Hypotheses (AI investigation; UNCONFIRMED, not root causes)
- The application process is in a degraded state that a restart may clear (unconfirmed). (confidence medium)

## Proposed action (what the AI recommended)
- `restart_demo_app` target `demo-app` citing 3 evidence record(s)

## Policy decisions (deterministic policy engine)
- 2026-09-23T13:31:29.613755+00:00 proposal: **ALLOW** rules AUT-1 (policy 5f01b017223b3c64) `65558059-a9f9-4f1f-93df-26c7c8af7f5c`
- 2026-09-23T13:31:29.620945+00:00 pre_execution: **ALLOW** rules AUT-1 (policy 5f01b017223b3c64) `e9bf4ac4-647b-465c-8c50-b1fea55de1d1`

## Approval history
- no approval was requested

## Actions actually executed (executor records)
- `f7d59ecd-631b-455c-9de0-995ca0c36c55` restart_demo_app: status **succeeded** (EXECUTED); started 2026-09-23T13:31:29.643009+00:00, completed 2026-09-23T13:31:30.317495+00:00; recorded via executor_response

## Recovery verification (deterministic verifier)
- `3782370e-4381-46c0-ac6e-4c6b25632061`: **passed** - 3 consecutive healthy probes under 2.0s and no new critical errors (3/4 healthy probes, 0 critical log lines)

## Outcome (durable state)
- Incident: resolved / remediated
- Task: resolved / recovery_verified

## Timeline
- 2026-09-23T13:31:17.844212+00:00 first failing http_error check
- 2026-09-23T13:31:27.844235+00:00 incident opened (http_error)
- 2026-09-23T13:31:28.603473+00:00 investigation started (mock-investigator-v1)
- 2026-09-23T13:31:28.620103+00:00 diagnostic get_incident (ok)
- 2026-09-23T13:31:28.656793+00:00 diagnostic get_application_logs (ok)
- 2026-09-23T13:31:28.683496+00:00 diagnostic get_container_status (ok)
- 2026-09-23T13:31:28.696294+00:00 investigation completed
- 2026-09-23T13:31:29.613755+00:00 policy proposal: ALLOW AUT-1
- 2026-09-23T13:31:29.620945+00:00 policy pre_execution: ALLOW AUT-1
- 2026-09-23T13:31:29.643009+00:00 restart_demo_app execution started
- 2026-09-23T13:31:30.317495+00:00 restart_demo_app succeeded
- 2026-09-23T13:31:36.378025+00:00 recovery verification passed
- 2026-09-23T13:31:36.381391+00:00 incident resolved (remediated)
- 2026-09-23T13:31:36.381391+00:00 task resolved (recovery_verified)

## AI usage and cost
- Investigation: 4 model calls, 2000 input / 400 output tokens
- Report drafting: 0 model calls, 0 input / 0 output tokens
- Cost: unavailable: no operator-configured prices

## Unresolved questions
- Is this unconfirmed hypothesis correct: The application process is in a degraded state that a restart may clear (unconfirmed).?

## Follow-up items
- Watch for recurrence: the restart addressed the symptom and no cause is confirmed.
- Confirm or refute the unconfirmed hypotheses with additional evidence.
