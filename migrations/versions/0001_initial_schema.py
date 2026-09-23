"""Initial SentinelOps schema: 12 core tables.

Conventions: UUID primary keys (gen_random_uuid, built into PG13+), all
timestamps ``timestamptz`` (stored as UTC), status values constrained by CHECK,
indexed status/incident keys, append-only audit log.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-22
"""

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

# Keep in sync with app/persistence/schema.py
INCIDENT_ACTIVE = "('open','investigating','remediating','waiting_approval','escalated')"
TASK_ACTIVE = "('queued','running','retry_scheduled','waiting_approval')"

UPGRADE_SQL = f"""
CREATE OR REPLACE FUNCTION sentinel_set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION sentinel_forbid_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_events is append-only'; END; $$ LANGUAGE plpgsql;

CREATE TABLE services (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{{0,62}}$'),
    base_url     text NOT NULL CHECK (base_url ~ '^https?://'),
    environment  text NOT NULL DEFAULT 'demo' CHECK (environment = 'demo'),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE health_checks (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id  uuid NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    outcome     text NOT NULL CHECK (outcome IN ('healthy','unhealthy','timeout','error')),
    http_status integer CHECK (http_status BETWEEN 100 AND 599),
    latency_ms  double precision CHECK (latency_ms >= 0),
    error_type  text
);
CREATE INDEX ix_health_checks_service_time ON health_checks (service_id, checked_at DESC);

CREATE TABLE incidents (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id       uuid NOT NULL REFERENCES services(id) ON DELETE RESTRICT,
    incident_type    text NOT NULL CHECK (incident_type IN
                       ('unavailable','http_error','high_latency','memory_pressure')),
    status           text NOT NULL DEFAULT 'open' CHECK (status IN
                       ('open','investigating','remediating','waiting_approval','escalated',
                        'resolved','closed')),
    severity         text NOT NULL DEFAULT 'medium'
                       CHECK (severity IN ('low','medium','high','critical')),
    summary          text NOT NULL DEFAULT '',
    occurrence_count integer NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    opened_at        timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_incidents_resolved_at
        CHECK ((status IN ('resolved','closed')) = (resolved_at IS NOT NULL)),
    CONSTRAINT ck_incidents_seen_after_open CHECK (last_seen_at >= opened_at)
);
CREATE INDEX ix_incidents_status ON incidents (status);
CREATE INDEX ix_incidents_service ON incidents (service_id, opened_at DESC);
CREATE UNIQUE INDEX uq_incidents_one_active_per_service_type
    ON incidents (service_id, incident_type) WHERE status IN {INCIDENT_ACTIVE};

CREATE TABLE tasks (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id       uuid NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
    status            text NOT NULL DEFAULT 'queued' CHECK (status IN
                        ('queued','running','retry_scheduled','waiting_approval','escalated',
                         'failed','resolved','dead_lettered')),
    idempotency_key   text NOT NULL UNIQUE,
    attempt           integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts      integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    next_attempt_at   timestamptz,
    lease_owner       text,
    lease_expires_at  timestamptz,
    fencing_token     bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    model_id          text,
    last_error        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_tasks_lease_pair CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    CONSTRAINT ck_tasks_attempt_bound CHECK (attempt <= max_attempts)
);
CREATE INDEX ix_tasks_status ON tasks (status);
CREATE INDEX ix_tasks_incident ON tasks (incident_id);
CREATE INDEX ix_tasks_due ON tasks (next_attempt_at) WHERE status = 'retry_scheduled';
CREATE UNIQUE INDEX uq_tasks_one_active_per_incident
    ON tasks (incident_id) WHERE status IN {TASK_ACTIVE};

CREATE TABLE outbox_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type  text NOT NULL,
    aggregate_id    uuid NOT NULL,
    event_type      text NOT NULL,
    dedup_key       text NOT NULL UNIQUE,
    payload         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz,
    publish_attempts integer NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
    last_error      text
);
CREATE INDEX ix_outbox_unpublished ON outbox_events (created_at) WHERE published_at IS NULL;
CREATE INDEX ix_outbox_aggregate ON outbox_events (aggregate_type, aggregate_id);

CREATE TABLE task_checkpoints (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id       uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step          integer NOT NULL CHECK (step BETWEEN 1 AND 10),
    state         text NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token >= 0),
    data          jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_task_checkpoints_step UNIQUE (task_id, step)
);

CREATE TABLE action_attempts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id          uuid NOT NULL UNIQUE,
    task_id            uuid NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    incident_id        uuid NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
    target_service_id  uuid NOT NULL REFERENCES services(id) ON DELETE RESTRICT,
    action_type        text NOT NULL CHECK (action_type IN ('restart_demo_app')),
    status             text NOT NULL DEFAULT 'pending' CHECK (status IN
                         ('pending','executing','succeeded','failed','unknown','reconciled')),
    fencing_token      bigint NOT NULL CHECK (fencing_token >= 0),
    result             jsonb,
    requested_at       timestamptz NOT NULL DEFAULT now(),
    started_at         timestamptz,
    completed_at       timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_action_attempts_task ON action_attempts (task_id);
CREATE INDEX ix_action_attempts_incident ON action_attempts (incident_id);
CREATE INDEX ix_action_attempts_status ON action_attempts (status);

CREATE TABLE approvals (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id            uuid NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    action_attempt_id  uuid REFERENCES action_attempts(id) ON DELETE RESTRICT,
    status             text NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','approved','rejected','expired')),
    requested_at       timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL,
    decided_at         timestamptz,
    decided_by         text,
    reason             text,
    CONSTRAINT ck_approvals_expiry CHECK (expires_at > requested_at),
    CONSTRAINT ck_approvals_decision
        CHECK ((status IN ('approved','rejected'))
               = (decided_at IS NOT NULL AND decided_by IS NOT NULL))
);
CREATE INDEX ix_approvals_status ON approvals (status, expires_at);
CREATE UNIQUE INDEX uq_approvals_one_pending_per_task ON approvals (task_id)
    WHERE status = 'pending';

CREATE TABLE evidence (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id   uuid NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
    task_id       uuid REFERENCES tasks(id) ON DELETE RESTRICT,
    source        text NOT NULL CHECK (source IN ('health_check','metrics','logs','tool','verification')),
    tool_name     text,
    content       jsonb NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
    collected_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_evidence_incident ON evidence (incident_id, collected_at);
CREATE INDEX ix_evidence_task ON evidence (task_id);

CREATE TABLE reports (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id   uuid NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
    task_id       uuid REFERENCES tasks(id) ON DELETE RESTRICT,
    model_id      text,
    body          text NOT NULL,
    verification  jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_reports_incident ON reports (incident_id);

CREATE TABLE audit_events (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    actor_type   text NOT NULL CHECK (actor_type IN ('system','ai','human')),
    actor_id     text NOT NULL,
    action       text NOT NULL,
    entity_type  text NOT NULL,
    entity_id    uuid,
    details      jsonb NOT NULL DEFAULT '{{}}'::jsonb
);
CREATE INDEX ix_audit_events_entity ON audit_events (entity_type, entity_id);
CREATE INDEX ix_audit_events_time ON audit_events (occurred_at);
CREATE TRIGGER trg_audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION sentinel_forbid_mutation();

-- Stores the selected auth MODE and model ID only. Credentials are never
-- stored in the database (see CLAUDE.md auth contract).
CREATE TABLE model_config (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_mode    text NOT NULL CHECK (auth_mode IN ('api_key','subscription')),
    model_id     text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 200),
    is_active    boolean NOT NULL DEFAULT true,
    selected_by  text NOT NULL,
    selected_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_model_config_single_active ON model_config (is_active) WHERE is_active;

CREATE TRIGGER trg_services_updated_at BEFORE UPDATE ON services
    FOR EACH ROW EXECUTE FUNCTION sentinel_set_updated_at();
CREATE TRIGGER trg_incidents_updated_at BEFORE UPDATE ON incidents
    FOR EACH ROW EXECUTE FUNCTION sentinel_set_updated_at();
CREATE TRIGGER trg_tasks_updated_at BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION sentinel_set_updated_at();
CREATE TRIGGER trg_action_attempts_updated_at BEFORE UPDATE ON action_attempts
    FOR EACH ROW EXECUTE FUNCTION sentinel_set_updated_at();
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS model_config, audit_events, reports, evidence, approvals,
    action_attempts, task_checkpoints, outbox_events, tasks, incidents,
    health_checks, services CASCADE;
DROP FUNCTION IF EXISTS sentinel_forbid_mutation();
DROP FUNCTION IF EXISTS sentinel_set_updated_at();
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
