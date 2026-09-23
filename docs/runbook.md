# SentinelOps operations runbook

For the always-on VM described in [deployment.md](deployment.md) and for the local
development stack. SentinelOps is **not** a guaranteed 24/7 service: it runs on one host (a
single point of failure), and its AI features depend on an external provider and a paid API
key. Monitoring, durable queueing, policy, the executor, verification, deterministic
reporting and notifications keep working when the AI is unavailable.

Commands assume the repository root. On the dev host, use the test-ports override so the
host can reach PostgreSQL and Redis:
`export COMPOSE_FILE=docker-compose.yml:docker-compose.test-ports.yml`.
`$READ` is the `SENTINEL_API_READ_TOKEN` value. Never paste tokens into tickets or chat.

## 1. Daily checks (about 2 minutes)

| Check | Command | Healthy |
|---|---|---|
| Containers | `docker compose ps` | 11 long-running services `(healthy)` (dev: includes `notify-sink`) |
| Core readiness | `curl -s 127.0.0.1:8000/health/ready` | `"status":"ready"` |
| Degraded modes | `curl -s -H "Authorization: Bearer $READ" 127.0.0.1:8000/v1/system/status` | `"overall":"healthy"`, `modes: ["CORE_HEALTHY"]` |
| Alerts | same response, `alerts_firing` | `[]` |
| Backups | `sentinel_backup_last_success_age_seconds` in `/v1/metrics` | < 26 h (VM) |
| AI usage today | `sentinel_ai_daily_tokens` in `/v1/metrics` | below `SENTINEL_AI_DAILY_MAX_TOKENS` |

`/health/live` is process liveness only. `/health/ready` is the core dependencies (DB, schema,
Redis). `/v1/system/status` reports the degraded modes:
`CORE_HEALTHY | AI_DEGRADED | NOTIFICATIONS_DEGRADED | EXECUTOR_DEGRADED | MONITOR_DEGRADED |
REPORTING_DEGRADED | BACKUPS_DEGRADED | REDIS_UNAVAILABLE | DATABASE_UNAVAILABLE`. An optional
provider being down (AI, webhook) degrades the service; it never makes it "dead".

## 2. Incident handling

| Notification (event type) | Meaning | What to do |
|---|---|---|
| `approval_required` | Policy returned `REQUIRE_APPROVAL` for the demo restart | Review `GET /v1/approvals/{id}` with an operator token, then `POST /v1/approvals/{id}/approve` or `/reject` with `{"action_fingerprint": ...}` and an **approver** token. The notification itself is **never** an approval. If you do nothing, the approval expires and the task escalates (fails closed). |
| `remediation_performed` | One restart ran and the deterministic verifier confirmed recovery | Read the report. Watch for recurrence: the restart treated a symptom, and no cause is confirmed. |
| `recovery_verification_failed` | The single permitted restart did not restore health | See [recovery verification failed](#recovery-verification-failed). |
| `incident_escalated` | Policy denied, the proposal escalated, the approval expired or was rejected, or the restart failed | See [escalated incidents](#escalated-incidents). |
| `task_dead_lettered` | The task exhausted its retries | See [dead-lettered tasks](#dead-lettered-tasks). |
| `report_ready` | The incident report is persisted (AI-validated or deterministic fallback) | `GET /v1/incidents/{id}/report` |
| `report_failed` | The report job exhausted its attempts | See [report failures](#report-failures). |
| `ai_paused` / `system_degraded` / `alert_resolved` | Alert state changes (edge-triggered; repeated hourly while firing) | Section 4 |

<a id="escalated-incidents"></a>
### Escalated incidents
The task is terminal; the **incident stays open** (`escalated`) and is owned by a human.
1. Read the report: `GET /v1/incidents/{id}/report`. The *Policy decisions* section gives the
   rule IDs (e.g. `ENV-2`, `APR-3`, `TGT-1`), and *Actions actually executed* shows what ran.
2. Inspect the demo app directly (`docker compose logs demo-app`, `curl 127.0.0.1:8001/health`).
3. Fix it manually if needed. SentinelOps never restarts an escalated incident again (one
   restart per incident, enforced by a DB unique index and the executor ledger).
4. Close the incident in PostgreSQL once it is resolved (there is no close API in V1):
   `UPDATE incidents SET status='closed', resolved_at=now(), resolution='manual' WHERE id='…';`
   and request a final report version: `docker compose run --rm onboard report-request <incident_id>`.

<a id="recovery-verification-failed"></a>
### Recovery verification failed
The restart ran once, and the deterministic verifier did not see 3 consecutive fast, healthy
probes within the deadline, or it saw new critical log lines. The incident is `escalated` and
open. Follow the escalated-incident steps. The report's *Recovery verification* section lists
the probe counts and the reason.

<a id="dead-lettered-tasks"></a>
### Dead-lettered tasks
Usually a dependency (executor, ops-reader, AI provider 5xx) failed repeatedly. Check the task's
`last_error` (`GET /v1/tasks/{id}`) and `docker compose logs worker`. The incident is escalated
and a report is still produced from the records.

<a id="report-failures"></a>
### Report failures
Report failure never loses history: every record stays in PostgreSQL and is readable at
`GET /v1/incidents/{id}`. A job fails only after `SENTINEL_REPORT_JOB_MAX_ATTEMPTS` (default 4).
The last attempt is always the deterministic renderer, so a failure means the database or the
worker was unavailable during every attempt. Fix the cause, then request a new version with
`docker compose run --rm onboard report-request <incident_id>`.

## 3. Reports

- `GET /v1/incidents/{id}/report[?version=N]` returns a `status`:
  - `validated`: an AI draft that passed the deterministic validator;
  - `fallback`: the deterministic renderer, with `fallback_reason` saying why;
  - `pending` / `generating`: not ready yet;
  - `failed`;
  - `not_requested`.
- The **facts sections always come from records**: detection, investigation, evidence,
  hypotheses (always labelled UNCONFIRMED), proposed action, policy decisions, approvals,
  actions actually executed, verification, outcome and AI usage. Only the summary, timeline
  wording, observations, questions and follow-ups come from the validated draft.
- A **mock** model is labelled `TEST/DEMO ONLY - not Claude` everywhere, and a fallback report
  is labelled `DETERMINISTIC FALLBACK`.
- Reports are immutable (append-only trigger). A newer version is a new row.

## 4. Alerts

The notifier evaluates these rules every `SENTINEL_ALERT_EVAL_SECONDS` (30 s) and notifies on
state *changes*, re-notifying hourly while an alert keeps firing. The same conditions exist as
Prometheus rules in `deploy/observability/alerts.yml`; those still fire when the notifier or
the host is down.

<a id="host-or-api-down"></a>
### Host or API down (external check only)
From *outside* the host: the external uptime check or Prometheus `up == 0` fires. SSH in and run
`docker compose ps`, `docker compose logs --since 30m api`, and check the host itself (disk,
memory, reboot). Every service has `restart: unless-stopped`, and PostgreSQL and Redis data are on
named volumes.

<a id="alert-monitor-silent"></a>
### monitor_silent
No health check has been recorded for more than `SENTINEL_ALERT_MONITOR_SILENCE_SECONDS`.
Run `docker compose ps monitor` and `docker compose logs --since 15m monitor`. The monitor
buffers results during a DB outage and flushes them later; a stale heartbeat means the process is
stuck, so `docker compose restart monitor`.

<a id="alert-queue-backlog"></a>
### queue_backlog
The outbox or queued tasks are older than `SENTINEL_ALERT_QUEUE_BACKLOG_SECONDS`. Check
`sentinel_redis_up` and the dispatcher (`docker compose logs dispatcher`). If Redis is down,
work is retained in PostgreSQL and published when Redis returns (see
[Redis unavailable](#redis-unavailable)).

<a id="alert-ai-auth-failed"></a>
### ai_auth_failed (expired, revoked or missing credentials)
AI is paused; monitoring and queueing continue. Rotate or re-enter the key with
`docker compose run --rm onboard set-key`, then confirm with
`docker compose run --rm onboard status`. Parked tasks resume automatically on the dispatcher's
next cycle, keeping their pinned model. Subscription sign-in is not available for this
application (see `docs/auth-decision.md`).

<a id="alert-ai-paused"></a>
### ai_paused
AI work is parked for one of these reasons (listed in the alert's `reasons`):
- `ai_paused_rate_limited`, `ai_paused_quota_exceeded`: provider limits or spend. Check your
  Claude Console limits.
- `ai_paused_budget_exhausted`: a SentinelOps budget; see the next alert.

Reports fall back to the deterministic renderer while AI is paused.

<a id="alert-ai-budget-exhausted"></a>
### ai_budget_exhausted
The daily token budget (`SENTINEL_AI_DAILY_MAX_TOKENS`) or daily cost ceiling is spent. AI resumes
at the next UTC day. To resume sooner, raise the budget deliberately and recreate the worker.
Costs are **estimates** from operator-configured prices; SentinelOps hardcodes no prices.

<a id="alert-stalled-incident"></a>
### stalled_incident
An `open`, `investigating` or `remediating` incident's tasks have made no progress for
`SENTINEL_ALERT_STALLED_INCIDENT_SECONDS`. Check its tasks with `GET /v1/incidents/{id}`. The
reconcilers re-dispatch stuck work every `SENTINEL_REDISPATCH_AFTER_SECONDS`; if a task keeps
failing it will dead-letter.

<a id="alert-backup-failed"></a>
### backup_failed
The last backup run failed, or no backup succeeded within `SENTINEL_BACKUP_MAX_AGE_HOURS`. Run
`scripts/backup.sh` manually and read its error. The error is also stored in
`backup_runs.error`.

<a id="alert-notifications-failing"></a>
### notifications_failing
Deliveries are dead-lettered or older than `SENTINEL_ALERT_NOTIFICATION_BACKLOG_SECONDS`. Check:
- `GET /v1/notifications` (per-channel `status`, `attempt`, `last_error`);
- the webhook URL and allowlist;
- the provider's status.

A `4xx` other than 408, 425 or 429 is **not retried**; it is almost always configuration.
Incident processing is never blocked by notification failures, and the log channel always
delivers.

<a id="alert-report-failures"></a>
### report_failures
See [report failures](#report-failures).

<a id="alert-service-down"></a>
### service_down
The worker or dispatcher has not written its heartbeat for more than 120 s. Run
`docker compose ps` and `docker compose logs <service>`.

<a id="alert-executor-unreachable"></a>
### executor_unreachable
The worker cannot reach the restricted executor. Remediation is impossible: attempts retry, then
dead-letter and escalate. Run `docker compose ps executor` and `docker compose logs executor`.

<a id="redis-unavailable"></a>
### Redis unavailable
Redis is transport only. Incidents, tasks, report jobs and outbox rows are committed to
PostgreSQL and published once Redis returns. Run `docker compose restart redis`; nothing needs
replaying by hand.

## 5. Notifications

| Setting | Meaning |
|---|---|
| `SENTINEL_NOTIFY_CHANNELS` | `log` (always safe) and/or `webhook` |
| `SENTINEL_NOTIFY_WEBHOOK_URL` | ONE trusted destination; **HTTPS required** outside development, test and demo |
| `SENTINEL_NOTIFY_WEBHOOK_ALLOWED_HOSTS` | Explicit host allowlist; the URL's host must be in it |
| `SENTINEL_NOTIFY_WEBHOOK_SECRET` | HMAC-SHA256 signing secret (≥ 32 chars; required in production) |
| `SENTINEL_NOTIFY_MAX_ATTEMPTS`, `..._RETRY_BASE_SECONDS`, `..._RETRY_MAX_SECONDS` | Bounded, jittered exponential retries; then the delivery is dead-lettered |

- Each request carries `Idempotency-Key: <event id>` (de-duplicate on the receiver; delivery is
  at-least-once), `X-SentinelOps-Timestamp`, and `X-SentinelOps-Signature:
  sha256=HMAC(secret, "<timestamp>.<body>")`.
- Redirects are refused, response reads are bounded, and link-local and metadata addresses are
  always refused.
- To test the pipeline, run `docker compose run --rm onboard notify-test`.
- To reach a real provider, add `docker-compose.notify-egress.yml` (it gives only the notifier
  an egress route).

## 6. Backup and restore

```bash
scripts/backup.sh                                   # backups/<UTC ts>/{postgres.dump, executor-ledger.sqlite3, manifest.json}
CONFIRM_RESTORE=yes scripts/restore.sh backups/<UTC ts>
```

- **Authoritative, backed up:**
  - PostgreSQL: everything, including reports, notifications and the audit trail;
  - the executor's SQLite action ledger, taken with SQLite's online backup API.
- **Rebuildable, not backed up:**
  - Redis: the outbox and reconcilers re-dispatch;
  - `demo_state`: demo only.
- **Credential, not backed up:** `ai_secrets`. Re-enter it with `onboard set-key`.
- The restore stops all writers, restores PostgreSQL, then **merges** the ledger. The merge only
  ever adds or advances entries. Then it **tombstones** every action PostgreSQL knows was started
  or executed, so an executed restart can never look new. Finally it restarts the writers and
  records `restore_completed` in the audit trail.
- Copy `backups/` **off the host** (for example with `rsync` or object storage) on a schedule. A
  backup on the same disk does not survive host loss.
- Drill: the resilience test `test_backup_and_restore_preserve_history_and_executor_idempotency`
  drops the database schema and deletes the ledger, restores both, and proves that a signed
  replay of the executed action is not re-run.

## 7. Reconciliation ownership (every non-terminal state has a recovery path)

| Durable state | Owner / recovery | Bound |
|---|---|---|
| Unpublished outbox row | dispatcher `publish_pending`, with backoff | publish retries forever while Redis is down (data safe in PG); alert `queue_backlog` |
| `queued` / parked tasks with a lost message | dispatcher `reconcile_stuck_tasks` (every 15 s, per `redispatch_after` window) | claim is the guard |
| `running` task, dead worker | lease expiry, then XAUTOCLAIM or the reconciler; fencing token++ | `task_max_attempts`, then dead-letter and escalate |
| `retry_scheduled` | dispatcher `schedule_due_retries` | attempts bounded |
| `awaiting_investigation` (AI paused) | dispatcher `schedule_parked_investigations` at `next_attempt_at` | attempt refunded; alert `ai_paused` |
| `awaiting_policy` / `waiting_approval` | dispatcher `schedule_policy_tasks`; approval expiry fails closed | expiry TTL |
| action attempt `executing` | worker `_reconcile` from the executor ledger and container `StartedAt`; never blind re-issue | one restart per incident |
| report job `pending` / `generating` | outbox dispatch; XAUTOCLAIM; dispatcher `schedule_report_jobs` | `report_job_max_attempts` (last attempt deterministic), then `failed` and a notification |
| notification delivery `pending` / `sending` | notifier: due retries; expired leases reclaimed | `notify_max_attempts`, then `dead_lettered` and an alert |
| alert state | notifier, every `alert_eval_seconds` | edge-triggered |

## 8. Key and token rotation

| Secret | Rotate |
|---|---|
| Anthropic API key | `docker compose run --rm onboard set-key`, then delete the old key in the Claude Console |
| Operator tokens | `onboard operator-add NAME ROLE` (new), then `onboard operator-disable OLD` |
| `SENTINEL_API_READ_TOKEN`, `..._OPS_READER_TOKEN`, `..._EXECUTOR_TOKEN`, `..._ACTION_SIGNING_KEY`, `..._NOTIFY_WEBHOOK_SECRET` | Update `.env` (or the secrets manager), then `docker compose up -d` |
| `SENTINEL_APPROVAL_SIGNING_KEY` | As above. Approvals decided **and not yet executed** under the old key are then treated as forged (`APR-5`, fail closed); re-approve them |

## 9. Soak / reliability run (session-independent)
The harness runs **detached** and persists everything under its `--dir`:
- `state.json` (real UTC start, deadline, pid, baseline);
- `status.json`;
- `samples.jsonl`, `events.jsonl`, `checks.jsonl`;
- `result.json`, only once the deadline is actually reached.

It survives the terminal or Claude Code session ending. It does **not** survive a host or
Codespace stop; use `resume` afterwards, and the downtime is recorded as a harness gap.

```bash
# accelerated LOCAL configuration: deterministic mock model (not Claude), autonomous demo
# restarts in the isolated demo only, 5 s monitor cadence, raised hourly restart caps
export COMPOSE_FILE=docker-compose.yml:docker-compose.test-ports.yml \
  SENTINEL_MONITOR_INTERVAL_SECONDS=5 SENTINEL_PROBE_TIMEOUT_SECONDS=2 \
  SENTINEL_LATENCY_THRESHOLD_SECONDS=1 SENTINEL_AI_GATEWAY=mock \
  SENTINEL_REMEDIATION_AUTO_ENABLED=true SENTINEL_REMEDIATION_ENVIRONMENT=isolated-demo \
  SENTINEL_REMEDIATION_MAX_RESTARTS_PER_HOUR=20 EXEC_MAX_RESTARTS_PER_HOUR=20
docker compose up -d --wait
printf '2\n1\n' | docker compose run --rm -T onboard            # select the mock model
uv run python scripts/soak.py start  --minutes 4320 --inject-every 5 --dir soak-results/72h
uv run python scripts/soak.py status --dir soak-results/72h       # progress / liveness / violations
uv run python scripts/soak.py resume --dir soak-results/72h       # if status says "interrupted"
uv run python scripts/soak.py verify --dir soak-results/72h       # final PASS / FAIL / NOT COMPLETE
```

`verify` passes only if:
- the run really lasted the requested time;
- the harness was not down for more than `--max-gap-minutes` (30) in total;
- every periodic and final invariant held:
  - no duplicate active incidents, no lost tasks;
  - ≤ 1 action per incident; the ledger matches executed actions;
  - every terminal task has a report; no failed report jobs; no dead-lettered notifications;
  - the monitor was never silent; no unexpected service restarts; bounded memory growth;
  - the queue drained.

**Do not run the resilience test suite while a soak is running**: the tests recreate and stop
services, which the soak correctly reports as unexpected restarts.

## 10. Live Claude validation (bounded, paid, only with explicit authorization)
Not performed so far (no key, no cost authorization). Procedure:
1. **Credentials.** Create a dedicated Console key with a **low spend limit**. Enter it with
   `docker compose run --rm onboard`, choose "Anthropic API Key", and pick a model from the live
   list. Check with `docker compose run --rm onboard status`.
2. **Strict budgets for the run.** Add to `.env`:
   - `SENTINEL_AI_MAX_TOOL_CALLS=6`, `SENTINEL_AI_MAX_TOTAL_TOKENS=60000`,
     `SENTINEL_AI_INCIDENT_MAX_TOKENS=120000`, `SENTINEL_AI_DAILY_MAX_TOKENS=250000`;
   - operator prices `SENTINEL_AI_INPUT_USD_PER_MTOK` / `..._OUTPUT_...` from the current Anthropic
     pricing page, plus `SENTINEL_AI_DAILY_MAX_COST_USD=2`.

   Then run `docker compose up -d worker` with `SENTINEL_AI_GATEWAY=anthropic` (the default).
3. **Mode.** Keep approval mode (`SENTINEL_REMEDIATION_ENVIRONMENT=isolated-demo`, autonomy
   **off**) for the first run.
4. **One controlled incident.** Inject `http_500`, then approve or reject by hand.
5. **Record:**
   - `GET /v1/incidents/<id>` (investigation model, auth mode, tool calls, tokens, cost estimate);
   - `GET /v1/incidents/<id>/report` (`validated` or `fallback`, plus `validation.rejections`);
   - `sentinel_ai_*` metrics.

   A validator rejection or a fallback is a **valid** result; never weaken validators.
6. **Prompt injection.** Optional: repeat with an injection line in the demo logs (the
   `memory_log` mode or a crafted log line) and confirm the proposal stays allowlisted and the
   report makes no injected claims.
7. **Clean up.** Remove the key with `docker compose run --rm onboard remove-key` if it was
   temporary.

## 11. Start, stop, onboarding, emergency controls
| Task | Command |
|---|---|
| Start | `docker compose up -d --build --wait` then `docker compose ps` (11 healthy) |
| Stop safely (keep data) | `docker compose stop` or `docker compose down`. **Never** `down -v` on a real deployment |
| Onboard the API key and select a model | `docker compose run --rm onboard` (options: `status`, `change-model`, `set-key`, `remove-key`) |
| Check status | `curl -s -H "Authorization: Bearer $READ" 127.0.0.1:8000/v1/system/status` |
| Incidents and reports | `GET /v1/incidents`, `/v1/incidents/<id>`, `/v1/incidents/<id>/report` |
| **Emergency: disable autonomous remediation** | set `SENTINEL_REMEDIATION_AUTO_ENABLED=false` (the default) in `.env`, then `docker compose up -d worker`. Restart proposals then need approval, or are DENIED (`ENV-2`) if no environment is declared |
| **Emergency: make any restart impossible** | `docker compose stop executor`. Remediation attempts then fail closed (transient, dead-letter, escalate; never executed); monitoring, AI, reports and notifications continue |
| Disable AI entirely | `docker compose run --rm onboard remove-key`. AI tasks park, reports fall back to deterministic, monitoring continues |
