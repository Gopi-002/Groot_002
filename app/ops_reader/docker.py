"""Minimal read-only Docker Engine client for ONE allowlisted container.

Only three GET endpoints are ever called (inspect, logs, stats) for the single
container carrying the configured compose project/service labels. Environment,
mounts and config are never returned - other containers' secrets live there.
"""

from __future__ import annotations

import json
import struct
from typing import Any

import httpx

from app.observability.logging import redact_text

MAX_LINE_CHARS = 500


class DockerUnavailable(Exception):
    pass


def demux(raw: bytes) -> list[tuple[str, str]]:
    """Split Docker's multiplexed log stream into (stream, text) chunks. Falls
    back to raw text for TTY containers (no frame headers)."""
    out: list[tuple[str, str]] = []
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00":
        i = 0
        while i + 8 <= len(raw):
            stream_type = raw[i]
            (size,) = struct.unpack(">I", raw[i + 4 : i + 8])
            chunk = raw[i + 8 : i + 8 + size]
            i += 8 + size
            name = {1: "stdout", 2: "stderr"}.get(stream_type, "stdin")
            out.append((name, chunk.decode("utf-8", errors="replace")))
        return out
    return [("stdout", raw.decode("utf-8", errors="replace"))]


class DockerReader:
    def __init__(self, client: httpx.Client, *, project: str, service: str) -> None:
        self.client = client
        self.project = project
        self.service = service

    def _get(self, path: str, **params: Any) -> httpx.Response:
        try:
            resp = self.client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise DockerUnavailable(f"docker api unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise DockerUnavailable(f"docker api returned {resp.status_code}")
        return resp

    def container_id(self) -> str:
        filters = json.dumps(
            {
                "label": [
                    f"com.docker.compose.project={self.project}",
                    f"com.docker.compose.service={self.service}",
                ]
            }
        )
        found = self._get("/containers/json", all="true", filters=filters).json()
        if len(found) != 1:
            raise DockerUnavailable(f"expected exactly one target container, found {len(found)}")
        return str(found[0]["Id"])

    def status(self) -> dict[str, Any]:
        info = self._get(f"/containers/{self.container_id()}/json").json()
        state = info.get("State") or {}
        health = state.get("Health") or {}
        return {
            "service": self.service,
            "state": state.get("Status"),
            "running": state.get("Running"),
            "restarting": state.get("Restarting"),
            "oom_killed": state.get("OOMKilled"),
            "exit_code": state.get("ExitCode"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "restart_count": info.get("RestartCount"),
            "health_status": health.get("Status"),
            "health_failing_streak": health.get("FailingStreak"),
        }

    def logs(self, tail: int, since: int | None = None) -> list[dict[str, str]]:
        params: dict[str, str] = {
            "stdout": "1",
            "stderr": "1",
            "tail": str(tail),
            "timestamps": "1",
        }
        if since is not None:
            params["since"] = str(since)
        raw = self._get(f"/containers/{self.container_id()}/logs", **params).content
        lines: list[dict[str, str]] = []
        for stream, text in demux(raw):
            for line in text.splitlines():
                ts, _, msg = line.partition(" ")
                msg = redact_text(msg)
                if len(msg) > MAX_LINE_CHARS:
                    msg = msg[:MAX_LINE_CHARS] + "...[truncated]"
                lines.append({"ts": ts, "stream": stream, "line": msg})
        return lines[-tail:]

    def stats(self) -> dict[str, Any]:
        s = self._get(f"/containers/{self.container_id()}/stats", stream="false").json()
        mem = s.get("memory_stats") or {}
        cpu, pre = s.get("cpu_stats") or {}, s.get("precpu_stats") or {}
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get(
            "total_usage", 0
        )
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or 1
        cpu_pct = round(cpu_delta / sys_delta * ncpu * 100, 3) if sys_delta > 0 else None
        usage, limit = mem.get("usage"), mem.get("limit")
        return {
            "cpu_percent": cpu_pct,
            "memory_usage_bytes": usage,
            "memory_limit_bytes": limit,
            "memory_percent": round(usage / limit * 100, 3) if usage and limit else None,
            "pids": (s.get("pids_stats") or {}).get("current"),
            "read_at": s.get("read"),
        }
