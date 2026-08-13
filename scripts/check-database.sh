#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
postgres_image="postgres@sha256:5c855ad7b85e68e48a62f34662853f38b57c1c1d80f3a927ab58034fd6d31c5e"
container_name=""

cleanup() {
  if [[ -n "${container_name}" ]]; then
    docker rm --force "${container_name}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ -z "${LLMROUTER_TEST_DATABASE_URL:-}" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker or LLMROUTER_TEST_DATABASE_URL is required for database checks." >&2
    exit 1
  fi
  container_name="llmrouter-postgres-$RANDOM-$$"
  docker run --detach --rm \
    --name "${container_name}" \
    --publish 127.0.0.1::5432 \
    --env POSTGRES_PASSWORD=postgres \
    "${postgres_image}" >/dev/null

  for _ in {1..60}; do
    if docker exec "${container_name}" pg_isready --username postgres >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if ! docker exec "${container_name}" pg_isready --username postgres >/dev/null 2>&1; then
    echo "The isolated PostgreSQL server did not become ready." >&2
    exit 1
  fi
  database_port="$(docker port "${container_name}" 5432/tcp | sed -n 's/^127\.0\.0\.1://p')"
  if [[ -z "${database_port}" ]]; then
    echo "The isolated PostgreSQL port is not available on localhost." >&2
    exit 1
  fi
  export LLMROUTER_TEST_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:${database_port}/postgres"
fi

cd "${repository_root}"
uv run --all-packages pytest packages/backend-role/tests/database

echo "Database checks passed."
