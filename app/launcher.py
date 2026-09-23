"""``sentinelops``: one-command launcher for the LOCAL SentinelOps stack.

    sentinelops            first run: guided setup, then start; later runs: start + status
    sentinelops setup      (re)run guided setup (never overwrites secrets without asking)
    sentinelops start      start / repair the stack and wait until it is healthy
    sentinelops stop       stop the stack (containers and data are kept)
    sentinelops status     services, AI mode and agent health (no AI call)
    sentinelops models     list the models your key can use and change the selection
    sentinelops logs       recent logs (``-f`` to follow, optional service name)
    sentinelops dashboard  print (and optionally open) the local dashboard URL
    sentinelops doctor     diagnose prerequisites and configuration

This is a thin orchestrator over what already exists; it adds no new agent,
authentication or model gateway:
* configuration is ``.env`` created from ``.env.example`` with freshly generated
  secrets (existing values are never replaced without confirmation);
* services, health checks and migrations are the ``docker-compose.yml`` ones;
* AI authentication and model selection ARE the existing ``onboard`` tool
  (``app/cli.py``) running in its container with this terminal attached, so the
  Anthropic key is typed into that tool's hidden prompt, verified with a free
  model-list call and stored only in the ``ai_secrets`` volume. The key never
  passes through this process, ``.env``, logs or Git.

The launcher never enables autonomous remediation without an explicit "yes",
never touches the policy or executor allowlist, never exposes the dashboard
beyond 127.0.0.1 and never makes a paid model call.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

REQUIRED_SECRETS = (
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "DEMO_INJECTION_TOKEN",
    "SENTINEL_API_READ_TOKEN",
    "SENTINEL_OPS_READER_TOKEN",
    "SENTINEL_EXECUTOR_TOKEN",
    "SENTINEL_ACTION_SIGNING_KEY",
    "SENTINEL_APPROVAL_SIGNING_KEY",
    "SENTINEL_NOTIFY_WEBHOOK_SECRET",
)
# Long-running services that must be healthy (existing compose names and health checks).
SERVICES = (
    "postgres",
    "redis",
    "api",
    "demo-app",
    "monitor",
    "dispatcher",
    "worker",
    "ops-reader",
    "executor",
    "notifier",
    "notify-sink",
)
IMAGE = "sentinelops:dev"
MOCK_BANNER = (
    "AI mode: MOCK / DEMO - deterministic test model, NOT Claude. "
    "No Anthropic calls, no cost. Findings are not real AI analysis."
)
SUBSCRIPTION_NOTE = (
    "A Claude subscription (Pro/Max/Team/Enterprise) alone does NOT give this app API "
    "access: SentinelOps uses an Anthropic API key from the Claude Console, billed "
    "pay-as-you-go and separately from any subscription (docs/auth-decision.md)."
)
_KEYLIKE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}|sop_[A-Za-z0-9_\-]{16,}|Bearer\s+\S+")


# --------------------------------------------------------------------------- plumbing
@dataclass
class Result:
    code: int
    out: str = ""
    err: str = ""


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        interactive: bool = False,
        timeout: float | None = None,
    ) -> Result: ...


def subprocess_runner(cwd: Path, env: dict[str, str]) -> Runner:
    def run(
        args: Sequence[str],
        *,
        input: str | None = None,
        interactive: bool = False,
        timeout: float | None = None,
    ) -> Result:
        try:
            if interactive:  # the child owns this terminal (hidden key prompts happen there)
                return Result(subprocess.run(list(args), cwd=cwd, env=env, check=False).returncode)
            p = subprocess.run(
                list(args),
                cwd=cwd,
                env=env,
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return Result(p.returncode, p.stdout, p.stderr)
        except FileNotFoundError as exc:
            return Result(127, "", str(exc))
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout}s: {' '.join(args[:3])}")

    return run


@dataclass
class IO:
    say: Callable[[str], None] = print
    ask: Callable[[str], str] = input
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty() and sys.stdout.isatty())
    open_url: Callable[[str], bool] = webbrowser.open

    def confirm(self, question: str, default: bool) -> bool:
        if not self.interactive:
            return default
        suffix = " [Y/n] " if default else " [y/N] "
        answer = self.ask(question + suffix).strip().lower()
        return default if not answer else answer in ("y", "yes")


def find_root(start: Path | None = None) -> Path:
    """SENTINELOPS_HOME, else this package's checkout, else the nearest parent of cwd."""
    env = os.environ.get("SENTINELOPS_HOME")
    candidates = [Path(env)] if env else []
    candidates.append(Path(__file__).resolve().parents[1])
    here = (start or Path.cwd()).resolve()
    candidates += [here, *here.parents]
    for c in candidates:
        if (c / "docker-compose.yml").is_file() and (c / ".env.example").is_file():
            return c
    raise SystemExit(
        "Cannot find the SentinelOps checkout (docker-compose.yml + .env.example). "
        "Run from the repository or set SENTINELOPS_HOME."
    )


# --------------------------------------------------------------------------- .env
def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value in place (first active line) or append; everything else is kept."""
    lines = path.read_text().splitlines() if path.is_file() else []
    todo = dict(updates)
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in todo:
                lines[i] = f"{k}={todo.pop(k)}"
    if todo:
        lines += ["", "# Written by the sentinelops launcher"]
        lines += [f"{k}={v}" for k, v in todo.items()]
    path.write_text("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)
    except OSError:  # e.g. some Windows filesystems
        pass


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def detect_docker_gid() -> tuple[str, str]:
    """(gid, how). Linux: the socket's group. Docker Desktop (macOS/Windows) mounts
    the VM's root-owned socket, so group 0 is used there (verified by `doctor`)."""
    sock = Path("/var/run/docker.sock")
    if platform.system() == "Linux" and sock.exists():
        return str(sock.stat().st_gid), f"group of {sock}"
    return "0", "Docker Desktop default (socket owned by root in the Docker VM)"


def secret_values(env: dict[str, str]) -> list[str]:
    return [env[k] for k in REQUIRED_SECRETS if len(env.get(k, "")) >= 8]


def sanitize(text: str, env: dict[str, str]) -> str:
    for v in sorted(secret_values(env), key=len, reverse=True):
        text = text.replace(v, "***")
    return _KEYLIKE.sub("***", text)


# --------------------------------------------------------------------------- launcher
@dataclass
class AIState:
    gateway: str | None
    model: str | None
    auth_mode: str | None
    key: str | None  # "configured (sha256:…)" or the reason it is not

    @property
    def configured(self) -> bool:
        if self.gateway == "mock":
            return self.auth_mode == "mock" and bool(self.model)
        if self.gateway == "anthropic":
            return (
                self.auth_mode == "api_key"
                and bool(self.model)
                and bool(self.key and self.key.startswith("configured"))
            )
        return False


class Launcher:
    def __init__(self, root: Path, run: Runner, io: IO) -> None:
        self.root, self.run, self.io = root, run, io
        self.env_path = root / ".env"

    # --- helpers -------------------------------------------------------------------
    @property
    def env(self) -> dict[str, str]:
        return read_env(self.env_path)

    def say(self, msg: str = "") -> None:
        self.io.say(sanitize(msg, self.env))

    def compose(self, *args: str, **kw: Any) -> Result:
        return self.run(["docker", "compose", "-f", "docker-compose.yml", *args], **kw)

    def fail(self, msg: str, fix: str) -> int:
        self.say(f"ERROR: {msg}")
        self.say(f"  Fix: {fix}")
        return 1

    # --- 1-3: prerequisites ------------------------------------------------------------
    def prerequisites(self) -> list[tuple[bool, str, str]]:
        """(ok, check, detail-or-fix) for each prerequisite; no side effects."""
        out: list[tuple[bool, str, str]] = []
        ok_py = sys.version_info >= (3, 12)
        out.append(
            (
                ok_py,
                "Python >= 3.12",
                platform.python_version() if ok_py else "install Python 3.12+ (uv does this)",
            )
        )
        cli = self.run(["docker", "--version"], timeout=20)
        out.append(
            (
                cli.code == 0,
                "Docker CLI",
                cli.out.strip()
                if cli.code == 0
                else "install Docker: https://docs.docker.com/get-docker/",
            )
        )
        if cli.code != 0:
            return out
        daemon = self.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
        out.append(
            (
                daemon.code == 0,
                "Docker daemon running",
                f"server {daemon.out.strip()}"
                if daemon.code == 0
                else "start Docker Desktop / `sudo systemctl start docker`, then retry",
            )
        )
        comp = self.run(["docker", "compose", "version", "--short"], timeout=20)
        out.append(
            (
                comp.code == 0,
                "Docker Compose v2",
                comp.out.strip()
                if comp.code == 0
                else "install the Compose v2 plugin: https://docs.docker.com/compose/install/",
            )
        )
        return out

    def check_prerequisites(self) -> bool:
        ok = True
        for good, name, detail in self.prerequisites():
            self.say(f"  {'ok  ' if good else 'FAIL'} {name}: {detail}")
            ok = ok and good
        return ok

    # --- 4-6: configuration --------------------------------------------------------------
    def ensure_config(self) -> bool:
        example = self.root / ".env.example"
        if not self.env_path.exists():
            self.say("No local configuration found: creating .env from .env.example.")
            fd = os.open(self.env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:  # secrets file: owner-only from the first byte
                fh.write(example.read_text())
            gid, how = detect_docker_gid()
            write_env_values(
                self.env_path,
                {
                    **{k: new_secret() for k in REQUIRED_SECRETS},
                    "DOCKER_GID": gid,
                    "SENTINEL_ENVIRONMENT": "development",
                    # isolated demo target; restarts need human approval unless opted in
                    "SENTINEL_REMEDIATION_ENVIRONMENT": "isolated-demo",
                    "SENTINEL_REMEDIATION_AUTO_ENABLED": "false",
                },
            )
            self.say(f"  generated {len(REQUIRED_SECRETS)} fresh random secrets (not shown)")
            self.say(f"  DOCKER_GID={gid} ({how})")
            self.say(f"  saved {self.env_path} (mode 0600; ignored by Git)")
            self.choose_remediation()
            return True
        env = self.env
        missing = [k for k in REQUIRED_SECRETS if not env.get(k)]
        if not env.get("DOCKER_GID"):
            missing.append("DOCKER_GID")
        if not missing:
            self.say(f"Using existing configuration {self.env_path} (unchanged).")
            return True
        self.say(f"Existing .env is missing: {', '.join(missing)}")
        if not self.io.confirm(
            "Fill ONLY these missing values (fresh secrets; existing values untouched)?", True
        ):
            self.say("Configuration left unchanged; the stack cannot start without them.")
            return False
        updates = {k: new_secret() for k in missing if k != "DOCKER_GID"}
        if "DOCKER_GID" in missing:
            updates["DOCKER_GID"] = detect_docker_gid()[0]
        write_env_values(self.env_path, updates)
        self.say(f"  filled {len(updates)} value(s).")
        return True

    def choose_remediation(self) -> None:
        self.say("")
        self.say("Remediation (isolated demo app only; the deterministic policy still decides):")
        self.say("  default: SentinelOps PROPOSES a restart and a human approves it via the API.")
        auto = self.io.confirm(
            "Allow AUTONOMOUS restarts of the isolated demo app without approval?", False
        )
        write_env_values(
            self.env_path,
            {
                "SENTINEL_REMEDIATION_ENVIRONMENT": "isolated-demo",
                "SENTINEL_REMEDIATION_AUTO_ENABLED": "true" if auto else "false",
            },
        )
        self.say(
            "  autonomous demo restarts ENABLED (you confirmed)"
            if auto
            else "  human approval required for restarts"
        )

    def validate_compose(self) -> bool:
        r = self.compose("config", "--quiet", timeout=60)
        if r.code != 0:
            self.fail(
                "docker-compose.yml did not validate with this .env:\n    "
                + sanitize(r.err.strip() or r.out.strip(), self.env)[:800],
                "run `sentinelops doctor`; check .env values",
            )
            return False
        return True

    def ensure_image(self) -> bool:
        if self.run(["docker", "image", "inspect", IMAGE], timeout=30).code == 0:
            return True
        self.say("Building the SentinelOps image (first run only; a few minutes)...")
        # build output streams to the terminal (it contains no secrets: no build args)
        r = self.compose("build", interactive=True)
        if r.code != 0:
            self.fail(
                "image build failed (see the output above)",
                "check network access to the Python/Docker registries, then retry",
            )
            return False
        return True

    # --- 8-9: infrastructure + migrations -----------------------------------------------
    def start_infrastructure(self) -> bool:
        self.say("Starting PostgreSQL and Redis...")
        r = self.compose("up", "-d", "--wait", "postgres", "redis", timeout=300)
        if r.code != 0:
            self.diagnose(["postgres", "redis"], r)
            return False
        self.say("Applying database migrations...")
        m = self.compose("run", "--rm", "-T", "migrate", timeout=300)
        if m.code != 0:
            self.fail(
                "migrations failed:\n" + sanitize((m.err or m.out)[-1200:], self.env),
                "the migrate output above shows the cause; run `sentinelops doctor`",
            )
            return False
        self.say("  database schema is at the latest migration")
        return True

    # --- 10: AI onboarding (the EXISTING onboard tool) ------------------------------------
    def ai_state(self) -> AIState:
        r = self.compose("run", "--rm", "-T", "onboard", "status", timeout=180)
        text = r.out if r.code == 0 else ""
        gw = re.search(r"^AI gateway: (\w+)", text, re.M)
        sel = re.search(r"^Selected: (\S+) \((\w+)\)", text, re.M)
        key = re.search(r"^API key: (.+)$", text, re.M)
        return AIState(
            gw.group(1) if gw else None,
            sel.group(1) if sel else None,
            sel.group(2) if sel else None,
            key.group(1).strip() if key else None,
        )

    def setup_ai(self) -> bool:
        self.say("")
        self.say("AI configuration")
        self.say("  1. Anthropic API key (real Claude; you choose the model)")
        self.say("  2. Skip: MOCK / DEMO mode (deterministic test model, NOT Claude, no cost)")
        self.say(f"  Note: {SUBSCRIPTION_NOTE}")
        choice = (
            self.io.ask("Selection [1-2] (default 2): ").strip() if self.io.interactive else "2"
        )
        if choice == "1":
            if not self.io.interactive:
                self.say("API-key setup needs an interactive terminal; using mock mode.")
            else:
                return self.setup_api_key()
        return self.setup_mock()

    def setup_api_key(self) -> bool:
        write_env_values(self.env_path, {"SENTINEL_AI_GATEWAY": "anthropic"})
        self.say("")
        self.say("Opening the SentinelOps onboarding tool. Choose option 2 (Anthropic API key).")
        self.say("Your key is typed into a HIDDEN prompt, checked with a free model-list call")
        self.say("(no tokens used) and stored only in the ai_secrets Docker volume.")
        self.compose("run", "--rm", "onboard", interactive=True)
        st = self.ai_state()
        if st.configured:
            self.say(f"Claude API configured: model {st.model}; key {st.key}")
            return True
        self.say("The Claude API was not configured (no verified key or no model selected).")
        if self.io.confirm("Continue in MOCK / DEMO mode instead?", True):
            return self.setup_mock()
        return False

    def setup_mock(self) -> bool:
        write_env_values(self.env_path, {"SENTINEL_AI_GATEWAY": "mock"})
        # existing onboarding flow: option 2, then the (only) mock model
        r = self.compose("run", "--rm", "-T", "onboard", input="2\n1\n", timeout=180)
        st = self.ai_state()
        if r.code != 0 or not st.configured:
            self.fail(
                "mock model selection failed:\n" + sanitize((r.out + r.err)[-800:], self.env),
                "run `sentinelops doctor`",
            )
            return False
        self.say(f"Selected {st.model}. {MOCK_BANNER}")
        return True

    # --- 6: start + health -----------------------------------------------------------------
    def service_states(self) -> dict[str, dict[str, str]]:
        r = self.compose("ps", "-a", "--format", "json", timeout=60)
        states: dict[str, dict[str, str]] = {}
        for line in r.out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows = json.loads(line)
            except ValueError:
                continue
            for row in rows if isinstance(rows, list) else [rows]:
                states[row.get("Service", "?")] = {
                    "state": str(row.get("State", "")),
                    "health": str(row.get("Health", "")),
                    "status": str(row.get("Status", "")),
                }
        return states

    def unhealthy(self, states: dict[str, dict[str, str]]) -> list[str]:
        bad = []
        for svc in SERVICES:
            s = states.get(svc)
            if not s or s["state"] != "running" or s["health"] not in ("healthy", ""):
                bad.append(svc)
        return bad

    def diagnose(self, services: Sequence[str], r: Result) -> bool:
        states = self.service_states()
        failing = [s for s in services if s in self.unhealthy(states)] or list(services)
        self.say("ERROR: startup did not complete.")
        if r.err.strip():
            self.say("  compose: " + sanitize(r.err.strip().splitlines()[-1], self.env)[:300])
        for svc in failing:
            st = states.get(svc, {"state": "missing", "health": "", "status": ""})
            self.say(f"  {svc}: {st['state']} {st['health']} {st['status']}".rstrip())
            logs = self.compose("logs", "--no-color", "--tail", "15", svc, timeout=60)
            for line in sanitize(logs.out, self.env).splitlines()[-15:]:
                self.say(f"    | {line[:220]}")
        self.say("  Fix: run `sentinelops doctor`; `sentinelops logs <service>` for more.")
        return False

    def start(self) -> int:
        if not self.env_path.exists():
            return self.fail("no configuration (.env)", "run `sentinelops setup` first")
        if not (self.validate_compose() and self.ensure_image()):
            return 1
        before = self.service_states()
        running = [s for s in SERVICES if before.get(s, {}).get("state") == "running"]
        self.say(
            "Starting SentinelOps (existing containers are reused; nothing is duplicated)..."
            if len(running) < len(SERVICES)
            else "SentinelOps is already running; checking health..."
        )
        r = self.compose("up", "-d", "--wait", "--wait-timeout", "240", timeout=600)
        states = self.service_states()
        bad = self.unhealthy(states)
        if r.code != 0 or bad:
            self.diagnose(bad or list(SERVICES), r)
            return 1
        self.say(f"All {len(SERVICES)} services are healthy:")
        self.say("  " + ", ".join(f"{s} ok" for s in SERVICES))
        self.show_agent()
        self.show_dashboard(offer_open=True)
        return 0

    def stop(self) -> int:
        r = self.compose("stop", timeout=300)
        self.say(
            "SentinelOps stopped (containers, data and configuration kept)."
            if r.code == 0
            else "stop failed: " + sanitize(r.err[-300:], self.env)
        )
        return r.code

    # --- status / dashboard -------------------------------------------------------------------
    def api_base(self) -> str:
        r = self.compose("port", "api", "8000", timeout=30)
        hostport = r.out.strip().splitlines()[0] if r.code == 0 and r.out.strip() else ""
        host, _, port = hostport.rpartition(":")
        if host in ("0.0.0.0", "::", "[::]", ""):  # noqa: S104 - only rewriting to loopback
            host = "127.0.0.1"
        return f"http://{host}:{port or '8000'}"

    def system_status(self) -> dict[str, Any] | None:
        token = self.env.get("SENTINEL_API_READ_TOKEN", "")
        req = urllib.request.Request(  # noqa: S310 - fixed http://127.0.0.1 URL
            self.api_base() + "/v1/system/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback http
                data: dict[str, Any] = json.loads(resp.read())
                return data
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def show_agent(self) -> None:
        env = self.env
        data = self.system_status()
        if data is None:
            self.say("Agent status: API not reachable (is the stack running?)")
            return
        comps = data.get("components", {})
        ai = comps.get("ai", {})
        sel = ai.get("selected") or "none"
        auth = sel.split(":", 1)[0] if ":" in sel else None
        # Mode = what the WORKER runs: its gateway (.env) + the stored selection's auth mode.
        if env.get("SENTINEL_AI_GATEWAY", "anthropic") == "mock":
            self.say(MOCK_BANNER)
            self.say(f"  selected model: {sel}")
        elif auth == "api_key":
            self.say(
                f"AI mode: Claude API (Anthropic API key); selected {sel}; "
                f"status {ai.get('status')}"
            )
            self.say("  API usage is billed to your Claude Console account.")
        else:
            self.say(
                f"AI mode: Claude API gateway, but no Claude model is selected ({sel}); "
                "AI investigations pause until you run `sentinelops setup`."
            )
        auto = env.get("SENTINEL_REMEDIATION_AUTO_ENABLED", "false") == "true"
        self.say(
            "Remediation: AUTONOMOUS demo restarts enabled (isolated demo only)"
            if auto
            else "Remediation: human approval required (isolated demo only)"
        )
        self.say(f"Agent overall: {data.get('overall', data.get('status', 'unknown'))}")
        for name, c in sorted(comps.items()):
            if isinstance(c, dict) and c.get("status") not in (None, "ok"):
                self.say(f"  {name}: {c.get('status')}")

    def show_dashboard(self, offer_open: bool) -> None:
        url = self.api_base() + "/dashboard/"
        self.say("")
        self.say(f"Dashboard: {url}   (local only; bound to 127.0.0.1)")
        self.say(
            "  It asks for the read-only API token: the SENTINEL_API_READ_TOKEN value in "
            f"{self.env_path} (not printed here)."
        )
        if (
            offer_open
            and self.io.interactive
            and self.io.confirm("Open the dashboard in your browser?", True)
            and not self.io.open_url(url)
        ):
            self.say("  Could not open a browser here; open the URL manually.")

    def status(self) -> int:
        if not self.env_path.exists():
            return self.fail("SentinelOps is not set up", "run `sentinelops`")
        states = self.service_states()
        for svc in SERVICES:
            s = states.get(svc)
            label = f"{s['state']} {s['health']}".strip() if s else "not created"
            self.say(f"  {svc:<12} {label}")
        bad = self.unhealthy(states)
        self.say("All services healthy." if not bad else f"Not healthy: {', '.join(bad)}")
        if not bad:
            self.show_agent()
            self.show_dashboard(offer_open=False)
        return 0 if not bad else 1

    # --- entry points --------------------------------------------------------------------------
    def setup(self, *, first_run: bool) -> int:
        self.say("Welcome to SentinelOps: a bounded incident-response agent for ONE isolated")
        self.say("demo application. This guided setup runs locally; nothing is deployed.")
        self.say("")
        self.say("Checking prerequisites...")
        if not self.check_prerequisites():
            self.say("Fix the failed prerequisite(s) above and run `sentinelops` again.")
            return 1
        existed = self.env_path.exists()
        if not self.ensure_config():
            return 1
        if (
            existed
            and not first_run
            and self.io.confirm("Change the remediation mode (approval vs autonomous)?", False)
        ):
            self.choose_remediation()
        if not (self.validate_compose() and self.ensure_image() and self.start_infrastructure()):
            return 1
        st = self.ai_state()
        if st.configured and not (
            not first_run and self.io.confirm("AI is configured. Reconfigure key/model?", False)
        ):
            self.say(
                f"Reusing AI configuration: {st.gateway} / {st.model}"
                + (f" / key {st.key}" if st.gateway == "anthropic" else "")
            )
        elif not self.setup_ai():
            return 1
        return self.start()

    def default(self) -> int:
        if not self.env_path.exists():
            return self.setup(first_run=True)
        if not self.check_prerequisites():
            return 1
        if not self.ensure_config() or not self.validate_compose() or not self.ensure_image():
            return 1
        st = self.ai_state()
        if not st.configured:
            self.say("AI is not configured yet (or the saved key/model is missing).")
            if not self.start_infrastructure() or not self.setup_ai():
                return 1
        else:
            self.say(f"Reusing saved configuration: AI {st.gateway} / {st.model}")
        return self.start()

    def models(self) -> int:
        if not self.io.interactive:
            return self.fail("model selection is interactive", "run it in a terminal")
        if self.env.get("SENTINEL_AI_GATEWAY") == "mock":
            self.say(MOCK_BANNER + " Run `sentinelops setup` to configure a Claude API key.")
        r = self.compose("run", "--rm", "onboard", "change-model", interactive=True)
        if r.code == 0:
            self.compose("up", "-d", "--wait", "worker", timeout=300)
        return r.code

    def logs(self, service: str | None, follow: bool, tail: int) -> int:
        args = ["logs", "--tail", str(tail)] + (["-f"] if follow else [])
        return self.compose(*args, *([service] if service else []), interactive=True).code

    def dashboard(self) -> int:
        if self.system_status() is None:
            return self.fail("the API is not reachable", "run `sentinelops start`")
        self.show_dashboard(offer_open=True)
        return 0

    def doctor(self) -> int:
        ok = self.check_prerequisites()
        env = self.env
        if not self.env_path.exists():
            self.say("  FAIL configuration: no .env (run `sentinelops setup`)")
            return 1
        missing = [k for k in (*REQUIRED_SECRETS, "DOCKER_GID") if not env.get(k)]
        self.say(
            "  ok   configuration: all required values set"
            if not missing
            else f"  FAIL configuration: missing {', '.join(missing)} (run `sentinelops setup`)"
        )
        ok = ok and not missing
        if os.name == "posix" and self.env_path.stat().st_mode & 0o077:
            self.say("  WARN .env is readable by other users: chmod 600 .env")
        if platform.system() == "Linux" and Path("/var/run/docker.sock").exists():
            gid = str(Path("/var/run/docker.sock").stat().st_gid)
            same = env.get("DOCKER_GID") == gid
            self.say(f"  {'ok  ' if same else 'FAIL'} DOCKER_GID matches the socket group ({gid})")
            ok = ok and same
        valid = self.compose("config", "--quiet", timeout=60).code == 0
        self.say(f"  {'ok  ' if valid else 'FAIL'} docker-compose.yml validates")
        img = self.run(["docker", "image", "inspect", IMAGE], timeout=30).code == 0
        self.say(
            f"  {'ok  ' if img else 'info'} image {IMAGE} {'built' if img else 'not built yet'}"
        )
        states = self.service_states()
        bad = self.unhealthy(states)
        self.say(
            "  ok   all services healthy"
            if not bad
            else f"  info not running/healthy: {', '.join(bad)} (`sentinelops start`)"
        )
        self.say(f"  info AI gateway in .env: {env.get('SENTINEL_AI_GATEWAY', 'anthropic')}")
        project = os.environ.get("COMPOSE_PROJECT_NAME")
        if project and project != "sentinelops":
            self.say(
                f"  WARN COMPOSE_PROJECT_NAME={project}: the executor and ops-reader are pinned to "
                "project 'sentinelops', so remediation/diagnostics report the target unavailable"
            )
        return 0 if ok and valid else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sentinelops", description="Set up, start and check the local SentinelOps stack."
    )
    sub = ap.add_subparsers(dest="cmd")
    for name, text in (
        ("setup", "guided setup (secrets, infrastructure, migrations, AI key/model)"),
        ("start", "start the stack and wait until healthy"),
        ("stop", "stop the stack (data kept)"),
        ("status", "services, AI mode and agent health"),
        ("models", "list models for your key and change the selection"),
        ("dashboard", "print/open the local dashboard URL"),
        ("doctor", "diagnose prerequisites and configuration"),
    ):
        sub.add_parser(name, help=text)
    lg = sub.add_parser("logs", help="show service logs")
    lg.add_argument("service", nargs="?")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("--tail", type=int, default=100)
    a = ap.parse_args(argv)
    root = find_root()
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_FILE"}
    launcher = Launcher(root, subprocess_runner(root, env), IO())
    try:
        match a.cmd:
            case None:
                return launcher.default()
            case "setup":
                return launcher.setup(first_run=not (root / ".env").exists())
            case "start":
                return launcher.start()
            case "stop":
                return launcher.stop()
            case "status":
                return launcher.status()
            case "models":
                return launcher.models()
            case "logs":
                return launcher.logs(a.service, a.follow, a.tail)
            case "dashboard":
                return launcher.dashboard()
            case "doctor":
                return launcher.doctor()
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
