import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.persistence.db import current_revision, head_revision
from app.persistence.schema import REQUIRED_TABLES
from tests.integration.conftest import downgrade, upgrade

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _tables(eng) -> set[str]:
    with eng.connect() as c:
        return set(
            c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars()
        )


def test_upgrade_twice_is_safe(db_url):
    eng = create_engine(db_url)
    upgrade(db_url)
    first = _tables(eng)
    upgrade(db_url)  # second run must be a no-op
    assert _tables(eng) == first
    assert set(REQUIRED_TABLES) | {"alembic_version"} <= first
    assert current_revision(eng) == head_revision()
    eng.dispose()


def test_downgrade_then_upgrade_roundtrip(db_url):
    eng = create_engine(db_url)
    upgrade(db_url)
    downgrade(db_url)
    assert _tables(eng) & set(REQUIRED_TABLES) == set()
    upgrade(db_url)
    assert set(REQUIRED_TABLES) <= _tables(eng)
    eng.dispose()


def test_concurrent_migrators_serialize(db_url):
    """Separate processes (like two containers) racing to migrate must both succeed."""
    downgrade(db_url)
    code = "import sys; from tests.integration.conftest import upgrade; upgrade(sys.argv[1])"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, db_url],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    results = [(p.wait(timeout=120), p.stderr.read() if p.stderr else b"") for p in procs]
    assert [rc for rc, _ in results] == [0, 0, 0], results
    eng = create_engine(db_url)
    assert current_revision(eng) == head_revision()
    assert set(REQUIRED_TABLES) <= _tables(eng)
    eng.dispose()


def test_timestamps_stored_as_utc(engine):
    with engine.connect() as c:
        assert c.execute(text("SHOW timezone")).scalar() in {"UTC", "Etc/UTC"}
        bad = c.execute(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                "AND data_type = 'timestamp without time zone'"
            )
        ).scalar()
        assert bad == 0
        non_uuid_pk = c.execute(
            text("""
            SELECT count(*) FROM information_schema.columns col
            JOIN information_schema.key_column_usage k
              ON k.table_name = col.table_name AND k.column_name = col.column_name
            JOIN information_schema.table_constraints tc
              ON tc.constraint_name = k.constraint_name AND tc.constraint_type = 'PRIMARY KEY'
            WHERE col.table_schema='public' AND col.table_name <> 'alembic_version'
              AND col.data_type <> 'uuid'""")
        ).scalar()
        assert non_uuid_pk == 0
