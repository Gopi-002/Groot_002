# PHASE_STATUS

| Phase | Status |
|---|---|
| 1 — Foundation and infrastructure | Implemented and tested |
| 2 — Monitoring, detection and durable dispatch | Implemented and tested |
| 3 — Claude auth, model selection, AI investigation | Implemented and tested (no live Claude call has been made) |
| 4 — Policy, approval, execution, recovery verification | Implemented and tested |
| 5 — Reliability and reporting | Implemented and tested (mock model only; no paid calls). **Phase 5 60-min soak: NOT VERIFIED** (started, interrupted, no artifact; see Phase 6 audit) |
| 6 — End-to-end validation | **FAILED VALIDATION (72-hour soak criterion not met).** Soak run `4aeb63b7dd76` was interrupted by a Codespace restart after 9.66 min observed; monitoring gap 6 h 07 min (limit 30 min). No application invariant was violated. A replacement soak needs approval. Live Claude, VM deployment and external host-down monitoring: NOT VERIFIED |

---

## Phase 1 — Foundation (2026-09-22)

### Environment inspected
- Repo contained only spec files (`CLAUDE.md`, `01`–`06-*.md`, `README.md`), with no code or tests.
- Host: Python 3.12.3 (`/usr/bin/python3.12`; the default `python3` is 3.14, so the project is pinned to `>=3.12,<3.14`), uv 0.12.17, Docker 29.8.0, Compose v5.5.1.
- Images: `python:3.12.14-slim`, `postgres:16.15-alpine`, `redis:7.4.11-alpine`, `ghcr.io/astral-sh/uv:0.12.17`.
- Locked Python deps (`uv.lock`): fastapi 0.141.1, starlette 1.6.0, uvicorn 0.53.0, pydantic 2.13.5, pydantic-settings 2.15.0, SQLAlchemy 2.0.54, alembic 1.20.0, psycopg 3.3.6, redis 8.1.0. Dev: pytest 9.1.1, httpx 0.28.1, ruff 0.16.8, mypy 2.3.1.

### Plan (as executed)
1. Toolchain: uv lock, ruff (format and lint, incl. bandit `S` rules), mypy strict, pytest.
2. `demo_application/`: `/health`, `/metrics`, and `/simulate-failure` gated on demo env plus token plus a private or loopback client, with modes `none|timeout|http_500|memory_log`.
3. `app/`: typed validated settings, JSON logging with redaction, error envelope, liveness/readiness probes, and a dev-only dashboard shell.
4. Alembic migration with 12 tables, UUIDs, UTC, CHECKs, indexes, partial unique active incident, and an advisory lock.
5. Compose: an internal backend network, volumes, health checks, restart policies, limits, hardening, and no public DB/Redis ports.
6. Tests at 3 tiers, plus README, architecture/schema docs, and this file.

### Deliverables
| Item | Location |
|---|---|
| Project/lock/tooling | `pyproject.toml`, `uv.lock` |
| Demo app | `demo_application/main.py` |
| API (live/ready, errors, dashboard mount) | `app/api/{main,health,errors}.py` |
| Settings | `app/config.py` |
| Redacting JSON logger | `app/observability/logging.py` |
| DB helpers / minimal repo | `app/persistence/{db,repositories,schema}.py` |
| Migrations | `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_initial_schema.py` |
| Compose / image | `docker-compose.yml`, `docker-compose.test-ports.yml` (test-only), `Dockerfile`, `.dockerignore` |
| Env template | `.env.example` (no credentials) |
| Backup/restore | `scripts/backup.sh`, `scripts/restore.sh` |
| Dashboard shell | `dashboard/` (served at `127.0.0.1:8000/dashboard/`, not in production) |
| Architecture diagram and schema overview | `docs/architecture.md` |
| Runbook | `README.md` |
| Placeholders for later phases | `app/{agent,monitoring,safety,auth}/__init__.py` (empty) |

### Commands run and results (final run, 2026-09-22)
| Command | Result |
|---|---|
| `docker compose config -q` | valid (exit 0) |
| `uv run ruff format --check .` | 44 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run mypy app demo_application migrations/versions` | Success: no issues in 19 source files |
| `docker compose -f docker-compose.yml -f docker-compose.test-ports.yml up -d --build --wait` | all 4 long-running services healthy; `migrate` exited 0 |
| `curl 127.0.0.1:8000/health/ready` | `{"status":"ready","checks":{"database":"ok","schema":"ok","redis":"ok"}}` |
| `uv run pytest tests/unit tests/integration` (with `TEST_DATABASE_URL`, `TEST_REDIS_*`) | **68 passed** |
| `RUN_RESILIENCE=1 uv run pytest tests/resilience` | **7 passed** (≈49 s) |
| `uv run pytest` (no service env) | 54 passed, 21 skipped (integration/resilience skip cleanly) |
| `./scripts/backup.sh`, then delete a row, then `CONFIRM_RESTORE=yes ./scripts/restore.sh <dump>` | backup 36 KB; row restored; `alembic_version` intact. Without `CONFIRM_RESTORE` the script refuses (exit 2) |
| Manual failure modes via curl | `timeout`: client gave up at 5.0 s; `memory_log`: metrics report 544 MB simulated while the container's real usage was 37.8 MiB; unauthenticated inject returned 401; reset to `none` gave healthy |
| Secret scan of all committable files for `.env` values | 0 hits; `.env` and `backups/` gitignored |

### Acceptance criteria → evidence
| Criterion | Evidence |
|---|---|
| `docker compose config` valid | command above; also `test_datastores_not_published_by_default` |
| Services start, readiness passes | `test_stack_ready_and_liveness_distinct` |
| Migrations run twice safely | `test_upgrade_twice_is_safe`, `test_downgrade_then_upgrade_roundtrip`, `test_concurrent_migrators_serialize` (3 processes), `test_migrations_rerun_safely` (compose `migrate` ×2) |
| Restart preserves DB data | `test_restart_preserves_db_data` (`restart postgres`, and full `down` then `up`) |
| Demo failure modes isolated | `tests/unit/test_demo_app.py` (all modes, expiry, per-instance isolation); `test_demo_failure_is_isolated_from_agent_stack` (API stays ready, demo can't open `postgres:5432`, no DB/Redis creds in demo env) |
| No secrets logged | `tests/unit/test_logging_redaction.py`; `test_liveness_independent_of_dependencies` / `test_unhandled_error_envelope_hides_details`; `test_no_secrets_in_logs` (scans live logs of all 5 services) |
| Liveness vs readiness distinguished | unit: `test_liveness_independent_of_dependencies`, `test_hung_dependency_bounded_by_deadline`; integration: `test_ready_only_when_schema_at_head`; live: `test_readiness_fails_when_db_down_but_liveness_holds` |
| Unit tests: settings, migrations, demo modes | `test_settings.py`, `test_migrations_static.py`, `test_demo_app.py` |
| Integration: insert and retrieve incident | `test_insert_and_retrieve_incident` (plus partial unique index, CHECKs, append-only audit, single active model config) |
| Negative: failure endpoint unavailable in production | `test_production_config_has_no_failure_endpoint`, `test_injection_cannot_be_enabled_in_production`, `test_default_settings_are_safe` |
| No public unsecured incident/admin endpoint | `test_no_incident_or_admin_endpoints`, `test_production_hides_docs_and_dashboard` |
| No public DB/Redis ports; no Docker socket | `test_datastores_not_published_by_default` |

### Issues found and fixed during this phase
1. The `alembic` CLI inside the container couldn't import `app`. Fixed with `prepend_sys_path` in `alembic.ini`. (Caught only by the real compose run.)
2. **Readiness could hang for more than 10 s** when Postgres was stopped. Docker's embedded DNS fell through to unreachable resolvers on the internal network. Fixed: checks now run in parallel on a bounded pool under one overall deadline and report `timeout`. There's a regression test for it.
3. `--no-access-log` had no effect because of the log re-routing, so I removed the flag. Access logs are kept and pass through redaction.

### Environment note (host, not project)
This Codespace had stale `iptables-legacy` rules (`FORWARD DROP`, allowing only `docker0`), which silently dropped **all** traffic between containers on user-defined bridge networks. I added a **runtime-only** host rule that accepts only L2-bridged (same-network) traffic, so routed traffic between networks and egress stay dropped:
`sudo iptables-legacy -I DOCKER-USER -m physdev --physdev-is-bridged -j ACCEPT`
It doesn't persist across a Codespace restart, so re-add it if containers can't reach each other. Remove it with `-D`. This is documented under README troubleshooting.

### Unresolved risks and open items
- **Dashboard has no authentication.** It's bound to 127.0.0.1, holds no data, is labelled NOT PRODUCTION-READY, and is disabled when `SENTINEL_ENVIRONMENT=production`. Real auth plus CSRF comes before any incident, approval or admin view (Phases 4–5).
- Secrets live in a local `.env` (mode 600) and are passed as container env vars, so they're visible to `docker inspect` for anyone with Docker access on the host. For the VM deployment, use a secrets manager or Docker secrets files.
- `readiness_timeout_seconds` is applied to psycopg's `connect_timeout` as an integer (minimum 1 s). The overall probe deadline is what bounds latency.
- `docker-compose.test-ports.yml` publishes DB/Redis on 127.0.0.1 for host-side tests. Never use it on a shared host.
- Single host, single Postgres, local backups only, so this is a single point of failure. Off-host backups, TLS, external uptime monitoring and alerting are deployment work for later phases. No availability, RTO or RPO has been measured yet.
- The Postgres and Redis containers don't use a read-only root filesystem or `cap_drop: ALL`, because the official images need to set up their data directories. They do use `no-new-privileges` and resource limits.
- No AI, auth-provider, monitoring loop, queue or remediation code exists yet. By design, the auth decision gate (`docs/auth-decision.md`) is Phase 3.
- ~~`01-FOUNDATION.md` had an uncommitted change I didn't make~~. **Resolved in Phase 2 (see below).**

### Next step
Phase 2 approved and executed; see below.

---

## Phase 2 — Monitoring, detection and durable dispatch (2026-09-22)

Scope: workflow steps 1–3 only, fully deterministic. **No LLM calls, no remediation.** Tasks end
Phase 2 in `awaiting_investigation` (an active state) until the Phase 3 investigator stage exists.
The request mentioned "detailed requirements in the prompt below", but no further text was
attached, so the implementation follows `02-MONITORING-QUEUE.md` and `CLAUDE.md`.

### Pre-work: Phase 1 verification and spec-file resolution
- **`01-FOUNDATION.md` restored.** The diff was a pure truncation of the first heading (`infrastructure\n\n## Objective…` became `infrastructu## Objective…`) with no content added. The modified copy and the diff were saved to the session scratchpad (`01-FOUNDATION.modified.bak`, `01-FOUNDATION.diff`) first, then `git checkout -- 01-FOUNDATION.md` restored the committed original. `git diff --quiet HEAD -- 01-FOUNDATION.md` passes.
- **Phase 1 results reproduced before any change:** ruff/mypy clean, `docker compose config` valid, **68 unit+integration passed, 7 resilience passed**, matching the Phase 1 report.
- The host firewall workaround from Phase 1 was still present (`iptables-legacy DOCKER-USER … --physdev-is-bridged -j ACCEPT`).

### Delivered
| Area | Files |
|---|---|
| Migration `0002_monitoring_queue` (reversible) | `detection_state` table; `degraded` check outcome; incident `first_failure_at/last_failure_at/resolution`; task `awaiting_investigation`, `outcome`, `completed_at`; outbox `next_attempt_at`, `stream_message_id`; idempotent evidence index; **fencing trigger** on `task_checkpoints` |
| Probe | `app/monitoring/probe.py` (5 s timeout, monotonic latency, UTC timestamp, classification) |
| Detector (pure) | `app/monitoring/detector.py` (per-type consecutive thresholds, disarm/bump, healthy hysteresis re-arm, stale-gap reset) |
| Recorder (one txn) | `app/monitoring/recorder.py` (check, state, and on breach incident + task + evidence + outbox + audit; auto-resolve; retention) |
| Monitor service | `app/monitoring/service.py`, `__main__.py` (probe loop decoupled from a recorder thread, bounded ordered buffer, advisory-lock leader/standby/presumed, non-overlapping schedule) |
| Outbox/dispatcher | `app/persistence/outbox.py`, `app/agent/dispatcher.py` (SKIP LOCKED publish, backoff on Redis failure, due-retry scheduler, stuck-task reconciler) |
| Worker | `app/agent/worker.py`, `tasks.py`, `stages.py` (consumer group, XAUTOCLAIM recovery, DB lease + fencing + heartbeat, checkpoints, retry/backoff, dead-letter + escalation, ACK after commit) |
| Status API | `app/api/v1.py`, `app/api/auth.py` (bearer, fail-closed, read-only) |
| Metrics | `app/observability/metrics.py` (DB/Redis-derived, Prometheus text at `/v1/metrics`) |
| Shared | `app/backoff.py`, `app/runtime.py`, `app/healthcheck.py`, `app/persistence/{audit,streams}.py`; bounded engines in `app/persistence/db.py` |
| Compose | new `monitor` (backend + demo_net), `dispatcher` and `worker` (backend only), heartbeat health checks, limits, hardening; `SENTINEL_API_READ_TOKEN` |
| Tooling/docs | `scripts/test.sh`, `docs/architecture.md` (topology + sequence diagrams, rules, guarantees, schema), README, `.env.example` |

### Defined behaviour (decisions a reviewer should check)
- **Thresholds:** 3 *consecutive* failures per type (`unavailable` = timeout/connection error, `http_error` = non-2xx), or 3 *consecutive* 2xx responses slower than 2 s (`high_latency`). Mixed failure types don't add up across types.
- **Dedup and hysteresis:** at threshold the type disarms and further failures bump `occurrence_count`. The partial unique index is the final guard. 3 consecutive healthy checks (degraded doesn't count) re-arm.
- **Auto-resolve:** on recovery, only an incident still in `open` is resolved `auto_recovered`. `escalated` and in-progress incidents stay open for their owner or a human.
- **Startup/restart:** the monitor probes only after acquiring leadership. Detection state persists. A gap of more than 3 intervals resets streaks but keeps armed/disarmed.
- **Worker poison/terminal handling:** non-claimable messages (terminal, not yet due, validly leased, unknown task) are ACKed. Malformed messages go to the dead-letter stream. On a lost lease the worker does **not** ACK.

### Commands run and results (final run, 2026-09-22)
| Command | Result |
|---|---|
| `docker compose config -q` | valid |
| `uv run ruff format --check .` | 76 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run mypy app demo_application migrations/versions` (strict) | Success: no issues in 38 source files |
| `scripts/test.sh unit` | **120 passed** |
| `scripts/test.sh integration` (real PG 16 + Redis 7.4) | **52 passed**, 3 consecutive runs green |
| `scripts/test.sh resilience` (live 7-service stack) | **15 passed** (~2 m 50 s), 3 consecutive full runs green |
| `uv run pytest` with no service env | 120 passed, 67 skipped (integration/resilience skip cleanly) |
| `docker compose ps` | api, monitor, dispatcher, worker, demo-app, postgres, redis all `(healthy)` |
| `scripts/backup.sh`, then `CONFIRM_RESTORE=yes scripts/restore.sh` | round trip OK on the new schema; `alembic_version = 0002_monitoring_queue` |
| Secret scan of committable files (4 `.env` values) | 0 hits; resilience `test_no_secrets_in_logs` scans live logs of all 8 services for all 4 secrets |
| Live `/v1/metrics` sample after the suite | `incidents_detected_total{http_error}=20, {unavailable}=5`, `queue_pending 0`, `queue_lag 0`, `outbox_unpublished 0`, `redis_up 1`, `last_check_age_seconds 13.9` |

### Acceptance criteria → evidence
| Criterion (02-MONITORING-QUEUE.md) | Tests |
|---|---|
| Healthy checks create no incident | unit `test_healthy_checks_create_no_incident`; integration `test_every_check_persisted_healthy_creates_nothing` |
| 3 failures create exactly one | unit `test_three_failures_open_exactly_one` (http_error/timeout/error); integration `test_three_failures_create_exactly_one_incident_task_and_outbox`; live `test_failure_detected_once_dispatched_and_not_duplicated` |
| Latency threshold creates incident | unit `test_latency_threshold_opens_high_latency`, `test_slow_2xx_is_degraded_high_latency`; integration `test_latency_threshold_creates_incident` |
| Subsequent checks don't duplicate | unit `test_prolonged_failure_never_duplicates`; integration `test_subsequent_failures_do_not_duplicate`, `test_concurrent_recorders_open_one_incident` (4 threads); live (occurrence_count grows, 1 incident) |
| Healthy re-arm allows a later separate incident | unit `test_rearm_requires_healthy_streak_then_allows_new_incident`; integration `test_healthy_rearm_auto_resolves_and_allows_later_separate_incident`; live `test_recovery_rearm_then_later_separate_incident` |
| One transaction for incident + task + outbox | `test_incident_creation_is_atomic` (crash mid-txn leaves no check, incident, task or outbox; retry succeeds) |
| Crash after commit, before publish, is eventually published | `test_crash_after_commit_before_publish_is_eventually_published` |
| Crash after publish, before mark, can't duplicate work | `test_crash_after_publish_before_mark_cannot_duplicate_work` (2 stream messages, 1 intake, 1 checkpoint) |
| Worker crash before ACK: task recovered | `test_worker_crash_after_commit_before_ack_is_recovered`, `test_expired_lease_message_reclaimed_via_pending_recovery`, `test_worker_crash_mid_task_is_recovered_by_another_worker`; live `test_worker_down_then_task_recovered_on_restart` |
| Two workers racing: only the current fencing holder advances | `test_two_workers_racing_only_current_fencing_holder_advances` (claim refused while leased; stale checkpoint rejected by DB trigger; stale transition and renewal rejected); `test_heartbeat_keeps_lease_during_long_stage` |
| Redis outage: DB retains work, retries later | `test_redis_outage_retains_work_in_db_and_retries`; live `test_redis_outage_db_retains_work_then_delivers` (Redis stopped: incident still opens, outbox retries, `/health/ready` 503, checks continue; delivered after restart) |
| Bounded retry/backoff, dead letter / FAILED | `test_transient_error_retries_with_backoff_then_succeeds`, `test_exhausted_retries_dead_letter_and_escalate`, `test_crash_on_final_attempt_dead_letters_on_next_delivery`, `test_permanent_error_fails_task_without_retry`, `test_malformed_message_dead_lettered_and_acked` |
| Lost queue message reconciled | `test_lost_stream_message_is_reconciled` |
| Monitor: no overlap, defined startup/restart, single leader | `test_run_loop_never_overlaps_and_skips_missed_ticks`, `test_single_leader_and_standby_takeover`, `test_lock_released_on_stop`, `test_restart_continues_streak_within_window`, `test_gap_longer_than_stale_window_resets_streak` |
| Monitoring continues through a DB outage | `test_checks_during_db_outage_are_recorded_later_and_detected`, `test_presumed_leader_keeps_probing_when_db_unreachable`; live `test_monitor_keeps_probing_through_database_outage` (checks timestamped during the outage recorded afterwards) |
| Read-only, access-controlled status API | `test_fails_closed_when_token_unconfigured`, `test_rejects_missing_or_wrong_token` (DB proven untouched before auth), `test_no_write_methods`, `test_incident_and_task_readable_with_token` |
| Counters: checks, incidents, queue lag, retry, dead letter | `test_metrics_report_checks_incidents_and_queue_lag`; live `test_queue_metrics_exposed` |
| Monotonic latency, UTC timestamps, 5 s timeout | `test_latency_uses_monotonic_clock_not_wall_clock`, `test_probe_sends_timeout`, `test_every_check_persisted_healthy_creates_nothing` (UTC offset 0) |
| Phase 1 preserved | all Phase 1 tests still pass; migration static tests generalised to the whole chain (initial migration still asserted to create exactly the 12 Phase 1 tables) |

### Defects found by the tests and fixed during this phase
1. **Monitor stalled during a PostgreSQL outage** (live test). Probing and DB writes shared one loop, and a call to a vanished DB host blocked for a long time. Fixed by decoupling probing from recording (bounded ordered buffer and recorder thread) and bounding all app DB calls (keepalives, `tcp_user_timeout`, `statement_timeout`).
2. **Advisory lock not released on stop.** `Connection.close()` returned the session to the pool, so it kept the lock and a standby could never take over. Fixed: explicit unlock, then invalidate. The regression test was shown to **fail against the old code**.
3. **Silent reconnect masked a lost lock.** `SELECT 1` succeeded on a transparently reconnected session. Fixed: leadership is verified via `pg_locks` for `pg_backend_pid()`.
4. **Stale fencing surfaced as a raw DB error.** It now maps SQLSTATE `SF001` to `LeaseLost` (the worker was already fail-safe).
5. `httpx` was only a dev dependency, which the container build caught. Moved to runtime deps and relocked.
6. Test-only issues fixed: cross-test contamination in a shared per-module DB, a backdating update overwritten by the `updated_at` trigger, and a live test that matched a *previous* run's incident.

### Unresolved risks and open items
- **Event trigger not yet implemented.** Phase 2 provides the scheduled monitor only. The "event + scheduled" trigger factor (CLAUDE.md) still needs an event source.
- **`memory_pressure` isn't detected.** The type exists in the schema, but Phase 2 detects health/latency only, as specified. The demo's `memory_log` mode is currently observable only via `/metrics`.
- **Per-type counting:** alternating failure types (e.g. 500, timeout, 500) never reach a threshold. This is a deliberate, documented choice; revisit if flapping matters.
- **Monitor check buffer is in memory.** A monitor crash *during* a DB outage loses the unrecorded checks (bounded at 2880). Health data is lost, but there's no inconsistency.
- **Duplicate-ACK edge case:** if a message is reclaimed while its task's lease is still valid, it's ACKed as a duplicate. If that holder then dies, recovery relies on the reconciler (`redispatch_after`, default 300 s) rather than on pending-entry recovery. Tested (`test_worker_crash_mid_task_is_recovered_by_another_worker`).
- **Redis `appendfsync everysec`** can lose up to about 1 s of stream writes on a crash. The reconciler re-dispatches affected tasks. PostgreSQL stays authoritative.
- The worker's `awaiting_investigation` is a parking state. Phase 3 must re-dispatch those tasks to the investigator stage.
- Consumer entries from dead worker containers accumulate in the Redis consumer group (harmless; cleanup is not implemented).
- Carried over from Phase 1:
  - The dashboard has no auth (localhost, non-production only).
  - Secrets are passed as container env vars.
  - Single host with local backups.
  - The Codespace firewall workaround is runtime-only.
- The resilience tests use an accelerated monitor cadence (2 s); the default 30 s/5 s/2 s contract is covered by unit and settings tests and restored after the run. No soak test has been run.

### Next step
Phase 3 approved and executed; see below.

---

## Phase 3 — Claude authentication, model selection and AI investigation (2026-09-22)

Scope: workflow steps 4–5. The AI selects read-only diagnostics, forms evidence-linked hypotheses and **proposes** an allowlisted action. **Nothing is executed**; Phase 4 owns policy and remediation.

### Pre-work: Phase 2 re-verified before any change
`docker compose config` valid · ruff format/lint clean · mypy strict clean · **120 unit, 52 integration and 15 resilience tests passed**. This reproduces the Phase 2 report exactly. Spec files matched HEAD.

### Authentication decision (full record: `docs/auth-decision.md`)
- **Claude Subscription is UNAVAILABLE for this application.** The Agent SDK docs state: *"Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK."* The login help article says subscription OAuth serves Anthropic's own apps and prohibits routing third-party traffic against subscription limits. The Agent SDK plan article says the June 15 change is **paused**, and that shared production automation should use API keys. The option is shown but disabled, with that reason. No credential reuse, no CLI or profile reading, no custom OAuth, no Claude Code impersonation.
- **Supported and implemented: Anthropic API key**, via the official Client SDK **`anthropic` 1.8.0** (verified against the installed package). The Claude Agent SDK (0.2.157) was deliberately not used, because it brings Claude Code's Bash, file and web tools. **Workload Identity Federation** is officially supported but not implemented yet; it is the production upgrade path.
- **Credential isolation, verified from the installed SDK source:** an explicit `api_key=` disables env, profile and WIF discovery, and the app additionally refuses to run if ambient `ANTHROPIC_*` credential, header or base-URL variables are present. The `claude` CLI on this host is never consulted.

### Implementation summary
| Area | What |
|---|---|
| Migration `0003_ai_investigation` | `investigations` table (one per task: status, result/failure, rejections, usage, cost, pinned model + auth mode, fencing token); task status `awaiting_policy`; `auth_mode` adds `mock` (test/demo only); parked-task and evidence indexes |
| Auth | `app/auth/decision.py` (verified decision and UI text), `app/auth/secrets.py` (0600 atomic key file, format checks, fingerprint, ambient-credential guard) |
| ModelGateway | `app/agent/gateway.py` (`authenticate` / `list_models` / `get_model` / `invoke`, `Usage`, 11 typed errors carrying `pause_ai` or `retryable`), `app/agent/anthropic_gateway.py` (real SDK; `tool_choice: auto`; no sampling, thinking or fallback params; refusal becomes a typed error), `app/agent/mock_gateway.py` (Scripted + evidence-driven Deterministic mock, labelled "not Claude") |
| Model selection | `app/agent/model_config.py`: active selection (mode + ID only), audited changes, gateway factory that never substitutes providers; model pinned per task via a fenced write |
| Onboarding CLI | `app/cli.py`, run as `docker compose run --rm onboard [status|change-model|set-key|remove-key]`: the Claude Code-like menu, masked key input, a free validation call, key saved only after acceptance, live model list, `models.retrieve` validation, billing and rotation notes |
| Restricted ops service | `app/ops_reader/` (only Docker-socket holder; GET-only inspect/logs/stats for one labelled container; no env or config returned; token auth; app metrics minus test-injection fields) |
| Tools | `app/agent/tools.py`: `get_incident`, `get_health_history`, `get_application_logs`, `get_container_status`, `get_resource_metrics`, `get_previous_incidents`. Each is typed, bounded and time-limited, writes an evidence row and an audit row, is redacted, returns an untrusted-data envelope, and reports `unavailable` instead of fabricating |
| Result and validation | `app/agent/schema.py` (strict schema, action allowlist, hypotheses pinned to `"hypothesis"`, no policy/execution/resolution fields), `app/agent/validator.py` (incident match; evidence existence, ownership and freshness; proposal consistency; no "I restarted…" claims) |
| Orchestrator | `app/agent/investigation.py`: one bounded manual loop inside the existing worker lease; budgets for 6 tool calls, 3 reasoning attempts, model calls, 600 s, 300k tokens and an optional USD cap; step-4 checkpoint after every call; one fenced transaction persists the result and transitions the task to `awaiting_policy` |
| Queue integration | `claim()` accepts due parked tasks; `transition(refund_attempt=)`; `pin_model()`; dispatcher `schedule_parked_investigations()` re-dispatches parked and paused tasks through the **same** outbox and Redis path; reconciler covers them; auto-resolve extended to `investigating` incidents |
| API and metrics | incident detail includes `investigations`; tasks show `model_id`; new metrics `sentinel_investigations_total{status}`, `sentinel_ai_tokens_total{kind}`, `sentinel_ai_tool_calls_total{tool}`, `sentinel_ai_paused_tasks{reason}` |
| Compose | new `ops-reader`, on-demand `onboard` (profile `tools`), `ops_net` (internal), `ai_egress` (bridge `sentinel-egress`, the only internet route, used by the worker and onboard), `ai_secrets` volume (ro in the worker) |

### Files
- **Created:**
  - `migrations/versions/0003_ai_investigation.py`
  - `app/auth/decision.py`, `app/auth/secrets.py`
  - `app/agent/{gateway,anthropic_gateway,mock_gateway,model_config,schema,validator,tools,investigation}.py`
  - `app/ops_reader/{__init__,docker,main}.py`
  - `app/cli.py`
  - `docs/auth-decision.md`
  - tests: `tests/unit/{test_auth,test_anthropic_gateway,test_investigation_schema,test_mock_gateway,test_ops_reader}.py`, `tests/integration/{test_investigation,test_cli_onboarding,test_worker_investigation}.py`, `tests/resilience/{test_phase3_ai,helpers}.py`
- **Modified:**
  - `pyproject.toml`/`uv.lock` (`anthropic>=1.8,<2`)
  - `app/config.py` (AI and ops settings with fail-closed validation)
  - `app/agent/{tasks,worker,dispatcher}.py`, `app/persistence/{outbox,schema}.py`, `app/monitoring/recorder.py`, `app/api/v1.py`, `app/observability/metrics.py`
  - `Dockerfile` (secret dir), `docker-compose.yml`, `.env.example`
  - `README.md`, `docs/architecture.md`, this file
  - `tests/unit/test_migrations_static.py` (the credential-column guard now checks precisely for added columns), `tests/resilience/test_compose_stack.py` (the socket invariant is now "only ops-reader")

### Commands run and results (final run on the final tree, 2026-09-22)
| Command | Result |
|---|---|
| `docker compose config -q` | valid |
| `uv run ruff format --check .` | 101 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run mypy app demo_application migrations/versions` | Success: no issues in 53 source files |
| `scripts/test.sh unit` | **197 passed** |
| `scripts/test.sh integration` | **98 passed** |
| `scripts/test.sh resilience` | **19 passed** (4 m 30 s; also green on the previous full run) |
| `uv run pytest` (no service env) | 197 passed, 117 skipped (= 314 total) |
| `docker compose ps` | 8 services healthy (api, monitor, dispatcher, worker, ops-reader, demo-app, postgres, redis); live DB at `0003_ai_investigation` |
| Secret scan of committable files (5 `.env` secrets) | 0 hits; `.env` contains no Anthropic key |

### Acceptance criteria (03-CLAUDE-BRAIN.md) → evidence
| Criterion | Evidence |
|---|---|
| Mock model proves diagnostic branching based on tool results | `test_diagnostic_branching_depends_on_tool_results` (logs available: container status; logs unavailable: health history); unit `test_mock_gateway.py`; live e2e |
| Invalid evidence IDs rejected | `test_fabricated_evidence_id_rejected_then_corrected`, `test_evidence_from_another_incident_rejected`, `test_stale_evidence_rejected` |
| Prompt injection in logs ignored | `test_prompt_injection_in_logs_is_contained`: injected text reaches the model only as redacted `data_is_untrusted` tool data, the system prompt and tools are unchanged across turns, the obeying mock's `run_shell` proposal and fake id are rejected, and nothing completes |
| Tool call / time / token budget enforced | `test_tool_call_budget_enforced` (7th+ call refused, 6 evidence rows), `test_time_budget_enforced`, `test_token_budget_escalates_with_insufficient_evidence`, `test_cost_budget_uses_operator_prices` |
| Unsupported action rejected | unit `test_unsupported_actions_rejected`; integration `test_invalid_outputs_exhaust_attempts_then_escalate[run_shell…]`; policy, execution and resolution fields rejected |
| Selected model used for investigation and proposal | `test_investigation_collects_real_evidence_and_completes` (every request uses the pinned ID; ID persisted on task and investigation) |
| Model change affects only new tasks | `test_model_change_affects_only_new_tasks`; live pause→resume keeps the pinned model |
| Unsupported subscription disabled | unit `test_subscription_marked_unsupported…`; integration `test_subscription_option_is_disabled_and_explained`; live `test_subscription_disabled_in_live_onboarding` |
| API-key path works with a mock provider | `test_api_key_onboarding_selects_and_persists_mode_and_model_only`; the real SDK is exercised offline in `test_anthropic_gateway.py` (17 tests) |
| No secrets in logs or DB | `test_key_never_stored_in_database` (every table scanned), `test_status_shows_fingerprint_not_key`, `test_error_classification` (key not in errors), live `test_no_secrets_in_any_service_logs` (5 secrets, 6 services) |
| Quota/expiry pauses AI while monitor and queue continue | `test_provider_auth_quota_rate_limits_pause_and_preserve_task` (401/402/429: task parked, attempt refunded, evidence kept, incidents still created); live `test_missing_credentials_pause_ai_while_monitoring_continues_then_resume` |
| Live auth smoke test only with authorization | **Not executed**: no credentials, no cost authorization. See below |

Additional required behaviours:

| Behaviour | Tests |
|---|---|
| Worker crash resume | `test_worker_crash_resumes_with_checkpointed_evidence_and_budget` |
| Duplicate delivery | `test_duplicate_delivery_after_completion_does_not_rerun`, `test_duplicate_message_after_completion_is_acked_without_rerun` |
| Stale executor | `test_stale_executor_cannot_persist_investigation` |
| Provider outage bounded retries | `test_provider_outage_bounded_retries_then_dead_letter` (exactly 3 provider calls) |
| Parked Phase 2 tasks resumed once | `test_phase2_parked_task_is_resumed_once_ai_configured`, `test_not_configured_parks_without_using_attempts` |
| Model gone, no switching | `test_model_unavailable_escalates_without_switching_models` |
| Unknown or malformed tool calls not executed | `test_unknown_and_malformed_tool_calls_rejected_not_executed` |
| Unavailable data not fabricated | `test_unavailable_source_is_recorded_not_fabricated` |

### End-to-end demonstration (live stack, `test_end_to_end_failure_to_validated_investigation`)
`http_500` injected into the real demo-app → the monitor detects a `http_error` incident → PostgreSQL commits the task and outbox event → the dispatcher publishes to the Redis Stream → the worker claims the lease, **pins the selected model** (the deterministic mock) and chooses tools from the evidence:
1. `get_incident` → `get_application_logs` → `get_container_status`.
2. These read the **real** container through ops-reader: `state: running`, plus the demo app's actual `"simulated internal error"` log line.
3. The mock submits structured findings, and the validator checks every cited evidence ID against this incident.
4. The investigation is persisted and the task reaches **`awaiting_policy`**, with proposal `restart_demo_app`. **Nothing is executed**: 0 `action_attempts`.

The findings are visible at `GET /v1/incidents/{id}`.

### Live model test status
- **No live Claude call was made.** No valid Anthropic credentials are available, and you have not authorized usage cost. All AI behaviour in tests uses deterministic mock models, labelled "TEST/DEMO ONLY - not Claude". **These tests do not prove that a real Claude model investigates well, or that live API or subscription authentication works.**
- What *was* verified live, without credentials or cost:
  - From inside the stack, `AnthropicGateway.authenticate()` against the real `api.anthropic.com` with a deliberately invalid placeholder key returned **HTTP 401**, classified as `AuthenticationFailed` (`pause_ai=True`). This proves the egress route, TLS and the live error mapping.
  - Onboarding menu, `status` and mock-mode selection all ran live in the `onboard` container.

### Environment note (host, not project)
The Codespace's stale `iptables-legacy` `FORWARD DROP` (see Phase 1) also blocked **routed egress** from user-defined bridges, so the worker could not reach Anthropic: DNS failed on `ai_egress` while `docker0` and the host got 401. I added a second **runtime-only** rule scoped to the fixed-name AI egress bridge only; the other bridges stay blocked (verified: `demo_net` still times out):
`sudo iptables-legacy -I DOCKER-USER -o sentinel-egress -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT && sudo iptables-legacy -I DOCKER-USER -i sentinel-egress ! -o sentinel-egress -j ACCEPT`
Remove it with `-D`. It is documented in README troubleshooting. Earlier temporary rules tied to a random bridge ID were removed.

### Unresolved issues and risks
1. **Real-model behaviour is unverified.** Tool-selection quality, schema adherence, cost per investigation and resistance to prompt injection have only been exercised with mocks. The *enforcement layer* is tested; the model's judgement is not. A live evaluation needs a key and cost authorization.
2. **ops-reader holds the Docker socket** (group access is root-equivalent on the host). The mitigations are GET-only code for one container, token auth, no published port, not on `ai_egress`, hardened container. However, it shares `demo_net`, which is not internal (the demo app publishes a port), so on a normal Docker host it *could* reach the internet. For the VM, move metrics collection or add an egress firewall.
3. **Worker egress is not allowlisted to `api.anthropic.com`.** An egress proxy with a domain allowlist is recommended for the VM.
4. **The API key is a static secret in a Docker volume.** Prefer a secrets manager or WIF (not implemented) for unattended production. Rotation is manual (`onboard set-key`).
5. **Evidence freshness is ≤ 1 h.** An investigation resumed after a long pause must re-collect evidence within its remaining budget, or it escalates as insufficient.
6. **Crash resume starts a new conversation** that is seeded with prior evidence IDs and counters. It is not a replay of the exact earlier turns.
7. `mock` mode exists in the production code path. It is guarded by settings validation (forbidden in `production`) and labelled everywhere.
8. Carried over: the dashboard has no auth; the other secrets are in `.env`/container env; single host; the Codespace firewall rules are runtime-only; no soak test.

### Phase 4 readiness
- **Ready:** every investigated task rests in `awaiting_policy`, with a validated, evidence-linked `investigations.result.proposed_action` from the allowlist (`restart_demo_app | no_action | escalate_to_human`). The pinned model, audit trail and checkpoints are in place. `action_attempts` (unique `action_id`, `restart_demo_app` only) and `approvals` (expiry, single pending) tables exist from Phase 1. Leases, fencing and the outbox path are reusable as they are.
- **Needed in Phase 4:** a deterministic policy engine (ALLOW/APPROVAL/DENY); a **separate** restricted *write* path for the single demo restart, since ops-reader is read-only by design; authenticated approvals with CSRF protection; post-action verification; and moving tasks out of `awaiting_policy`.

### Next step
Phase 4 approved and executed; see below.

---

## Phase 4 — Deterministic policy, approval, restricted execution, recovery verification (2026-09-22/23)

Scope: workflow steps 6–8. The AI proposes; a deterministic policy authorizes; a restricted executor performs **one** action (`restart_demo_app` on the trusted `demo-app`); a deterministic verifier decides recovery. No new AI calls, no additional actions, no Phase 5 reporting.

### Pre-work: Phase 3 re-verified before any change
`docker compose config` valid · ruff format/lint clean · mypy strict clean · **197 unit, 98 integration, 19 resilience passed** · 8 services healthy · migration `0003_ai_investigation`. This matches the Phase 3 report exactly. Spec files matched HEAD, and the working tree had no unrelated changes.

### Conflicts and deviations (documented before implementation)
1. **Target name:** the prompt's example `checkout-demo` doesn't exist. The trusted target is the existing `demo-app`, taken from configuration (`SENTINEL_REMEDIATION_TARGET_SERVICE`), never from model output.
2. **Autonomy default:** the prompt says off by default and the spec says "configured preauthorization". These are consistent. Autonomy is **off**. It requires `SENTINEL_REMEDIATION_AUTO_ENABLED=true` **and** `SENTINEL_REMEDIATION_ENVIRONMENT=isolated-demo`, and settings validation forbids it in production. With autonomy off, all checks passing gives `REQUIRE_APPROVAL`; with no environment declared, it gives DENY.
3. **CSRF:** approvals use per-operator **header** bearer tokens (no cookies), so browsers can't forge authorized requests. The API also requires `application/json` and refuses `Sec-Fetch-Site: cross-site` or a foreign `Origin`. The unauthenticated dev dashboard stays read-only and has **no** approval UI.
4. **Test updates, stated openly (no guarantee weakened):**
   - Two Phase 3 live assertions expected tasks to *stay* in `awaiting_policy`. The pipeline now legitimately continues, so they assert that the investigation completed with the model pinned and, if the policy ran, that it **denied** (`ENV-2`) with **0 action attempts**.
   - The Phase 1/3 "socket holders = {ops-reader}" invariant is now exactly `{ops-reader, executor}`. New assertions: the executor is on internal `exec_net` only, and the worker still has no socket.
5. **ops-reader** gained two **read-only** endpoints for verification: a fresh health probe and logs `since`. It remains read-only. The write path is a **separate** executor, as required.
6. The demo app gained a demo-only **`sticky`** failure flag, gated exactly like failure injection, so that a *failed* recovery can be demonstrated honestly.
7. **Test hook:** `EXEC_TEST_PRE_RESTART_DELAY_SECONDS` (default 0, **rejected in production**) widens the live window for the "worker killed during execution" test.
8. **Notification** of pending approvals is a WARNING log line plus the `/v1/approvals` listing. There is no external channel yet; that belongs to Phase 5 alerts.

### Implementation summary
| Area | What |
|---|---|
| Migration `0004_policy_execution` | `policy_decisions` (append-only, unique per task/phase/fencing token); `operators` (SHA-256 token hashes, roles `viewer`/`approver`); `approvals` bound to action ID, fingerprint, target, policy version, risk, signed decision, with one pending per action; `action_attempts` gains pre/post state, fingerprint, decision/approval links, error, and a **unique `(incident_id, action_type)`** (one restart per incident); `verifications` |
| Policy engine | `app/safety/policy.py`: pure typed function; 21 rule IDs; default DENY (`SYS-1`); config-derived `policy_version`; action fingerprint; deterministic `action_id = uuid5(incident)` |
| Approvals | `app/safety/approvals.py`, `app/api/approvals.py`: operators, conditional single-winner decisions, HMAC-signed decisions verified by the worker, expiry fails closed, resume via the outbox |
| Executor | `app/executor/`: separate service; one fixed target; signed, expiring, action-bound requests; durable SQLite ledger (at-most-once per action ID, fencing, fingerprint, in-progress refusal); hourly cap; internal network only |
| Remediation stage | `app/safety/remediation.py`: policy → approval wait → reserve → **pre-execution re-check** → fenced intent → execute → record → verify → finalize; reconciliation from the ledger and container `StartedAt` |
| Verification | `app/safety/verification.py`: ≥3 consecutive fresh probes, each successful and under 2 s, within 120 s; no new ERROR/CRITICAL logs since restart; fails closed on missing evidence |
| Queue integration | `app/agent/pipeline.py` routes by durable state; `claim()` accepts due `awaiting_policy`/`waiting_approval`; dispatcher `schedule_policy_tasks()`; reconciler covers both states; worker wires `TaskPipeline(Investigation, Remediation)` |
| API / CLI | incident detail adds `policy_decisions`, `action_attempts`, `verifications`, `approvals`; CLI `operator-add`, `operator-disable`, `approvals` |
| Compose | new `executor` (socket + `executor_state` volume, internal `exec_net` only); worker joins `exec_net`; demo `demo_state` volume; new secrets `SENTINEL_EXECUTOR_TOKEN`, `SENTINEL_ACTION_SIGNING_KEY`, `SENTINEL_APPROVAL_SIGNING_KEY` |

### Files
- **Created:**
  - `migrations/versions/0004_policy_execution.py`
  - `app/safety/{policy,approvals,executor_client,verification,remediation}.py`
  - `app/executor/{__init__,main,ledger,signing}.py`
  - `app/api/approvals.py`, `app/agent/pipeline.py`
  - tests: `tests/support/fake_docker.py`, `tests/unit/{test_policy,test_executor,test_verification}.py`, `tests/integration/{test_remediation,test_approval_api}.py`, `tests/resilience/test_phase4_remediation.py`
- **Modified:**
  - `app/config.py` (remediation, executor and verification settings with fail-closed validation)
  - `app/agent/{tasks,worker,dispatcher}.py`, `app/persistence/{outbox,schema}.py`, `app/api/{main,v1}.py`, `app/cli.py`, `app/ops_reader/{main,docker}.py` (read-only additions), `app/safety/__init__.py`, `demo_application/main.py` (sticky demo flag)
  - `Dockerfile`, `docker-compose.yml`, `.env.example`, `pyproject.toml` (one per-file lint allowance for shared pytest fixtures)
  - `README.md`, `docs/architecture.md`, this file
  - `tests/resilience/{test_phase3_ai,test_compose_stack}.py` (see deviation 4)

### Commands run and results (final run on the final tree, 2026-09-23)
| Command | Result |
|---|---|
| `docker compose config -q` | valid |
| `uv run ruff format --check .` | 121 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run mypy app demo_application migrations/versions` | Success: no issues in 65 source files |
| `scripts/test.sh unit` | **277 passed** (197 + 80 new) |
| `scripts/test.sh integration` | **135 passed** (98 + 37 new); remediation module also green on two earlier consecutive runs |
| `scripts/test.sh resilience` | **30 passed** in 14 m 44 s (19 + 11 new live Phase 4 tests) |
| `uv run pytest` (no service env) | 277 passed, 165 skipped (= 442 total) |
| `docker compose ps` | 9 services healthy (api, monitor, dispatcher, worker, ops-reader, **executor**, demo-app, postgres, redis); DB at `0004_policy_execution` |

### Acceptance criteria (04-SAFETY-AUTONOMY.md) → evidence
| Criterion | Evidence |
|---|---|
| Preauthorized demo restart executes without interactive approval | integration `test_preauthorized_restart_executes_once_and_verifies`; live `test_allowed_autonomous_restart_end_to_end` (real container `StartedAt` changed, ledger = 1, `proposal=ALLOW{AUT-1} pre_execution=ALLOW{AUT-1}`, verification passed, incident `resolved:remediated`) |
| Unauthorized target denied | `test_denied_actions_never_execute` (TGT-1, 0 restarts); unit TGT-1/TGT-2; executor rejects any target field (`test_request_cannot_choose_target`) |
| Second restart denied | unit LIM-1/DUP-1; DB `uq_action_attempts_one_per_incident` (`test_second_restart_for_incident_blocked_by_database`); failed recovery never restarts twice (integration + live) |
| Expired approval denied | `test_expired_approval_fails_closed_and_late_decision_rejected`; live `test_approval_expiry_fails_closed` (late decision gets 409, 0 restarts) |
| Forged approval denied | `test_forged_approval_row_is_not_authorization` (APR-5); API rejects forged, disabled and viewer tokens; HMAC unit test |
| Changed action fingerprint denied | `test_changed_action_fingerprint_invalidates_approval` (APR-4); API 409 on a wrong fingerprint; executor `fingerprint_mismatch` |
| Crash before/after restart reconciles without duplicates | the crash matrix in `test_remediation.py` (reservation, before request, after execution, executor interrupted with and without an observed restart, after recording); live `test_worker_killed_during_execution_reconciles_without_duplicate` (worker **killed** mid-restart, ledger entry for the action = 1) |
| No privileged Docker socket available to the model | live `test_privilege_boundaries_live` (no socket in worker, api, monitor or dispatcher; executor unreachable from api, monitor and demo); `test_datastores_not_published_by_default` (socket holders exactly `{ops-reader, executor}`); the model has only the 6 read-only tools (Phase 3) |
| Failed verification cannot close the incident | `test_failed_recovery_escalates_without_second_restart` (integration + live sticky failure: verification failed, incident **escalated/open**, `resolution` NULL, exactly 1 restart); `test_new_critical_errors_fail_verification` |
| Approvals resume the paused workflow | `test_approved_action_resumes_revalidates_and_executes`; live `test_approval_required_then_authenticated_approval_executes`; resume after PostgreSQL restart and Redis outage (live) |
| Denial and escalation leave the incident open | `test_denied_actions_never_execute` / live `test_blocked_action_does_not_execute` (incident `escalated`) |
| Policy checked immediately before side effects | `pre_execution` decision recorded in every executed case; `test_app_recovers_while_approval_pending_is_not_restarted` and live `test_app_recovers_before_remediation_is_not_restarted` (HLT-2 at pre-execution, 0 restarts) |
| Demonstration of one allowed and one blocked action | live allowed (above) + live blocked (`ENV-2`, 0 attempts, ledger unchanged) |

### Additional required tests
- **Unit:**
  - ALLOW, REQUIRE_APPROVAL and DENY, with every rule in isolation (28 deny cases);
  - missing and stale evidence; unsupported action; invalid target; wrong environment;
  - disabled autonomy; restart limits; expired, rejected and forged approvals; duplicate action;
  - verification success, failure, deadline and latency; unsafe settings rejected.
- **Integration:**
  - dispatch of `awaiting_policy` tasks; decision, attempt and approval persistence;
  - authorized and unauthorized execution; approval create, approve, reject and expire;
  - concurrent approvals (6 racing approvers, exactly one wins);
  - idempotency, checkpoints (steps 6/7/8), incident transitions, stale fencing token, executor unavailable;
  - duplicate Redis delivery (1 restart).
- **Resilience (live):**
  - worker crash during execution; duplicate delivery;
  - PostgreSQL and Redis interruptions; executor unavailable (dead-lettered, attempt stays `pending`, never executed);
  - restart that doesn't fix the app (sticky failure) and the verification deadline;
  - app recovery before remediation; approval after expiry.

### End-to-end demonstration evidence (final live run, deterministic mock model)
Tasks created in that window: **5 × `resolved:recovery_verified`**, 3 × `policy_denied`, 1 × `approval_expired`, 1 × `recovery_failed`, 1 × `service_recovered_before_action`, 2 × `dead_lettered` (executor stopped). Policy trails recorded:
- **Allowed (autonomous):** `proposal=ALLOW{AUT-1} pre_execution=ALLOW{AUT-1}` → attempt `succeeded` → verification `passed` → incident `resolved:remediated`.
- **Approval:** `proposal=REQUIRE_APPROVAL{APR-0}` → (authenticated approve) → `proposal=ALLOW{APR-1} pre_execution=ALLOW{APR-1}` → `succeeded` → `passed`.
- **Blocked:** DENY `{ENV-2}` → 0 action attempts, executor ledger unchanged, incident `escalated`.
- **Failed recovery:** ALLOW → 1 restart → verification `failed` → task `escalated:recovery_failed`, incident open. (It was later closed by the *next* test's cleanup fixture, after the assertions.)

The executor's durable ledger held only `completed` entries, one per action ID. **No live Claude call was made**; the AI step used the labelled mock model.

### Unresolved risks
1. **Docker socket in two services** (`ops-reader`, `executor`): group access is root-equivalent on the host. The narrow APIs, hardening, token auth, fixed target and network isolation (executor: internal network only, no egress) reduce but **do not eliminate** this risk. A VM deployment should consider a rootless or socket-proxy design.
2. **The executor trusts the worker's signing key.** The worker holds both the executor token and the action-signing key, so a fully compromised worker could sign a restart of the *one* target. It is still bounded by the ledger (once per action ID), the hourly cap and the single fixed target. The executor has no DB access to re-verify policy independently.
3. **Static secrets** (executor token, signing keys, operator tokens) are in `.env` / container env / DB hashes. There is no automated rotation or expiry for operator tokens yet.
4. **Real Claude behaviour is still untested.** Proposals came from the mock. The policy doesn't trust model prose, but how often a real model proposes a restart (and gets it right) is unmeasured.
5. **The restart makes the demo app briefly unavailable.** At the default 30 s cadence this is below the 3-failure threshold. At very fast cadences it could open a separate `unavailable` incident (not observed in tests).
6. Log checks use Docker's `since` (second granularity), and only JSON `level` or ERROR/CRITICAL/Traceback lines count as critical.
7. **Approval notification is log/API only**; there's no pager, email or chat yet.
8. Carried over: worker egress isn't allowlisted to `api.anthropic.com`; the dev dashboard has no auth (read-only, localhost); single host; the Codespace firewall rules are runtime-only; no 72-hour soak test.

### Phase 5 readiness
- **Ready:** every incident now has a complete, durable, record-backed history. That covers: detection evidence, the investigation (model, evidence, hypotheses), append-only policy decisions with rule IDs, approvals with signed decisions, action attempts with pre/post state and the executor ledger, verification criteria and observations, audit events and metrics. That is exactly what record-backed reports, notifications and RTO/availability measurement need.
- **Phase 5 needs:** AI-drafted reports that cite only these records (the `reports` table exists); an external alert/notification channel (approvals pending, escalations, AI paused); metrics for policy decisions, actions and verifications; backup/restore drills including the executor ledger; and the soak test.

### Next step
Phase 5 approved and executed; see below.

---

## Phase 5 — Reliability, reporting, notifications and operational hardening (2026-09-23)

Scope: workflow steps 9–10 and the cross-cutting factors from `05-RELIABILITY-REPORTING.md`. **No new remediation actions**. The AI gained no new privileges; policy and verification remain deterministic.

### Pre-work: Phase 4 baseline reproduced before any change
All results matched the Phase 4 report exactly:
- `docker compose config -q`: valid.
- ruff format/lint: clean (121 files).
- mypy strict: clean (65 files).
- **277 unit, 135 integration and 30 resilience tests passed** (14 m 40 s).
- 9 services healthy.
- DB at `0004_policy_execution`.

Environment notes:
- The Codespace had restarted, so the documented runtime-only `iptables-legacy` rules were missing and were re-added exactly as in README troubleshooting.
- The resilience script's final `up --build` picked up the newly written `0005` migration and applied it to the dev DB **after** the baseline tests had finished. They ran at `0004`.

### Conflicts and deviations (decided before coding)
1. **13-state lifecycle vs existing statuses.** The contract states are *derived* from durable records (`app/agent/lifecycle.py`, exposed as `lifecycle_state`), with an explicit legal-edge table. The DB trigger `trg_tasks_guard_status` enforces the `tasks.status` edges; terminal statuses are final (SQLSTATE `SF002`). Existing statuses were not renamed.
2. **Dashboard auth (spec) vs "no management UI" (prompt).** The dashboard stays a read-only dev shell. All its data now comes only from the bearer-protected `/v1` API, with the token held in memory. There are no controls, it has a strict CSP, and it is disabled in production.
3. **Report reuse of existing tables.** The Phase 1 `reports` table is reused, extended additively and made append-only. The job lifecycle lives in a new `report_jobs` table, because incident tasks are one-per-incident and many tests assume that.
4. **Notification provider.** The spec names none, so there is a generic signed webhook plus a log channel, and a dev/test-only `notify-sink` on an internal network. A real provider needs `docker-compose.notify-egress.yml`.
5. **Jitter.** The spec requires jittered backoff. `jittered_backoff` samples uniformly in [80 %, 100 %] of the existing deterministic schedule, so it never exceeds the documented bound and the existing retry-timing test still holds. It is used for task retries, report jobs and notifications.
6. **Primary keys.** An existing integration invariant requires UUID primary keys, so the new `ai_job_slots`, `alert_state` and `service_heartbeats` tables have UUID `id` columns plus unique natural keys. This was caught by `test_timestamps_stored_as_utc` and fixed in the migration before finalizing.
7. **Soak.** `05` defines none, and the 72 h run belongs to Phase 6. A harness was built. The bounded 60-minute soak was started but **did not complete** (see below: NOT VERIFIED).
8. **Host-down alerting** needs an external vantage point. Prometheus rules are provided and unit-tested with promtool; no external monitor is deployed, so this is NOT VERIFIED.
9. **Local `.env`.** A generated `SENTINEL_NOTIFY_WEBHOOK_SECRET` was appended to the local, gitignored `.env` (never printed), so the signed-webhook path is exercised live.
10. **Test changes (no guarantee weakened).**
    - No existing test was modified.
    - The dashboard keeps the literal `NOT PRODUCTION-READY` marker that a Phase 1 test checks.
    - The API request-ID logic reuses the existing middleware instead of replacing it; an early version broke `test_error_envelope_for_unknown_route`, and this was fixed in the code, not the test.

### Implementation summary
| Area | What |
|---|---|
| Migration `0005_reliability_reporting` (additive, reversible) | `report_jobs`; `reports` gains version, generation mode, content, record SHA-256, provenance and usage, is append-only (UPDATE/DELETE/TRUNCATE refused), with CHECK "model/auth iff AI"; `notification_events` and `notification_deliveries`; `ai_usage`; `ai_job_slots`; `alert_state`; `service_heartbeats`; `backup_runs`; task-lifecycle guard trigger; TRUNCATE guards on `audit_events` and `policy_decisions` |
| Step 10 hook | `app/agent/finalize.py`: in the **same transaction** as every terminal task transition (fenced `transition`, `dead_letter_if_exhausted`) it requests a report job and enqueues the outcome notification. The monitor's auto-resolve also requests a report |
| Canonical record | `app/reporting/record.py`: typed, bounded and redacted, built from explicit queries; untrusted strings in explicitly named fields; SHA-256 provenance |
| AI drafting | `app/reporting/stage.py`, via the **same `ModelGateway`**. The job pins the incident's investigation model (else the active selection). Every call is metered. Budgets are checked before each call, and there is a concurrency slot. At most 3 validation attempts |
| Validator | `app/reporting/validator.py`: structured claims must equal the record in both directions; evidence ownership; timeline timestamps; clause-level prose checks with negation handling (execution, recovery, root cause, approval, policy, verification, operator names, Claude/model claims, timestamps, costs and tokens) |
| Fallback / rendering | `app/reporting/render.py`: the facts sections are always rendered from records; a deterministic narrative; honest labels (`TEST/DEMO ONLY - not Claude`, `DETERMINISTIC FALLBACK`) |
| Report jobs | `app/reporting/{jobs,consumer}.py`: lease with fencing, heartbeat, ACK after commit, retry with jittered backoff; the final attempt is always deterministic; `failed` sends a notification. A separate Redis stream `sentinel:reports` (group `sentinel-reporters`) is read by the worker; `schedule_report_jobs` reconciles in the dispatcher |
| Notifications | `app/notifications/{events,channels,delivery,service}.py`: whitelisted payloads, dedup keys, per-channel deliveries with lease and fencing, a signed idempotent webhook, SSRF-safe destination validation, bounded retries, then dead-letter; a new least-privilege `notifier` service |
| Alerts | `app/notifications/alerts.py`: 11 deterministic rules, edge-triggered with hourly repeat. `deploy/observability/alerts.yml` has 15 Prometheus rules including host/API down, plus `alerts_test.yml` (promtool) and `grafana-dashboard.json` |
| Degraded modes | `GET /v1/system/status` (`CORE_HEALTHY`, `AI_DEGRADED`, `NOTIFICATIONS_DEGRADED`, `EXECUTOR_DEGRADED`, `MONITOR_DEGRADED`, `REPORTING_DEGRADED`, `BACKUPS_DEGRADED`, `REDIS_UNAVAILABLE`, `DATABASE_UNAVAILABLE`). `service_heartbeats` rows are written by the worker (including executor reachability), the dispatcher and the notifier. Liveness and readiness are unchanged |
| Cost | `app/agent/usage.py`: `metered_invoke`; per-incident and daily token/currency ceilings (daily exhaustion parks AI until the next UTC day); max concurrent AI jobs; operator-priced estimates only |
| Metrics | about 30 new families (monitoring, incidents, AI, policy, remediation, verification, queue, reporting, notifications, alerts, backups, heartbeats, API auth failures). Labels come from bounded sets (`BOUNDED_LABELS`) |
| Logging | `log_context` correlation (`incident_id`, `task_id`, `report_job_id`, `notification_id`, `request_id`); extra redaction for `sop_…` operator tokens, `sha256=` signatures, `redis://:pw@`, and signing-key/signature/secret keys; container log rotation |
| API | `GET /v1/incidents/{id}/report[?version]` (`validated`/`fallback`/`pending`/`generating`/`failed`/`not_requested`), `GET /v1/reports/{id}`, `GET /v1/notifications`, `GET /v1/system/status`; `lifecycle_state`, `report_jobs` and `reports` in the incident detail; security headers |
| Backup/restore | `scripts/backup.sh`: `pg_dump` plus a **SQLite online-backup** snapshot of the executor ledger, a checksummed manifest, `backup_runs` rows and audit events. `scripts/restore.sh`: stops writers, verifies checksums, restores PostgreSQL, **merges** the ledger (never downgrades), adds PostgreSQL tombstones, restarts, audits. `app/executor/ledger_tool.py` |
| CLI | `onboard report-request INCIDENT_ID`, `onboard notify-test` |
| Soak harness | `scripts/soak.py` (real start/end, injection schedule, invariants) |

### Files
- **Created:**
  - `migrations/versions/0005_reliability_reporting.py`
  - `app/reporting/{__init__,record,schema,validator,render,jobs,stage,consumer}.py`
  - `app/notifications/{__init__,events,channels,delivery,alerts,service}.py`
  - `app/agent/{usage,lifecycle,finalize}.py`, `app/api/status.py`, `app/persistence/heartbeats.py`, `app/executor/ledger_tool.py`
  - `demo_application/notify_sink.py` (dev/test only)
  - `docker-compose.notify-egress.yml`
  - `deploy/observability/{prometheus.yml,alerts.yml,alerts_test.yml,grafana-dashboard.json}`
  - `scripts/soak.py`
  - `docs/{runbook,deployment,traceability}.md`
  - tests: `tests/support/records.py`; `tests/unit/{test_report_validator,test_report_render,test_notifications_unit,test_phase5_misc,test_ledger_tool}.py`; `tests/integration/{test_reporting,test_notifications}.py`; `tests/resilience/test_phase5_reporting.py`
- **Modified:**
  - `app/config.py` (Phase 5 settings with fail-closed validation)
  - `app/agent/{tasks,investigation,worker,dispatcher,mock_gateway}.py`
  - `app/monitoring/recorder.py`, `app/safety/remediation.py` (approval notification)
  - `app/persistence/{outbox,streams,schema}.py`, `app/backoff.py`
  - `app/observability/{logging,metrics}.py`
  - `app/api/{main,v1,auth,approvals,errors}.py`, `app/cli.py`
  - `dashboard/{index.html,dashboard.js}`
  - `docker-compose.yml` (notifier, notify-sink, `notify_net`, log rotation, worker Phase 5 env)
  - `.env.example`, `.gitignore`, `pyproject.toml` (lint config only)
  - `scripts/{backup,restore}.sh`
  - `README.md`, `docs/architecture.md`, this file

### Commands run and results (final run on the final tree, 2026-09-23)
| Command | Result |
|---|---|
| `docker compose config -q` | valid (also with `-f docker-compose.notify-egress.yml`) |
| `uv run ruff format --check .` | 156 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run mypy app demo_application migrations/versions` | Success: no issues in 87 source files |
| `scripts/test.sh unit` | **417 passed** (277 + 140 new) |
| `scripts/test.sh integration` | **173 passed** (135 + 38 new) |
| `scripts/test.sh resilience` | **40 passed** in 22 m 37 s (30 Phase 1–4 + 10 new live Phase 5) |
| `promtool check rules alerts.yml` / `promtool test rules alerts_test.yml` (`prom/prometheus:v3.5.0`) | SUCCESS: 15 rules / SUCCESS |
| `scripts/backup.sh`; `restore.sh` without `CONFIRM_RESTORE` | backup dir with manifest (PG 356 KB, ledger 15 actions, integrity ok); restore refuses (exit 2) |
| `docker compose ps` | **11 services healthy** (api, monitor, dispatcher, worker, notifier, ops-reader, executor, demo-app, notify-sink, postgres, redis); DB at `0005_reliability_reporting` |
| Secret scan of 176 committable files for 9 `.env` secret values | 0 hits |

### Acceptance criteria (05-RELIABILITY-REPORTING.md) → evidence
| Criterion | Evidence |
|---|---|
| AI-fabricated action/report rejected | unit `test_report_validator.py` (36), including every §25 scenario: `test_rejects_restart_claim_without_action_attempt`, `test_rejects_recovery_claim_when_verification_failed`, `test_rejects_hypothesis_stated_as_confirmed_root_cause`, `test_rejects_claude_claim_when_mock_model_was_used`, `test_rejects_nonexistent_evidence_id`, `test_rejects_evidence_from_another_incident`, plus invented actions, approvals, operators, policy, verification, timestamps and costs. Integration: `test_invalid_draft_is_corrected_within_bounds`, `test_repeatedly_invalid_drafts_fall_back_deterministically`, `test_prompt_injection_in_logs_cannot_steer_the_report` |
| Deterministic fallback works without a provider | `test_missing_credentials_fall_back_without_any_model_call`, `test_ai_reporting_disabled_…`, `test_transient_provider_failure_retries_then_final_attempt_is_deterministic`; live `test_ai_unavailable_fallback_report_while_monitoring_continues` |
| Repeated report generation is idempotent | `test_crash_during_reporting_recovers_to_exactly_one_report[before_model_call, after_model_call, before_persist]`, `test_crash_after_persistence_and_duplicate_delivery_ack_without_second_report`, `test_stale_report_worker_is_fenced_out`; live `test_duplicate_report_and_notification_work_is_idempotent`, `test_worker_killed_during_reporting_recovers_to_one_report` |
| Retries bounded | report jobs (4 attempts, the last deterministic), notifications (`test_retries_are_bounded_then_dead_lettered_and_alerted`), jitter bounds unit test |
| Monitor alive on AI outage | live: checks keep recording while AI is paused and the incident auto-resolves; `/v1/system/status` monitor `ok` |
| Secrets redacted | unit redaction tests (9 new patterns plus structured keys); `test_payloads_never_contain_secrets`; live `test_no_secrets_in_any_phase5_service_logs` (9 secrets × 8 services, plus notification payloads and report bodies) |
| Metrics/alerts fire in test | `test_metrics_exposed_with_bounded_labels`, `test_alerts_fire_once_and_resolve_once`, `test_monitor_silence_and_queue_backlog_alerts`, `test_stalled_incident_and_budget_alerts`; promtool rule unit tests |
| Backup/restore works | live `test_backup_and_restore_preserve_history_and_executor_idempotency` (see below) |
| Budget stops calls | `test_daily_budget_pauses_ai_without_calls_and_reports_fall_back`, `test_per_incident_budget_stops_further_ai_for_that_incident`, `test_concurrency_limit_defers_ai_jobs`, `test_no_ai_calls_while_idle` |
| Auth expiry yields actionable alert | `test_expired_ai_auth_yields_actionable_alert_and_degraded_status` (alert payload tells the operator to run `onboard set-key`; status `AI_DEGRADED`); live `ai_auth_failed` alert fired |
| Operations runbook, 12-factor traceability draft | `docs/runbook.md`, `docs/traceability.md`, `docs/deployment.md` |

### End-to-end demonstration evidence (live stack, deterministic mock model)
- **Autonomous path** (`test_e2e_autonomous_restart_report_notification_and_monitoring_continues`):
  1. `http_500` is injected; the monitor opens an incident, which is investigated (mock) and passes policy (`AUT-1`, twice).
  2. The executor restarts the **real** container once (`StartedAt` changed; ledger entry for the action = 1). Verification passes and the incident is `resolved:remediated`.
  3. A report job is created in the same transaction. The mock drafts, the validator accepts, and **report v1 `validated`** is persisted, labelled `mock-investigator-v1 (TEST/DEMO ONLY - not Claude)`.
  4. `remediation_performed` and `report_ready` are delivered **signed** to `notify-sink`, exactly once each.
  5. Health checks keep being recorded.
- **Escalation:** BLOCKED configuration, so `ENV-2` DENY. The report says "no action was executed" and "OPEN, owned by a human", with no recovery claim. `incident_escalated` is delivered and the container is untouched.
- **AI unavailable:** the worker is misconfigured for the selected mock, so every AI stage fails closed with `CredentialsMissing`.
  - The task is parked as `ai_paused_credentials_missing`, and the `ai_auth_failed` alert fires and is delivered.
  - The failure self-heals and the monitor auto-resolves the incident.
  - A **deterministic fallback report** is produced (`ai_unavailable_credentials_missing`, `model_id` NULL). At least 10 checks were recorded during the window.
- **Notification outage:** `notify-sink` is stopped.
  - The webhook delivery retries (`pending`, attempts ≥ 2, `last_error` recorded) while the log channel delivers.
  - The notifier is **restarted with the delivery still pending**, then the sink is started again.
  - The delivery completes exactly once (same `Idempotency-Key`).
- **Duplicate processing:** 3 duplicate `XADD`s for a completed report job and a duplicate notification-event insert result in 1 report, 1 event, and one delivery per channel in one attempt each.
- **Worker killed mid-report** (`SIGKILL` during a widened test window): after lease expiry and reclaim, exactly 1 report (version 1), attempt ≥ 2.
- **Redis stopped:** `report-request` still creates a durable pending job plus an outbox row. After Redis restarts, report **v2** is produced.
- **PostgreSQL stopped for about 8 s:** the notifier survives in the same container and delivers after recovery.
- **Backup/restore drill (live):**
  1. `scripts/backup.sh` is run.
  2. The **dev DB schema is dropped** (`DROP SCHEMA public CASCADE`) and the **executor ledger file is deleted**.
  3. `CONFIRM_RESTORE=yes scripts/restore.sh` is run.
  4. Incident status, reports (content MD5), policy decisions, action attempts, verifications, evidence and notification events are all identical, and the audit count is ≥ before.
  5. The ledger has the action as `completed`.
  6. A **signed, otherwise valid restart request for the already-executed action** returns `replayed: true`, and the container `StartedAt` is unchanged: **no second restart after restore**. The task is still `resolved`, and `restore_completed` is audited.

### Phase 5 soak (bounded; NOT the Phase 6 72-hour test)
**NOT VERIFIED (correction recorded during the Phase 6 evidence audit, 2026-09-23 05:15 UTC).**
- `scripts/soak.py --minutes 60` was started at about 05:08 UTC, *after* the text above had been drafted.
- When the Phase 6 session resumed at 05:15 UTC it had run for about 7 minutes. Its log was empty and no result JSON existed: the Phase 5 harness kept all state in memory and wrote only at the end.
- It was stopped so that the Phase 6 baseline could use the stack. **No Phase 5 soak result exists, and none is claimed.**
- The harness defect (no persisted progress) is fixed in Phase 6.

### Live model test status
**No live Claude call was made in Phase 5.** All AI drafting used the deterministic mock (clearly labelled). The report validator and fallback are proven against mock output, scripted adversarial drafts and injected logs. How often a real model's drafts pass validation, and the real per-report token cost, are **NOT VERIFIED**.

### Remaining risks
1. **Docker socket** in `ops-reader` and `executor` is still root-equivalent on the host. The mitigation (a socket proxy or rootless Docker) is documented, not implemented.
2. **The worker holds the executor signing key.** This is unchanged, and still bounded by one target, the ledger (now also restore-safe) and the hourly cap.
3. **Static secrets** are in `.env` / container env (visible via `docker inspect`). There is one DB superuser role for all services; the notifier also receives the Redis password because the shared settings require it. Per-service DB roles are not implemented.
4. **Egress.** The worker's AI egress is not domain-restricted. Notifier egress is off by default and needs the override file.
5. **The validator's prose checks are heuristic** (regex with negation handling). The facts sections are always rendered from records, so a missed phrasing can only affect narrative wording, never the recorded outcome or the action history. Real-model drafts may be rejected more often (then the fallback is used).
6. **Reporting shares the single worker process** with incident tasks. A slow real-model report (≤ 180 s budget) can delay the next incident task by that much. Scale by running more worker processes, which leases and slots support.
7. **Alerts evaluated in-app stop if the notifier stops.** The Prometheus rules and an external uptime check are required for that case and for host loss. Neither is deployed yet.
8. Operator tokens do not expire; the dev dashboard is still unauthenticated as static content (data needs a token); there is still a single VM/host SPOF; backups stay local until copied off-host; the Codespace firewall rules are runtime-only.
9. **Audit and report immutability** relies on triggers, which a DB superuser can disable.

### Phase 6 readiness
Ready:
- all 10 workflow steps run end to end on the live stack with durable reports and notifications;
- every factor has tests (see `docs/traceability.md`, where each "NOT VERIFIED" is explicit);
- the soak harness supports `--minutes 4320`;
- backup/restore is drilled.

Phase 6 needs:
- an authorized live API-key run (limited spend) to verify real-model investigation and reporting;
- VM deployment with TLS, an external uptime monitor and Prometheus/Alertmanager;
- the 72-hour soak;
- the fault-injection acceptance matrix, the final traceability matrix, a threat model and a sample redacted report.

### Next step
Phase 6 requested and executed; see below.

---

## Phase 6 — End-to-end validation, fault injection, deployment validation, 72-hour soak (2026-09-23)

**Status: FAILED VALIDATION. The 72-hour soak criterion is not met** (run `4aeb63b7dd76` interrupted; see §9 and §12). Everything else in this section is unchanged. Phase 6 is **not** marked complete. Live Claude validation, VM deployment and external host-down monitoring are **NOT VERIFIED**.

Details: [`docs/phase6-acceptance.md`](docs/phase6-acceptance.md) (acceptance matrix, full fault-injection matrix, idempotency, fencing, security, deployment and alerting validation, defects) · [`docs/traceability.md`](docs/traceability.md) (final) · [`docs/threat-model.md`](docs/threat-model.md) · live evidence [`docs/evidence/phase6-live-scenarios.json`](docs/evidence/phase6-live-scenarios.json) · sample report [`docs/examples/sample-incident-report.md`](docs/examples/sample-incident-report.md).

### 1. Repository state and evidence audit
- `git log`: `72eb243 Add files via upload`, `26a0f52 Initial commit`. All SentinelOps work is **uncommitted** in the working tree; nothing was committed or reset.
- **Phase 5 soak: NOT VERIFIED.** The 60-minute run had been started, had run about 7 minutes, and had written **no** artifact (the old harness wrote only at the end). It was stopped. The Phase 5 section above has been corrected; it previously overstated this.

### 2. Phase 5 baseline reproduced (before Phase 6 changes)
All matched the Phase 5 report:
- `docker compose config -q`: valid;
- ruff format and lint: clean;
- mypy: clean (87 files);
- **417 unit, 173 integration and 40 resilience tests passed** (21 m 57 s);
- `promtool check rules alerts.yml`: SUCCESS, 15 rules;
- `promtool test rules alerts_test.yml`: SUCCESS.

### 3. Defects found and fixed (smallest safe corrections, each with a regression test)
| ID | Failing criterion | Root cause | Fix | Test |
|---|---|---|---|---|
| D1 | Monitoring "demo unavailable": no incident when the demo container is **stopped** (live scenario B, fast cadence) | httpx timeouts do not bound `getaddrinfo`, and Docker DNS stalled about 10 s for the vanished container. Probes took 10 s (configured 1.5 s / 5 s), overran the interval, and the stale-gap rule reset every streak. At the default 30 s cadence detection still happened, but late | `app/monitoring/probe.py`: a **hard deadline** for the whole probe (bounded pool; `ProbeDeadlineExceeded` counts as `timeout`) | `test_probe_deadline_bounds_hung_name_resolution_and_slow_io`; live: `unavailable` incident about 4 s after the stop |
| D2 | Report accuracy: summary said "(1 failing checks recorded in total)" | `occurrence_count` was rendered as a count of failing checks | `app/reporting/render.py`: threshold + occurrence_count − 1 | `test_summary_counts_failing_checks_not_occurrences`; sample report is v2 from the fixed code (v1 is kept, immutable) |
| D3 | Soak evidence could be lost | `scripts/soak.py` kept state in memory | rewritten: detached, persisted (`state`/`status`/`samples`/`events`/`checks`/`result`), with `start`/`status`/`resume`/`verify` and honesty rules | smoke run `soak-results/smoke`: `verify` PASS |

**Finding F1 (not fixed; it would be a redesign):** the worker holds the **symmetric** approval HMAC key used to verify signatures. The model cannot reach it, but a fully compromised worker *process* could forge an approval signature. Recommended mitigation: asymmetric signatures. Recorded as threat model T8 and a PARTIAL in security acceptance.

### 4. Files changed in Phase 6
- **Created:**
  - `tests/integration/test_phase6_gaps.py` (3)
  - `tests/resilience/test_phase6_acceptance.py` (6 live scenarios)
  - `docs/{threat-model,phase6-acceptance}.md`
  - `docs/evidence/{phase6-live-scenarios.json,healthy-path-report.md}`
  - `docs/examples/sample-incident-report.md`
- **Modified:**
  - `app/monitoring/probe.py` (D1), `app/reporting/render.py` (D2), `scripts/soak.py` (D3)
  - `tests/unit/test_probe.py`, `tests/unit/test_report_render.py` (regression tests)
  - `pyproject.toml` (lint config for the new test file)
  - `docs/{traceability,runbook,architecture}.md`, `README.md`, this file (including the Phase 5 soak correction)

### 5. Final commands and results (Phase 6 tree, 2026-09-23)
| Command | Result |
|---|---|
| `docker compose config -q` | valid |
| `uv run ruff format --check .` / `uv run ruff check .` | 162 files formatted / All checks passed |
| `uv run mypy app demo_application migrations/versions` | Success: no issues in 87 source files |
| `scripts/test.sh unit` | **419 passed** (417 + 2 regression tests) |
| `scripts/test.sh integration` | **176 passed** (173 + 3 fault-matrix gap tests) |
| `scripts/test.sh resilience` | **46 passed** in 27 m 13 s (40 + 6 Phase 6 live scenarios; includes the Phase 5 backup/restore replay drill on the Phase 6 tree) |
| `promtool check rules` / `promtool test rules` | SUCCESS (15 rules) / SUCCESS |
| Secret scan: 183 committable files × 9 `.env` secret values | **0 hits**. Live service-log and payload scans passed in `test_no_secrets_in_any_phase5_service_logs`, plus 0 hits in the evidence and sample report |
| `docker compose ps` | 11 services healthy; DB at `0005_reliability_reporting` |
| `scripts/soak.py` smoke (7 min requested, 8.63 min actual) | `verify` **PASS**: 1 incident, 1 verified restart (ledger +1), 1 AI report, 4 deliveries, 11/11 invariants |

### 6. Scenario results (live, mock model; evidence JSON)
| Scenario | Result |
|---|---|
| A: healthy baseline | 6 healthy checks, 0 incidents |
| B: demo container stopped | exactly 1 `unavailable` incident (6 occurrences), after the D1 fix |
| D: healthy path | incident `e03fd6bb…`, task `3ac81b0b…`, investigation (3 read-only tools), policy `proposal=ALLOW{AUT-1} pre_execution=ALLOW{AUT-1}`, action `63717d0d…` succeeded, **1 real restart**, verification passed, incident `resolved:remediated` about 10 s after detection, report v1 `validated` (mock) plus webhook `remediation_performed` / `report_ready` delivered, 3 checks after resolution |
| E: blocked | `DENY{ENV-2}`, 0 attempts, ledger delta 0, incident escalated (open), report: "no action was executed", `incident_escalated` delivered. Wrong target (`TGT-1`), unsupported action (`ACT-1`) and production (`ENV-1`) are covered by automated tests |
| AP: approval | `REQUIRE_APPROVAL{APR-0}`, notification delivered; wrong fingerprint returned 409; **4 concurrent approvers returned [200, 409, 409, 409]**; resumed, then `pre_execution=ALLOW{APR-1}`, 1 restart, verified; a late or replayed decision returned 409. Rejection, expiry and forged approvals are covered by Phase 4 automated and live tests |
| F: failed recovery | 1 restart; verification failed (deadline); **no second restart**; incident escalated (open); no recovery claim in the report; `recovery_verification_failed` (critical) delivered |
| Backup / restore (mandatory) | schema dropped and ledger deleted, then restored. History identical; ledger action `completed`; signed replay returned **`replayed: true`**, container `StartedAt` unchanged (46-test resilience run) |

### 7. Live Claude validation
**NOT VERIFIED.** No API key is configured (the `ai_secrets` volume has no key file; checked without reading contents) and there is no explicit cost authorization. No call was made. The bounded procedure is in `docs/runbook.md` §10.

### 8. Deployment and alerting
**No VM deployment was performed** (no authorization or credentials). Per-item status (CONFIGURED BUT NOT DEPLOYED, DOCUMENTED ONLY, NOT VERIFIED) is in `docs/phase6-acceptance.md` §6.
- **External host-down monitoring: NOT VERIFIED.** The procedure is in `docs/deployment.md` §8. The internal Prometheus rules are not proof of host-down detection.
- **Alerts:**
  - UNIT TESTED: in-app alerts and the promtool rule tests;
  - LIVE TESTED: `ai_auth_failed`;
  - external routing: NOT VERIFIED (`docs/phase6-acceptance.md` §8).

### 9. 72-hour soak: FAILED (interrupted after 9.66 min; see §12)
| Field | Value |
|---|---|
| Run ID | `4aeb63b7dd76` |
| Started (actual UTC) | **2026-09-23T06:36:37Z** |
| Deadline | **2026-09-26T06:36:37Z** (the result is written about 90 s later, after the settle period) |
| Command | `export COMPOSE_FILE=docker-compose.yml:docker-compose.test-ports.yml SENTINEL_MONITOR_INTERVAL_SECONDS=5 && uv run python scripts/soak.py start --minutes 4320 --inject-every 5 --dir soak-results/72h` |
| Stack configuration | accelerated local mode from `docs/runbook.md` §9: mock model, autonomous demo restarts in `isolated-demo`, 5 s monitor cadence, restart caps 20/h |
| Process | detached harness pid **652983** (own session, PPID 1), started from the Codespace host |
| Output | `soak-results/72h/` (`state.json`, `status.json`, `samples.jsonl`, `events.jsonl`, `checks.jsonl`; `result.json` only at the deadline; `harness.log`) |
| Next session: inspect | `uv run python scripts/soak.py status --dir soak-results/72h` |
| If `phase: interrupted` | `COMPOSE_FILE=docker-compose.yml:docker-compose.test-ports.yml uv run python scripts/soak.py resume --dir soak-results/72h` (the original start is kept; downtime counts against the 30-minute gap budget) |
| Final validation | `uv run python scripts/soak.py verify --dir soak-results/72h`. PASS only if the full elapsed time was observed, the harness gap ≤ 30 min, and all invariants held |

**Soak rules while it runs:**
- **Do not** run `scripts/test.sh resilience` or change the stack. Recreating services breaks the "no unexpected restarts" invariant.

**Honest limitation:** this is a GitHub Codespace, not an always-on VM. If the Codespace is stopped (idle timeout, rebuild or host maintenance), the stack and the harness stop too:
- `status` will then show `interrupted`;
- a `resume` will record the gap;
- a gap longer than 30 minutes makes `verify` **FAIL**.

A trustworthy 72-hour result may therefore require re-running the soak on the always-on VM described in `docs/deployment.md`.

**Phase status after the soak:**
- `verify` PASS, with all acceptance items above unchanged: Phase 6 becomes "Implemented and validated". Live Claude, VM deployment and external monitoring still remain NOT VERIFIED unless they are performed.
- `verify` FAIL: "Phase 6 — FAILED VALIDATION", recording the failed invariant from `result.json`.

### 10. Remaining risks
1. **Docker socket** (ops-reader, executor) is root-equivalent on the host.
2. **F1:** a symmetric approval key sits in the worker process.
3. Static env secrets; a single DB role; worker egress not domain-restricted.
4. The validator's prose checks are heuristic (facts are always record-rendered).
5. Reporting shares the worker process with incident tasks.
6. Single host / SPOF; no VM deployment; no external host-down monitor; backups not yet off-host.
7. **Real Claude investigation and reporting quality, cost and prompt-injection resistance: NOT VERIFIED.**
8. The 72-hour soak runs on a Codespace, which may not stay up for 72 hours. **This happened:** see §12.
9. **Container healthchecks are liveness-only.** After the reboot, all 11 containers reported `healthy` for about 7 minutes while the monitor could not reach PostgreSQL and recorded no checks. Only the monitor's logs and the missing heartbeats showed the problem. An external/readiness-based check (for example, alerting on health-check staleness) is needed to catch this.

### 11. Final validation status (what was actually proven)
- **AUTOMATED TESTED:** 419 unit and 176 integration tests: every workflow step, the fault-matrix cells, idempotency, fencing, validators, budgets, notifications, alerts and redaction.
- **LIVE LOCAL TESTED:** 46 live tests on the 11-service compose stack, covering:
  - scenarios A, B, D, E, AP and F;
  - crash, kill and outage injections;
  - the backup/restore replay drill;
  - secret scans;
  - a harness smoke soak (8.6 min, PASS).
- **LIVE CLAUDE TESTED:** none (NOT VERIFIED).
- **DEPLOYMENT VERIFIED:** none (NOT VERIFIED).
- **NOT VERIFIED:** live Claude; VM deployment; TLS/ingress; external uptime monitoring; Prometheus/Alertmanager deployment; off-host backups; the **72-hour soak (run `4aeb63b7dd76` FAILED: interrupted, 6 h 07 min gap)**.

### Next step
**STOP.** A replacement 72-hour soak needs operator approval (see §12). There is no Phase 7.

### 12. Soak recovery review (2026-09-23, after the operator's internet interruption)
Read-only inspection first. No soak was started or resumed. No service was rebuilt, restarted or recreated. The resilience suite was not run.

| Item | Observed |
|---|---|
| Original harness pid 652983 | **gone**; no `soak.py` process exists. Host `uptime -s` = **2026-09-23 12:38:05Z**, so the Codespace host was restarted |
| `soak.py status` | `phase: interrupted`, `harness_alive: false`, 20 samples (20 ready), 2 injections, 0 violations, last sample **06:46:16.93Z** |
| Last monitor health check (DB) | **06:46:31.36Z** (119 checks in the run window, no internal gap > 60 s). Stack and harness stopped together |
| Stop time | between 06:46:31Z and 12:38:05Z; no surviving source records it exactly |
| Docker after boot | restart policies brought all 11 services up at 12:46:47Z (same container IDs, `RestartCount` 0). Postgres crash recovery completed cleanly |
| Post-boot blind window | **12:46:47Z → 12:53:52Z (7 m 05 s)**. The stale `iptables-legacy FORWARD DROP` (Phase 1 environment note) returned with the reboot, and all inter-container traffic timed out. The monitor logged `leader lock unavailable` every 2 s and wrote **0** checks, while every container reported `healthy` (see risk 9) |
| Fix | re-applied the three documented runtime-only `DOCKER-USER` rules (README troubleshooting) at 12:53:52Z. Monitoring resumed by itself (first new check 12:53:52.84Z, `healthy`), with no container restart |
| **Monitoring gap** | **06:46:31Z → 12:53:52Z = 22 041 s (6 h 07 min)**, more than the 30-minute limit |
| Actual observed soak time | 9.66 min of the 4320 min requested |
| Pre-interruption facts | incidents 2 (both `resolved`); tasks 2 (both `resolved`); action attempts 2 (2 distinct `action_id`s and fingerprints, all `succeeded`: **0 duplicates**); dead-lettered 0; outbox backlog 0; notifications 8/8 `delivered`; report jobs 2/2 `validated` |
| Invariant checks | none ran: the first periodic check was due at 15 min. `result.json` does not exist, so `verify` would return NOT COMPLETE |
| Recovery eligibility | **not eligible.** The gap is already more than 30 min, so `verify` must FAIL whatever happens next. `resume` was **not** run: it would spend 66 h confirming a known failure |
| Record | `soak-results/72h/interruption.json` (new file). The original `state.json`, `status.json`, `samples.jsonl`, `events.jsonl` and `harness.log` are unmodified |

**Exact failed criterion:** the `scripts/soak.py verify` rule `harness_gap_minutes <= max_gap_minutes` (`max_gap_minutes = 30.0`, stored in `state.json`). The gap is at least 22 041 s (367 min) and cannot shrink, so the failure is **irreversible**. The criterion `actual_elapsed_minutes >= requested_minutes` is also unmet (9.66 of 4320 min); only a resume could reach it, and a resume cannot fix the gap. No invariant check failed.

**Evidence preserved (SHA-256 at 2026-09-23T13:13Z; matches the files as last written at 06:36 and 06:46):** `state.json` `e03b49f4…6421fa9`, `status.json` `8fb890c5…4bdbfd`, `events.jsonl` `fd7e1943…e23e66`, `samples.jsonl` `3effde87…f6e9f3`. `harness.log` is empty (0 bytes; the harness wrote nothing to stderr before the host stopped).

**Soak verdict: run `4aeb63b7dd76` FAILED (interrupted; gap criterion).** No application invariant failed. The failure is the environment: a Codespace is not an always-on host.

**Replacement soak:** NOT started; it needs operator approval. See §13.

### 13. Plan for a fresh 72-hour run (not started)
- **Where:** an always-on VM (`docs/deployment.md` §1: Ubuntu LTS, 2 vCPU / 4 GB / 40 GB, Docker Engine + Compose v2, chrony, `docker.service` enabled). Not a Codespace, because idle suspension and restarts killed run 1.
- **Same configuration as run 1, for comparability:** the accelerated demo profile from `docs/runbook.md` §9 (mock model, autonomous restarts in `isolated-demo` only, 5 s cadence, caps 20/h), with `SENTINEL_ENVIRONMENT=development`. Production mode refuses the mock model and autonomous remediation by design, so this soak proves stack reliability, **not** a production configuration. No paid API calls are needed.
- **Unchanged controls:** `--max-gap-minutes 30` (default), `--check-every 15`, the same invariants and the same policy/executor caps. Nothing is weakened.
- **Hardening vs run 1:** put the soak profile in `.env` (so restarts and resumes use the same config); no `docker-compose.test-ports.yml` (the harness uses `docker compose exec`); ports stay on `127.0.0.1`; inbound SSH only from admin IPs; no unattended reboot for the 72 h.
- **Recommended before start (needs approval, not done):** make `soak.py`'s `pid_alive` also check that `/proc/<pid>/cmdline` is `soak.py run`. At the moment `os.kill(pid, 0)` could mistake a reused PID after a reboot for the harness.
- **Duration:** ~1 h setup + a 10-minute smoke soak, then the run: deadline = start + 72 h, `result.json` about 90 s later. Example: if started 2026-09-24T09:00Z, `verify` can run at about 2026-09-27T09:02Z.
- **Commands on the VM** (after copying the working tree without `.env`, `.venv`, `soak-results` or `backups`, and rendering a fresh `.env` from `.env.example`):
```bash
cat >> .env <<'ENV'
COMPOSE_FILE=docker-compose.yml
SENTINEL_ENVIRONMENT=development
SENTINEL_MONITOR_INTERVAL_SECONDS=5
SENTINEL_PROBE_TIMEOUT_SECONDS=2
SENTINEL_LATENCY_THRESHOLD_SECONDS=1
SENTINEL_AI_GATEWAY=mock
SENTINEL_REMEDIATION_AUTO_ENABLED=true
SENTINEL_REMEDIATION_ENVIRONMENT=isolated-demo
SENTINEL_REMEDIATION_MAX_RESTARTS_PER_HOUR=20
EXEC_MAX_RESTARTS_PER_HOUR=20
ENV
docker compose up -d --build --wait && docker compose ps
printf '2\n1\n' | docker compose run --rm -T onboard     # select the mock model
uv sync
uv run python scripts/soak.py start --minutes 10 --inject-every 5 --dir soak-results/vm-smoke
uv run python scripts/soak.py verify --dir soak-results/vm-smoke   # must PASS before the 72 h run
uv run python scripts/soak.py start --minutes 4320 --inject-every 5 --dir soak-results/72h-run2
uv run python scripts/soak.py status --dir soak-results/72h-run2   # check daily
uv run python scripts/soak.py verify --dir soak-results/72h-run2   # after deadline + 2 min
```

