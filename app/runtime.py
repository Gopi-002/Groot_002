"""Process plumbing shared by long-running services: graceful stop and a
heartbeat file used by container health checks (no HTTP port needed)."""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path

log = logging.getLogger("sentinelops.runtime")

HEARTBEAT_DIR = Path("/tmp")  # noqa: S108 - tmpfs in containers; not a security boundary


class StopFlag:
    def __init__(self) -> None:
        self._event = threading.Event()

    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: object) -> None:
            log.info("stop signal received", extra={"signal": signum})
            self._event.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; returns True if stop was requested."""
        return self._event.wait(max(0.0, seconds))


def beat(service: str) -> None:
    (HEARTBEAT_DIR / f"{service}.heartbeat").write_text(str(time.time()))


def heartbeat_age(service: str) -> float:
    path = HEARTBEAT_DIR / f"{service}.heartbeat"
    return time.time() - float(path.read_text())
