"""A small in-memory Docker Engine API for executor tests (GET inspect + POST
restart for ONE labelled container). Counts restarts so tests can prove
'no duplicate restart'."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx


class FakeDocker:
    def __init__(self, containers: int = 1, fail_restart: bool = False) -> None:
        self.containers = containers
        self.fail_restart = fail_restart
        self.restarts = 0
        self.started_at = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
        self.requests: list[httpx.Request] = []

    def started_iso(self) -> str:
        return self.started_at.isoformat().replace("+00:00", ".123456789Z")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p = request.url.path
        if p == "/containers/json":
            flt = json.loads(request.url.params["filters"])
            assert "com.docker.compose.service=demo-app" in flt["label"]
            return httpx.Response(200, json=[{"Id": f"c{i}"} for i in range(self.containers)])
        if request.method == "POST" and p.endswith("/restart"):
            if self.fail_restart:
                return httpx.Response(500, json={"message": "restart failed"})
            self.restarts += 1
            self.started_at += timedelta(minutes=1)
            return httpx.Response(204)
        if p.endswith("/json"):
            return httpx.Response(
                200,
                json={
                    "State": {
                        "Status": "running",
                        "Running": True,
                        "StartedAt": self.started_iso(),
                        "Health": {"Status": "healthy"},
                    },
                    "RestartCount": self.restarts,
                    "Config": {"Env": ["SECRET=nope"]},
                },
            )
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url="http://docker")
