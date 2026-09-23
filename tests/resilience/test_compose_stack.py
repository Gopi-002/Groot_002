"""Drives the real docker compose stack. Opt-in: RUN_RESILIENCE=1.

Precondition: `docker compose up -d --build` is running with a populated .env.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest

from app.persistence.db import head_revision
from app.persistence.schema import REQUIRED_TABLES
from tests.resilience.helpers import API, DEMO, dc, env_file, http, psql, wait_ready

pytestmark = [
    pytest.mark.resilience,
    pytest.mark.skipif(os.environ.get("RUN_RESILIENCE") != "1", reason="RUN_RESILIENCE!=1"),
]

SECRET_KEYS = {
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "DEMO_INJECTION_TOKEN",
    "SENTINEL_API_READ_TOKEN",
}


def test_stack_ready_and_liveness_distinct():
    wait_ready()
    assert http("GET", f"{API}/health/live") == (200, {"status": "alive"})
    code, body = http("GET", f"{API}/health/ready")
    assert code == 200 and body["checks"] == {"database": "ok", "schema": "ok", "redis": "ok"}


def test_migrations_rerun_safely():
    dc("run", "--rm", "migrate")
    dc("run", "--rm", "migrate")
    expected = len(REQUIRED_TABLES) + 1  # + alembic_version
    assert psql("SELECT count(*) FROM pg_tables WHERE schemaname='public'") == str(expected)
    assert psql("SELECT version_num FROM alembic_version") == head_revision()


def test_readiness_fails_when_db_down_but_liveness_holds():
    wait_ready()
    dc("stop", "postgres")
    try:
        deadline = time.time() + 30
        code = 200
        while time.time() < deadline and code == 200:
            code, body = http("GET", f"{API}/health/ready", timeout=10)
            time.sleep(1)
        assert code == 503 and body["checks"]["database"] in {"fail", "timeout"}
        assert http("GET", f"{API}/health/live")[0] == 200
    finally:
        dc("start", "postgres")
    wait_ready()


def test_restart_preserves_db_data():
    marker = f"persist-{uuid.uuid4().hex[:8]}"
    psql(f"INSERT INTO services (name, base_url) VALUES ('{marker}', 'http://demo-app:8001')")
    dc("restart", "postgres")
    wait_ready()
    assert psql(f"SELECT name FROM services WHERE name='{marker}'") == marker
    dc("down")  # containers removed, named volumes kept
    dc("up", "-d", "--wait")
    wait_ready()
    assert psql(f"SELECT name FROM services WHERE name='{marker}'") == marker


def test_demo_failure_is_isolated_from_agent_stack():
    wait_ready()
    token = {"X-Demo-Token": env_file()["DEMO_INJECTION_TOKEN"]}
    assert http("POST", f"{DEMO}/simulate-failure", {"mode": "http_500"})[0] == 401
    try:
        assert http("POST", f"{DEMO}/simulate-failure", {"mode": "http_500"}, token)[0] == 200
        assert http("GET", f"{DEMO}/health")[0] == 500
        assert http("GET", f"{API}/health/ready")[0] == 200  # agent stack unaffected
    finally:
        http("POST", f"{DEMO}/simulate-failure", {"mode": "none"}, token)
    assert http("GET", f"{DEMO}/health")[0] == 200
    # demo container has no DB/Redis network path or credentials
    probe = (
        "import socket,sys\n"
        "try:\n socket.create_connection(('postgres',5432),2); sys.exit(1)\n"
        "except OSError: sys.exit(0)"
    )
    dc("exec", "-T", "demo-app", "python", "-c", probe)
    env = dc("exec", "-T", "demo-app", "env")
    assert "SENTINEL_DB_PASSWORD" not in env and "REDIS_PASSWORD" not in env


def test_no_secrets_in_logs():
    secrets = [v for k, v in env_file().items() if k in SECRET_KEYS and v]
    assert len(secrets) == 4
    logs = dc(
        "logs",
        "--no-color",
        "api",
        "migrate",
        "demo-app",
        "postgres",
        "redis",
        "monitor",
        "dispatcher",
        "worker",
    )
    for s in secrets:
        assert s not in logs


def test_datastores_not_published_by_default():
    ports = dc("-f", "docker-compose.yml", "config", "--format", "json")
    cfg = json.loads(ports)
    assert "ports" not in cfg["services"]["postgres"]
    assert "ports" not in cfg["services"]["redis"]
    for svc in ("api", "demo-app"):
        for p in cfg["services"][svc]["ports"]:
            assert p["host_ip"] == "127.0.0.1"
    # Only the restricted ops-reader (read-only) and executor (restart-only) hold
    # the Docker socket; the AI worker and every other service never do.
    socket_holders = {
        name
        for name, svc in cfg["services"].items()
        if any("docker.sock" in str(v.get("source", "")) for v in svc.get("volumes", []))
    }
    # Phase 4 adds exactly one more: the restart-only executor. The AI worker never.
    assert socket_holders == {"ops-reader", "executor"}
    assert "ai_egress" not in cfg["services"]["ops-reader"]["networks"]
    assert set(cfg["services"]["executor"]["networks"]) == {"exec_net"}
    assert cfg["networks"]["exec_net"].get("internal") is True
    assert set(cfg["services"]["worker"]["networks"]) == {
        "backend",
        "ops_net",
        "exec_net",
        "ai_egress",
    }
    # only monitor, API and ops-reader share demo_net; worker/dispatcher cannot reach it
    for svc in ("worker", "dispatcher"):
        assert "demo_net" not in cfg["services"][svc]["networks"]
    assert set(cfg["services"]["monitor"]["networks"]) == {"backend", "demo_net"}
    assert set(cfg["services"]["demo-app"]["networks"]) == {"demo_net"}
