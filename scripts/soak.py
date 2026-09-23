#!/usr/bin/env python3
"""Soak / reliability harness for the LOCAL compose stack - session independent.

    # start detached (survives the terminal / Claude Code session; NOT a host reboot)
    uv run python scripts/soak.py start --minutes 4320 --inject-every 5 --dir soak-results/72h
    uv run python scripts/soak.py status --dir soak-results/72h     # progress, liveness, violations
    uv run python scripts/soak.py resume --dir soak-results/72h     # after an interruption
    uv run python scripts/soak.py verify --dir soak-results/72h     # final PASS/FAIL (exit code)

Everything is persisted in ``--dir`` as it happens:
  state.json      run id, REAL UTC start, deadline, config, harness pid, baseline
  status.json     updated every sample: phase, elapsed, counters, last invariant check
  samples.jsonl   one line per sample (readiness, overall status, outbox backlog)
  events.jsonl    injections, resumes, failed checks
  checks.jsonl    periodic invariant snapshots (every --check-every minutes)
  result.json     final result + invariants (only when the deadline was actually reached)
  harness.log     stdout/stderr of the detached process

Honesty rules built in: elapsed time is wall-clock from the persisted UTC start;
a resumed run keeps the ORIGINAL start and records the gap; ``verify`` fails if
the run did not reach its deadline, if the harness was down for longer than
``--max-gap-minutes`` in total, or if any invariant was violated.
Injection schedule (round robin): http_500, timeout, http_500 self-healing.
Uses docker compose + psql + HTTP only; secrets are read from .env, never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API, DEMO = "http://127.0.0.1:8000", "http://127.0.0.1:8001"
SERVICES = (
    "api",
    "monitor",
    "dispatcher",
    "worker",
    "notifier",
    "ops-reader",
    "executor",
    "postgres",
    "redis",
    "notify-sink",
    "demo-app",
)
SCHEDULE: list[tuple[str, int | None]] = [("http_500", None), ("timeout", None), ("http_500", 45)]


# --- plumbing ------------------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def env() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def sh(*args: str, check: bool = True, timeout: float = 120) -> str:
    return subprocess.run(
        args, cwd=ROOT, check=check, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


def psql(sql: str) -> str:
    return sh(
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "sentinelops",
        "-d",
        "sentinelops",
        "-tA",
        "-c",
        sql,
    )


def http(
    method: str, url: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, OSError):
        return 0, {}


def containers() -> dict[str, dict[str, Any]]:
    out = {}
    for svc in SERVICES:
        cid = sh("docker", "compose", "ps", "-q", svc, check=False)
        if not cid:
            continue
        info = json.loads(sh("docker", "inspect", cid))[0]
        out[svc] = {
            "id": cid[:12],
            "started_at": info["State"]["StartedAt"],
            "restart_count": info["RestartCount"],
            "running": info["State"]["Running"],
        }
    return out


def memory() -> dict[str, float]:
    raw = sh("docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}", check=False)
    out = {}
    mult = {"GiB": 1024.0, "MiB": 1.0, "KiB": 1 / 1024, "B": 1 / 1024 / 1024}
    for line in raw.splitlines():
        name, usage = line.split(" ", 1)
        val = usage.split("/")[0].strip()
        num = float("".join(c for c in val if c.isdigit() or c == ".") or 0)
        unit = next((u for u in mult if val.endswith(u)), "MiB")
        out[name.replace("sentinelops-", "").rsplit("-", 1)[0]] = round(num * mult[unit], 1)
    return out


def ledger_total() -> int:
    raw = sh(
        "docker",
        "compose",
        "exec",
        "-T",
        "executor",
        "python",
        "-m",
        "app.executor.ledger_tool",
        "verify",
        check=False,
    )
    try:
        return int(json.loads(raw)["total"])
    except (ValueError, KeyError):
        return -1


def inject(token: str, mode: str, duration: int | None = None) -> int:
    body: dict[str, Any] = {"mode": mode}
    if duration:
        body["duration_seconds"] = duration
    return http("POST", f"{DEMO}/simulate-failure", body, {"X-Demo-Token": token})[0]


def load(d: Path, name: str) -> dict[str, Any]:
    p = d / name
    return json.loads(p.read_text()) if p.exists() else {}


def save(d: Path, name: str, data: dict[str, Any]) -> None:
    tmp = d / f".{name}.tmp"
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(d / name)  # atomic


def append(d: Path, name: str, data: dict[str, Any]) -> None:
    with (d / name).open("a") as f:
        f.write(json.dumps(data, default=str) + "\n")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --- invariants ------------------------------------------------------------------------------------


def invariants(state: dict[str, Any], *, final: bool) -> dict[str, Any]:
    start = state["db_start"]
    w = f"created_at >= '{start}'"
    q = psql
    interval = float(state["config"]["monitor_interval_seconds"])
    r: dict[str, Any] = {
        "at": now_iso(),
        "health_checks": int(
            q(f"SELECT count(*) FROM health_checks WHERE checked_at >= '{start}'")
        ),
        "max_check_gap_seconds": float(
            q(
                "SELECT COALESCE(max(g), 0) FROM (SELECT extract(epoch FROM checked_at - "
                f"lag(checked_at) OVER (ORDER BY checked_at)) g FROM health_checks "
                f"WHERE checked_at >= '{start}') x"
            )
            or 0
        ),
        "incidents": int(q(f"SELECT count(*) FROM incidents WHERE {w}")),
        "tasks": int(q(f"SELECT count(*) FROM tasks WHERE {w}")),
        "task_outcomes": json.loads(
            q(
                "SELECT COALESCE(json_object_agg(k, n), '{}') FROM (SELECT status || ':' || "
                f"COALESCE(outcome,'-') k, count(*) n FROM tasks WHERE {w} GROUP BY 1) x"
            )
        ),
        "duplicate_active_incidents": int(
            q(
                "SELECT count(*) FROM (SELECT service_id, incident_type FROM incidents WHERE status "
                "NOT IN ('resolved','closed') GROUP BY 1, 2 HAVING count(*) > 1) x"
            )
        ),
        "lost_tasks": int(
            q(
                f"SELECT count(*) FROM tasks WHERE {w} AND status NOT IN ('escalated','failed',"
                "'resolved','dead_lettered','waiting_approval') AND updated_at < now() - interval "
                "'15 minutes'"
            )
        ),
        "actions_per_incident_max": int(
            q(
                "SELECT COALESCE(max(n), 0) FROM (SELECT count(*) n FROM action_attempts WHERE "
                f"requested_at >= '{start}' GROUP BY incident_id) x"
            )
        ),
        "executed_actions": int(
            q(
                f"SELECT count(*) FROM action_attempts WHERE requested_at >= '{start}' "
                "AND status IN ('succeeded','reconciled')"
            )
        ),
        "ledger_delta": ledger_total() - int(state["baseline"]["ledger_total"]),
        "outbox_unpublished": int(
            q("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
        ),
        "dead_lettered_tasks": int(
            q(f"SELECT count(*) FROM tasks WHERE {w} AND status='dead_lettered'")
        ),
        "failed_report_jobs": int(
            q(f"SELECT count(*) FROM report_jobs WHERE {w} AND status='failed'")
        ),
        "terminal_tasks_without_report": int(
            q(
                f"SELECT count(*) FROM tasks t WHERE t.{w} AND t.status IN ('escalated','failed',"
                "'resolved','dead_lettered') AND t.completed_at < now() - interval '10 minutes' "
                "AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.incident_id = t.incident_id)"
            )
        ),
        "reports": json.loads(
            q(
                "SELECT COALESCE(json_object_agg(generation_mode, n), '{}') FROM (SELECT "
                f"generation_mode, count(*) n FROM reports WHERE {w} GROUP BY 1) x"
            )
        ),
        "notifications": json.loads(
            q(
                "SELECT COALESCE(json_object_agg(k, n), '{}') FROM (SELECT channel || ':' || status k, "
                f"count(*) n FROM notification_deliveries WHERE {w} GROUP BY 1) x"
            )
        ),
        "dead_lettered_notifications": int(
            q(f"SELECT count(*) FROM notification_deliveries WHERE {w} AND status='dead_lettered'")
        ),
        "ai_usage": json.loads(
            q(
                "SELECT json_build_object('calls', count(*), 'tokens', COALESCE(sum(input_tokens+"
                "output_tokens+cache_read_tokens+cache_write_tokens),0), 'errors', count(*) FILTER "
                f"(WHERE outcome <> 'ok')) FROM ai_usage WHERE occurred_at >= '{start}'"
            )
        ),
        "memory_mib": memory(),
    }
    now_c = containers()
    base_c = state["baseline"]["containers"]
    r["unexpected_restarts"] = sorted(
        s
        for s in base_c
        if s != "demo-app" and now_c.get(s, {}).get("started_at") != base_c[s]["started_at"]
    )
    growth = {
        k: round(v - state["baseline"]["memory_mib"].get(k, v), 1)
        for k, v in r["memory_mib"].items()
    }
    r["memory_growth_mib"] = growth
    r["ok"] = {
        "no_duplicate_active_incidents": r["duplicate_active_incidents"] == 0,
        "no_lost_tasks": r["lost_tasks"] == 0,
        "at_most_one_action_per_incident": r["actions_per_incident_max"] <= 1,
        "ledger_matches_executed_actions": r["ledger_delta"] == r["executed_actions"],
        "every_terminal_task_has_a_report": r["terminal_tasks_without_report"] == 0,
        "no_unexpected_service_restarts": not r["unexpected_restarts"],
        "no_failed_report_jobs": r["failed_report_jobs"] == 0,
        "no_dead_lettered_notifications": r["dead_lettered_notifications"] == 0,
        "monitor_never_silent": r["max_check_gap_seconds"] <= max(60.0, 6 * interval),
        # memory growth is bounded (< 150 MiB per container over the run)
        "bounded_memory_growth": all(v < 150 for v in growth.values()),
    }
    if final:
        r["ok"]["queue_drained"] = r["outbox_unpublished"] == 0
    return r


# --- commands ------------------------------------------------------------------------------------


def _spawn(a: argparse.Namespace, d: Path) -> int:
    log = (d / "harness.log").open("a")
    proc = subprocess.Popen(  # detached: new session, no controlling terminal
        [sys.executable, str(Path(__file__).resolve()), "run", "--dir", a.dir],
        cwd=ROOT,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def cmd_start(a: argparse.Namespace) -> int:
    d = ROOT / a.dir
    if (d / "state.json").exists():
        print(f"refusing: {d} already has a run (use status/resume)", file=sys.stderr)
        return 2
    d.mkdir(parents=True, exist_ok=True)
    e = env()
    state: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "started_at": now_iso(),
        "started_epoch": time.time(),
        "db_start": psql("SELECT now()"),
        "requested_minutes": a.minutes,
        "config": {
            "inject_every_minutes": a.inject_every,
            "sample_seconds": a.sample_seconds,
            "check_every_minutes": a.check_every,
            "max_gap_minutes": a.max_gap_minutes,
            "monitor_interval_seconds": float(
                os.environ.get("SENTINEL_MONITOR_INTERVAL_SECONDS")
                or e.get("SENTINEL_MONITOR_INTERVAL_SECONDS")
                or 30
            ),
            "compose_file": os.environ.get("COMPOSE_FILE"),
        },
        "baseline": {
            "containers": containers(),
            "memory_mib": memory(),
            "ledger_total": ledger_total(),
        },
    }
    state["deadline_epoch"] = state["started_epoch"] + a.minutes * 60
    save(d, "state.json", state)
    state["pid"] = _spawn(a, d)
    save(d, "state.json", state)
    print(
        json.dumps(
            {
                "run_id": state["run_id"],
                "started_at": state["started_at"],
                "deadline": datetime.fromtimestamp(state["deadline_epoch"], UTC).isoformat(),
                "pid": state["pid"],
                "dir": str(d),
            },
            indent=2,
        )
    )
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    d = ROOT / a.dir
    state = load(d, "state.json")
    if not state:
        print("no run in this directory", file=sys.stderr)
        return 2
    if pid_alive(state.get("pid")):
        print(f"harness still running (pid {state['pid']})", file=sys.stderr)
        return 2
    if (d / "result.json").exists():
        print("run already completed; nothing to resume", file=sys.stderr)
        return 2
    state["pid"] = _spawn(a, d)
    save(d, "state.json", state)
    print(f"resumed (pid {state['pid']}); original start {state['started_at']} is kept")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    d = ROOT / a.dir
    state = load(d, "state.json")
    cfg = state["config"]
    e = env()
    read = {"Authorization": f"Bearer {e['SENTINEL_API_READ_TOKEN']}"}
    demo_token = e["DEMO_INJECTION_TOKEN"]
    st = load(d, "status.json") or {
        "samples": 0,
        "ready_samples": 0,
        "injections": 0,
        "harness_gap_seconds": 0.0,
        "violations": [],
    }
    last = st.get("last_sample_epoch")
    if last is not None:  # resumed: account for the time nobody was observing
        gap = max(0.0, time.time() - float(last) - cfg["sample_seconds"])
        st["harness_gap_seconds"] = round(st["harness_gap_seconds"] + gap, 1)
        append(
            d, "events.jsonl", {"at": now_iso(), "event": "resumed", "gap_seconds": round(gap, 1)}
        )
    inject(demo_token, "none")
    next_inject = time.time() + 60
    next_check = time.time() + cfg["check_every_minutes"] * 60
    i = int(st.get("injections", 0))
    deadline = float(state["deadline_epoch"])
    while time.time() < deadline:
        now = time.time()
        if now >= next_inject and now < deadline - 300:  # last 5 min: let things settle
            mode, dur = SCHEDULE[i % len(SCHEDULE)]
            code = inject(demo_token, mode, dur)
            append(
                d,
                "events.jsonl",
                {
                    "at": now_iso(),
                    "event": "inject",
                    "mode": mode,
                    "self_healing_after_s": dur,
                    "http": code,
                },
            )
            i += 1
            next_inject = now + cfg["inject_every_minutes"] * 60
        ready, _ = http("GET", f"{API}/health/ready")
        _, status = http("GET", f"{API}/v1/system/status", headers=read)
        try:
            backlog = int(
                psql("SELECT count(*) FROM outbox_events WHERE published_at IS NULL") or 0
            )
        except (subprocess.SubprocessError, ValueError):
            backlog = -1
        sample = {
            "t": now_iso(),
            "elapsed_s": round(now - state["started_epoch"], 1),
            "ready": ready == 200,
            "overall": status.get("overall") if isinstance(status, dict) else None,
            "modes": status.get("modes") if isinstance(status, dict) else None,
            "outbox_unpublished": backlog,
        }
        append(d, "samples.jsonl", sample)
        st.update(
            phase="running",
            samples=st["samples"] + 1,
            ready_samples=st["ready_samples"] + int(sample["ready"]),
            injections=i,
            max_outbox_unpublished=max(st.get("max_outbox_unpublished", 0), backlog),
            last_sample_at=sample["t"],
            last_sample_epoch=now,
            elapsed_minutes=round((now - state["started_epoch"]) / 60, 2),
        )
        if now >= next_check:
            try:
                snap = invariants(state, final=False)
                append(d, "checks.jsonl", snap)
                bad = [k for k, v in snap["ok"].items() if not v]
                st["last_check"] = {"at": snap["at"], "violations": bad}
                if bad:
                    st["violations"].append({"at": snap["at"], "invariants": bad})
            except (subprocess.SubprocessError, ValueError, KeyError) as exc:
                append(
                    d,
                    "events.jsonl",
                    {
                        "at": now_iso(),
                        "event": "check_failed",
                        "error": type(exc).__name__,
                    },
                )
            next_check = now + cfg["check_every_minutes"] * 60
        save(d, "status.json", st)
        time.sleep(cfg["sample_seconds"])
    inject(demo_token, "none")
    time.sleep(90)  # let in-flight remediation/reporting settle
    ended = time.time()
    final = invariants(state, final=True)
    result = {
        "run_id": state["run_id"],
        "started_at": state["started_at"],
        "ended_at": datetime.fromtimestamp(ended, UTC).isoformat(),
        "requested_minutes": state["requested_minutes"],
        "actual_elapsed_minutes": round((ended - state["started_epoch"]) / 60, 2),
        "harness_gap_minutes": round(st["harness_gap_seconds"] / 60, 2),
        "samples": st["samples"],
        "uptime_ready_ratio": round(st["ready_samples"] / max(1, st["samples"]), 5),
        "injections": i,
        "max_outbox_unpublished": st.get("max_outbox_unpublished", 0),
        "violations_during_run": st["violations"],
        "final": final,
        "invariants": final["ok"],
    }
    save(d, "result.json", result)
    st.update(phase="completed", ended_at=result["ended_at"])
    save(d, "status.json", st)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "started_at",
                    "ended_at",
                    "actual_elapsed_minutes",
                    "uptime_ready_ratio",
                    "invariants",
                )
            },
            indent=2,
        )
    )
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    d = ROOT / a.dir
    state, st = load(d, "state.json"), load(d, "status.json")
    if not state:
        print("no run in this directory", file=sys.stderr)
        return 2
    alive = pid_alive(state.get("pid"))
    done = (d / "result.json").exists()
    phase = "completed" if done else ("running" if alive else "interrupted")
    now = time.time()
    last_epoch = st.get("last_sample_epoch")
    out = {
        "run_id": state["run_id"],
        "phase": phase,
        "started_at": state["started_at"],
        "deadline": datetime.fromtimestamp(state["deadline_epoch"], UTC).isoformat(),
        "requested_minutes": state["requested_minutes"],
        "wall_elapsed_minutes": round((now - state["started_epoch"]) / 60, 2),
        "remaining_minutes": round(max(0, state["deadline_epoch"] - now) / 60, 2),
        "harness_pid": state.get("pid"),
        "harness_alive": alive,
        "last_sample_at": st.get("last_sample_at"),
        "last_sample_age_seconds": round(now - last_epoch, 1) if last_epoch else None,
        "samples": st.get("samples", 0),
        "uptime_ready_ratio": round(st.get("ready_samples", 0) / max(1, st.get("samples", 0)), 5),
        "injections": st.get("injections", 0),
        "harness_gap_minutes": round(st.get("harness_gap_seconds", 0) / 60, 2),
        "last_invariant_check": st.get("last_check"),
        "violations_so_far": st.get("violations", []),
    }
    print(json.dumps(out, indent=2))
    if phase == "interrupted":
        print(
            f"harness not running: uv run python scripts/soak.py resume --dir {a.dir}",
            file=sys.stderr,
        )
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    d = ROOT / a.dir
    state, res = load(d, "state.json"), load(d, "result.json")
    if not res:
        print(
            json.dumps(
                {
                    "verdict": "NOT COMPLETE",
                    "reason": "no result.json: the run has not reached its deadline",
                },
                indent=2,
            )
        )
        return 1
    reasons = []
    if res["actual_elapsed_minutes"] < res["requested_minutes"]:
        reasons.append("elapsed shorter than requested")
    if res["harness_gap_minutes"] > state["config"]["max_gap_minutes"]:
        reasons.append(f"harness was not observing for {res['harness_gap_minutes']} min")
    bad = [k for k, v in res["invariants"].items() if not v]
    if bad:
        reasons.append(f"final invariants violated: {bad}")
    if res["violations_during_run"]:
        reasons.append(f"{len(res['violations_during_run'])} periodic check(s) saw violations")
    verdict = "PASS" if not reasons else "FAIL"
    print(
        json.dumps(
            {
                "verdict": verdict,
                "reasons": reasons,
                "started_at": res["started_at"],
                "ended_at": res["ended_at"],
                "actual_elapsed_minutes": res["actual_elapsed_minutes"],
                "uptime_ready_ratio": res["uptime_ready_ratio"],
                "invariants": res["invariants"],
            },
            indent=2,
        )
    )
    return 0 if verdict == "PASS" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--minutes", type=float, required=True)
    s.add_argument("--inject-every", type=float, default=5.0)
    s.add_argument("--sample-seconds", type=float, default=30.0)
    s.add_argument(
        "--check-every", type=float, default=15.0, help="minutes between invariant checks"
    )
    s.add_argument(
        "--max-gap-minutes",
        type=float,
        default=30.0,
        help="total harness downtime tolerated by verify",
    )
    s.add_argument("--dir", required=True)
    for name in ("run", "resume", "status", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True)
    a = ap.parse_args()
    return {
        "start": cmd_start,
        "run": cmd_run,
        "resume": cmd_resume,
        "status": cmd_status,
        "verify": cmd_verify,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
