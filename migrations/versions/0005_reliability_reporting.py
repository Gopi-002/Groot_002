"""Phase 5: versioned incident reports + report jobs, durable notifications,
AI usage ledger and concurrency slots, alert state, service heartbeats,
backup runs, guarded task lifecycle transitions, append-only hardening.

Revision ID: 0005_reliability_reporting
Revises: 0004_policy_execution
Create Date: 2026-09-23
"""

from alembic import op

revision = "0005_reliability_reporting"
down_revision = "0004_policy_execution"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
-- Guarded task lifecycle (see app/agent/lifecycle.py). Terminal task statuses are
-- final; an active task can only move along the documented edges. A buggy or
-- stale code path can therefore never resurrect a finished task.
CREATE OR REPLACE FUNCTION sentinel_guard_task_status() RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status IN ('escalated','failed','resolved','dead_lettered') THEN
        RAISE EXCEPTION 'illegal task transition % -> % (terminal)', OLD.status, NEW.status
            USING ERRCODE = 'SF002';
    END IF;
    IF NEW.status IN ('escalated','failed','resolved','dead_lettered') THEN
        RETURN NEW;
    END IF;
    IF NEW.status = 'running' AND OLD.status IN
        ('queued','retry_scheduled','awaiting_investigation','awaiting_policy','waiting_approval') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'running' AND NEW.status IN
        ('awaiting_investigation','awaiting_policy','waiting_approval','retry_scheduled') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal task transition % -> %', OLD.status, NEW.status
        USING ERRCODE = 'SF002';
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_tasks_guard_status BEFORE UPDATE OF status ON tasks
    FOR EACH ROW EXECUTE FUNCTION sentinel_guard_task_status();

-- Append-only hardening: row triggers do not fire on TRUNCATE.
CREATE OR REPLACE FUNCTION sentinel_forbid_truncate() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_audit_events_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION sentinel_forbid_truncate();
CREATE TRIGGER trg_policy_decisions_no_truncate BEFORE TRUNCATE ON policy_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION sentinel_forbid_truncate();

-- Report jobs: one durable, leased, fenced job per report request (workflow
-- step 9). Created in the SAME transaction as the terminal task transition.
CREATE TABLE report_jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id      uuid NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
    task_id          uuid REFERENCES tasks(id) ON DELETE RESTRICT,
    dedup_key        text NOT NULL UNIQUE,
    reason           text NOT NULL,
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN
                       ('pending','generating','validated','fallback','failed')),
    attempt          integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts     integer NOT NULL DEFAULT 4 CHECK (max_attempts >= 1),
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    lease_owner      text,
    lease_expires_at timestamptz,
    fencing_token    bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    model_id         text CHECK (model_id IS NULL OR length(model_id) BETWEEN 1 AND 200),
    auth_mode        text CHECK (auth_mode IS NULL OR auth_mode IN ('api_key','subscription','mock')),
    progress         jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error       text,
    report_id        uuid,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz,
    CONSTRAINT ck_report_jobs_lease_pair CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    CONSTRAINT ck_report_jobs_attempt_bound CHECK (attempt <= max_attempts),
    CONSTRAINT ck_report_jobs_done
        CHECK ((status IN ('validated','fallback')) = (report_id IS NOT NULL))
);
CREATE INDEX ix_report_jobs_incident ON report_jobs (incident_id);
CREATE INDEX ix_report_jobs_active ON report_jobs (status, next_attempt_at)
    WHERE status IN ('pending','generating');
CREATE TRIGGER trg_report_jobs_updated_at BEFORE UPDATE ON report_jobs
    FOR EACH ROW EXECUTE FUNCTION sentinel_set_updated_at();

-- Reports become immutable, versioned documents with provenance.
ALTER TABLE reports ADD COLUMN job_id uuid UNIQUE REFERENCES report_jobs(id) ON DELETE RESTRICT;
ALTER TABLE reports ADD COLUMN version integer CHECK (version IS NULL OR version >= 1);
ALTER TABLE reports ADD COLUMN generation_mode text
    CHECK (generation_mode IS NULL OR generation_mode IN ('ai','deterministic_fallback'));
ALTER TABLE reports ADD COLUMN auth_mode text
    CHECK (auth_mode IS NULL OR auth_mode IN ('api_key','subscription','mock'));
ALTER TABLE reports ADD COLUMN content jsonb;
ALTER TABLE reports ADD COLUMN record_sha256 text
    CHECK (record_sha256 IS NULL OR record_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE reports ADD COLUMN fallback_reason text;
ALTER TABLE reports ADD COLUMN model_calls integer NOT NULL DEFAULT 0 CHECK (model_calls >= 0);
ALTER TABLE reports ADD COLUMN input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0);
ALTER TABLE reports ADD COLUMN output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0);
ALTER TABLE reports ADD COLUMN cost_usd numeric(12, 6) CHECK (cost_usd IS NULL OR cost_usd >= 0);
ALTER TABLE reports ADD CONSTRAINT ck_reports_versioned CHECK (job_id IS NULL OR (
    version IS NOT NULL AND generation_mode IS NOT NULL AND content IS NOT NULL
    AND record_sha256 IS NOT NULL
    AND (generation_mode = 'ai') = (model_id IS NOT NULL AND auth_mode IS NOT NULL)));
CREATE UNIQUE INDEX uq_reports_incident_version ON reports (incident_id, version);
ALTER TABLE report_jobs ADD CONSTRAINT fk_report_jobs_report
    FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE RESTRICT;
CREATE TRIGGER trg_reports_append_only BEFORE UPDATE OR DELETE ON reports
    FOR EACH ROW EXECUTE FUNCTION sentinel_forbid_mutation();
CREATE TRIGGER trg_reports_no_truncate BEFORE TRUNCATE ON reports
    FOR EACH STATEMENT EXECUTE FUNCTION sentinel_forbid_truncate();

-- Logical notification events (deduplicated) and their per-channel deliveries.
-- Payloads are built deterministically from records and never contain secrets.
CREATE TABLE notification_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type    text NOT NULL CHECK (event_type IN
                    ('approval_required','incident_escalated','remediation_performed',
                     'recovery_verification_failed','ai_paused','task_dead_lettered',
                     'system_degraded','alert_resolved','report_ready','report_failed',
                     'test')),
    severity      text NOT NULL CHECK (severity IN ('info','warning','critical')),
    dedup_key     text NOT NULL UNIQUE,
    incident_id   uuid REFERENCES incidents(id) ON DELETE RESTRICT,
    payload       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    fanned_out_at timestamptz
);
CREATE INDEX ix_notification_events_fanout ON notification_events (created_at)
    WHERE fanned_out_at IS NULL;
CREATE INDEX ix_notification_events_incident ON notification_events (incident_id);

CREATE TABLE notification_deliveries (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         uuid NOT NULL REFERENCES notification_events(id) ON DELETE RESTRICT,
    channel          text NOT NULL CHECK (channel IN ('log','webhook')),
    status           text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','sending','delivered','dead_lettered')),
    attempt          integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts     integer NOT NULL DEFAULT 6 CHECK (max_attempts >= 1),
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    lease_owner      text,
    lease_expires_at timestamptz,
    last_error       text,
    last_http_status integer,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    delivered_at     timestamptz,
    CONSTRAINT uq_notification_deliveries_channel UNIQUE (event_id, channel),
    CONSTRAINT ck_notification_deliveries_lease CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    CONSTRAINT ck_notification_deliveries_attempts CHECK (attempt <= max_attempts),
    CONSTRAINT ck_notification_deliveries_delivered
        CHECK ((status = 'delivered') = (delivered_at IS NOT NULL))
);
CREATE INDEX ix_notification_deliveries_due ON notification_deliveries (next_attempt_at)
    WHERE status IN ('pending','sending');
CREATE TRIGGER trg_notification_deliveries_updated_at BEFORE UPDATE ON notification_deliveries
    FOR EACH ROW EXECUTE FUNCTION sentinel_set_updated_at();

-- Every model call (success or typed failure) with provider-reported usage.
-- cost_usd_estimate is set only when the operator configured prices.
CREATE TABLE ai_usage (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    stage               text NOT NULL CHECK (stage IN ('investigation','report')),
    incident_id         uuid REFERENCES incidents(id) ON DELETE RESTRICT,
    task_id             uuid REFERENCES tasks(id) ON DELETE RESTRICT,
    report_job_id       uuid REFERENCES report_jobs(id) ON DELETE RESTRICT,
    model_id            text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 200),
    auth_mode           text NOT NULL CHECK (auth_mode IN ('api_key','subscription','mock')),
    outcome             text NOT NULL CHECK (outcome ~ '^[a-z_]{2,40}$'),
    input_tokens        bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens       bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens   bigint NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens  bigint NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    latency_ms          double precision NOT NULL CHECK (latency_ms >= 0),
    cost_usd_estimate   numeric(12, 6) CHECK (cost_usd_estimate IS NULL OR cost_usd_estimate >= 0),
    provider_request_id text
);
CREATE INDEX ix_ai_usage_time ON ai_usage (occurred_at);
CREATE INDEX ix_ai_usage_incident ON ai_usage (incident_id);

-- Global cap on concurrently running AI jobs (leased slots; crash-safe via expiry).
CREATE TABLE ai_job_slots (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slot        integer NOT NULL UNIQUE CHECK (slot >= 1),
    holder      text,
    acquired_at timestamptz,
    expires_at  timestamptz,
    CONSTRAINT ck_ai_job_slots_pair CHECK ((holder IS NULL) = (expires_at IS NULL))
);

-- Deterministic alert evaluation state (fire once, re-notify on repeat interval).
CREATE TABLE alert_state (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL UNIQUE CHECK (name ~ '^[a-z_]{2,64}$'),
    status           text NOT NULL CHECK (status IN ('ok','firing')),
    severity         text NOT NULL CHECK (severity IN ('info','warning','critical')),
    since            timestamptz NOT NULL DEFAULT now(),
    last_notified_at timestamptz,
    details          jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Liveness/degradation reported by long-running services (component status).
CREATE TABLE service_heartbeats (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service      text NOT NULL CHECK (service ~ '^[a-z-]{2,32}$'),
    instance     text NOT NULL,
    started_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    status       text NOT NULL CHECK (status IN ('ok','degraded')),
    details      jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_service_heartbeats UNIQUE (service, instance)
);

-- Backup runs recorded by scripts/backup.sh (drives the failed-backup alert).
CREATE TABLE backup_runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at   timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status       text NOT NULL CHECK (status IN ('running','succeeded','failed')),
    artifacts    jsonb NOT NULL DEFAULT '{}'::jsonb,
    error        text
);
CREATE INDEX ix_backup_runs_time ON backup_runs (started_at DESC);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS backup_runs;
DROP TABLE IF EXISTS service_heartbeats;
DROP TABLE IF EXISTS alert_state;
DROP TABLE IF EXISTS ai_job_slots;
DROP TABLE IF EXISTS ai_usage;
DROP TABLE IF EXISTS notification_deliveries;
DROP TABLE IF EXISTS notification_events;
DROP TRIGGER IF EXISTS trg_reports_no_truncate ON reports;
DROP TRIGGER IF EXISTS trg_reports_append_only ON reports;
ALTER TABLE report_jobs DROP CONSTRAINT IF EXISTS fk_report_jobs_report;
DROP INDEX IF EXISTS uq_reports_incident_version;
ALTER TABLE reports DROP CONSTRAINT IF EXISTS ck_reports_versioned;
DELETE FROM reports WHERE job_id IS NOT NULL;
ALTER TABLE reports DROP COLUMN IF EXISTS cost_usd;
ALTER TABLE reports DROP COLUMN IF EXISTS output_tokens;
ALTER TABLE reports DROP COLUMN IF EXISTS input_tokens;
ALTER TABLE reports DROP COLUMN IF EXISTS model_calls;
ALTER TABLE reports DROP COLUMN IF EXISTS fallback_reason;
ALTER TABLE reports DROP COLUMN IF EXISTS record_sha256;
ALTER TABLE reports DROP COLUMN IF EXISTS content;
ALTER TABLE reports DROP COLUMN IF EXISTS auth_mode;
ALTER TABLE reports DROP COLUMN IF EXISTS generation_mode;
ALTER TABLE reports DROP COLUMN IF EXISTS version;
ALTER TABLE reports DROP COLUMN IF EXISTS job_id;
DROP TABLE IF EXISTS report_jobs;
DROP TRIGGER IF EXISTS trg_policy_decisions_no_truncate ON policy_decisions;
DROP TRIGGER IF EXISTS trg_audit_events_no_truncate ON audit_events;
DROP FUNCTION IF EXISTS sentinel_forbid_truncate();
DROP TRIGGER IF EXISTS trg_tasks_guard_status ON tasks;
DROP FUNCTION IF EXISTS sentinel_guard_task_status();
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
