from __future__ import annotations

import os

import pytest

TEST_SECRETS = {
    "SENTINEL_DB_PASSWORD": "unit-db-pw-9f8e7d6c5b4a",
    "SENTINEL_REDIS_PASSWORD": "unit-redis-pw-1a2b3c4d5e6f",
}


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for k in list(os.environ):
        if k.startswith(("SENTINEL_", "DEMO_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in TEST_SECRETS.items():
        monkeypatch.setenv(k, v)
    return dict(TEST_SECRETS)
