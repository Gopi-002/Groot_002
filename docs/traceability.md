# Traceability matrix: FINAL (Phase 6)

**Statuses:** `PASS`, `PARTIAL`, `FAIL`, `NOT VERIFIED`. Nothing is marked PASS on
architecture alone.

**Tiers:**
- *Automated*: unit, or integration against real PostgreSQL/Redis.
- *Live*: the local 11-service compose stack, with a real Docker restart of the demo app.

**All AI behaviour is exercised with the deterministic mock model**, and no live Claude call
has ever been made. Real-model quality and cost are therefore **NOT VERIFIED** everywhere.
Nothing has been deployed to a VM. Detailed fault matrix and evidence:
[`phase6-acceptance.md`](phase6-acceptance.md) and
[`evidence/phase6-live-scenarios.json`](evidence/phase6-live-scenarios.json).

## Ten workflow steps
| # | Requirement | Code | Automated test | Live test | Evidence | Status | Residual risk |
|---|---|---|---|---|---|---|---|
| 1 | Monitor HTTP health and latency | `app/monitoring/{probe,service}.py` | `test_probe.py` (incl. hard-deadline D1 fix), `test_monitor_service.py` | scenario A; `test_b_demo_container_down_…` | 6 healthy checks, 0 incidents; probe bounded at 1.5 s when the target is gone | PASS | single monitor host; high latency tested automated only |
| 2 | Threshold + deduplicated incident | `detector.py`, `recorder.py` | `test_three_failures_create_exactly_one_…`, `test_concurrent_recorders_open_one_incident` | `test_failure_detected_once_…`, scenario B | 1 incident, 6 occurrences | PASS | — |
| 3 | Transactional task/outbox + durable dispatch | `outbox.py`, `dispatcher.py`, `worker.py` | Phase 2 crash matrix, `test_lost_stream_message_is_reconciled` | `test_redis_outage_db_retains_work_then_delivers` | at-least-once, idempotent | PASS | Redis AOF window (reconciled) |
| 4 | AI selects bounded read-only diagnostics | `investigation.py`, `tools.py` | `test_diagnostic_branching_…`, `test_tool_call_budget_enforced` | scenario D | 3 tools, all read-only, all ok | PASS (mock) | real-model tool choice NOT VERIFIED |
| 5 | Evidence-linked hypothesis + allowlisted proposal | `schema.py`, `validator.py` | `test_fabricated_evidence_id_…`, `test_prompt_injection_in_logs_is_contained` | scenario D | validated `restart_demo_app` | PASS (mock) | real-model quality NOT VERIFIED |
| 6 | Deterministic policy ALLOW / APPROVAL / DENY | `safety/policy.py` | `test_policy.py` (28 deny cases), `test_crash_during_policy_evaluation_…` | scenarios D (ALLOW), AP (REQUIRE_APPROVAL then ALLOW), E (DENY) | rule IDs recorded append-only | PASS | — |
| 7 | Restricted executor, one action, reconciliation | `safety/remediation.py`, `executor/` | crash matrix (`test_remediation.py`) | `test_worker_killed_during_execution_…`, scenarios D, AP, F (1 restart each) | ledger delta = executed attempts | PASS | Docker socket is root-equivalent |
| 8 | Deterministic recovery verification | `safety/verification.py` | `test_failed_recovery_…`, `test_stale_worker_cannot_overwrite_verification` | scenarios D (passed), F (failed, no second restart) | incident stays escalated on failure | PASS | log check has 1 s granularity |
| 9 | AI report with record-backed validation | `app/reporting/*` | `test_report_validator.py`, `test_reporting.py`, `test_summary_counts_failing_checks_not_occurrences` | scenarios D, E, F, AP; Phase 5 live | reports `validated`; fallback under AI outage | PASS (mock) | prose checks are heuristic; facts are record-rendered |
| 10 | Durable final state, metrics, notifications, continued monitoring | `finalize.py`, `notifications/*`, `metrics.py` | `test_notifications.py`, `test_metrics_exposed_with_bounded_labels` | scenario D (webhook delivered, 3 checks after resolution) | signed deliveries, exactly once per channel | PASS | no real vendor webhook tested |

**Branches:**

| Branch | Evidence | Status |
|---|---|---|
| `RETRY_SCHEDULED` | `test_transient_error_retries_with_backoff_then_succeeds` | PASS |
| `WAITING_APPROVAL` | scenario AP | PASS |
| `ESCALATED` | scenarios E and F | PASS |
| `FAILED` | `test_permanent_error_fails_task_without_retry` | PASS |
| `RESOLVED` | scenario D | PASS |
| Incident stays open when the task escalates | scenarios E and F | PASS |
| Guarded lifecycle | `test_database_guards_task_lifecycle_transitions` | PASS |

## Twelve reliability factors
| # | Factor | Implementation | Automated tests | Live validation | Observed | Status | Remaining risk |
|---|---|---|---|---|---|---|---|
| 1 | Availability | restart policies, health checks, live vs ready, `/v1/system/status`, hard probe deadline | `test_api_health.py`, `test_probe.py` | `test_readiness_fails_when_db_down_but_liveness_holds`, kill/restart tests | services recover; the monitor keeps working through DB/Redis/AI outages | PARTIAL | single-VM SPOF; no VM deployment; external host-down monitor NOT VERIFIED; 72 h soak not complete |
| 2 | Triggering | scheduled monitor + event-driven outbox/streams | Phase 2 suite | Phase 2 live | as specified | PASS | — |
| 3 | State | PostgreSQL authoritative; checkpoints, fencing, report jobs, notifications, ledger | `test_restart_preserves_db_data`, reporting crash tests | backup/restore drill (schema dropped and restored) | history identical after restore | PASS | backups are local until copied off-host |
| 4 | Failure recovery | retries with jitter, dead letters, reconcilers, ledger reconciliation, restore merge + tombstones | fault matrix | Phase 4/5/6 live | no loss or duplicates in tested faults | PASS | untested fault classes (disk full, clock skew) NOT VERIFIED |
| 5 | Autonomy limits | allowlist, default-deny policy, one restart per incident, approvals, autonomy off by default | `test_policy.py`, approval tests | scenarios E and AP | DENY → 0 attempts; concurrent approvals → exactly 1 | PASS | — |
| 6 | Security | network isolation, token auth, signed actions and approvals, redaction, SSRF-safe webhook, append-only | security tests (`phase6-acceptance.md` §5) | `test_privilege_boundaries_live`, secret scans | 0 secret hits | PARTIAL | Docker socket (root-equivalent); symmetric approval key in the worker (F1); env secrets; one DB role |
| 7 | Cost | per-investigation, per-incident and daily budgets; slots; usage ledger | budget/slot/idle tests | — | calls stop at budget | PASS (mock) | real token cost NOT VERIFIED |
| 8 | Concurrency / idempotency | dedup keys, leases, fencing, unique constraints | idempotency and fencing tests (`phase6-acceptance.md` §3–4) | duplicate XADD tests, 4 concurrent approvers | at-least-once, idempotent effects | PASS | — |
| 9 | Observability | JSON logs with correlation, bounded-label metrics, in-app alerts, Prometheus rules, dashboard | metrics/alerts tests; `promtool test rules` | `ai_auth_failed` fired live | rules pass promtool | PARTIAL | Prometheus/Alertmanager not deployed; external alerting NOT VERIFIED |
| 10 | Integrations | typed provider errors, timeouts, webhook classification, auth expiry | SDK-offline tests; webhook tests | invalid-key 401 against the real API (Phase 3); sink outage | as specified | PARTIAL | live Anthropic API calls with a valid key NOT VERIFIED; no real webhook vendor |
| 11 | Validation (quality) | schema, evidence, policy, report validator, deterministic fallback | validator §25 tests | scenario reports validated; injection test | fabricated claims rejected | PASS (mock) | real-model drafts may fall back more often (safe) |
| 12 | Human handoff | authenticated approvals with expiry, escalation notifications, runbook | approval API tests | scenario AP; escalation notifications delivered | as specified | PASS | operator tokens don't expire; no pager integration beyond a generic webhook |

**Overall:** the functional contract (10 steps, safety boundaries, idempotency) PASSES on the
local stack with the mock model. The following are **NOT VERIFIED**:
- live Claude behaviour;
- VM deployment;
- external host-down monitoring;
- the 72-hour soak (see `PHASE_STATUS.md`).
