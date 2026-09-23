"""The ``sentinelops`` launcher against a simulated Docker/Compose (no containers)."""

from __future__ import annotations

import json
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app import launcher as L
from app.launcher import IO, AIState, Launcher, Result, read_env, sanitize

ROOT = Path(__file__).resolve().parents[2]
FAKE_KEY = "sk-ant-api03-LAUNCHER-TEST-0123456789abcdefghij"


class FakeDocker:
    """Just enough docker / docker compose behaviour to drive the launcher."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.docker_cli = True
        self.daemon = True
        self.image = False
        self.up_fails_for: set[str] = set()
        self.running = False
        self.selection: tuple[str, str] | None = None  # (model, auth_mode)
        self.key_stored = False
        self.user_enters_valid_key = True
        self.leaky_log = ""

    def gateway(self) -> str:
        return read_env(self.root / ".env").get("SENTINEL_AI_GATEWAY", "anthropic")

    def __call__(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        interactive: bool = False,
        timeout: float | None = None,
    ) -> Result:
        a = list(args)
        self.calls.append((a, {"input": input, "interactive": interactive}))
        if not self.docker_cli:
            return Result(127, "", "docker: not found")
        if a[:2] == ["docker", "--version"]:
            return Result(0, "Docker version 27.0.0\n")
        if a[:2] == ["docker", "info"]:
            return Result(0, "27.0.0\n") if self.daemon else Result(1, "", "Cannot connect")
        if a[:3] == ["docker", "compose", "version"]:
            return Result(0, "2.29.0\n")
        if a[:3] == ["docker", "image", "inspect"]:
            return Result(0 if self.image else 1)
        assert a[:4] == ["docker", "compose", "-f", "docker-compose.yml"], a
        c = a[4:]
        if c[:1] == ["config"]:
            return Result(0)
        if c[:1] == ["build"]:
            self.image = True
            return Result(0)
        if c[:3] == ["up", "-d", "--wait"] and c[3:] == ["postgres", "redis"]:
            return Result(0)
        if c[:3] == ["up", "-d", "--wait"]:
            self.running = True
            return Result(1, "", "dependency failed to start") if self.up_fails_for else Result(0)
        if c[:4] == ["run", "--rm", "-T", "migrate"]:
            return Result(0, "INFO  [alembic] upgrade head\n")
        if c[:5] == ["run", "--rm", "-T", "onboard", "status"]:
            lines = [f"AI gateway: {self.gateway()}"]
            lines.append(
                f"Selected: {self.selection[0]} ({self.selection[1]})"
                if self.selection
                else "Selected: none"
            )
            if self.gateway() != "mock":
                lines.append(
                    "API key: configured (sha256:abcdef012345)"
                    if self.key_stored
                    else "API key: no API key configured"
                )
            return Result(0, "\n".join(lines) + "\n")
        if c[:4] == ["run", "--rm", "-T", "onboard"] and input is not None:
            assert self.gateway() == "mock"
            self.selection = ("mock-investigator-v1", "mock")
            return Result(0, "!! MOCK AI GATEWAY ACTIVE !!\nSelected model: mock-investigator-v1\n")
        if c[:3] == ["run", "--rm", "onboard"]:
            assert interactive, "key/model onboarding must own the terminal"
            if c[3:] == ["change-model"]:
                self.selection = ("claude-model-b", "api_key")
                return Result(0)
            if self.user_enters_valid_key:
                self.key_stored = True
                self.selection = ("claude-model-a", "api_key")
                return Result(0)
            return Result(1)
        if c[:1] == ["ps"]:
            rows = []
            for svc in L.SERVICES:
                if not self.running:
                    continue
                bad = svc in self.up_fails_for
                rows.append(
                    {
                        "Service": svc,
                        "State": "running",
                        "Health": "unhealthy" if bad else "healthy",
                        "Status": "Up",
                    }
                )
            return Result(0, "\n".join(json.dumps(r) for r in rows))
        if c[:1] == ["logs"]:
            return Result(0, f"worker boot failed: {self.leaky_log}\n")
        if c[:1] == ["port"]:
            return Result(0, "127.0.0.1:8000\n")
        if c[:1] == ["stop"]:
            self.running = False
            return Result(0)
        raise AssertionError(f"unexpected call {a}")

    def compose_calls(self) -> list[list[str]]:
        return [a[4:] for a, _ in self.calls if a[:2] == ["docker", "compose"] and len(a) > 4]


class ScriptedIO(IO):
    def __init__(self, answers: list[str] | None = None, interactive: bool = True) -> None:
        self.out: list[str] = []
        self.asked: list[str] = []
        self.answers = list(answers or [])
        self.opened: list[str] = []
        super().__init__(
            say=self.out.append,
            ask=self._ask,
            interactive=interactive,
            open_url=lambda u: self.opened.append(u) or True,
        )

    def _ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    @property
    def text(self) -> str:
        return "\n".join(self.out)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text())
    (tmp_path / "docker-compose.yml").write_text("name: sentinelops\n")
    return tmp_path


def make(
    repo: Path, io: ScriptedIO, docker: FakeDocker | None = None
) -> tuple[Launcher, FakeDocker]:
    d = docker or FakeDocker(repo)
    lz = Launcher(repo, d, io)

    def fake_status() -> dict[str, Any] | None:
        if not d.running:
            return None
        sel = f"{d.selection[1]}:{d.selection[0]}" if d.selection else None
        return {
            "overall": "healthy",
            "components": {"ai": {"gateway": d.gateway(), "selected": sel, "status": "ok"}},
        }

    lz.system_status = fake_status  # type: ignore[method-assign]
    return lz, d


# --------------------------------------------------------------------------- fresh install
def test_fresh_install_generates_config_starts_and_skips_ai_to_mock(repo: Path) -> None:
    # answers: autonomy? (default No) / AI choice (default 2 = mock) / open browser? no
    io = ScriptedIO(["", "", "n"])
    lz, d = make(repo, io)
    assert lz.default() == 0
    env = read_env(repo / ".env")
    assert all(len(env[k]) >= 32 for k in L.REQUIRED_SECRETS)
    assert len({env[k] for k in L.REQUIRED_SECRETS}) == len(L.REQUIRED_SECRETS)  # all distinct
    assert env["DOCKER_GID"] and env["SENTINEL_ENVIRONMENT"] == "development"
    assert env["SENTINEL_REMEDIATION_ENVIRONMENT"] == "isolated-demo"
    assert env["SENTINEL_REMEDIATION_AUTO_ENABLED"] == "false"  # no silent autonomy
    assert env["SENTINEL_AI_GATEWAY"] == "mock"
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o600
    calls = d.compose_calls()
    order = [
        calls.index(c)
        for c in (
            ["config", "--quiet"],
            ["build"],
            ["up", "-d", "--wait", "postgres", "redis"],
            ["run", "--rm", "-T", "migrate"],
        )
    ]
    assert order == sorted(order)  # validate -> build -> infra -> migrations
    assert "AI mode: MOCK / DEMO" in io.text and "NOT Claude" in io.text
    assert "http://127.0.0.1:8000/dashboard/" in io.text
    assert "human approval required" in io.text


def test_autonomy_only_with_explicit_yes(repo: Path) -> None:
    io = ScriptedIO(["y", "", "n"])
    lz, _ = make(repo, io)
    assert lz.default() == 0
    assert read_env(repo / ".env")["SENTINEL_REMEDIATION_AUTO_ENABLED"] == "true"
    assert "autonomous demo restarts ENABLED (you confirmed)" in io.text


# --------------------------------------------------------------------------- prerequisites
def test_missing_docker_is_actionable_and_changes_nothing(repo: Path) -> None:
    d = FakeDocker(repo)
    d.docker_cli = False
    io = ScriptedIO()
    lz, _ = make(repo, io, d)
    assert lz.default() == 1
    assert "install Docker: https://docs.docker.com/get-docker/" in io.text
    assert not (repo / ".env").exists()


def test_docker_daemon_down_is_actionable(repo: Path) -> None:
    d = FakeDocker(repo)
    d.daemon = False
    io = ScriptedIO()
    lz, _ = make(repo, io, d)
    assert lz.setup(first_run=True) == 1
    assert "start Docker Desktop" in io.text


def test_start_without_configuration_points_to_setup(repo: Path) -> None:
    io = ScriptedIO()
    lz, d = make(repo, io)
    assert lz.start() == 1
    assert "run `sentinelops setup` first" in io.text
    assert not any(c[:1] == ["up"] for c in d.compose_calls())


def test_existing_config_is_never_overwritten_only_missing_values_filled(repo: Path) -> None:
    (repo / ".env").write_text("POSTGRES_PASSWORD=keep-this-password-value\nREDIS_PASSWORD=\n")
    io = ScriptedIO(["y"])
    lz, _ = make(repo, io)
    assert lz.ensure_config()
    env = read_env(repo / ".env")
    assert env["POSTGRES_PASSWORD"] == "keep-this-password-value"
    assert len(env["REDIS_PASSWORD"]) >= 32 and env["DOCKER_GID"]


def test_declining_fill_leaves_config_untouched(repo: Path) -> None:
    (repo / ".env").write_text("POSTGRES_PASSWORD=\n")
    io = ScriptedIO(["n"])
    lz, _ = make(repo, io)
    assert not lz.ensure_config()
    assert (repo / ".env").read_text() == "POSTGRES_PASSWORD=\n"


# --------------------------------------------------------------------------- AI configuration
def test_api_key_is_entered_in_the_existing_onboard_tool_never_in_the_launcher(repo: Path) -> None:
    io = ScriptedIO(["", "1", "n"])  # autonomy no / API key / don't open browser
    lz, d = make(repo, io)
    assert lz.default() == 0
    onboard = [(a, kw) for a, kw in d.calls if a[4:7] == ["run", "--rm", "onboard"]]
    assert len(onboard) == 1
    args, kw = onboard[0]
    assert kw["interactive"] and kw["input"] is None and "-T" not in args  # terminal handed over
    assert not any("key" in p.lower() for p in io.asked)  # launcher never prompts for the key
    assert read_env(repo / ".env")["SENTINEL_AI_GATEWAY"] == "anthropic"
    assert "Claude API configured: model claude-model-a" in io.text
    assert "AI mode: Claude API" in io.text and "MOCK" not in io.text.split("All 11")[1]
    assert "does NOT give this app API access" in io.text  # subscription note shown


def test_failed_key_setup_offers_mock_mode(repo: Path) -> None:
    d = FakeDocker(repo)
    d.user_enters_valid_key = False
    io = ScriptedIO(["", "1", "y", "n"])
    lz, _ = make(repo, io, d)
    assert lz.default() == 0
    assert (
        "was not configured" in io.text and read_env(repo / ".env")["SENTINEL_AI_GATEWAY"] == "mock"
    )


def test_api_key_setup_refuses_without_a_terminal(repo: Path) -> None:
    io = ScriptedIO(["1"], interactive=False)
    lz, d = make(repo, io)
    assert lz.default() == 0  # non-interactive: explicit mock, never a hidden key prompt
    assert not any(kw["interactive"] for a, kw in d.calls if "onboard" in a)  # no key prompt
    assert read_env(repo / ".env")["SENTINEL_AI_GATEWAY"] == "mock"


def test_models_command_uses_existing_change_model_flow(repo: Path) -> None:
    io = ScriptedIO(["", "1", "n"])
    lz, d = make(repo, io)
    lz.default()
    assert lz.models() == 0
    assert d.selection == ("claude-model-b", "api_key")
    calls = d.compose_calls()
    assert ["run", "--rm", "onboard", "change-model"] in calls
    assert calls[-1] == [
        "up",
        "-d",
        "--wait",
        "worker",
    ]  # worker picks up nothing secret, just restarts


def test_ai_state_configured_rules() -> None:
    assert AIState("mock", "mock-investigator-v1", "mock", None).configured
    assert not AIState("anthropic", "m", "api_key", "no API key configured").configured
    assert not AIState("anthropic", "mock-investigator-v1", "mock", "configured (x)").configured
    assert AIState("anthropic", "m", "api_key", "configured (sha256:x)").configured


# --------------------------------------------------------------------------- repeated launches
def test_second_launch_reuses_everything_without_onboarding(repo: Path) -> None:
    io = ScriptedIO(["", "", "n"])
    lz, d = make(repo, io)
    assert lz.default() == 0
    before = (repo / ".env").read_text()
    n_calls = len(d.compose_calls())
    io2 = ScriptedIO(["n"])
    lz2, _ = make(repo, io2, d)
    assert lz2.default() == 0
    later = d.compose_calls()[n_calls:]
    assert (repo / ".env").read_text() == before  # no regenerated secrets
    assert "Reusing saved configuration: AI mock / mock-investigator-v1" in io2.text
    assert not any(c[:3] == ["run", "--rm", "onboard"] for c in later)
    assert not any(c[:4] == ["run", "--rm", "-T", "onboard"] and len(c) == 4 for c in later)
    assert ["build"] not in later  # image reused
    assert sum(c[:3] == ["up", "-d", "--wait"] for c in later) == 1  # idempotent compose up


def test_status_makes_no_ai_or_onboarding_call(repo: Path) -> None:
    io = ScriptedIO(["", "", "n"])
    lz, d = make(repo, io)
    lz.default()
    n = len(d.calls)
    io2 = ScriptedIO()
    lz2, _ = make(repo, io2, d)
    assert lz2.status() == 0
    assert not any("onboard" in a for a, _ in d.calls[n:])
    assert "All services healthy." in io2.text


# --------------------------------------------------------------------------- failures + health
def test_partial_startup_failure_names_service_with_sanitized_logs(repo: Path) -> None:
    io = ScriptedIO(["", "", "n"])
    lz, d = make(repo, io)
    lz.ensure_config()
    secret = read_env(repo / ".env")["SENTINEL_EXECUTOR_TOKEN"]
    d.image, d.up_fails_for = True, {"worker"}
    d.leaky_log = f"token={secret} key={FAKE_KEY}"
    assert lz.start() == 1
    assert "worker: running unhealthy" in io.text
    assert "worker boot failed" in io.text
    assert secret not in io.text and FAKE_KEY not in io.text and "***" in io.text


def test_health_classification() -> None:
    lz = Launcher(Path("."), FakeDocker(Path(".")), ScriptedIO())
    states = {s: {"state": "running", "health": "healthy", "status": ""} for s in L.SERVICES}
    assert lz.unhealthy(states) == []
    states["executor"]["health"] = "starting"
    del states["redis"]
    states["api"]["state"] = "exited"
    assert lz.unhealthy(states) == ["redis", "api", "executor"]


# --------------------------------------------------------------------------- secrets + modes
def test_no_secret_leaks_in_any_output(repo: Path) -> None:
    io = ScriptedIO(["", "1", "n"])
    lz, _ = make(repo, io)
    lz.default()
    lz.status()
    lz.doctor()
    env = read_env(repo / ".env")
    for k in L.REQUIRED_SECRETS:
        assert env[k] not in io.text, k
    assert "SENTINEL_API_READ_TOKEN value in" in io.text  # tells WHERE, never the value
    assert FAKE_KEY not in (repo / ".env").read_text()


def test_sanitize_masks_env_secrets_and_key_patterns() -> None:
    env = {"REDIS_PASSWORD": "supersecretredisvalue"}
    s = sanitize(f"x supersecretredisvalue {FAKE_KEY} Bearer abc.def sop_{'a' * 20}", env)
    assert "supersecret" not in s and "sk-ant" not in s and "abc.def" not in s and "sop_" not in s


def test_mock_and_real_status_are_distinct(repo: Path) -> None:
    io = ScriptedIO(["", "", "n"])
    lz, d = make(repo, io)
    lz.default()
    io_m = ScriptedIO()
    make(repo, io_m, d)[0].show_agent()
    assert "MOCK / DEMO" in io_m.text and "NOT Claude" in io_m.text
    L.write_env_values(repo / ".env", {"SENTINEL_AI_GATEWAY": "anthropic"})
    d.selection = ("claude-model-a", "api_key")
    io_r = ScriptedIO()
    make(repo, io_r, d)[0].show_agent()
    assert "AI mode: Claude API" in io_r.text and "MOCK" not in io_r.text


def test_dashboard_is_loopback_and_browser_only_on_consent(repo: Path) -> None:
    io = ScriptedIO(["", "", "y"])
    lz, _ = make(repo, io)
    lz.default()
    assert io.opened == ["http://127.0.0.1:8000/dashboard/"]


def test_compose_file_env_override_is_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher manages the standard stack only (never the test-ports override)."""
    captured: dict[str, Any] = {}
    monkeypatch.setenv("COMPOSE_FILE", "docker-compose.yml:docker-compose.test-ports.yml")
    monkeypatch.setattr(
        L, "subprocess_runner", lambda root, env: captured.update(env=env) or FakeDocker(root)
    )
    monkeypatch.setattr(L.Launcher, "doctor", lambda self: 0)
    assert L.main(["doctor"]) == 0
    assert "COMPOSE_FILE" not in captured["env"]


def test_gateway_selection_mismatch_is_not_shown_as_claude(repo: Path) -> None:
    io = ScriptedIO(["", "", "n"])
    lz, d = make(repo, io)
    lz.default()  # mock selected
    L.write_env_values(repo / ".env", {"SENTINEL_AI_GATEWAY": "anthropic"})
    io2 = ScriptedIO()
    make(repo, io2, d)[0].show_agent()
    assert "no Claude model is selected (mock:mock-investigator-v1)" in io2.text
    assert "AI mode: Claude API (Anthropic API key)" not in io2.text


def test_mock_mode_label_does_not_trust_the_api_gateway_field(repo: Path) -> None:
    """Regression: /v1/system/status reported gateway 'anthropic' in mock mode."""
    io = ScriptedIO(["", "", "n"])
    lz, _ = make(repo, io)
    lz.default()
    lz.system_status = lambda: {  # type: ignore[method-assign]
        "overall": "healthy",
        "components": {"ai": {"gateway": "anthropic", "selected": "mock:mock-investigator-v1"}},
    }
    io.out.clear()
    lz.show_agent()
    assert io.text.startswith("AI mode: MOCK / DEMO") and "Claude API" not in io.text
