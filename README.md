# Groot_002 — SentinelOps

A bounded incident-response agent for **one** isolated FastAPI demo application.
The master contract is [`CLAUDE.md`](CLAUDE.md). Phase progress is tracked in [`PHASE_STATUS.md`](PHASE_STATUS.md).

> **Status: Phases 1–6 validated locally with the deterministic mock model (see
> `PHASE_STATUS.md`); live Claude, VM deployment and the 72-hour soak are NOT yet verified.**
> - Incidents are detected and investigated by Claude with read-only tools. The AI's proposal is
>   checked by a **deterministic policy**.
> - The only possible action is a restart of the isolated demo app. A restricted executor runs it
>   either automatically (only if you enable that for the isolated demo) or after an
>   authenticated human approval. The restart is then **verified**.
> - Every finished incident gets a **record-backed report**. The AI drafts it, a deterministic
>   validator checks it, and a **deterministic fallback** report is produced when AI is
>   unavailable.
> - Operators are **notified** through a durable, signed webhook plus logs.
> - Metrics, alerts, degraded-mode status, AI budgets, and backup/restore of PostgreSQL **and**
>   the executor ledger are covered.
>
> Local Docker Compose is a development environment, **not** a guaranteed always-on deployment
> (see [`docs/deployment.md`](docs/deployment.md)). The dev dashboard is read-only, served on
> `127.0.0.1` only, and is **not production-ready**.

Architecture and schema: [`docs/architecture.md`](docs/architecture.md). Authentication decision
(why subscription login is unavailable, how the API key is handled): [`docs/auth-decision.md`](docs/auth-decision.md).
Operations runbook (alerts, reports, notifications, backup/restore, rotation):
[`docs/runbook.md`](docs/runbook.md). VM deployment guide: [`docs/deployment.md`](docs/deployment.md).
Final traceability matrix: [`docs/traceability.md`](docs/traceability.md). Phase 6 acceptance and
fault-injection matrix: [`docs/phase6-acceptance.md`](docs/phase6-acceptance.md). Threat model:
[`docs/threat-model.md`](docs/threat-model.md). Sample incident report (mock model, TEST/DEMO ONLY):
[`docs/examples/sample-incident-report.md`](docs/examples/sample-incident-report.md).

## Requirements
- Docker Engine with Compose v2 (tested: Docker 29.8, Compose 5.5.1)
- For running tests on the host: Python 3.12 and [`uv`](https://docs.astral.sh/uv/) (tested: uv 0.12.17)

## 1. Configure the environment
```bash
cp .env.example .env
chmod 600 .env
# Fill in POSTGRES_PASSWORD, REDIS_PASSWORD, DEMO_INJECTION_TOKEN, SENTINEL_API_READ_TOKEN,
# SENTINEL_OPS_READER_TOKEN, SENTINEL_EXECUTOR_TOKEN, SENTINEL_ACTION_SIGNING_KEY,
# SENTINEL_APPROVAL_SIGNING_KEY and SENTINEL_NOTIFY_WEBHOOK_SECRET (each >=32 chars) with
# strong random values:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# DOCKER_GID: the group owning the Docker socket (granted only to ops-reader)
echo "DOCKER_GID=$(stat -c %g /var/run/docker.sock)" >> .env
```
**Do not put the Anthropic API key in `.env`.** It's entered in step 2b and stored only in the
`ai_secrets` volume.
`.env` is gitignored. Compose refuses to start if a required secret is missing, and the API
rejects empty or placeholder passwords. Never commit `.env` or paste its values into issues or logs.

## 2. Build and start
```bash
docker compose config -q          # validate the compose file
docker compose up -d --build --wait
docker compose ps
```
Startup order is enforced with health checks: `postgres` + `redis` healthy → `migrate` (runs
`alembic upgrade head`, then exits 0) → `api`, `monitor`, `dispatcher`, `worker`, `notifier`.
`demo-app` doesn't depend on the agent stack. Expect 11 running services, all `(healthy)`:
api, monitor, dispatcher, worker, notifier, ops-reader, executor, demo-app, postgres, redis, and
the **dev/test-only** `notify-sink` webhook receiver (remove it on a real deployment).

## 2b. Connect Claude (onboarding)
```bash
docker compose run --rm onboard            # interactive
```
```text
Welcome to SentinelOps

Choose authentication:
1. Claude Subscription  [unavailable]
2. Anthropic API Key
3. Exit
```
- **1** explains why subscription login is unavailable to this app and changes nothing.
- **2** asks for the key with masked input. It checks the key with a **free** model-list call
  (no tokens) and saves it only if it works. It then lists the models your key can use, straight
  from the provider, and saves the auth mode and model ID you pick. The key itself never goes to
  the database.
- API usage is billed pay-as-you-go to your Console account, **separately from any Claude
  subscription**. Set spend limits in the Console.

Other commands: `docker compose run --rm onboard status | change-model | set-key | remove-key`.
`change-model` affects only new investigations; a running investigation keeps its pinned model.
Until a model is selected, incidents are still detected and queued, and their tasks wait in
`awaiting_investigation`. They resume automatically once AI is configured.

## 3. Health checks
```bash
curl -s http://127.0.0.1:8000/health/live    # liveness: process only
curl -s http://127.0.0.1:8000/health/ready   # readiness: DB + schema at head + Redis (503 if not)
curl -s http://127.0.0.1:8001/health         # demo app health
curl -s http://127.0.0.1:8001/metrics        # demo app safe measurements
open http://127.0.0.1:8000/dashboard/        # dev-only shell
```

### Read-only status API (bearer token)
```bash
set -a; . ./.env; set +a
H="Authorization: Bearer $SENTINEL_API_READ_TOKEN"
curl -s -H "$H" 'http://127.0.0.1:8000/v1/incidents?status=open'
curl -s -H "$H"  http://127.0.0.1:8000/v1/incidents/<id>    # with tasks + evidence
curl -s -H "$H"  http://127.0.0.1:8000/v1/tasks/<id>        # with checkpoints
curl -s -H "$H"  http://127.0.0.1:8000/v1/metrics           # Prometheus text
curl -s -H "$H"  http://127.0.0.1:8000/v1/system/status     # degraded modes per component
curl -s -H "$H"  http://127.0.0.1:8000/v1/incidents/<id>/report   # latest report (+ status)
curl -s -H "$H"  http://127.0.0.1:8000/v1/notifications     # events + per-channel deliveries
```
Without a configured token, `/v1` returns 503 (fail closed). With a missing or wrong token it
returns 401.

### Watch detection and investigation end to end
Inject `http_500` (below). After 3 failed checks (about 90 s at the default 30 s cadence), one
`http_error` incident opens. The worker investigates it with the selected model, and the task
reaches `awaiting_policy`. The findings (observations, hypotheses, evidence IDs and the proposed
action) are shown under `investigations` in `GET /v1/incidents/<id>`. It stays at one
incident while failures continue. Reset to `none`: after 3 healthy checks the incident
auto-resolves (`resolution=auto_recovered`). Follow it with `docker compose logs -f monitor worker`.

### Demo failure injection (demo configuration only)
```bash
set -a; . ./.env; set +a
curl -s -X POST http://127.0.0.1:8001/simulate-failure \
  -H "X-Demo-Token: $DEMO_INJECTION_TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode":"http_500","duration_seconds":60}'     # modes: none|timeout|http_500|memory_log
```
The endpoint needs the token and a loopback or private-network client. With `DEMO_ENV=production`
it isn't registered (404), and enabling injection in production is rejected at startup.
`memory_log` only *reports* simulated memory pressure; it doesn't allocate memory.

## 3b. Remediation policy, approvals and demo
**Defaults are safe:** autonomous remediation is **off**, and no remediation environment is
declared. With these defaults every restart proposal is **denied** (rule `ENV-2`) and escalated
to a human. Production never restarts anything (`ENV-1`).

| Mode | `.env` settings | What a restart proposal does |
|---|---|---|
| Default (blocked) | none | DENY → incident escalated, stays open |
| Human approval | `SENTINEL_REMEDIATION_ENVIRONMENT=isolated-demo` | REQUIRE_APPROVAL → waits for an approver (15 min TTL) |
| Autonomous (demo only) | the above + `SENTINEL_REMEDIATION_AUTO_ENABLED=true` | ALLOW → restart → verification |

After changing these, run `docker compose up -d worker`. The policy always also requires:
- the trusted target (`demo-app`) and an active incident;
- fresh evidence from this incident;
- a fresh health check showing the app is **still failing**;
- no earlier restart for this incident (max 1), and at most 3 restarts per hour.

It's evaluated again immediately before the restart. Rule IDs are listed in
[`docs/architecture.md`](docs/architecture.md).

**Approvals** use per-operator tokens, stored only as SHA-256:
```bash
docker compose run --rm onboard operator-add alice approver   # prints the token ONCE
docker compose run --rm onboard approvals                     # pending approvals + fingerprints
curl -s -X POST http://127.0.0.1:8000/v1/approvals/<id>/approve \
  -H "Authorization: Bearer <alice-token>" -H 'Content-Type: application/json' \
  -d '{"action_fingerprint": "<fingerprint from the listing>", "reason": "checked logs"}'
# or .../reject ; viewers can only GET /v1/approvals
```
A decision must quote the exact action fingerprint. It is accepted once (replays get 409), only
before expiry, and is signed. If nobody answers before expiry, the request **expires** and the
incident is escalated; silence never counts as approval.

**Demo: failure → detection → AI → policy → restart → verification.** Use autonomous mode as
above, with a model selected (`onboard`) or the test-only mock model (`SENTINEL_AI_GATEWAY=mock`):
```bash
set -a; . ./.env; set +a
curl -s -X POST http://127.0.0.1:8001/simulate-failure -H "X-Demo-Token: $DEMO_INJECTION_TOKEN" \
  -H 'Content-Type: application/json' -d '{"mode":"http_500"}'
# about 90 s to detect at the default 30 s cadence, then investigation, policy, restart and verification
curl -s -H "Authorization: Bearer $SENTINEL_API_READ_TOKEN" http://127.0.0.1:8000/v1/incidents/<id>
#  -> investigations, policy_decisions, action_attempts, verifications
```
To demonstrate a **failed** recovery, add `"sticky": true`. The failure then survives the
restart, so verification fails and the incident is escalated with **no** second restart.
Reset with `{"mode":"none"}`.

### Remediation runbook
| Task outcome | Meaning / what to do |
|---|---|
| `waiting_approval:approval_required` | Review with `onboard approvals`, then approve or reject via the API before `expires_at`. |
| `escalated:policy_denied` | Policy refused. `policy_decisions.rule_ids` shows why (e.g. `ENV-2` autonomy not configured, `TGT-1` wrong target, `LIM-1` already restarted). A human takes over; the incident stays open. |
| `escalated:approval_expired` / `approval_rejected` | Nothing ran. Handle the incident manually. |
| `resolved:service_recovered_before_action` | The app recovered before the restart; nothing ran. The monitor closes the incident after 3 healthy checks. |
| `resolved:recovery_verified` | Restart ran once and passed verification; incident `resolved:remediated`. |
| `escalated:recovery_failed` | Restart ran but verification failed. The incident stays open and no further automatic restart happens (limit 1). Investigate by hand; the `verifications` row lists every probe and log finding. |
| `escalated:restart_failed` | Docker reported a failed restart (`action_attempts.error`). |
| `escalated:action_outcome_unknown` | The executor was interrupted mid-restart and no restart was observed. It was **not** retried automatically. Check the container, then act by hand. |
| `dead_lettered` with attempt `pending` | The executor was unreachable for every retry; nothing ran. Check `docker compose ps executor`. |

## 3c. Reports and notifications (Phase 5)
When an incident's task finishes (resolved, escalated, failed or dead-lettered) or the monitor
auto-resolves it, the **same transaction** does three things:
- creates a durable **report job**;
- queues an outcome **notification**;
- writes both to the transactional outbox.

**Reports.** The worker builds a canonical, bounded record of the incident from PostgreSQL. The
selected model (pinned; the mock in tests) drafts a structured report, and a deterministic
validator checks every ID, status, timestamp, approval, action and verification claim against
the record. The model gets at most 3 attempts. After that, or if AI is unavailable, disabled or
over budget, a **deterministic fallback** report is produced.

The published facts sections always come from records. Reports are versioned and immutable, and
labelled `validated` (AI) or `fallback`:
```bash
curl -s -H "$H" http://127.0.0.1:8000/v1/incidents/<id>/report | python3 -m json.tool
docker compose run --rm onboard report-request <incident_id>   # a new version on demand
```

**Notifications.** The `notifier` service delivers these events:
- `approval_required`, `incident_escalated`, `remediation_performed`,
  `recovery_verification_failed`;
- `ai_paused`, `task_dead_lettered`;
- `system_degraded` / `alert_resolved`;
- `report_ready`, `report_failed`.

It sends them to the `log` channel and a **signed webhook** (`Idempotency-Key`, HMAC signature).
Retries use bounded, jittered backoff, then the delivery is dead-lettered. Locally the webhook is
the dev/test `notify-sink`; to inspect it, run `docker compose exec notify-sink cat /tmp/notify-sink.jsonl`.

To use a real provider, set an HTTPS URL, its host in the allowlist and a secret (see
`.env.example`), and add `docker-compose.notify-egress.yml`. Test with
`docker compose run --rm onboard notify-test`.

**Alerts** (in-app, and as Prometheus rules in `deploy/observability/`): monitor silent, queue
backlog, AI auth failed/expired, AI paused, AI budget exhausted, stalled incident, backup failed
or overdue, notifications failing, report failures, service down, executor unreachable. **Host
down** needs an external uptime check (`docs/deployment.md`).

**AI budgets** (`.env.example`): per-investigation, per-incident and daily token/cost ceilings,
enforced from the `ai_usage` ledger before every model call. Exhaustion pauses AI; monitoring
continues. Costs are **estimates**, and only when you configure prices.

## 4. Stop
```bash
docker compose stop          # stop containers, keep everything
docker compose down          # remove containers/networks, KEEP named volumes (data persists)
docker compose down -v       # DESTRUCTIVE: also deletes pgdata/redisdata volumes
```

## 5. Backup and restore (PostgreSQL + executor ledger)
```bash
./scripts/backup.sh                                            # -> backups/<UTC>/ (mode 600)
CONFIRM_RESTORE=yes ./scripts/restore.sh backups/<UTC>          # DESTRUCTIVE for PostgreSQL
```
A backup directory holds:
- `postgres.dump`;
- `executor-ledger.sqlite3`, taken with SQLite's **online backup API**;
- a checksummed `manifest.json`.

Every run is recorded in `backup_runs` (a failed or overdue backup alerts) and in the audit
trail.

The restore:
1. stops the writers;
2. verifies the checksums;
3. restores PostgreSQL;
4. **merges** the executor ledger. Entries are never removed or downgraded, and every action
   PostgreSQL knows was started or executed is tombstoned. A restore can never make an executed
   restart look new.

Redis is rebuildable and not backed up. The API key (`ai_secrets`) is a credential: re-enter it
with `onboard set-key`. Backups stay local to this host; copy them off-host for real disaster
recovery. Legacy single-file `.dump` backups still restore PostgreSQL.

## 6. Tests and checks
```bash
uv sync                                     # installs locked deps incl. dev tools
uv run ruff format --check . && uv run ruff check .
uv run mypy app demo_application migrations/versions
scripts/test.sh unit                        # no services needed
scripts/test.sh integration                 # real PostgreSQL + Redis
scripts/test.sh resilience                  # drives the live stack (~35 min)
scripts/test.sh all
# Prometheus rules (optional; needs Docker):
docker run --rm -v "$PWD/deploy/observability":/cfg:ro -w /cfg --entrypoint promtool \
  prom/prometheus:v3.5.0 test rules alerts_test.yml
# session-independent soak (persisted progress; see docs/runbook.md §9)
uv run python scripts/soak.py start --minutes 4320 --inject-every 5 --dir soak-results/72h
uv run python scripts/soak.py status --dir soak-results/72h
```
`scripts/test.sh` reads `.env` without printing it. It starts the stack with
`docker-compose.test-ports.yml`, which publishes DB/Redis on 127.0.0.1 only (never use it on a
shared host), and exports `COMPOSE_FILE` so the tests' own compose calls use the same files.
The resilience suite temporarily recreates `monitor` with a 2 s cadence and restores the
default 30 s cadence afterwards. The Phase 3 and Phase 4 live tests switch the worker to the deterministic
**mock** model (`SENTINEL_AI_GATEWAY=mock`, labelled "not Claude") and restore the defaults
afterwards. No test calls Claude or spends money. The Phase 4 live tests **really restart the
demo-app container** several times. They raise the hourly restart caps for the run, use a short
approval TTL, and briefly stop the executor, Redis and PostgreSQL. The Phase 5 live tests:
- recreate the notifier with fast retries;
- stop and start `notify-sink`, Redis and PostgreSQL;
- kill the worker mid-report;
- run a **backup/restore drill that drops the dev database schema and deletes the executor
  ledger**, then restores both from the fresh backup.

Expect about 35 minutes in total.

## 7. Troubleshooting
| Symptom | Check / fix |
|---|---|
| `required variable POSTGRES_PASSWORD is missing` | Create `.env` from `.env.example` (step 1). |
| `service "migrate" didn't complete successfully` | `docker compose logs migrate`. Usually the DB isn't reachable or the credentials changed after the volume was initialised (the Postgres password is set only on first init). |
| `monitor`/`dispatcher`/`worker` unhealthy | Their health check is a heartbeat file age. Check `docker compose logs <svc>`. The monitor logs `standby` if another monitor holds the leader lock. |
| Tasks stay `awaiting_investigation` with outcome `ai_not_configured` | No model selected yet: `docker compose run --rm onboard`. |
| Outcome `ai_paused_credentials_missing` / `_authentication_failed` / `_quota_exceeded` / `_rate_limited` | The key is missing, invalid or expired, credit is exhausted, or you're rate-limited. Fix it (`onboard set-key`, Console billing), then tasks resume by themselves after `SENTINEL_AI_PAUSE_SECONDS` (default 300). Monitoring is unaffected. See `sentinel_ai_paused_tasks` in `/v1/metrics`. |
| Task `escalated` with outcome `invalid_ai_output`, `budget_exhausted` or `model_unavailable` | The AI couldn't produce a valid, evidence-backed result within its limits, or the pinned model is gone. A human takes over. The details are in the incident's `investigations` entry. |
| Worker can't reach `api.anthropic.com` (`provider_unavailable`) on this Codespace | The same stale `iptables-legacy` issue as below also blocks routed egress. A runtime-only rule scoped to the AI egress bridge: `sudo iptables-legacy -I DOCKER-USER -o sentinel-egress -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT && sudo iptables-legacy -I DOCKER-USER -i sentinel-egress ! -o sentinel-egress -j ACCEPT` (remove with `-D`). |
| `/v1/...` returns 503 | `SENTINEL_API_READ_TOKEN` isn't set in `.env` (fail closed). |
| Incidents open but tasks stay `queued` | Is `worker` running? Check `sentinel_queue_lag` / `sentinel_outbox_unpublished` in `/v1/metrics`. If Redis is down, the outbox rows keep retrying with backoff. |
| `/health/ready` returns 503 | The body names the failing check (`database`, `schema`, `redis`) as `fail` or `timeout`. `schema` means migrations haven't reached head: `docker compose run --rm migrate`. |
| Containers can't reach each other (connection timeouts on `postgres:5432`) although all are healthy | Host firewall issue. Some hosts, including this GitHub Codespace, have stale `iptables-legacy` rules with `FORWARD DROP` that drop traffic between containers on user-defined bridges. Check with `sudo iptables-legacy -S FORWARD`. A runtime-only workaround that allows only same-bridge (L2) traffic: `sudo iptables-legacy -I DOCKER-USER -m physdev --physdev-is-bridged -j ACCEPT`. Remove it with `-D` in place of `-I`. |
| Port 8000/8001 already in use | Stop the other process or change the host side of `ports:` in `docker-compose.yml` (keep `127.0.0.1:`). |
| Need a clean slate | `docker compose down -v` (deletes all data; back up first). |
| Report `status: pending` for a long time | Is the worker consuming the reports stream? Check `sentinel_report_queue_lag` and `docker compose logs worker`. Jobs are re-dispatched by the dispatcher if a message is lost. |
| Report is `fallback` | `report.fallback_reason` says why (for example `ai_unavailable_credentials_missing`, `validation_failed`, `ai_budget_exhausted_daily`, `final_attempt_deterministic`). The facts are still complete. |
| Notifications not arriving | `GET /v1/notifications` shows each channel's `status`, `attempt` and `last_error`. Common causes: a 4xx from the receiver (not retried), a host not in `SENTINEL_NOTIFY_WEBHOOK_ALLOWED_HOSTS` (the notifier refuses to start), or no egress (add `docker-compose.notify-egress.yml`). |
| `/v1/system/status` shows `AI_DEGRADED` | AI work is paused (auth, quota, rate limit or budget). See `docs/runbook.md` §4. |
