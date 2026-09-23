"""Shared helpers for resilience tests that drive the live compose stack."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_ = os  # COMPOSE_FILE is inherited from the environment (see scripts/test.sh)
API = "http://127.0.0.1:8000"
DEMO = "http://127.0.0.1:8001"


def dc(*args: str, check: bool = True) -> str:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        timeout=180,
    ).stdout


def env_file() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def http(
    method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: float = 5
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_ready(timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if http("GET", f"{API}/health/ready")[0] == 200:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    raise AssertionError("API never became ready")


def psql(sql: str) -> str:
    return dc(
        "exec", "-T", "postgres", "psql", "-U", "sentinelops", "-d", "sentinelops", "-tA", "-c", sql
    ).strip()
