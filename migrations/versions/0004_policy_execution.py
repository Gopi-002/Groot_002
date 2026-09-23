"""Phase 4: policy decisions, bound approvals, operators, action attempt
state, recovery verifications.

Revision ID: 0004_policy_execution
Revises: 0003_ai_investigation
Create Date: 2026-09-22
"""

from alembic import op

revision = "0004_policy_execution"
down_revision = "0003_ai_investigation"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
-- Every deterministic policy evaluation, with its rule ids, reasons and inputs.
CREATE TABLE policy_decisions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id           uuid NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    incident_id       uuid NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
    investigation_id  uuid REFERENCES investigations(id) ON DELETE RESTRICT,
    phase             text NOT NULL CHECK (phase IN ('proposal','pre_execution')),
    proposed_action   text,
    decision          text NOT NULL CHECK (decision IN ('ALLOW','REQUIRE_APPROVAL','DENY')),
    rule_ids          text[] NOT NULL,
    reasons           jsonb NOT NULL DEFAULT '[]'::jsonb,
    inputs            jsonb NOT NULL DEFAULT '{}'::jsonb,
    policy_version    text NOT NULL CHECK (policy_version ~ '^[0-9a-f]{16}$'),
    action_fingerprint text CHECK (action_fingerprint IS NULL
                                   OR action_fingerprint ~ '^[0-9a-f]{64}$'),
    fencing_token     bigint NOT NULL CHECK (fencing_token >= 0),
    evaluated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_policy_decisions_eval UNIQUE (task_id, phase, fencing_token)
);
CREATE INDEX ix_policy_decisions_incident ON policy_decisions (incident_id, evaluated_at);
CREATE TRIGGER trg_policy_decisions_append_only BEFORE UPDATE OR DELETE ON policy_decisions
    FOR EACH ROW EXECUTE FUNCTION sentinel_forbid_mutation();

-- Authenticated operators. Only a SHA-256 of a high-entropy random token is
-- stored; the token is shown once at creation.
CREATE TABLE operators (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9._-]{1,62}$'),
    role          text NOT NULL CHECK (role IN ('viewer','approver')),
    token_sha256  text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    disabled_at   timestamptz
);

-- Approvals are bound to one exact action (id, fingerprint, target, policy
-- version). Decisions are HMAC-signed by the API; the worker verifies them.
ALTER TABLE approvals ADD COLUMN incident_id uuid REFERENCES incidents(id) ON DELETE RESTRICT;
ALTER TABLE approvals ADD COLUMN action_id uuid;
ALTER TABLE approvals ADD COLUMN proposed_action text;
ALTER TABLE approvals ADD COLUMN target_service text;
ALTER TABLE approvals ADD COLUMN action_fingerprint text
    CHECK (action_fingerprint IS NULL OR action_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE approvals ADD COLUMN policy_version text;
ALTER TABLE approvals ADD COLUMN risk text;
ALTER TABLE approvals ADD COLUMN policy_decision_id uuid REFERENCES policy_decisions(id);
ALTER TABLE approvals ADD COLUMN decision_signature text;
CREATE UNIQUE INDEX uq_approvals_one_pending_per_action ON approvals (action_id)
    WHERE status = 'pending';
CREATE INDEX ix_approvals_incident ON approvals (incident_id);

-- Action attempts: pre/post external state for reconciliation, binding to the
-- authorizing decision/approval, and ONE restart per incident as a hard guard.
ALTER TABLE action_attempts ADD COLUMN action_fingerprint text
    CHECK (action_fingerprint IS NULL OR action_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE action_attempts ADD COLUMN policy_decision_id uuid REFERENCES policy_decisions(id);
ALTER TABLE action_attempts ADD COLUMN approval_id uuid REFERENCES approvals(id);
ALTER TABLE action_attempts ADD COLUMN pre_state jsonb;
ALTER TABLE action_attempts ADD COLUMN post_state jsonb;
ALTER TABLE action_attempts ADD COLUMN error text;
CREATE UNIQUE INDEX uq_action_attempts_one_per_incident
    ON action_attempts (incident_id, action_type);

-- Deterministic post-action recovery verification (one per action attempt).
CREATE TABLE verifications (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_attempt_id  uuid NOT NULL UNIQUE REFERENCES action_attempts(id) ON DELETE RESTRICT,
    task_id            uuid NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    incident_id        uuid NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
    status             text NOT NULL CHECK (status IN ('passed','failed')),
    reason             text NOT NULL,
    criteria           jsonb NOT NULL,
    observations       jsonb NOT NULL,
    started_at         timestamptz NOT NULL,
    completed_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_verifications_incident ON verifications (incident_id);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS verifications;
DROP INDEX IF EXISTS uq_action_attempts_one_per_incident;
ALTER TABLE action_attempts DROP COLUMN IF EXISTS error;
ALTER TABLE action_attempts DROP COLUMN IF EXISTS post_state;
ALTER TABLE action_attempts DROP COLUMN IF EXISTS pre_state;
ALTER TABLE action_attempts DROP COLUMN IF EXISTS approval_id;
ALTER TABLE action_attempts DROP COLUMN IF EXISTS policy_decision_id;
ALTER TABLE action_attempts DROP COLUMN IF EXISTS action_fingerprint;
DROP INDEX IF EXISTS ix_approvals_incident;
DROP INDEX IF EXISTS uq_approvals_one_pending_per_action;
ALTER TABLE approvals DROP COLUMN IF EXISTS decision_signature;
ALTER TABLE approvals DROP COLUMN IF EXISTS policy_decision_id;
ALTER TABLE approvals DROP COLUMN IF EXISTS risk;
ALTER TABLE approvals DROP COLUMN IF EXISTS policy_version;
ALTER TABLE approvals DROP COLUMN IF EXISTS action_fingerprint;
ALTER TABLE approvals DROP COLUMN IF EXISTS target_service;
ALTER TABLE approvals DROP COLUMN IF EXISTS proposed_action;
ALTER TABLE approvals DROP COLUMN IF EXISTS action_id;
ALTER TABLE approvals DROP COLUMN IF EXISTS incident_id;
DROP TABLE IF EXISTS operators;
DROP TABLE IF EXISTS policy_decisions;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
