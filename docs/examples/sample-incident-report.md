<!--
SAMPLE INCIDENT REPORT - TEST/DEMO ONLY — NOT CLAUDE
Source: an actual Phase 6 live acceptance incident on the local compose stack
(scenario D, 2026-09-23), report version 2, retrieved verbatim from
GET /v1/incidents/<id>/report?version=2. Drafted by the deterministic MOCK model
and accepted by the deterministic validator; every facts section is rendered from
PostgreSQL records. Redaction: the report format contains no credentials, tokens,
signatures or endpoints (checked: 0 matches for any .env secret value); UUIDs and
content hashes are record identifiers, not secrets, and are kept for traceability.
Version 1 of the same incident exhibited a wording defect found in Phase 6
("1 failing checks recorded in total"); it was fixed and v2 generated. Reports are
immutable, so v1 still exists in the database.
-->

# Incident report e03fd6bb-2777-4a9c-b753-2d79e2de1c43 (v2)

_Narrative drafted by the deterministic MOCK model `mock-investigator-v1` (TEST/DEMO ONLY - not Claude) and accepted by SentinelOps' deterministic validator after 1 attempt(s). All facts sections are rendered directly from records._

- Incident ID: `e03fd6bb-2777-4a9c-b753-2d79e2de1c43`
- Service: `demo-app`
- Started (first failing check): 2026-09-23T05:51:56.552705+00:00
- Detected (incident opened): 2026-09-23T05:52:00.552774+00:00
- Severity / classification: high / http_error
- Current status: resolved (remediated)
- Record SHA-256: `98294f67e4ad44bae17d3909d0264e194600894c66fef7e98ec098d82a47182e`; generated 2026-09-23T05:56:40.104063+00:00

## Summary
The monitor opened a http_error incident for demo-app after 3 consecutive failing checks (3 failing checks attributed to it in total). An AI investigation with model mock-investigator-v1 (a deterministic test model) proposed restart_demo_app; its hypotheses remain unconfirmed. The deterministic policy's last decision (pre_execution) was ALLOW with rules AUT-1. SentinelOps executed one restart of the demo application through the restricted executor. Recovery verification passed. The incident was resolved as remediated.

## Detection evidence (observed facts)
- Evidence `6e897b14-c4e1-4275-a453-dbe449ea04ef`: 3 consecutive `http_error` checks (threshold 3), collected 2026-09-23T05:52:00.556992+00:00
  - 2026-09-23T05:51:56.552705+00:00 outcome=unhealthy http=500 latency_ms=2.326
  - 2026-09-23T05:51:58.552754+00:00 outcome=unhealthy http=500 latency_ms=2.367
  - 2026-09-23T05:52:00.552774+00:00 outcome=unhealthy http=500 latency_ms=2.715
- Monitoring window 2026-09-23T05:46:56.552705+00:00 to 2026-09-23T05:52:10.553861+00:00: error=16, healthy=56, timeout=10, unhealthy=26

## Investigation (observed facts)
- Investigation `0ca41dd3-ec89-48f1-86d0-4021244d4ee9`: status completed
- Model: `mock-investigator-v1`; auth mode `mock`; DETERMINISTIC MOCK (TEST/DEMO ONLY - not Claude)
- Diagnostic tool calls: 3; model calls: 4; reasoning attempts: 0

## Evidence reviewed
- `6e897b14-c4e1-4275-a453-dbe449ea04ef` health_check at 2026-09-23T05:52:00.556992+00:00
- `da75ed41-f0f3-47b3-8453-04e71e3a662f` tool/get_incident (ok) at 2026-09-23T05:52:00.939975+00:00
- `77f0e27d-fa18-446d-acfe-8fd7879297d6` tool/get_application_logs (ok) at 2026-09-23T05:52:01.022753+00:00
- `ffc8016e-0e54-4eef-b058-2c77a8588e9e` tool/get_container_status (ok) at 2026-09-23T05:52:01.054430+00:00
- `e0d94cf7-8d5c-403b-b416-f237eabd9d61` verification/recovery_verification at 2026-09-23T05:52:10.549355+00:00

### Observations (report narrative; each cites evidence)
- The monitor recorded 3 consecutive failing checks of type http_error. [`6e897b14-c4e1-4275-a453-dbe449ea04ef`]
- Diagnostic get_incident returned status ok. [`da75ed41-f0f3-47b3-8453-04e71e3a662f`]
- Diagnostic get_application_logs returned status ok. [`77f0e27d-fa18-446d-acfe-8fd7879297d6`]
- Diagnostic get_container_status returned status ok. [`ffc8016e-0e54-4eef-b058-2c77a8588e9e`]

## Hypotheses (AI investigation; UNCONFIRMED, not root causes)
- The application process is in a degraded state that a restart may clear (unconfirmed). (confidence medium)

## Proposed action (what the AI recommended)
- `restart_demo_app` target `demo-app` citing 3 evidence record(s)

## Policy decisions (deterministic policy engine)
- 2026-09-23T05:52:01.846130+00:00 proposal: **ALLOW** rules AUT-1 (policy 5f01b017223b3c64) `08bff6f6-c5b9-430e-94ac-6f666e6fb0b5`
- 2026-09-23T05:52:01.863902+00:00 pre_execution: **ALLOW** rules AUT-1 (policy 5f01b017223b3c64) `6cfe03db-f2e6-4f2e-b003-4c67b9734141`

## Approval history
- no approval was requested

## Actions actually executed (executor records)
- `a25a325e-6f5e-48ad-bbe3-f5d58b4330f8` restart_demo_app: status **succeeded** (EXECUTED); started 2026-09-23T05:52:01.891726+00:00, completed 2026-09-23T05:52:02.451173+00:00; recorded via executor_response

## Recovery verification (deterministic verifier)
- `c194100b-b7e0-457b-b48b-d3302e504436`: **passed** - 3 consecutive healthy probes under 2.0s and no new critical errors (3/5 healthy probes, 0 critical log lines)

## Outcome (durable state)
- Incident: resolved / remediated
- Task: resolved / recovery_verified

## Timeline
- 2026-09-23T05:51:56.552705+00:00 first failing http_error check
- 2026-09-23T05:52:00.552774+00:00 incident opened (http_error)
- 2026-09-23T05:52:00.844217+00:00 investigation started (mock-investigator-v1)
- 2026-09-23T05:52:00.939975+00:00 diagnostic get_incident (ok)
- 2026-09-23T05:52:01.022753+00:00 diagnostic get_application_logs (ok)
- 2026-09-23T05:52:01.054430+00:00 diagnostic get_container_status (ok)
- 2026-09-23T05:52:01.134862+00:00 investigation completed
- 2026-09-23T05:52:01.846130+00:00 policy proposal: ALLOW AUT-1
- 2026-09-23T05:52:01.863902+00:00 policy pre_execution: ALLOW AUT-1
- 2026-09-23T05:52:01.891726+00:00 restart_demo_app execution started
- 2026-09-23T05:52:02.451173+00:00 restart_demo_app succeeded
- 2026-09-23T05:52:10.549355+00:00 recovery verification passed
- 2026-09-23T05:52:10.553861+00:00 incident resolved (remediated)
- 2026-09-23T05:52:10.553861+00:00 task resolved (recovery_verified)

## AI usage and cost
- Investigation: 4 model calls, 2000 input / 400 output tokens
- Report drafting: 1 model calls, 1200 input / 400 output tokens
- Cost: unavailable: no operator-configured prices

## Unresolved questions
- Is this unconfirmed hypothesis correct: The application process is in a degraded state that a restart may clear (unconfirmed).?

## Follow-up items
- Watch for recurrence: the restart addressed the symptom and no cause is confirmed.
- Confirm or refute the unconfirmed hypotheses with additional evidence.
