"""Executor ledger backup/restore: consistent online snapshots, merges that
never downgrade, and PostgreSQL tombstones - a restore can never make an
executed action look safe to run again."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import uuid

import pytest

from app.executor import ledger_tool
from app.executor.ledger import Ledger


def seed(path, n_completed=2, started=1):
    led = Ledger(path)
    ids = []
    for i in range(n_completed + started):
        aid = str(uuid.uuid4())
        verdict, _ = led.reserve(aid, 1, "f" * 64, {"started_at": "a"}, max_per_hour=100)
        assert verdict == "execute"
        if i < n_completed:
            led.finish(aid, "completed", {"started_at": "b"}, None)
        ids.append(aid)
    return led, ids


def test_backup_is_a_consistent_self_contained_copy(tmp_path):
    src = tmp_path / "live.sqlite3"
    seed(src)
    dest = tmp_path / "backup.sqlite3"
    ledger_tool.backup_to(src, dest)
    assert ledger_tool.verify(dest) == {
        "integrity": "ok",
        "counts": {"completed": 2, "started": 1},
        "total": 3,
    }
    assert not (tmp_path / "backup.sqlite3-wal").exists()


def test_restore_after_ledger_loss_keeps_completed_actions_non_repeatable(tmp_path):
    live = tmp_path / "live.sqlite3"
    _, ids = seed(live)
    backup = tmp_path / "b.sqlite3"
    ledger_tool.backup_to(live, backup)
    live.unlink()  # executor volume lost
    fresh = tmp_path / "live.sqlite3"
    n = ledger_tool.apply(
        fresh, ledger_tool.merge_plan(ledger_tool.rows(fresh), ledger_tool.rows(backup).values())
    )
    assert n == 3
    led2 = Ledger(fresh)
    for aid in ids[:2]:
        verdict, entry = led2.reserve(aid, 5, "f" * 64, {}, max_per_hour=100)
        assert verdict == "replay" and entry is not None and entry.status == "completed"
    verdict, _ = led2.reserve(ids[2], 5, "f" * 64, {}, max_per_hour=100)
    assert verdict == "in_progress"  # interrupted action is never redone


def test_merge_never_downgrades_or_deletes(tmp_path):
    live = tmp_path / "live.sqlite3"
    _, ids = seed(live, n_completed=1, started=0)
    old = tmp_path / "old.sqlite3"
    # an OLDER backup where the same action was still 'started' and another is unknown
    conn = sqlite3.connect(old)
    conn.execute(ledger_tool.SCHEMA)
    conn.execute(
        "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)",
        (ids[0], "started", 1, "f" * 64, "2026-01-01", None, None, None, None),
    )
    conn.commit()
    conn.close()
    plan = ledger_tool.merge_plan(ledger_tool.rows(live), ledger_tool.rows(old).values())
    assert plan == []
    ledger_tool.apply(live, plan)
    assert ledger_tool.rows(live)[ids[0]]["status"] == "completed"


def test_postgres_tombstones_block_actions_missing_from_an_old_ledger(tmp_path):
    live = tmp_path / "live.sqlite3"
    Ledger(live)
    executed, interrupted, pending = (str(uuid.uuid4()) for _ in range(3))
    attempts = [
        {
            "action_id": executed,
            "status": "succeeded",
            "fencing_token": 3,
            "action_fingerprint": "a" * 64,
            "started_at": "t1",
            "completed_at": "t2",
        },
        {
            "action_id": interrupted,
            "status": "executing",
            "fencing_token": 2,
            "action_fingerprint": "b" * 64,
            "started_at": "t1",
            "completed_at": None,
        },
        {"action_id": pending, "status": "pending", "fencing_token": 1},
    ]
    n = ledger_tool.apply(
        live, ledger_tool.merge_plan(ledger_tool.rows(live), ledger_tool.tombstones(attempts))
    )
    assert n == 2
    led = Ledger(live)
    assert led.reserve(executed, 9, "a" * 64, {}, 100)[0] == "replay"
    assert led.reserve(interrupted, 9, "b" * 64, {}, 100)[0] == "in_progress"
    # a pending (never sent) attempt is not tombstoned: policy may still run it once
    assert led.get(pending) is None


def test_cli_backup_merge_roundtrip(tmp_path, monkeypatch, capsysbinary):
    live = tmp_path / "live.sqlite3"
    seed(live)
    monkeypatch.setenv("EXEC_LEDGER_PATH", str(live))
    assert ledger_tool.main(["x", "backup"]) == 0
    blob = capsysbinary.readouterr().out
    target = tmp_path / "restored.sqlite3"
    monkeypatch.setenv("EXEC_LEDGER_PATH", str(target))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(blob)))
    assert ledger_tool.main(["x", "merge"]) == 0
    out = json.loads(capsysbinary.readouterr().out)
    assert out["merged"] == 3 and out["integrity"] == "ok"


def test_cli_refuses_corrupt_backup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EXEC_LEDGER_PATH", str(tmp_path / "live.sqlite3"))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not a database" * 100)))
    with pytest.raises(sqlite3.DatabaseError):
        ledger_tool.main(["x", "merge"])
