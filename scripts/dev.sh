#!/usr/bin/env bash
# One-shot local bring-up: Postgres (docker or local service), migrations, seeds, api + worker.
set -euo pipefail
cd "$(dirname "$0")/.."
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://postgres:dev@localhost:5432/kintsua}"
if command -v pg_isready >/dev/null && pg_isready -q; then
  echo "postgres: local server up"
elif command -v docker >/dev/null; then
  docker start kintsua-pg 2>/dev/null || docker run -d --name kintsua-pg -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=kintsua -p 5432:5432 postgres:16
  until pg_isready -h localhost -q 2>/dev/null; do sleep 1; done
else
  echo "need postgres (local or docker)" >&2; exit 1
fi
uv run python -c "import psycopg; psycopg.connect('${DATABASE_URL/+psycopg/}'.rsplit('/',1)[0]+'/postgres', autocommit=True).execute('CREATE DATABASE kintsua')" 2>/dev/null || true
uv run alembic upgrade head
uv run python -m kintsua.seed
uv run python -m kintsua.worker &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT
uv run python -m kintsua.api
