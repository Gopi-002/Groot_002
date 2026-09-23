"""Integration fixtures: each test module gets a fresh, throwaway database.

Requires TEST_DATABASE_URL pointing at a PostgreSQL server where the user can
CREATE DATABASE (e.g. the compose stack with docker-compose.test-ports.yml).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.persistence.db import alembic_config

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")


def pytest_collection_modifyitems(items):
    if ADMIN_URL is None:
        skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
        for item in items:
            if "integration" in str(item.fspath):
                item.add_marker(skip)


@pytest.fixture(scope="module")
def db_url() -> Iterator[str]:
    assert ADMIN_URL is not None
    name = f"sentinel_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def upgrade(url: str, rev: str = "head") -> None:
    command.upgrade(alembic_config(url), rev)


def downgrade(url: str, rev: str = "base") -> None:
    command.downgrade(alembic_config(url), rev)


@pytest.fixture(scope="module")
def engine(db_url: str) -> Iterator[Engine]:
    upgrade(db_url)
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


# --- Phase 2 fixtures --------------------------------------------------------

REDIS_PASSWORD = os.environ.get("TEST_REDIS_PASSWORD")
REDIS_PORT = int(os.environ.get("TEST_REDIS_PORT", "56379"))


@pytest.fixture
def rclient():
    import redis

    if REDIS_PASSWORD is None:
        pytest.skip("TEST_REDIS_PASSWORD not set")
    client = redis.Redis(
        host="127.0.0.1",
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
        socket_timeout=10,
    )
    yield client
    client.close()


@pytest.fixture
def settings(base_env, rclient, monkeypatch):
    """Fast timings and an isolated stream namespace per test."""
    from app.config import Settings

    prefix = f"test-{uuid.uuid4().hex[:10]}"
    s = Settings(
        stream_prefix=prefix,
        lease_ttl_seconds=2,
        heartbeat_seconds=1,
        pending_idle_seconds=2.5,
        task_max_attempts=3,
        task_retry_base_seconds=1,
        task_retry_max_seconds=4,
        worker_block_seconds=0.2,
        redispatch_after_seconds=5,
        publish_retry_base_seconds=1,
        publish_retry_max_seconds=2,
    )
    yield s
    for key in rclient.scan_iter(f"{prefix}:*"):
        rclient.delete(key)
