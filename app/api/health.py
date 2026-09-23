"""Liveness vs readiness.

* ``/health/live``  — process is up and serving. Never touches dependencies,
  so a DB outage does not cause the orchestrator to kill a healthy process.
* ``/health/ready`` — DB reachable, schema at migration head, Redis reachable.
  Returns 503 with per-check detail (no secrets, no exception text).
  Checks run in parallel under one overall deadline, so a hung dependency
  (e.g. DNS stalls for a stopped container) cannot make the probe hang.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("sentinelops.api.health")

Check = Callable[[], None]  # raises on failure


@dataclass
class ReadinessChecks:
    checks: dict[str, Check] = field(default_factory=dict)
    timeout_seconds: float = 3.0
    # Bounded pool: repeatedly hung checks queue up and time out instead of
    # spawning unbounded threads.
    _pool: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=4, thread_name_prefix="ready"),
        repr=False,
    )

    def run(self) -> tuple[bool, dict[str, str]]:
        futures: dict[str, Future[None]] = {
            name: self._pool.submit(check) for name, check in self.checks.items()
        }
        wait(futures.values(), timeout=self.timeout_seconds)
        results: dict[str, str] = {}
        for name, fut in futures.items():
            if not fut.done():
                fut.cancel()
                results[name] = "timeout"
                log.warning("readiness check timed out", extra={"check": name})
            elif (exc := fut.exception()) is not None:
                results[name] = "fail"
                log.warning(
                    "readiness check failed",
                    extra={"check": name, "error_type": type(exc).__name__},
                )
            else:
                results[name] = "ok"
        return all(v == "ok" for v in results.values()), results


router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    checks: ReadinessChecks = request.app.state.readiness
    ok, results = checks.run()
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": results},
        status_code=200 if ok else 503,
    )
