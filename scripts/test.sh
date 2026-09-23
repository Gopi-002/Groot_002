#!/usr/bin/env bash
# Run test tiers against the local stack. Secrets are read from .env, never printed.
# usage: scripts/test.sh [unit|integration|resilience|all] [extra pytest args]
set -euo pipefail
cd "$(dirname "$0")/.."
tier="${1:-all}"; shift || true
set -a; . ./.env; set +a
export TEST_DATABASE_URL="postgresql+psycopg://sentinelops:${POSTGRES_PASSWORD}@127.0.0.1:55432/sentinelops"
export TEST_REDIS_PASSWORD="$REDIS_PASSWORD" TEST_REDIS_PORT=56379
# Every `docker compose` call (including inside resilience tests) uses the same files.
export COMPOSE_FILE="docker-compose.yml:docker-compose.test-ports.yml"
up() { docker compose up -d --build --wait >/dev/null 2>&1; }
case "$tier" in
  unit) uv run pytest tests/unit -q -p no:warnings "$@" ;;
  integration) up; uv run pytest tests/integration -q -p no:warnings "$@" ;;
  resilience) up; RUN_RESILIENCE=1 uv run pytest tests/resilience -q -p no:warnings "$@"; up ;;
  all) up; uv run pytest tests/unit tests/integration -q -p no:warnings "$@"
       RUN_RESILIENCE=1 uv run pytest tests/resilience -q -p no:warnings "$@"; up ;;
  *) echo "unknown tier: $tier" >&2; exit 2 ;;
esac
