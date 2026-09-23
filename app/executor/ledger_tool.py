"""Executor ledger backup / restore (safety-critical state).

    python -m app.executor.ledger_tool backup  > ledger.sqlite3   # online, consistent
    python -m app.executor.ledger_tool verify                      # integrity + counts
    python -m app.executor.ledger_tool list                        # action ids + status (JSON)
    python -m app.executor.ledger_tool merge   < ledger.sqlite3   # restore (never downgrades)
    python -m app.executor.ledger_tool mark-executed < attempts.jsonl

Rules that keep a restore from ever re-enabling a restart:
* ``backup`` uses SQLite's online backup API (consistent snapshot of a live
  WAL database - never a raw file copy).
* ``merge`` is a UNION: entries are only added or advanced
  (started -> completed/failed); nothing is deleted or downgraded, whichever
  of the live ledger and the backup is newer.
* ``mark-executed`` adds tombstones for every action PostgreSQL knows was
  started/executed (from ``action_attempts``) but the ledger lacks, so an older
  ledger backup cannot make an executed action look new. A ``started``
  tombstone makes the executor refuse the action (``in_progress``), a
  ``completed`` one replays the recorded result.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.executor.ledger import SCHEMA, Ledger

RANK = {"started": 1, "completed": 2, "failed": 2}
PG_TO_LEDGER = {
    "succeeded": "completed",
    "reconciled": "completed",
    "failed": "failed",
    "executing": "started",
    "unknown": "started",
}
COLUMNS = (
    "action_id",
    "status",
    "fencing_token",
    "fingerprint",
    "started_at",
    "completed_at",
    "pre_state",
    "post_state",
    "error",
)


def ledger_path() -> Path:
    return Path(os.environ.get("EXEC_LEDGER_PATH", "/var/lib/sentinel-executor/ledger.sqlite3"))


def backup_to(src: Path, dest: Path) -> None:
    """Consistent online snapshot via sqlite3's backup API."""
    Ledger(src)  # ensure schema exists
    s, d = sqlite3.connect(src, timeout=10), sqlite3.connect(dest)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")  # single self-contained file
    finally:
        s.close()
        d.close()


def rows(path: Path) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(path)
    try:
        conn.execute(SCHEMA)
        cur = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM actions")  # noqa: S608 - constants
        return {r[0]: dict(zip(COLUMNS, r, strict=True)) for r in cur.fetchall()}
    finally:
        conn.close()


def merge_plan(
    live: dict[str, dict[str, Any]], incoming: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rows to upsert: new action ids, or strictly more final status. Pure."""
    out = []
    for row in incoming:
        cur = live.get(row["action_id"])
        if cur is None or RANK.get(row["status"], 0) > RANK.get(cur["status"], 0):
            out.append(row)
    return out


def apply(path: Path, upserts: list[dict[str, Any]]) -> int:
    Ledger(path)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for r in upserts:
            conn.execute(
                "INSERT INTO actions (action_id, status, fencing_token, fingerprint, started_at, "
                "completed_at, pre_state, post_state, error) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(action_id) DO UPDATE SET status=excluded.status, "
                "completed_at=excluded.completed_at, post_state=excluded.post_state, "
                "error=excluded.error, fencing_token=max(actions.fencing_token, "
                "excluded.fencing_token)",
                tuple(r[c] for c in COLUMNS),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return len(upserts)


def _iso(value: object, default: str) -> str:
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return default


def tombstones(attempts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ledger rows for PostgreSQL action attempts that were started or executed."""
    now = datetime.now(UTC).isoformat()
    out = []
    for a in attempts:
        status = PG_TO_LEDGER.get(str(a.get("status")))
        if status is None:  # 'pending' never reached the executor
            continue
        out.append(
            {
                "action_id": str(a["action_id"]),
                "status": status,
                "fencing_token": int(a.get("fencing_token") or 0),
                "fingerprint": str(a.get("action_fingerprint") or ""),
                "started_at": _iso(a.get("started_at"), now),
                "completed_at": _iso(a.get("completed_at"), now) if status != "started" else None,
                "pre_state": None,
                "post_state": None,
                "error": "restored from PostgreSQL action_attempts (tombstone)",
            }
        )
    return out


def verify(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.execute(SCHEMA)
        counts = dict(conn.execute("SELECT status, count(*) FROM actions GROUP BY 1").fetchall())
    finally:
        conn.close()
    return {"integrity": integrity, "counts": counts, "total": sum(counts.values())}


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    path = ledger_path()
    if cmd == "backup":
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ledger.sqlite3"
            backup_to(path, dest)
            sys.stdout.buffer.write(dest.read_bytes())
        return 0
    if cmd == "verify":
        print(json.dumps(verify(path)))
        return 0
    if cmd == "list":
        print(json.dumps({k: v["status"] for k, v in sorted(rows(path).items())}))
        return 0
    if cmd == "merge":
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "incoming.sqlite3"
            src.write_bytes(sys.stdin.buffer.read())
            if verify(src)["integrity"] != "ok":
                print("refusing: backup ledger failed integrity_check", file=sys.stderr)
                return 2
            n = apply(path, merge_plan(rows(path), rows(src).values()))
        print(json.dumps({"merged": n, **verify(path)}))
        return 0
    if cmd == "mark-executed":
        attempts = [json.loads(line) for line in sys.stdin if line.strip()]
        n = apply(path, merge_plan(rows(path), tombstones(attempts)))
        print(json.dumps({"tombstones_added": n, **verify(path)}))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
