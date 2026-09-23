import json
import struct

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ops_reader.docker import DockerReader, DockerUnavailable, demux
from app.ops_reader.main import OpsSettings, create_ops_app

TOKEN = "o" * 40
CID = "abc123"


def frame(stream, text):
    data = text.encode()
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(data)) + data


def docker_handler(containers=1, requests=None):
    def handler(request: httpx.Request):
        if requests is not None:
            requests.append(request)
        assert request.method == "GET"
        p = request.url.path
        if p == "/containers/json":
            flt = json.loads(request.url.params["filters"])
            assert "com.docker.compose.service=demo-app" in flt["label"]
            return httpx.Response(200, json=[{"Id": f"{CID}{i}"} for i in range(containers)])
        if p.endswith("/json"):
            return httpx.Response(
                200,
                json={
                    "State": {
                        "Status": "running",
                        "Running": True,
                        "OOMKilled": False,
                        "ExitCode": 0,
                        "StartedAt": "2026-09-22T00:00:00Z",
                        "Health": {"Status": "healthy", "FailingStreak": 0},
                    },
                    "RestartCount": 2,
                    "Config": {"Env": ["DEMO_INJECTION_TOKEN=supersecretvalue123"]},
                },
            )
        if p.endswith("/logs"):
            body = frame(1, '2026-09-22T00:00:01Z {"level": "ERROR", "msg": "boom"}\n') + frame(
                2, "2026-09-22T00:00:02Z password=hunter2 leaked\n"
            )
            return httpx.Response(200, content=body)
        if p.endswith("/stats"):
            return httpx.Response(
                200,
                json={
                    "read": "now",
                    "cpu_stats": {
                        "cpu_usage": {"total_usage": 2_000},
                        "system_cpu_usage": 20_000,
                        "online_cpus": 2,
                    },
                    "precpu_stats": {
                        "cpu_usage": {"total_usage": 1_000},
                        "system_cpu_usage": 10_000,
                    },
                    "memory_stats": {"usage": 50, "limit": 200},
                    "pids_stats": {"current": 7},
                },
            )
        return httpx.Response(404)

    return handler


def reader(**kw):
    return DockerReader(
        httpx.Client(transport=httpx.MockTransport(docker_handler(**kw)), base_url="http://docker"),
        project="sentinelops",
        service="demo-app",
    )


def test_status_never_returns_env_or_config():
    s = reader().status()
    assert s["running"] is True and s["restart_count"] == 2 and s["health_status"] == "healthy"
    assert "supersecretvalue123" not in json.dumps(s) and "Env" not in json.dumps(s)


def test_logs_demuxed_and_redacted():
    lines = reader().logs(10)
    assert [entry["stream"] for entry in lines] == ["stdout", "stderr"]
    assert "hunter2" not in json.dumps(lines) and "[REDACTED]" in lines[1]["line"]


def test_stats_cpu_and_memory():
    s = reader().stats()
    assert s["cpu_percent"] == 20.0 and s["memory_percent"] == 25.0 and s["pids"] == 7


@pytest.mark.parametrize("n", [0, 2])
def test_ambiguous_or_missing_target_unavailable(n):
    with pytest.raises(DockerUnavailable, match="exactly one"):
        reader(containers=n).status()


def test_demux_tty_fallback():
    assert demux(b"plain text\n") == [("stdout", "plain text\n")]


def app_client(seen=None):
    settings = OpsSettings(token=TOKEN)
    docker = httpx.Client(
        transport=httpx.MockTransport(docker_handler(requests=seen)), base_url="http://docker"
    )
    metrics = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "uptime_seconds": 5,
                    "requests_total": 9,
                    "health_failures_total": 3,
                    "simulated_memory_mb": 544.0,
                    "failure_mode": "memory_log",
                    "failure_injection_available": True,
                },
            )
        )
    )
    return TestClient(create_ops_app(settings, docker, metrics))


@pytest.mark.parametrize(
    "path", ["/v1/target/status", "/v1/target/logs", "/v1/target/stats", "/v1/target/app-metrics"]
)
def test_ops_endpoints_require_token(path):
    c = app_client()
    assert c.get(path).status_code == 401
    assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_ops_service_is_get_only_and_bounded():
    c = app_client()
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert c.post("/v1/target/status", headers=h).status_code == 405
    assert c.get("/v1/target/logs?tail=100000", headers=h).status_code == 422
    assert c.get("/v1/containers/other/status", headers=h).status_code == 404


def test_app_metrics_withhold_test_injection_fields():
    body = (
        app_client()
        .get("/v1/target/app-metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        .json()
    )
    assert body["data"]["simulated_memory_mb"] == 544.0
    assert "failure_mode" not in body["data"] and "failure_injection_available" not in body["data"]


def test_docker_calls_are_get_only():
    seen = []
    c = app_client(seen)
    h = {"Authorization": f"Bearer {TOKEN}"}
    for p in ("status", "logs", "stats"):
        c.get(f"/v1/target/{p}", headers=h)
    assert seen and all(r.method == "GET" for r in seen)
