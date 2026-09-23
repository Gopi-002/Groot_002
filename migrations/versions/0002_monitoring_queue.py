"""Phase 2: detection state, outbox scheduling, task lifecycle, fencing guard.

Revision ID: 0002_monitoring_queue
Revises: 0001_initial_schema
Create Date: 2026-09-22
"""

from alembic import op

revision = "0002_monitoring_queue"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

# Keep in sync with app/persistence/schema.py
TASK_STATUSES = (
    "('queued','running','retry_scheduled','waiting_approval','awaiting_investigation',"
    "'escalated','failed','resolved','dead_lettered')"
)
TASK_ACTIVE = "('queued','running','retry_scheduled','waiting_approval','awaiting_investigation')"
OLD_TASK_STATUSES = (
    "('queued','running','retry_scheduled','waiting_approval','escalated',"
    "'failed','resolved','dead_lettered')"
)
OLD_TASK_ACTIVE = "('queued','running','retry_scheduled','waiting_approval')"

UPGRADE_SQL = f"""
-- health checks: a 2xx response slower than the latency threshold is 'degraded'
ALTER TABLE health_checks DROP CONSTRAINT health_checks_outcome_check;
ALTER TABLE health_checks ADD CONSTRAINT health_checks_outcome_check
    CHECK (outcome IN ('healthy','degraded','unhealthy','timeout','error'));
CREATE INDEX ix_health_checks_time ON health_checks (checked_at);

-- incidents: threshold evidence window and how the incident ended
ALTER TABLE incidents ADD COLUMN first_failure_at timestamptz;
ALTER TABLE incidents ADD COLUMN last_failure_at timestamptz;
ALTER TABLE incidents ADD COLUMN resolution text
    CHECK (resolution IS NULL OR resolution IN ('auto_recovered','remediated','manual'));

-- tasks: parked state until the next pipeline stage exists; terminal outcome
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN {TASK_STATUSES});
DROP INDEX uq_tasks_one_active_per_incident;
CREATE UNIQUE INDEX uq_tasks_one_active_per_incident
    ON tasks (incident_id) WHERE status IN {TASK_ACTIVE};
ALTER TABLE tasks ADD COLUMN outcome text;
ALTER TABLE tasks ADD COLUMN completed_at timestamptz;
CREATE INDEX ix_tasks_running_lease ON tasks (lease_expires_at) WHERE status = 'running';

-- outbox: publish backoff and the Redis stream id actually written
ALTER TABLE outbox_events ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE outbox_events ADD COLUMN stream_message_id text;
CREATE INDEX ix_outbox_due ON outbox_events (next_attempt_at) WHERE published_at IS NULL;

-- evidence is written idempotently
CREATE UNIQUE INDEX uq_evidence_content ON evidence (incident_id, source, content_sha256);

-- per service + failure type detection state (survives monitor restarts)
CREATE TABLE detection_state (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id         uuid NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    failure_type       text NOT NULL CHECK (failure_type IN
                         ('unavailable','http_error','high_latency','memory_pressure')),
    armed              boolean NOT NULL DEFAULT true,
    consecutive_count  integer NOT NULL DEFAULT 0 CHECK (consecutive_count >= 0),
    healthy_streak     integer NOT NULL DEFAULT 0 CHECK (healthy_streak >= 0),
    streak_check_ids   uuid[] NOT NULL DEFAULT '{{}}',
    first_failure_at   timestamptz,
    last_failure_at    timestamptz,
    last_check_at      timestamptz,
    active_incident_id uuid REFERENCES incidents(id) ON DELETE SET NULL,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_detection_state UNIQUE (service_id, failure_type)
);
CREATE TRIGGER trg_detection_state_updated_at BEFORE UPDATE ON detection_state
    FOR EACH ROW EXECUTE FUNCTION sentinel_set_updated_at();

-- Fencing guard: a checkpoint is only accepted from the current, unexpired
-- lease holder. A reclaimed (stale) executor cannot write progress.
CREATE OR REPLACE FUNCTION sentinel_check_fence() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM tasks
        WHERE id = NEW.task_id AND status = 'running'
          AND fencing_token = NEW.fencing_token AND lease_expires_at > clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'stale fencing token % for task %', NEW.fencing_token, NEW.task_id
            USING ERRCODE = 'SF001';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_task_checkpoints_fence BEFORE INSERT OR UPDATE ON task_checkpoints
    FOR EACH ROW EXECUTE FUNCTION sentinel_check_fence();
"""

DOWNGRADE_SQL = f"""
DROP TRIGGER IF EXISTS trg_task_checkpoints_fence ON task_checkpoints;
DROP FUNCTION IF EXISTS sentinel_check_fence();
DROP TABLE IF EXISTS detection_state;
DROP INDEX IF EXISTS uq_evidence_content;
DROP INDEX IF EXISTS ix_outbox_due;
ALTER TABLE outbox_events DROP COLUMN IF EXISTS stream_message_id;
ALTER TABLE outbox_events DROP COLUMN IF EXISTS next_attempt_at;
DROP INDEX IF EXISTS ix_tasks_running_lease;
ALTER TABLE tasks DROP COLUMN IF EXISTS completed_at;
ALTER TABLE tasks DROP COLUMN IF EXISTS outcome;
UPDATE tasks SET status = 'queued' WHERE status = 'awaiting_investigation';
DROP INDEX uq_tasks_one_active_per_incident;
CREATE UNIQUE INDEX uq_tasks_one_active_per_incident
    ON tasks (incident_id) WHERE status IN {OLD_TASK_ACTIVE};
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN {OLD_TASK_STATUSES});
ALTER TABLE incidents DROP COLUMN IF EXISTS resolution;
ALTER TABLE incidents DROP COLUMN IF EXISTS last_failure_at;
ALTER TABLE incidents DROP COLUMN IF EXISTS first_failure_at;
DROP INDEX IF EXISTS ix_health_checks_time;
UPDATE health_checks SET outcome = 'healthy' WHERE outcome = 'degraded';
ALTER TABLE health_checks DROP CONSTRAINT health_checks_outcome_check;
ALTER TABLE health_checks ADD CONSTRAINT health_checks_outcome_check
    CHECK (outcome IN ('healthy','unhealthy','timeout','error'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
