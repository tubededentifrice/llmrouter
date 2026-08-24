#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_directory="${repository_root}/.local-development"
stopped_storage=""
stopped_postgres=""

cleanup() {
  if [[ -n "${stopped_storage}" ]]; then
    docker start "${stopped_storage}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${stopped_postgres}" ]]; then
    docker start "${stopped_postgres}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "$#" -ne 0 ]]; then
  echo "This proof command does not accept arguments." >&2
  exit 2
fi

for command in curl docker flock uv; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "${command} is required for the simplified product proof." >&2
    exit 1
  fi
done
if [[ ! -x /usr/bin/google-chrome ]]; then
  echo "Google Chrome is required for the hydrated administration proof." >&2
  exit 1
fi

lock_deployment() {
  local operation_lock="${state_directory}/.operation-lock"
  local inherited_lock
  if [[ -L "${state_directory}" ]]; then
    echo "The local development state directory must not be a symbolic link." >&2
    exit 1
  fi
  mkdir -p "${state_directory}"
  if [[ ! -d "${state_directory}" ]] || [[ -L "${state_directory}" ]]; then
    echo "The local development state directory is unsafe." >&2
    exit 1
  fi
  chmod 700 "${state_directory}"
  inherited_lock="$(readlink "/proc/$$/fd/9" 2>/dev/null || true)"
  if [[ "${inherited_lock}" != "${operation_lock}" ]]; then
    exec 9>>"${operation_lock}"
  fi
  if ! flock --nonblock 9; then
    echo "Another local development operation is active." >&2
    exit 1
  fi
}

find_container() {
  local service="$1"
  local found
  found="$(docker ps --all --quiet \
    --filter label=com.docker.compose.project=llmrouter-development \
    --filter "label=com.docker.compose.service=${service}")"
  if [[ -z "${found}" ]] || [[ "${found}" == *$'\n'* ]]; then
    echo "The ${service} localhost container is missing or ambiguous." >&2
    exit 1
  fi
  printf '%s' "${found}"
}

wait_for_url() {
  local url="$1"
  for _attempt in {1..90}; do
    if curl --fail --silent --show-error --max-time 2 "${url}" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  echo "The localhost endpoint did not become ready: ${url}" >&2
  exit 1
}

wait_for_health() {
  local container="$1"
  for _attempt in {1..90}; do
    if [[ "$(docker inspect --format '{{.State.Health.Status}}' "${container}" 2>/dev/null)" == "healthy" ]]; then
      return
    fi
    sleep 1
  done
  echo "A localhost dependency did not become healthy." >&2
  exit 1
}

cd "${repository_root}"
lock_deployment
unset LLMROUTER_TEST_DATABASE_URL
uv run python scripts/check-backend-reset.py
./scripts/check-database.sh

if [[ ! -f "${repository_root}/../opendle-lib/tests/test_router_client.py" ]]; then
  echo "The adjacent OpenDLE Lib checkout is required for SDK proof." >&2
  exit 1
fi
uv run --project "${repository_root}/../opendle-lib" --frozen pytest \
  --no-cov \
  "${repository_root}/../opendle-lib/tests/test_router_client.py"

./scripts/local-development.sh reset
./scripts/local-development.sh start
uv run python scripts/prove-localhost.py

backend_container="$(find_container backend)"
storage_container="$(find_container object-storage)"
postgres_container="$(find_container postgres)"

docker exec "${backend_container}" /python-environment/.venv/bin/python \
  scripts/verify-object-storage.py put
docker restart "${storage_container}" >/dev/null
wait_for_health "${storage_container}"
docker exec "${backend_container}" /python-environment/.venv/bin/python \
  scripts/verify-object-storage.py get
stopped_storage="${storage_container}"
docker stop "${storage_container}" >/dev/null
if docker exec "${backend_container}" /python-environment/.venv/bin/python \
  scripts/verify-object-storage.py failure; then
  :
else
  echo "The object-storage dependency-failure proof did not pass." >&2
  exit 1
fi
docker start "${storage_container}" >/dev/null
wait_for_health "${storage_container}"
stopped_storage=""

docker restart "${backend_container}" >/dev/null
docker restart "${storage_container}" >/dev/null
wait_for_health "${storage_container}"
wait_for_url "http://127.0.0.1:8010/ready"
docker exec "${backend_container}" /python-environment/.venv/bin/python \
  scripts/verify-object-storage.py get
curl --fail --silent --show-error --max-time 5 \
  "http://127.0.0.1:5174/" >/dev/null

stopped_postgres="${postgres_container}"
docker stop "${postgres_container}" >/dev/null
if curl --fail --silent --max-time 5 "http://127.0.0.1:8010/ready" >/dev/null; then
  echo "Readiness stayed successful while PostgreSQL was unavailable." >&2
  exit 1
fi
docker start "${postgres_container}" >/dev/null
wait_for_health "${postgres_container}"
wait_for_url "http://127.0.0.1:8010/ready"
stopped_postgres=""

docker exec "${backend_container}" /python-environment/.venv/bin/python \
  scripts/verify-object-storage.py delete

echo "Simplified product proof passed."
