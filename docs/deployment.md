# Deployment guide: first unattended deployment (always-on VM)

> **Status:** guidance only. As of Phase 5, **no VM deployment has been performed**; Phase 6 does
> it, and only with explicit authorization and credentials. Local Docker Compose is a
> development environment, not an always-on service.

**What this is and is not.** This setup is one VM running the compose stack. It is a
**single point of failure**: host, disk, Docker daemon or network loss stops monitoring,
remediation and notifications until the host returns. There is **no guaranteed 24/7 SLA**. AI
features depend on an external provider, a paid API key (billed separately from any Claude
subscription) and its rate and spend limits. Subscription sign-in is not supported for this
application (`docs/auth-decision.md`), so there are no "unlimited" subscription calls and no
unattended OAuth refresh. When AI is unavailable, SentinelOps keeps monitoring, queueing,
applying policy, verifying and producing **deterministic** reports.

## 1. Host
- 2 vCPU, 4 GB RAM and 40 GB SSD is plenty. The compose limits total about 3.3 GB, and the
  containers log to rotated JSON files (10 MB × 5 each).
- A current Linux LTS with Docker Engine and the Compose v2 plugin.
- Automatic security updates on, and a firewall allowing inbound 443 (and 22 from admin IPs) only.
- Time sync (chrony or systemd-timesyncd): leases, approvals and signatures depend on the clock.
- Enable `docker.service` at boot. Every service has `restart: unless-stopped`.

## 2. Persistent storage
Named volumes: `pgdata` (authoritative), `executor_state` (the executor's action ledger:
**safety-critical**), `redisdata` (AOF; rebuildable), `ai_secrets` (API key file), and
`demo_state`. Put `/var/lib/docker` on a persistent, backed-up disk. Never run
`docker compose down -v` on the VM.

## 3. Secrets
- Generate each secret with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
  Keep them in a secrets manager and render `.env` (mode 0600, owned by the deploy user) at
  deploy time. Never commit them.
- Prefer Docker secrets or files mounted from the secrets manager over environment variables,
  because environment variables are visible to anyone with Docker access (`docker inspect`).
  This is a **known limitation** of the current compose file (see architecture "Security review").
- The Anthropic key lives only in the `ai_secrets` volume (`onboard set-key`). For production,
  prefer Workload Identity Federation (officially supported; not yet implemented).
- Set `SENTINEL_ENVIRONMENT=production`. Settings validation then refuses the mock model,
  autonomous remediation, test hooks, HTTP webhooks and unsigned webhooks.

## 4. Secure ingress (TLS + auth)
Keep every container port on `127.0.0.1` (the compose default) and terminate TLS in a reverse
proxy on the host. Example with Caddy (automatic certificates):

```caddyfile
sentinelops.example.com {
    encode gzip
    @api path /health/* /v1/*
    reverse_proxy @api 127.0.0.1:8000
    respond /dashboard* 404          # the dev dashboard is never exposed (it is disabled in production anyway)
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        -Server
    }
}
```

- `/v1/*` requires bearer tokens: the read token for status, reports and metrics, and operator
  tokens for approvals. Approvals are header-authenticated, cookie-less and CSRF-hardened.
- Restrict `/v1/approvals` by source IP or VPN where possible.
- Do **not** publish `8001` (the demo app) beyond what the demo needs, and never publish
  PostgreSQL, Redis, ops-reader or the executor.
- Do not use `docker-compose.test-ports.yml` on the VM.

## 5. Start
```bash
cp .env.example .env && $EDITOR .env        # or render from the secrets manager
docker compose up -d --build --wait
docker compose run --rm onboard              # API-key auth + model selection
docker compose run --rm onboard operator-add alice approver
docker compose ps && curl -fsS https://sentinelops.example.com/health/ready
```

## 6. Notifications
Set `SENTINEL_NOTIFY_CHANNELS=log,webhook`, an HTTPS `SENTINEL_NOTIFY_WEBHOOK_URL`, its host in
`SENTINEL_NOTIFY_WEBHOOK_ALLOWED_HOSTS`, and `SENTINEL_NOTIFY_WEBHOOK_SECRET`. Then:
- start with `-f docker-compose.yml -f docker-compose.notify-egress.yml`, which gives only the
  notifier an egress route;
- remove the dev `notify-sink` service, or leave the URL pointing elsewhere;
- restrict egress on the host firewall to the webhook host (bridge `sentinel-notify`), and the
  worker's `sentinel-egress` bridge to `api.anthropic.com`;
- test with `docker compose run --rm onboard notify-test`.

## 7. Backups
```cron
# /etc/cron.d/sentinelops (as the deploy user)
15 2 * * * deploy cd /opt/sentinelops && scripts/backup.sh >> /var/log/sentinelops-backup.log 2>&1 && rclone copy backups/ offsite:sentinelops-backups/
```
- Set `SENTINEL_BACKUP_MAX_AGE_HOURS=26` so a missed or failed backup alerts.
- **Copy backups off-host** (object storage with versioning and retention).
- Drill a restore at least monthly (`docs/runbook.md` §6). The drill proves that PostgreSQL
  history, reports and executor-ledger idempotency survive.

## 8. External monitoring (required: in-host checks cannot report host failure)
- An **external uptime check** (a hosted uptime service, or Prometheus + blackbox_exporter on a
  different machine) should hit `https://sentinelops.example.com/health/ready` every minute and
  page on 2 failures. This is the only way to detect **host down**.
- Prometheus scrapes `/v1/metrics` with the read token (`deploy/observability/prometheus.yml`)
  and loads `deploy/observability/alerts.yml`. Validate the rules with
  `promtool check rules` and `promtool test rules deploy/observability/alerts_test.yml`.
  Alertmanager routes to the same on-call destination as the webhook.
- Optional: import `deploy/observability/grafana-dashboard.json` into Grafana.

## 9. Maintenance
- **Upgrades:** take a backup, then `git pull`, then `docker compose up -d --build --wait`.
  Migrations run in the `migrate` one-shot, serialized with an advisory lock, and are
  forward-only in practice. Downgrades exist but drop Phase data.
- **Key rotation:** `docs/runbook.md` §8.
- **Retention:**
  - health checks 7 days;
  - finished notifications 30 days (`SENTINEL_NOTIFY_RETENTION_DAYS`);
  - container logs 50 MB per service;
  - the audit trail and reports are append-only and kept. Archive them externally if volume matters.
- **Docker socket:** `ops-reader` (read-only) and `executor` (one restart action) hold the socket
  group, which is **root-equivalent on the host**. Consider a socket proxy that allows only
  `GET /containers/*` and `POST /containers/{id}/restart` for the labelled container, or
  rootless Docker.

## 10. RTO / RPO (targets: not yet measured on a VM)
| Scenario | Recovery | Data loss (RPO) |
|---|---|---|
| Container crash | automatic restart (seconds); leases, fencing and reconcilers resume work | none (PostgreSQL is authoritative) |
| Host reboot | Docker starts the stack at boot; readiness in about 1 min | none on persistent volumes |
| Disk or host loss | a new VM plus a restore from the last off-host backup (about 30–60 min manual) | since the last backup (≤ 24 h with daily backups) |

Measured availability, RTO and RPO on the real VM are Phase 6 deliverables. The Phase 5 local
drill evidence is in `PHASE_STATUS.md`.
