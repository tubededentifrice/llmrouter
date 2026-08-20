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
  command -v curl >/dev/null 2>&1 || fail "curl is required."
  command -v docker >/dev/null 2>&1 || fail "Docker is required."
  docker compose version >/dev/null 2>&1 || fail "Docker Compose is required."
  command -v flock >/dev/null 2>&1 || fail "flock is required."
  command -v uv >/dev/null 2>&1 || fail "uv is required."
}

validate_environment() {
  if [[ "${LLMROUTER_BIND_ADDRESS:-127.0.0.1}" != "127.0.0.1" ]]; then
    fail "The local development deployment can bind only to 127.0.0.1."
  fi
  if [[ -L "${state_directory}" ]]; then
    fail "The local development state directory must not be a symbolic link."
  fi
}

validate_secret() {
  local target="$1"
  [[ -f "${target}" && ! -L "${target}" ]] || fail "A local secret path is unsafe."
  [[ "$(stat -c %h "${target}")" == "1" ]] || fail "A local secret path is unsafe."
  chmod 600 "${target}"
}

install_secret() {
  local target="$1"
  local length="$2"
  local temporary
  if [[ -e "${target}" ]]; then
    validate_secret "${target}"
    return
  fi
  temporary="$(mktemp "${state_directory}/.secret.XXXXXX")"
  chmod 600 "${temporary}"
  if [[ "${length}" == "0" ]]; then
    : >"${temporary}"
  else
    if ! uv run python -c "import secrets; print(secrets.token_urlsafe(${length}))" >"${temporary}"; then
      rm -f "${temporary}"
      fail "A local secret could not be created."
    fi
  fi
  if ! ln "${temporary}" "${target}" 2>/dev/null; then
    rm -f "${temporary}"
    validate_secret "${target}"
    return
  fi
  rm -f "${temporary}"
  validate_secret "${target}"
}

prepare_state_directory() {
  umask 077
  mkdir -p "${state_directory}"
  [[ -d "${state_directory}" && ! -L "${state_directory}" ]] ||
    fail "The local development state directory is unsafe."
  chmod 700 "${state_directory}"
}

prepare_secrets() {
  install_secret "${state_directory}/postgres-password" 32
  install_secret "${state_directory}/machine-digest-key" 32
  install_secret "${state_directory}/example-host-token" 0
}

compose() {
  docker compose --project-directory "${repository_root}" -f "${compose_file}" "$@"
}

cleanup_failed_start() {
  local status="$?"
  if [[ "${status}" != "0" && "${cleanup_new_deployment:-0}" == "1" ]]; then
    compose down --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "${status}"
}

lock_operation() {
  exec 9>>"${state_directory}/.operation-lock"
  flock --nonblock 9 || fail "Another local development operation is active."
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
      echo "The LLM Router localhost foundation is available."
      echo "Administration: http://127.0.0.1:5174"
      echo "Embed example: http://127.0.0.1:5176"
      echo "Runtime components: http://127.0.0.1:8010/ready"
      return
    fi
    sleep 2
  done
  compose ps >&2
  fail "The local development deployment did not become ready."
}

main() {
  local action="${1:-start}"
  local cleanup_new_deployment=0
  validate_environment
  require_tools
  prepare_state_directory
  case "${action}" in
    start)
      lock_operation
      trap cleanup_failed_start EXIT
      prepare_secrets
      if [[ -z "$(compose ps --quiet)" ]]; then
        cleanup_new_deployment=1
      fi
      compose up --detach --remove-orphans
      compose restart embed-example >/dev/null
      wait_until_ready
      cleanup_new_deployment=0
      trap - EXIT
      ;;
    stop)
      lock_operation
      compose down --remove-orphans
      ;;
    reset)
      lock_operation
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
