#!/usr/bin/env bash
# Restore a backup produced by scripts/backup.sh. DESTRUCTIVE for PostgreSQL:
# replaces the current database objects with the backup's.
#
#   CONFIRM_RESTORE=yes scripts/restore.sh backups/<UTC timestamp>
#
# Safety rules (never make an executed action look new):
#   * the executor ledger is MERGED, never overwritten: action ids are only added
#     or advanced (started -> completed/failed), whichever copy is newer;
#   * every action PostgreSQL records as started/executed is then tombstoned into
#     the ledger, so an older ledger snapshot cannot re-enable a restart;
#   * all writers are stopped during the restore; Redis is left as transport only
#     (stale messages for finished tasks are acknowledged as duplicates, and the
#     reconcilers re-dispatch any unfinished work from PostgreSQL).
# Legacy single-file dumps (backups/*.dump from Phase 1-4) are still accepted
# for PostgreSQL only.
set -euo pipefail
cd "$(dirname "$0")/.."
src="${1:?usage: CONFIRM_RESTORE=yes scripts/restore.sh backups/<dir>|<file>.dump}"
if [[ "${CONFIRM_RESTORE:-}" != "yes" ]]; then
  echo "Refusing: set CONFIRM_RESTORE=yes to overwrite the sentinelops database." >&2
  exit 2
fi
actor="${SUDO_USER:-${USER:-operator}}"

if [[ -f "$src" ]]; then  # legacy PostgreSQL-only dump
  dump="$src"; ledger=""
elif [[ -f "$src/manifest.json" ]]; then
  python3 - "$src" <<'PY' || { echo "manifest checksum mismatch: refusing" >&2; exit 3; }
import hashlib, json, os, sys
d = sys.argv[1]
m = json.load(open(os.path.join(d, "manifest.json")))
for name, meta in m["files"].items():
    h = hashlib.sha256(open(os.path.join(d, name), "rb").read()).hexdigest()
    if h != meta["sha256"]:
        sys.exit(f"checksum mismatch for {name}")
print(f"manifest ok: schema {m['alembic_version']}, created {m['created_at']}")
PY
  dump="$src/postgres.dump"; ledger="$src/executor-ledger.sqlite3"
else
  echo "no such backup: $src" >&2; exit 1
fi

writers=(worker dispatcher monitor notifier api executor)
echo "stopping writers: ${writers[*]}"
docker compose stop "${writers[@]}" >/dev/null

docker compose exec -T postgres pg_restore -U sentinelops -d sentinelops --clean --if-exists \
  --single-transaction < "$dump"
echo "postgres restored from $dump"

if [[ -n "$ledger" ]]; then
  docker compose run --rm --no-deps -T executor python -m app.executor.ledger_tool merge < "$ledger"
fi
# Tombstones from PostgreSQL's record of started/executed actions.
docker compose exec -T postgres psql -U sentinelops -d sentinelops -tA -c \
  "SELECT json_build_object('action_id', action_id, 'status', status, 'fencing_token',
          fencing_token, 'action_fingerprint', action_fingerprint, 'started_at', started_at,
          'completed_at', completed_at) FROM action_attempts" \
  | docker compose run --rm --no-deps -T executor python -m app.executor.ledger_tool mark-executed

docker compose exec -T postgres psql -U sentinelops -d sentinelops -v ON_ERROR_STOP=1 -tA -c \
  "INSERT INTO audit_events (actor_type, actor_id, action, entity_type, details)
   VALUES ('human', '$actor', 'restore_completed', 'backup', jsonb_build_object('source', '$src'))" >/dev/null

docker compose up -d --wait "${writers[@]}" >/dev/null
echo "restore complete from $src; schema $(docker compose exec -T postgres psql -U sentinelops -d sentinelops -tA -c 'SELECT version_num FROM alembic_version')"
docker compose exec -T executor python -m app.executor.ledger_tool verify
