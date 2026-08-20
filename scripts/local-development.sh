#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repository_root}/docker-compose.dev.yml"
state_directory="${repository_root}/.local-development"

fail() {
  echo "$1" >&2
  exit 1
}

require_tools() {
  command -v docker >/dev/null 2>&1 || fail "Docker is required."
  docker compose version >/dev/null 2>&1 || fail "Docker Compose is required."
}

validate_environment() {
  if [[ "${LLMROUTER_BIND_ADDRESS:-127.0.0.1}" != "127.0.0.1" ]]; then
    fail "The local development deployment can bind only to 127.0.0.1."
  fi
  if [[ -L "${state_directory}" ]]; then
    fail "The local development state directory must not be a symbolic link."
  fi
}

create_secret() {
  local target="$1"
  local length="$2"
  if [[ -e "${target}" ]]; then
    [[ -f "${target}" && ! -L "${target}" ]] || fail "A local secret path is unsafe."
    chmod 600 "${target}"
    return
  fi
  uv run python -c "import secrets; print(secrets.token_urlsafe(${length}))" >"${target}"
  chmod 600 "${target}"
}

create_empty_secret() {
  local target="$1"
  if [[ -e "${target}" ]]; then
    [[ -f "${target}" && ! -L "${target}" ]] || fail "A local secret path is unsafe."
  else
    : >"${target}"
  fi
  chmod 600 "${target}"
}

prepare_state() {
  umask 077
  mkdir -p "${state_directory}"
  chmod 700 "${state_directory}"
  create_secret "${state_directory}/postgres-password" 32
  create_secret "${state_directory}/machine-digest-key" 32
  create_empty_secret "${state_directory}/example-host-token"
}

compose() {
  docker compose --project-directory "${repository_root}" -f "${compose_file}" "$@"
}

wait_until_ready() {
  local deadline=$((SECONDS + 180))
  while ((SECONDS < deadline)); do
    if curl --fail --silent --show-error --max-time 2 \
      http://127.0.0.1:8010/ready >/dev/null 2>&1 &&
      curl --fail --silent --show-error --max-time 2 \
        http://127.0.0.1:5174/ >/dev/null 2>&1 &&
      curl --fail --silent --show-error --max-time 2 \
        http://127.0.0.1:5176/ >/dev/null 2>&1; then
      echo "LLM Router is ready on localhost."
      echo "Administration: http://127.0.0.1:5174"
      echo "Embed example: http://127.0.0.1:5176"
      return
    fi
    sleep 2
  done
  compose ps >&2
  fail "The local development deployment did not become ready."
}

main() {
  local action="${1:-start}"
  validate_environment
  require_tools
  case "${action}" in
    start)
      prepare_state
      compose up --detach --remove-orphans
      compose restart embed-example >/dev/null
      wait_until_ready
      ;;
    stop)
      compose down --remove-orphans
      ;;
    reset)
      compose down --volumes --remove-orphans
      ;;
    status)
      compose ps
      ;;
    logs)
      compose logs --follow --tail 100
      ;;
    *)
      fail "Usage: ./scripts/local-development.sh {start|stop|reset|status|logs}"
      ;;
  esac
}

main "$@"
