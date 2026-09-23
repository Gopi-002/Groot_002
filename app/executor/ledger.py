"""Durable action ledger (SQLite, on the executor's own volume).

Gives the executor its own at-most-once guarantee per action id, independent of
the worker and PostgreSQL: the ``started`` row is committed BEFORE the Docker
call, so after any crash the executor can report whether it ever attempted the
restart. A request carrying a lower fencing token than one already seen for the
action is rejected (fencing at the side-effect boundary)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    action_id     TEXT PRIMARY KEY,
    status        TEXT NOT NULL CHECK (status IN ('started','completed','failed')),
    fencing_token INTEGER NOT NULL,
    fingerprint   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    pre_state     TEXT,
    post_state    TEXT,
    error         TEXT
);
"""


@dataclass(frozen=True)
class LedgerEntry:
    action_id: str
    status: str
    fencing_token: int
    fingerprint: str
    started_at: datetime
    completed_at: datetime | None
    pre_state: dict[str, Any] | None
    post_state: dict[str, Any] | None
    error: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "status": self.status,
            "fencing_token": self.fencing_token,
            "fingerprint": self.fingerprint,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "pre_state": self.pre_state,
            "post_state": self.post_state,
            "error": self.error,
        }


def _now() -> datetime:
    return datetime.now(UTC)


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row(r: tuple[Any, ...]) -> LedgerEntry:
        return LedgerEntry(
            action_id=r[0],
            status=r[1],
            fencing_token=int(r[2]),
            fingerprint=r[3],
            started_at=datetime.fromisoformat(r[4]),
            completed_at=datetime.fromisoformat(r[5]) if r[5] else None,
            pre_state=json.loads(r[6]) if r[6] else None,
            post_state=json.loads(r[7]) if r[7] else None,
            error=r[8],
        )

    def get(self, action_id: str) -> LedgerEntry | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
        return self._row(r) if r else None

    def reserve(
        self,
        action_id: str,
        fencing_token: int,
        fingerprint: str,
        pre_state: dict[str, Any],
        max_per_hour: int,
    ) -> tuple[str, LedgerEntry | None]:
        """Atomically decide whether this request may execute.
        Returns (verdict, existing) with verdict in:
        'execute' | 'replay' (already finished) | 'in_progress' | 'stale' |
        'fingerprint_mismatch' | 'rate_limited'."""
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
                if r is not None:
                    e = self._row(r)
                    c.execute("ROLLBACK")
                    if e.fingerprint != fingerprint:
                        return "fingerprint_mismatch", e
                    if fencing_token < e.fencing_token:
                        return "stale", e
                    if e.status in ("completed", "failed"):
                        return "replay", e
                    return "in_progress", e
                since = (_now() - timedelta(hours=1)).isoformat()
                (recent,) = c.execute(
                    "SELECT count(*) FROM actions WHERE started_at >= ?", (since,)
                ).fetchone()
                if recent >= max_per_hour:
                    c.execute("ROLLBACK")
                    return "rate_limited", None
                c.execute(
                    "INSERT INTO actions (action_id, status, fencing_token, fingerprint, "
                    "started_at, pre_state) VALUES (?, 'started', ?, ?, ?, ?)",
                    (
                        action_id,
                        fencing_token,
                        fingerprint,
                        _now().isoformat(),
                        json.dumps(pre_state),
                    ),
                )
                c.execute("COMMIT")
                return "execute", None
            except Exception:
                c.execute("ROLLBACK")
                raise

    def finish(
        self, action_id: str, status: str, post_state: dict[str, Any] | None, error: str | None
    ) -> LedgerEntry:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE actions SET status=?, completed_at=?, post_state=?, error=? "
                "WHERE action_id=? AND status='started'",
                (
                    status,
                    _now().isoformat(),
                    json.dumps(post_state) if post_state else None,
                    error,
                    action_id,
                ),
            )
        entry = self.get(action_id)
        assert entry is not None
        return entry
