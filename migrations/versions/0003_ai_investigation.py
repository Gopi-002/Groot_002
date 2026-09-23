"""Phase 3: AI investigation records, awaiting_policy task state, mock auth mode.

Revision ID: 0003_ai_investigation
Revises: 0002_monitoring_queue
Create Date: 2026-09-22
"""

from alembic import op

revision = "0003_ai_investigation"
down_revision = "0002_monitoring_queue"
branch_labels = None
depends_on = None

# Keep in sync with app/persistence/schema.py
TASK_STATUSES = (
    "('queued','running','retry_scheduled','waiting_approval','awaiting_investigation',"
    "'awaiting_policy','escalated','failed','resolved','dead_lettered')"
)
TASK_ACTIVE = (
    "('queued','running','retry_scheduled','waiting_approval','awaiting_investigation',"
    "'awaiting_policy')"
)
OLD_TASK_STATUSES = (
    "('queued','running','retry_scheduled','waiting_approval','awaiting_investigation',"
    "'escalated','failed','resolved','dead_lettered')"
)
OLD_TASK_ACTIVE = (
    "('queued','running','retry_scheduled','waiting_approval','awaiting_investigation')"
)
AUTH_MODES = "('api_key','subscription','mock')"

UPGRADE_SQL = f"""
-- awaiting_policy: investigation complete, resting until the Phase 4 policy stage
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN {TASK_STATUSES});
DROP INDEX uq_tasks_one_active_per_incident;
CREATE UNIQUE INDEX uq_tasks_one_active_per_incident
    ON tasks (incident_id) WHERE status IN {TASK_ACTIVE};
CREATE INDEX ix_tasks_parked_due ON tasks (next_attempt_at)
    WHERE status = 'awaiting_investigation';

-- 'mock' is a TEST/DEMO-ONLY mode (rejected by settings in production);
-- it exists so automated end-to-end runs never masquerade as Claude.
ALTER TABLE model_config DROP CONSTRAINT model_config_auth_mode_check;
ALTER TABLE model_config ADD CONSTRAINT model_config_auth_mode_check
    CHECK (auth_mode IN {AUTH_MODES});

-- One investigation record per task (the latest attempt's outcome). Written in
-- the same fenced transaction as the task transition, so a stale executor
-- cannot persist a result.
CREATE TABLE investigations (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id            uuid NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE RESTRICT,
    incident_id        uuid NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
    model_id           text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 200),
    auth_mode          text NOT NULL CHECK (auth_mode IN {AUTH_MODES}),
    status             text NOT NULL CHECK (status IN
                         ('completed','insufficient_evidence','failed')),
    failure_reason     text,
    result             jsonb,
    rejections         jsonb NOT NULL DEFAULT '[]'::jsonb,
    tool_calls         integer NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),
    model_calls        integer NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
    reasoning_attempts integer NOT NULL DEFAULT 0 CHECK (reasoning_attempts >= 0),
    input_tokens       bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens      bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_usd           numeric(12, 6) CHECK (cost_usd IS NULL OR cost_usd >= 0),
    fencing_token      bigint NOT NULL CHECK (fencing_token >= 0),
    started_at         timestamptz NOT NULL,
    completed_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_investigations_result
        CHECK ((status = 'completed') = (result IS NOT NULL)),
    CONSTRAINT ck_investigations_failure
        CHECK ((status = 'completed') = (failure_reason IS NULL))
);
CREATE INDEX ix_investigations_incident ON investigations (incident_id);
CREATE INDEX ix_investigations_status ON investigations (status);
CREATE INDEX ix_evidence_incident_time ON evidence (incident_id, collected_at DESC);
"""

DOWNGRADE_SQL = f"""
DROP INDEX IF EXISTS ix_evidence_incident_time;
DROP TABLE IF EXISTS investigations;
DELETE FROM model_config WHERE auth_mode = 'mock';
ALTER TABLE model_config DROP CONSTRAINT model_config_auth_mode_check;
ALTER TABLE model_config ADD CONSTRAINT model_config_auth_mode_check
    CHECK (auth_mode IN ('api_key','subscription'));
DROP INDEX IF EXISTS ix_tasks_parked_due;
UPDATE tasks SET status = 'awaiting_investigation' WHERE status = 'awaiting_policy';
DROP INDEX uq_tasks_one_active_per_incident;
CREATE UNIQUE INDEX uq_tasks_one_active_per_incident
    ON tasks (incident_id) WHERE status IN {OLD_TASK_ACTIVE};
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN {OLD_TASK_STATUSES});
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
