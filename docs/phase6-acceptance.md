# Phase 6: final acceptance matrix, fault-injection matrix and validation evidence

**Date:** 2026-09-23.

**Model under test:** the deterministic **mock** (`mock-investigator-v1`, TEST/DEMO ONLY, not
Claude). Live Claude validation is **NOT VERIFIED** (§7).

**Tiers:**
- **AUTO**: automated unit or integration test (real PostgreSQL 16 and Redis 7.4 for
  integration).
- **LIVE**: live test against the local 11-service compose stack (real containers, a real Docker
  restart of `demo-app`, the real executor ledger).
- **DEPLOY**: verified on a deployed VM. **Nothing is DEPLOY-verified.**

Statuses are `PASS`, `PARTIAL`, `FAIL` or `NOT VERIFIED`. Command results are those of the final
Phase 6 run recorded in `PHASE_STATUS.md`. Live scenario evidence (IDs, decisions, counts; no
secrets) is in [`evidence/phase6-live-scenarios.json`](evidence/phase6-live-scenarios.json).

## 1. Acceptance matrix (`06-END-TO-END-VALIDATION.md`)
| ID | Requirement | Implementation | Test / evidence | Result | Status | Remaining risk |
|---|---|---|---|---|---|---|
| R1 | Traceability matrix: 10 steps and 12 factors, untested marked NOT VERIFIED | `docs/traceability.md` | this document | written | PASS | — |
| R2 | Unit, integration and resilience runs with recorded commands, versions and results | `scripts/test.sh` | `PHASE_STATUS.md` Phase 6 | see status | PASS | — |
| R2-A | Scenario A: healthy baseline | monitor | LIVE `test_a_healthy_baseline_records_checks_and_opens_no_incident` | 6 healthy checks, 0 incidents | PASS | — |
| R2-B | Scenario B: 3 failures create exactly one incident | detector, recorder, partial unique index | AUTO `test_three_failures_create_exactly_one_incident_task_and_outbox`, `test_concurrent_recorders_open_one_incident`; LIVE `test_failure_detected_once_dispatched_and_not_duplicated`, `test_timeout_mode_detected_as_unavailable`, `test_b_demo_container_down_opens_exactly_one_unavailable_incident` | 1 incident (`unavailable`, 6 occurrences) | PASS (after defect D1 fix) | — |
| R2-C | Scenario C: model picks read-only tools, evidence-backed hypothesis, allowlisted proposal | investigation, tools, validator | AUTO `test_diagnostic_branching_depends_on_tool_results`, `test_fabricated_evidence_id_rejected_then_corrected`; LIVE scenario D | `get_incident`, `get_application_logs`, `get_container_status` all ok; `restart_demo_app` proposed | PASS (mock) | real-model behaviour NOT VERIFIED |
| R2-D | Scenario D: policy allows demo restart, verification passes, report saved | remediation, verification, reporting | LIVE `test_d_healthy_path_end_to_end_with_evidence` | `ALLOW{AUT-1}` twice; 1 restart; verification passed; incident `resolved:remediated`; report v1 `validated`; webhook delivered | PASS | — |
| R2-E | Scenario E: policy denies an unauthorized action | policy (`ENV-*`, `TGT-*`, `ACT-1`) | LIVE `test_e_blocked_action_denied_with_no_side_effect_and_accurate_report`; AUTO `test_denied_actions_never_execute` (wrong target `TGT-1`), `test_unsupported_action_in_stored_result_denied` (`ACT-1`), `test_production_never_restarts` (`ENV-1`) | `DENY{ENV-2}`; 0 attempts; ledger delta 0; incident escalated; report says "no action was executed" | PASS | — |
| R2-F | Scenario F: recovery fails and escalates without closing the incident | verifier, one restart limit | LIVE `test_f_failed_recovery_one_restart_no_recovery_claim_critical_alert`, `test_failed_recovery_escalates_without_second_restart` | 1 restart; verification failed; incident escalated (open); no recovery claim; critical notification delivered | PASS | — |
| R3 | Fault injection with recovery measured and no unauthorized or duplicate side effects | see §2 | §2 | — | PASS (local) / PARTIAL (VM outage) | host/VM outage detection NOT VERIFIED |
| R4 | Both auth paths | `app/auth/decision.py` | subscription: UNSUPPORTED (documented, disabled, tested); API key: offline SDK tests plus a live 401 with an invalid key (Phase 3) | subscription UNSUPPORTED; paid API run NOT VERIFIED | PARTIAL | no authorized key or spend |
| R4-b | Model selection, per-task pinning, all AI stages use the chosen model | `model_config`, `pin_model`, report job pinning | AUTO `test_model_change_affects_only_new_tasks`, `test_resolved_incident_gets_validated_ai_report_and_notifications` (both stages use `mock-investigator-v1`) | pass | PASS (mock) | — |
| R4-c | AI unavailable: monitoring and queue persist, no fake analysis, alerts fire, tasks resume after auth is restored | parking, alerts, fallback reports | LIVE `test_missing_credentials_pause_ai_while_monitoring_continues_then_resume`, `test_ai_unavailable_fallback_report_while_monitoring_continues`; AUTO `test_expired_ai_auth_yields_actionable_alert_and_degraded_status` | pass | PASS | — |
| R5 | VM deployment (TLS, ingress, volumes, backups, external monitor, alerts, least privilege, RTO/RPO) | `docs/deployment.md` | §6 | not deployed (no authorization or credentials) | NOT VERIFIED | — |
| R6 | 72 h soak, or PENDING with exact operator instructions | `scripts/soak.py` | `PHASE_STATUS.md` Phase 6 soak section | run `4aeb63b7dd76` interrupted by a Codespace restart after 9.66 min; gap ≥ 367 min, so the `verify` rule `harness_gap_minutes <= max_gap_minutes (30)` fails irreversibly; no invariant check failed (`soak-results/72h/interruption.json`) | **FAILED** (not recoverable; replacement needs approval) | a Codespace is not always-on; use the VM |
| R7 | README quickstart, architecture, runbook, threat model, auth decision, sample report, test evidence, traceability, limitations | docs | `README.md`, `docs/{architecture,runbook,threat-model,auth-decision,traceability,deployment}.md`, `docs/examples/sample-incident-report.md`, `docs/evidence/` | written | PASS | — |

## 2. Fault-injection matrix
**Test tiers** are named in the Test column. Every "no duplicate or unauthorized side effect"
claim is backed by a restart count taken from the fake Docker API (AUTO) or from the executor
ledger and container `StartedAt` (LIVE).

### Monitoring
| Fault | Expected | Observed | Persistent state / recovery | Test | Result |
|---|---|---|---|---|---|
| timeout | `unavailable` after 3 | as expected | detection_state streak | LIVE `test_timeout_mode_detected_as_unavailable` | PASS |
| HTTP 500 | `http_error` after 3 | as expected | same | LIVE `test_failure_detected_once_…` | PASS |
| high latency | `high_latency` after 3 slow 2xx | as expected | same | AUTO `test_latency_threshold_creates_incident`, `test_slow_2xx_is_degraded_high_latency` | PASS (AUTO only; the demo has no slow mode) |
| demo unavailable (container stopped) | `unavailable` within about 3 intervals | **FAILED at first (defect D1)**, then about 4 s after the fix | probe bounded by a hard deadline | LIVE `test_b_…`; AUTO `test_probe_deadline_bounds_hung_name_resolution_and_slow_io` | PASS after fix |

### PostgreSQL
| Fault | Expected | Observed | Recovery | Test | Result |
|---|---|---|---|---|---|
| down during monitoring | probes continue, results buffered and recorded later | as expected | ordered buffer | LIVE `test_monitor_keeps_probing_through_database_outage`, `test_readiness_fails_when_db_down_but_liveness_holds` | PASS |
| down during task processing | no loss; resume | as expected | lease/fencing; reconciler | LIVE `test_postgres_and_redis_interruptions_while_awaiting_approval` | PASS |
| down / error during reporting | job retried; one report | as expected | job retry with backoff | AUTO `test_database_error_during_reporting_retries_to_one_report` (new); LIVE `test_postgres_interruption_notifier_survives_and_delivers` | PASS |
| restart | data preserved | as expected | volume | LIVE `test_restart_preserves_db_data` | PASS |

### Redis
| Fault | Expected | Observed | Recovery | Test | Result |
|---|---|---|---|---|---|
| down before dispatch | work kept in PG, published later | as expected | outbox backoff | AUTO `test_redis_outage_retains_work_in_db_and_retries`; LIVE `test_redis_outage_db_retains_work_then_delivers`, `test_redis_outage_delays_but_never_loses_report_work` | PASS |
| down after DB commit | row unpublished, then published | as expected | outbox | AUTO `test_crash_after_commit_before_publish_is_eventually_published` | PASS |
| lost stream delivery | reconciler re-dispatches | as expected | `reconcile_stuck_tasks`, `schedule_report_jobs` | AUTO `test_lost_stream_message_is_reconciled` | PASS |
| duplicate delivery | ACK; no second effect | as expected | claim guard | AUTO `test_crash_after_publish_before_mark_cannot_duplicate_work`, `test_duplicate_stream_delivery_never_restarts_twice`; LIVE `test_duplicate_deliveries_do_not_restart_again`, `test_duplicate_report_and_notification_work_is_idempotent` | PASS |
| pending-entry recovery | XAUTOCLAIM | as expected | PEL | AUTO `test_expired_lease_message_reclaimed_via_pending_recovery`; LIVE `test_worker_down_then_task_recovered_on_restart` | PASS |

### Worker crash points
| Crash point | Expected | Test | Result |
|---|---|---|---|
| during investigation | resume from the step-4 checkpoint, model kept pinned | AUTO `test_worker_crash_resumes_with_checkpointed_evidence_and_budget` | PASS |
| after a model call (report) | usage recorded; one report | AUTO `test_crash_during_reporting_recovers_to_exactly_one_report[after_model_call]` | PASS |
| during policy | nothing durable; re-evaluated; 1 restart | AUTO `test_crash_during_policy_evaluation_leaves_no_side_effect_and_recovers` (new) | PASS |
| before action (after reservation) | re-checked, then 1 restart | AUTO `test_crash_after_reservation_before_execution`, `test_crash_before_request_reached_executor` | PASS |
| during action | reconcile; no duplicate | AUTO crash matrix; LIVE `test_worker_killed_during_execution_reconciles_without_duplicate` | PASS |
| after action, before record | reconciled from the ledger; no repeat | AUTO `test_crash_after_execution_before_recording_reconciles_no_repeat` | PASS |
| during verification | verify once; no second restart | AUTO `test_crash_after_recording_before_verification` (crash inside the probe) | PASS |
| during report generation | one report | AUTO `test_crash_during_reporting_…[before_model_call, before_persist]`; LIVE `test_worker_killed_during_reporting_recovers_to_one_report` | PASS |
| after report persistence, before ACK | duplicate ACKed | AUTO `test_crash_after_persistence_and_duplicate_delivery_ack_without_second_report` | PASS |

### Executor
| Fault | Expected | Test | Result |
|---|---|---|---|
| unavailable | transient; dead-letter; never executes | AUTO `test_executor_unavailable_before_execution_is_transient`; LIVE `test_executor_unavailable_never_executes_and_escalates` | PASS |
| timeout / no response | outcome unknown, then reconcile (never blind re-issue) | AUTO `test_crash_before_request_reached_executor`, `test_executor_interrupted_mid_action_outcome_unknown_escalates` | PASS |
| request replay | recorded result returned | AUTO ledger `replay` tests; LIVE post-restore replay | PASS |
| uncertain result | `unknown`, then escalate / reconcile from `StartedAt` | AUTO `test_executor_interrupted_but_restart_observed_is_reconciled` | PASS |
| completed-action replay after restore | `replayed: true`, no restart | LIVE `test_backup_and_restore_preserve_history_and_executor_idempotency` | PASS |

### AI provider (mock / SDK-offline)
| Fault | Test | Result |
|---|---|---|
| credentials missing | AUTO `test_missing_credentials_pause`, `test_missing_credentials_fall_back_without_any_model_call`; LIVE Phase 3 and Phase 5 | PASS |
| auth expired / invalid | AUTO `test_provider_auth_quota_rate_limits_pause_and_preserve_task`, `test_error_classification`; alert `test_expired_ai_auth_yields_actionable_alert_and_degraded_status` | PASS |
| rate limited | same (429 → park) | PASS |
| quota / budget exhausted | same (402) + `test_daily_budget_…`, `test_per_incident_budget_…` | PASS |
| provider unavailable | `test_provider_outage_bounded_retries_then_dead_letter`, `test_transient_provider_failure_retries_then_final_attempt_is_deterministic` | PASS |
| malformed output | `test_invalid_outputs_exhaust_attempts_then_escalate`, `test_repeatedly_invalid_drafts_fall_back_deterministically` | PASS |
| fabricated evidence | `test_fabricated_evidence_id_rejected_then_corrected`, validator §25 tests | PASS |
| prompt injection | `test_prompt_injection_in_logs_is_contained`, `test_prompt_injection_in_logs_cannot_steer_the_report` | PASS (mock) |

### Notifications
| Fault | Test | Result |
|---|---|---|
| provider unavailable, retry | AUTO `test_provider_outage_retries_with_backoff_then_delivers`; LIVE `test_notification_outage_retries_and_restart_with_pending_notifications` | PASS |
| dead letter | AUTO `test_retries_are_bounded_then_dead_lettered_and_alerted`, `test_client_errors_are_not_retried` | PASS |
| restart with pending delivery | LIVE (same outage test); AUTO `test_pending_notifications_survive_notifier_restart` | PASS |
| duplicate delivery | AUTO `test_duplicate_events_are_deduplicated`, `test_concurrent_notifiers_send_each_delivery_once`, `test_crash_after_send_before_record_redelivers_same_idempotency_key` | PASS (at-least-once with `Idempotency-Key`) |

### Backup / recovery
| Fault | Test | Result |
|---|---|---|
| PostgreSQL lost (schema dropped), then restore | LIVE `test_backup_and_restore_…` | PASS |
| executor ledger lost, then restore (merge + tombstones) | LIVE same; AUTO `test_ledger_tool.py` (6) | PASS |
| restored action cannot execute twice | LIVE: signed replay gives `replayed: true`, `StartedAt` unchanged | PASS |

## 3. Idempotency (at-least-once delivery, idempotent / reconciled side effects)
We **do not** claim exactly-once queue delivery.

| Logical unit | Guard | Evidence |
|---|---|---|
| one incident | partial unique index plus detector disarm | `test_concurrent_recorders_open_one_incident`; LIVE scenario B (6 occurrences, 1 incident) |
| one investigation result | `investigations.task_id` unique; fenced persist | `test_duplicate_delivery_after_completion_does_not_rerun` |
| one policy path per lease | unique `(task, phase, fencing_token)`, append-only | `test_crash_during_policy_evaluation_…` |
| ≤ 1 restart per incident | deterministic action ID; `uq_action_attempts_one_per_incident`; ledger | crash matrix; LIVE restart counts = 1 in D, AP and F |
| one verification per action | `verifications.action_attempt_id` unique; fenced | `test_stale_worker_cannot_overwrite_verification` (new) |
| one report per report version | `reports.job_id` unique; `(incident_id, version)` unique; fenced | Phase 5 reporting crash tests |
| one notification event per dedup key | `notification_events.dedup_key` unique; one delivery per channel | `test_duplicate_events_are_deduplicated` |

## 4. Fencing and stale-worker safety
A stale worker **cannot**:
- persist an investigation: `test_stale_executor_cannot_persist_investigation`;
- advance or authorize a task: `test_two_workers_racing_only_current_fencing_holder_advances`, plus the DB fencing trigger on checkpoints;
- execute: `test_stale_worker_cannot_execute`, where the executor refuses a lower fencing token;
- overwrite a verification: `test_stale_worker_cannot_overwrite_verification`;
- publish a competing report: `test_stale_report_worker_is_fenced_out`.

All PASS.

## 5. Security acceptance
| Check | Evidence | Result |
|---|---|---|
| worker has no Docker socket, no host shell (read-only root FS, `cap_drop: ALL`), is not on `demo_net`; the model has only 6 read-only tools and no filesystem, network-scan or URL tool | LIVE `test_privilege_boundaries_live`, `test_datastores_not_published_by_default`; tool allowlist tests | PASS |
| only `ops-reader` and `executor` hold the socket | `test_datastores_not_published_by_default` (holders are exactly `{ops-reader, executor}`) | PASS |
| executor accepts only the trusted target and action | `test_request_cannot_choose_target`, signature/expiry/fingerprint tests | PASS |
| ops-reader is read-only | `test_ops_reader.py` (GET-only) | PASS |
| executor network restricted | `exec_net` internal only; LIVE unreachability from api, monitor and demo | PASS |
| notification destinations come only from config | `test_untrusted_webhook_destinations_rejected`, settings validation | PASS |
| API fails closed; approval authorization works | `test_api_v1_auth.py`, `test_approval_api.py`, LIVE scenario AP (wrong fingerprint 409; 4 concurrent approvers give 200/409/409/409) | PASS |
| secrets redacted in logs; none in reports, payloads or AI DB records | unit redaction; `test_payloads_never_contain_secrets`; `test_key_never_stored_in_database`; LIVE `test_no_secrets_in_any_phase5_service_logs` | PASS |
| committable-file secret scan | `PHASE_STATUS.md` Phase 6: 0 hits | PASS |
| the **AI worker process holds approval-verification credentials** (symmetric HMAC key) | threat model T8 | **PARTIAL (finding)**: the model cannot reach it, but a compromised worker process could forge an approval signature. Recommended: asymmetric signatures. Not changed in Phase 6 (it would be a redesign). |
| Docker socket is root-equivalent | threat model T11/T16 | Accepted risk; documented |

## 6. Deployment validation
| Item | Status |
|---|---|
| always-on VM | NOT VERIFIED (no VM or credentials provided; nothing deployed) |
| persistent storage (named volumes) | CONFIGURED BUT NOT DEPLOYED (verified locally: `test_restart_preserves_db_data`) |
| Docker restart policies (`unless-stopped`) | CONFIGURED BUT NOT DEPLOYED (locally verified by the kill / restart tests) |
| TLS reverse proxy | DOCUMENTED ONLY (`docs/deployment.md` Caddy example) |
| authenticated API | CONFIGURED BUT NOT DEPLOYED (locally tested) |
| secret handling (secrets manager / Docker secrets) | DOCUMENTED ONLY (compose uses `.env` and env vars) |
| external uptime monitoring | **NOT VERIFIED**; no external monitor is available. Procedure in `docs/deployment.md` §8 |
| Prometheus | CONFIGURED BUT NOT DEPLOYED (`deploy/observability/prometheus.yml`) |
| Alertmanager / routing | DOCUMENTED ONLY |
| backups | CONFIGURED BUT NOT DEPLOYED (`scripts/backup.sh`; locally drilled) |
| off-host backup copy | DOCUMENTED ONLY |
| firewall / network restrictions | internal compose networks CONFIGURED (locally verified); host firewall DOCUMENTED ONLY |
| AI egress restriction to `api.anthropic.com` | DOCUMENTED ONLY |
| notification egress | CONFIGURED BUT NOT DEPLOYED (`docker-compose.notify-egress.yml`; off by default) |
| log retention | CONFIGURED BUT NOT DEPLOYED (json-file 10 MB × 5; health checks 7 d; notifications 30 d) |
| resource limits | CONFIGURED BUT NOT DEPLOYED (compose `deploy.resources.limits`) |

## 7. Live Claude validation
**NOT VERIFIED.**
- **Reason:** no Anthropic API key is configured (the `ai_secrets` volume holds no key file,
  checked 2026-09-23 without reading contents), and there is no explicit authorization for paid
  calls.
- The exact bounded procedure for a later run is in `docs/runbook.md` §10.
- Everything else was validated with the mock model, which is clearly labelled.

## 8. Alerting validation
| Alert | Unit (in-app) | promtool | Live | External |
|---|---|---|---|---|
| monitor silence | UNIT TESTED `test_monitor_silence_and_queue_backlog_alerts` | UNIT TESTED (`alerts_test.yml`) | — | NOT VERIFIED |
| queue backlog | UNIT TESTED | UNIT TESTED (negative case) | — | NOT VERIFIED |
| stalled incident | UNIT TESTED `test_stalled_incident_and_budget_alerts` | rule checked | — | NOT VERIFIED |
| AI auth failure | UNIT TESTED | UNIT TESTED | LIVE TESTED (`ai_auth_failed` fired, delivered as `ai_paused`) | NOT VERIFIED |
| budget exhaustion | UNIT TESTED | rule checked | — | NOT VERIFIED |
| reporting failure | rule evaluated in-app (`report_failures`) | rule checked | — | NOT VERIFIED |
| notification failure | UNIT TESTED `test_retries_are_bounded_then_dead_lettered_and_alerted` | rule checked | — | NOT VERIFIED |
| backup failure | UNIT TESTED `test_alerts_fire_once_and_resolve_once` | UNIT TESTED | — | NOT VERIFIED |
| service heartbeat loss / host down | in-app `service_down` rule | `SentinelOpsHostOrApiDown` UNIT TESTED | — | **NOT VERIFIED** (needs an external vantage point) |

## 9. Defects found in Phase 6
| ID | Failing criterion | Root cause | Fix | Regression test |
|---|---|---|---|---|
| D1 | R2-B / monitoring "demo unavailable": no incident when the demo container is **stopped** (fast cadence) | httpx timeouts do not bound `getaddrinfo`, and Docker DNS stalled about 10 s for the vanished container. Probes overran the 1.5 s timeout, and at fast cadence the stale-gap rule reset every streak. At the default 30 s cadence detection still happened, but probes took 10 s instead of ≤ 5 s | `HealthProbe.check` enforces a **hard deadline** (bounded thread pool; `ProbeDeadlineExceeded` counts as `timeout`, i.e. `unavailable`) | `test_probe_deadline_bounds_hung_name_resolution_and_slow_io`; LIVE `test_b_…` (incident about 4 s after the stop) |
| D2 | Report accuracy: the summary said "(1 failing checks recorded in total)" | `occurrence_count` (1 + bumps) was rendered as a count of failing checks | render `threshold + occurrence_count - 1` | `test_summary_counts_failing_checks_not_occurrences`; sample report is v2, produced by the fixed code |
| D3 | Phase 5 soak evidence | the harness kept state in memory and wrote only at the end; the run was interrupted, so no artifact exists | `scripts/soak.py` rewritten: detached, persisted, resumable, `status` / `verify` commands | smoke-tested; the 72 h run uses it |
| F1 (finding, not fixed) | "AI worker has no approval credentials" | the worker holds the symmetric approval HMAC key (to verify signatures) | recommended: asymmetric signatures | threat model T8 |
