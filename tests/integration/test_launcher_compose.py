"""The launcher's generated configuration against the REAL docker compose (read-only:
``docker compose config`` renders the file; no container is created or changed)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.launcher import IO, REQUIRED_SECRETS, Launcher, read_env, subprocess_runner

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


def test_generated_env_renders_a_valid_localhost_only_default_safe_stack(tmp_path: Path) -> None:
    for name in ("docker-compose.yml", ".env.example"):
        shutil.copy(ROOT / name, tmp_path / name)
    say: list[str] = []
    lz = Launcher(
        tmp_path,
        subprocess_runner(tmp_path, {"PATH": "/usr/bin:/bin:/usr/local/bin"}),
        IO(say=say.append, ask=lambda _p: "", interactive=False, open_url=lambda _u: False),
    )
    assert lz.ensure_config()
    assert lz.validate_compose(), "\n".join(say)
    out = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    cfg = json.loads(out)
    services = cfg["services"]
    published = [p for s in services.values() for p in s.get("ports", []) if p.get("published")]
    assert published and all(p["host_ip"] == "127.0.0.1" for p in published)
    worker = services["worker"]["environment"]
    assert worker["SENTINEL_REMEDIATION_AUTO_ENABLED"] == "false"
    assert worker["SENTINEL_REMEDIATION_ENVIRONMENT"] == "isolated-demo"
    assert worker["SENTINEL_ENVIRONMENT"] == "development"
    # the Anthropic key is never an environment variable of any service
    assert not any("ANTHROPIC_API_KEY" in (s.get("environment") or {}) for s in services.values())
    env = read_env(tmp_path / ".env")
    assert all(env[k] for k in REQUIRED_SECRETS)
    assert not any(env[k] in "\n".join(say) for k in REQUIRED_SECRETS)
