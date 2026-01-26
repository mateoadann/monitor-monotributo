#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

docker compose up -d postgres
docker compose build web

until docker compose exec -T postgres pg_isready -U monitor; do
  sleep 1
done

exists=$(docker compose exec -T postgres psql -U monitor -tc "SELECT 1 FROM pg_database WHERE datname='monitor_test';" | tr -d '[:space:]')
if [ "$exists" != "1" ]; then
  docker compose exec -T postgres psql -U monitor -c "CREATE DATABASE monitor_test;"
fi

docker compose run --rm -e DATABASE_URL_TEST=postgresql+psycopg2://monitor:monitor@postgres:5432/monitor_test web python -m pytest
