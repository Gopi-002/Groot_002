"""Readiness against a real PostgreSQL (and Redis when TEST_REDIS_* is set)."""

import os

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.health import ReadinessChecks
from app.api.main import create_app
from app.config import Settings
from app.persistence.db import current_revision, head_revision
from tests.integration.conftest import downgrade, upgrade

pytestmark = pytest.mark.integration


def _checks(db_url: str) -> ReadinessChecks:
    eng = create_engine(db_url)

    def database() -> None:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))

    def schema() -> None:
        if current_revision(eng) != head_revision():
            raise RuntimeError("behind")

    checks = {"database": database, "schema": schema}
    if os.environ.get("TEST_REDIS_PASSWORD"):
        r = redis.Redis(
            host="127.0.0.1",
            port=int(os.environ.get("TEST_REDIS_PORT", "56379")),
            password=os.environ["TEST_REDIS_PASSWORD"],
            socket_timeout=2,
        )
        checks["redis"] = lambda: None if r.ping() else (_ for _ in ()).throw(RuntimeError())
    return ReadinessChecks(checks)


def test_ready_only_when_schema_at_head(base_env, db_url):
    client = TestClient(create_app(Settings(), _checks(db_url)))
    upgrade(db_url)
    assert client.get("/health/ready").status_code == 200
    downgrade(db_url)
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["schema"] == "fail"
    assert client.get("/health/live").status_code == 200
    upgrade(db_url)
