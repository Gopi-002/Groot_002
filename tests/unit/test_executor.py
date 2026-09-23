import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.executor import signing
from app.executor.ledger import Ledger
from app.executor.main import ExecSettings, create_executor_app
from tests.support.fake_docker import FakeDocker

TOKEN, KEY = "t" * 40, "k" * 40
FP = "a" * 64
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def env(tmp_path):
    docker = FakeDocker()
    settings = ExecSettings(
        token=TOKEN,
        signing_key=KEY,
        max_restarts_per_hour=3,
        ledger_path=tmp_path / "ledger.sqlite3",
    )
    app = create_executor_app(settings, docker.client(), Ledger(settings.ledger_path))
    return TestClient(app), docker, tmp_path


def body(action_id=None, token=1, fp=FP, ttl=30, key=KEY, **over):
    aid = action_id or uuid.uuid4()
    na = (datetime.now(UTC) + timedelta(seconds=ttl)).replace(microsecond=0)
    b = {
        "action_id": str(aid),
        "fencing_token": token,
        "action_fingerprint": fp,
        "not_after": na.isoformat(),
        "signature": signing.sign(SecretStr(key), aid, token, fp, na),
    }
    b.update(over)
    return b


def restart(client, b):
    return client.post("/v1/actions/restart-demo-app", json=b, headers=H)


def test_valid_request_restarts_once_and_records_ledger(env):
    client, docker, _ = env
    b = body()
    r = restart(client, b)
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert r.json()["replayed"] is False and docker.restarts == 1
    assert r.json()["pre_state"]["started_at"] != r.json()["post_state"]["started_at"]
    assert "SECRET" not in r.text
    got = client.get(f"/v1/actions/{b['action_id']}", headers=H).json()
    assert got["status"] == "completed"


def test_duplicate_request_replays_without_second_restart(env):
    client, docker, _ = env
    b = body()
    restart(client, b)
    r2 = restart(client, b)
    assert r2.status_code == 200 and r2.json()["replayed"] is True
    assert docker.restarts == 1


def test_stale_fencing_token_refused(env):
    client, docker, _ = env
    aid = uuid.uuid4()
    restart(client, body(action_id=aid, token=2))
    r = restart(client, body(action_id=aid, token=1))
    assert r.status_code == 409 and r.json()["reason"] == "stale" and docker.restarts == 1


def test_fingerprint_change_refused(env):
    client, docker, _ = env
    aid = uuid.uuid4()
    restart(client, body(action_id=aid))
    r = restart(client, body(action_id=aid, token=2, fp="b" * 64))
    assert r.status_code == 409 and r.json()["reason"] == "fingerprint_mismatch"
    assert docker.restarts == 1


@pytest.mark.parametrize(
    ("mut", "detail"),
    [
        ({"key": "x" * 40}, "invalid authorization"),
        ({"ttl": -5}, "authorization expired"),
        ({"ttl": 5000}, "lifetime too long"),
    ],
)
def test_forged_expired_or_overlong_authorization_refused(env, mut, detail):
    client, docker, _ = env
    r = restart(client, body(**mut))
    assert r.status_code == 403 and detail in r.text and docker.restarts == 0


def test_tampered_signed_field_refused(env):
    client, docker, _ = env
    b = body()
    b["fencing_token"] = 99
    assert restart(client, b).status_code == 403 and docker.restarts == 0


def test_request_cannot_choose_target(env):
    client, docker, _ = env
    r = restart(client, body(container="postgres"))
    assert r.status_code == 422 and docker.restarts == 0


def test_requires_token(env):
    client, docker, _ = env
    assert client.post("/v1/actions/restart-demo-app", json=body()).status_code == 401
    assert client.get("/v1/target/state").status_code == 401
    assert docker.restarts == 0


def test_hourly_rate_limit(env):
    client, docker, _ = env
    for _ in range(3):
        restart(client, body())
    r = restart(client, body())
    assert r.status_code == 429 and docker.restarts == 3


def test_docker_failure_recorded_as_failed(tmp_path):
    docker = FakeDocker(fail_restart=True)
    s = ExecSettings(token=TOKEN, signing_key=KEY, ledger_path=tmp_path / "l.sqlite3")
    client = TestClient(create_executor_app(s, docker.client(), Ledger(s.ledger_path)))
    r = restart(client, body())
    assert r.status_code == 200 and r.json()["status"] == "failed" and r.json()["error"]


def test_in_progress_action_is_not_redone(env):
    client, docker, tmp = env
    aid = uuid.uuid4()
    Ledger(tmp / "ledger.sqlite3").reserve(str(aid), 1, FP, {}, 10)  # crashed mid-action
    r = restart(client, body(action_id=aid))
    assert r.status_code == 409 and r.json()["reason"] == "in_progress" and docker.restarts == 0


def test_ambiguous_target_unavailable(tmp_path):
    docker = FakeDocker(containers=2)
    s = ExecSettings(token=TOKEN, signing_key=KEY, ledger_path=tmp_path / "l.sqlite3")
    client = TestClient(create_executor_app(s, docker.client(), Ledger(s.ledger_path)))
    assert restart(client, body()).status_code == 503 and docker.restarts == 0


def test_ledger_survives_process_restart(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    aid = uuid.uuid4()
    Ledger(path).reserve(str(aid), 1, FP, {"a": 1}, 10)
    Ledger(path).finish(str(aid), "completed", {"b": 2}, None)
    e = Ledger(path).get(str(aid))
    assert e.status == "completed" and e.pre_state == {"a": 1} and e.post_state == {"b": 2}


def test_test_hook_forbidden_in_production(tmp_path):
    with pytest.raises(ValueError, match="forbidden in production"):
        ExecSettings(
            token=TOKEN, signing_key=KEY, environment="production", test_pre_restart_delay_seconds=5
        )
