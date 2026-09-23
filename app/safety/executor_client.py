"""Worker-side client for the restricted executor. Every restart request is
signed for exactly one action id / fencing token / fingerprint and expires
quickly. Transport failures mean the outcome is UNKNOWN - callers must
reconcile, never blindly re-issue."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from app.executor import signing


class ExecutorUnavailable(Exception):
    """No response: whether the action ran is unknown until reconciled."""


class ExecutorRefused(Exception):
    def __init__(self, reason: str, body: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.body = body


class ExecutorClient:
    def __init__(
        self,
        base_url: str,
        token: SecretStr | None,
        signing_key: SecretStr | None,
        timeout_seconds: float,
        auth_ttl_seconds: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token
        self.signing_key = signing_key
        self.auth_ttl = auth_ttl_seconds
        self.client = client or httpx.Client(
            base_url=base_url, timeout=timeout_seconds, trust_env=False, follow_redirects=False
        )

    @property
    def configured(self) -> bool:
        return self.token is not None and self.signing_key is not None

    def _headers(self) -> dict[str, str]:
        assert self.token is not None
        return {"Authorization": f"Bearer {self.token.get_secret_value()}"}

    def state(self) -> dict[str, Any]:
        try:
            resp = self.client.get("/v1/target/state", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ExecutorUnavailable(f"executor unreachable: {type(exc).__name__}") from exc
        body = resp.json() if resp.status_code == 200 else {}
        if body.get("status") != "ok":
            raise ExecutorUnavailable(
                f"target state unavailable: {body.get('reason') or resp.status_code}"
            )
        data: dict[str, Any] = body["data"]
        return data

    def get_action(self, action_id: uuid.UUID) -> dict[str, Any] | None:
        try:
            resp = self.client.get(f"/v1/actions/{action_id}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ExecutorUnavailable(f"executor unreachable: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ExecutorUnavailable(f"ledger lookup HTTP {resp.status_code}")
        body: dict[str, Any] = resp.json()
        return body

    def restart(self, action_id: uuid.UUID, fencing_token: int, fingerprint: str) -> dict[str, Any]:
        assert self.signing_key is not None
        not_after = (datetime.now(UTC) + timedelta(seconds=self.auth_ttl)).replace(microsecond=0)
        body = {
            "action_id": str(action_id),
            "fencing_token": fencing_token,
            "action_fingerprint": fingerprint,
            "not_after": not_after.isoformat(),
            "signature": signing.sign(
                self.signing_key, action_id, fencing_token, fingerprint, not_after
            ),
        }
        try:
            resp = self.client.post(
                "/v1/actions/restart-demo-app", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise ExecutorUnavailable(f"no response from executor: {type(exc).__name__}") from exc
        data = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if resp.status_code == 200:
            return dict(data)
        if resp.status_code in (409, 429, 403):
            reason = str(
                data.get("reason") or (data.get("error") or {}).get("message") or resp.status_code
            )
            raise ExecutorRefused(reason, dict(data))
        raise ExecutorUnavailable(f"executor HTTP {resp.status_code}")
