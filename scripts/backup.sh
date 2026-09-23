#!/usr/bin/env bash
# Back up ALL authoritative SentinelOps state into ./backups/<UTC timestamp>/:
#   postgres.dump          - pg_dump custom format (incidents, tasks, evidence,
#                            investigations, policy, approvals, actions,
#                            verifications, reports, notifications, audit, ...)
#   executor-ledger.sqlite3 - the executor's at-most-once action ledger, taken with
#                            SQLite's ONLINE BACKUP API (consistent; never a raw copy)
#   manifest.json          - sha256 + size of each file, schema version, ledger counts
# NOT included (by design): Redis (transport only; rebuilt from PostgreSQL by the
# outbox/reconcilers), the ai_secrets volume (a credential - re-enter it with
# `onboard set-key`), demo_state (demo-only). Every run is recorded in the
# backup_runs table (drives the backup_failed alert) and the audit trail.
# Copy the directory OFF this host: a local backup does not survive host loss.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

ts="$(date -u +%Y%m%dT%H%M%SZ)"
dir="backups/$ts"
mkdir -p "$dir"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
actor="${SUDO_USER:-${USER:-operator}}"

psql_c() { docker compose exec -T postgres psql -U sentinelops -d sentinelops -v ON_ERROR_STOP=1 -tA -c "$1"; }

record() { # status, artifacts-json, error
  psql_c "INSERT INTO backup_runs (started_at, completed_at, status, artifacts, error)
          VALUES ('$started', now(), '$1', CAST('$2' AS jsonb), NULLIF('$3',''));
          INSERT INTO audit_events (actor_type, actor_id, action, entity_type, details)
          VALUES ('human', '$actor', 'backup_$1', 'backup', jsonb_build_object('dir', '$dir'));" \
    >/dev/null || echo "WARNING: could not record backup run in PostgreSQL" >&2
}
fail() { record failed '{}' "$1"; echo "BACKUP FAILED: $1" >&2; exit 1; }
trap 'fail "unexpected error at line $LINENO"' ERR

psql_c "INSERT INTO audit_events (actor_type, actor_id, action, entity_type, details)
        VALUES ('human', '$actor', 'backup_started', 'backup', jsonb_build_object('dir', '$dir'))" >/dev/null

# 1. PostgreSQL (authoritative)
docker compose exec -T postgres pg_dump -U sentinelops -d sentinelops -Fc > "$dir/postgres.dump"
docker compose exec -T postgres pg_restore --list < "$dir/postgres.dump" > /dev/null \
  || fail "pg_dump archive is not readable"
schema="$(psql_c 'SELECT version_num FROM alembic_version')"

# 2. Executor ledger (safety-critical): online, transaction-consistent snapshot
docker compose exec -T executor python -m app.executor.ledger_tool backup > "$dir/executor-ledger.sqlite3"
ledger="$(python3 - "$dir/executor-ledger.sqlite3" <<'PY'
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
counts = dict(c.execute("SELECT status, count(*) FROM actions GROUP BY 1").fetchall())
print(json.dumps({"integrity": ok, "counts": counts}))
PY
)"
[[ "$ledger" == *'"integrity": "ok"'* ]] || fail "ledger snapshot failed integrity_check"

# 3. Manifest with checksums
python3 - "$dir" "$schema" "$ledger" "$started" <<'PY'
import hashlib, json, os, sys
d, schema, ledger, started = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]
files = {}
for name in ("postgres.dump", "executor-ledger.sqlite3"):
    p = os.path.join(d, name)
    files[name] = {"sha256": hashlib.sha256(open(p, "rb").read()).hexdigest(), "bytes": os.path.getsize(p)}
json.dump({"format": 1, "created_at": started, "alembic_version": schema, "files": files,
           "executor_ledger": ledger,
           "excluded": ["redis (rebuildable)", "ai_secrets (credential)", "demo_state (demo only)"]},
          open(os.path.join(d, "manifest.json"), "w"), indent=2)
PY
chmod 600 "$dir"/*

artifacts="$(python3 -c "import json,sys; m=json.load(open('$dir/manifest.json')); print(json.dumps({'dir': '$dir', 'alembic_version': m['alembic_version'], 'files': {k: v['bytes'] for k, v in m['files'].items()}, 'ledger_actions': sum(m['executor_ledger']['counts'].values())}))")"
trap - ERR
record succeeded "$artifacts" ""
echo "backup written: $dir"
cat "$dir/manifest.json"
