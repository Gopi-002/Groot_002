"""Static checks of the migration chain (no database needed)."""

import importlib.util
import itertools
import re

from alembic.script import ScriptDirectory

from app.persistence.db import alembic_config, head_revision
from app.persistence.schema import (
    INCIDENT_ACTIVE_STATUSES,
    INCIDENT_TYPES,
    REQUIRED_TABLES,
    TASK_ACTIVE_STATUSES,
    TASK_TERMINAL_STATUSES,
)

PHASE1_TABLES = {
    "services",
    "health_checks",
    "incidents",
    "tasks",
    "outbox_events",
    "task_checkpoints",
    "action_attempts",
    "approvals",
    "evidence",
    "reports",
    "audit_events",
    "model_config",
}


def _chain():
    """Migration modules from base to head."""
    script = ScriptDirectory.from_config(alembic_config())
    mods = []
    for rev in reversed(list(script.walk_revisions())):
        spec = importlib.util.spec_from_file_location(rev.revision, rev.path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods.append(mod)
    return mods


def test_single_linear_head():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert head_revision() == script.get_current_head()
    mods = _chain()
    assert mods[0].down_revision is None
    for prev, cur in itertools.pairwise(mods):
        assert cur.down_revision == prev.revision


def test_initial_migration_creates_phase1_tables():
    assert set(re.findall(r"CREATE TABLE (\w+)", _chain()[0].UPGRADE_SQL)) == PHASE1_TABLES


def test_all_required_tables_created_and_dropped():
    mods = _chain()
    created = set().union(*(re.findall(r"CREATE TABLE (\w+)", m.UPGRADE_SQL) for m in mods))
    assert created == set(REQUIRED_TABLES)
    downs = " ".join(m.DOWNGRADE_SQL for m in mods)
    for t in REQUIRED_TABLES:
        assert t in downs


def test_uuid_ids_and_utc_timestamps():
    for mod in _chain():
        sql = mod.UPGRADE_SQL
        tables = re.findall(r"CREATE TABLE (\w+) \(\s*\n\s*id\s+(\w+) PRIMARY KEY", sql)
        assert all(kind == "uuid" for _, kind in tables)
        assert not re.search(r"\btimestamp\b(?! ?tz)", sql.replace("timestamptz", ""))


def test_partial_unique_indexes_match_constants():
    mods = _chain()
    assert "uq_incidents_one_active_per_service_type" in mods[0].UPGRADE_SQL
    assert set(re.findall(r"'(\w+)'", mods[0].INCIDENT_ACTIVE)) == INCIDENT_ACTIVE_STATUSES
    latest = next(m for m in reversed(mods) if hasattr(m, "TASK_ACTIVE"))
    assert set(re.findall(r"'(\w+)'", latest.TASK_ACTIVE)) == TASK_ACTIVE_STATUSES
    all_statuses = set(re.findall(r"'(\w+)'", latest.TASK_STATUSES))
    assert all_statuses == TASK_ACTIVE_STATUSES | TASK_TERMINAL_STATUSES


def test_incident_types_constant_matches_schema():
    sql = _chain()[0].UPGRADE_SQL
    block = sql.split("incident_type    text NOT NULL CHECK (incident_type IN", 1)[1]
    assert tuple(re.findall(r"'(\w+)'", block.split(")", 1)[0])) == INCIDENT_TYPES


def test_model_config_has_no_credential_columns():
    for mod in _chain():
        if "CREATE TABLE model_config" not in mod.UPGRADE_SQL:
            continue
        block = mod.UPGRADE_SQL.split("CREATE TABLE model_config", 1)[1].split(");", 1)[0]
        assert not re.search(
            r"(key|token|secret|password|credential)\w*\s+(text|bytea)", block, re.I
        )
    # later migrations may change constraints but must never add columns
    later = " ".join(m.UPGRADE_SQL for m in _chain()[1:])
    assert not re.search(r"ALTER TABLE model_config\s+ADD\s+COLUMN", later, re.I)
    assert not re.search(r"ALTER TABLE investigations\s+ADD\s+COLUMN", later, re.I)


def test_fencing_trigger_present():
    sql = " ".join(m.UPGRADE_SQL for m in _chain())
    assert "trg_task_checkpoints_fence" in sql and "fencing_token = NEW.fencing_token" in sql


def test_investigations_table_stores_no_credentials():
    sql = " ".join(m.UPGRADE_SQL for m in _chain())
    block = sql.split("CREATE TABLE investigations", 1)[1].split(");", 1)[0]
    columns = re.findall(
        r"^\s{4}(\w+)\s+(?:uuid|text|jsonb|integer|bigint|numeric|timestamptz)", block, re.M
    )
    assert "model_id" in columns and "auth_mode" in columns
    for col in columns:
        assert not re.search(r"(key|token|secret|password|credential)", col, re.I) or col in {
            "input_tokens",
            "output_tokens",
            "fencing_token",
        }, col
