"""DEV/TEST-ONLY webhook receiver used as the "configured test channel".

Not part of SentinelOps. It sits on the internal ``notify_net`` (no published
port, no egress), verifies the HMAC signature when ``SINK_SECRET`` is set,
de-duplicates by ``Idempotency-Key`` (as a real receiver should), and appends
each received delivery to a JSON-lines file on tmpfs for tests to inspect.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

LOG = Path(os.environ.get("SINK_LOG", "/tmp/notify-sink.jsonl"))  # noqa: S108 - tmpfs


def create_sink() -> FastAPI:
    app = FastAPI(title="notify-sink (dev/test only)", docs_url=None, redoc_url=None)
    secret = os.environ.get("SINK_SECRET", "")
    seen: set[str] = set()
    lock = threading.Lock()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/hook")
    async def hook(
        request: Request,
        idempotency_key: str | None = Header(default=None),
        x_sentinelops_timestamp: str | None = Header(default=None),
        x_sentinelops_signature: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await request.body()
        if secret:
            expected = (
                "sha256="
                + hmac.new(
                    secret.encode(),
                    (x_sentinelops_timestamp or "").encode() + b"." + body,
                    hashlib.sha256,
                ).hexdigest()
            )
            if not hmac.compare_digest(expected, x_sentinelops_signature or ""):
                raise HTTPException(status_code=401, detail="bad signature")
        payload = json.loads(body)
        with lock:
            duplicate = (idempotency_key or "") in seen
            seen.add(idempotency_key or "")
            with LOG.open("a") as f:  # noqa: ASYNC230 - tiny append on tmpfs, test-only
                f.write(
                    json.dumps(
                        {
                            "received_at": time.time(),
                            "idempotency_key": idempotency_key,
                            "duplicate": duplicate,
                            "signed": bool(x_sentinelops_signature),
                            "type": payload.get("type"),
                            "event": payload,
                        }
                    )
                    + "\n"
                )
        return {"accepted": True, "duplicate": duplicate}

    return app


def app_factory() -> FastAPI:
    return create_sink()
