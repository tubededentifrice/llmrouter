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
  local mode="${2:-600}"
  [[ -f "${target}" && ! -L "${target}" ]] || fail "A local secret path is unsafe."
  [[ "$(stat -c %h "${target}")" == "1" ]] || fail "A local secret path is unsafe."
  chmod "${mode}" "${target}"
}

install_secret() {
  local target="$1"
  local length="$2"
  local mode="${3:-600}"
  local temporary
  if [[ -e "${target}" ]]; then
    validate_secret "${target}" "${mode}"
    return
  fi
  temporary="$(mktemp "${state_directory}/.secret.XXXXXX")"
  if [[ "${length}" == "0" ]]; then
    : >"${temporary}"
  else
    if ! uv run python -c "import secrets; print(secrets.token_urlsafe(${length}))" >"${temporary}"; then
      rm -f "${temporary}"
      fail "A local secret could not be created."
    fi
  fi
  chmod "${mode}" "${temporary}"
  if ! ln "${temporary}" "${target}" 2>/dev/null; then
    rm -f "${temporary}"
    validate_secret "${target}" "${mode}"
    return
  fi
  rm -f "${temporary}"
  validate_secret "${target}" "${mode}"
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
  install_secret "${state_directory}/credential-wrapping-key" 32
  install_secret "${state_directory}/idempotency-digest-key" 32
  install_secret "${state_directory}/distribution-key" 32
  install_secret "${state_directory}/canonical-replay-key" 32
  install_secret "${state_directory}/administrator-session" 32
  install_secret "${state_directory}/administrator-csrf" 32
  install_secret "${state_directory}/administrator-digest-key" 32
  install_secret "${state_directory}/administrator-encryption-key" 32
  install_secret "${state_directory}/pocket-id-client-id" 0 400
  install_secret "${state_directory}/pocket-id-client-secret" 0 400
  install_secret "${state_directory}/data-plane-token" 0
}

compose() {
  local public_admin_auth=0
  local configured=0
  local target
  for target in pocket-id-client-id pocket-id-client-secret; do
    if [[ -s "${state_directory}/${target}" ]]; then
      configured=$((configured + 1))
    fi
  done
  if [[ "${configured}" == "2" ]]; then
    public_admin_auth=1
  elif [[ "${configured}" != "0" ]]; then
    fail "The Pocket ID client configuration is incomplete."
  fi
  env -u OPENROUTER_API_KEY LLMROUTER_PUBLIC_ADMIN_AUTH="${public_admin_auth}" docker compose \
    --project-directory "${repository_root}" -f "${compose_file}" "$@"
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
        http://127.0.0.1:5174/ >/dev/null 2>&1; then
      echo "The LLM Router localhost foundation is available."
      echo "Administration: http://127.0.0.1:5174"
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
      compose restart admin-dev backend >/dev/null
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
      if [[ -d "${state_directory}/backend-replay" ]]; then
        compose run --rm --no-deps backend /bin/sh -euc \
          'find /local-state/backend-replay -maxdepth 1 -type f \( -name "accounting-replay.bin" -o -name "accounting-replay.bin.lock" -o -name "accounting-replay.bin.owner" -o -name "accounting-replay.bin.state" -o -name ".accounting-replay.bin.*.tmp" \) -delete; rmdir /local-state/backend-replay' \
          >/dev/null
        compose down --volumes --remove-orphans >/dev/null
      fi
      ;;
    status)
      compose ps
      ;;
    logs)
      compose logs --follow --tail 100
      ;;
    live-openrouter|live-openrouter-mimo|live-openrouter-granite|live-openrouter-granite-stream)
      if [[ ! -v OPENROUTER_API_KEY ]]; then
        fail "OPENROUTER_API_KEY is required through an inherited secret environment input."
      fi
      model_arguments=()
      if [[ "$1" == "live-openrouter-mimo" ]]; then
        model_arguments=(--model mimo)
      elif [[ "$1" == "live-openrouter-granite" ]]; then
        model_arguments=(--model granite)
      elif [[ "$1" == "live-openrouter-granite-stream" ]]; then
        model_arguments=(--model granite --stream-only)
      fi
      trap 'env -u OPENROUTER_API_KEY "${repository_root}/scripts/local-development.sh" reset >/dev/null 2>&1 || true' EXIT
      env -u OPENROUTER_API_KEY \
        "${repository_root}/scripts/local-development.sh" reset
      env -u OPENROUTER_API_KEY LLMROUTER_LOCAL_OPENROUTER_LIVE=1 \
        "${repository_root}/scripts/local-development.sh" start
      uv run --package llmrouter-backend python \
        scripts/local-development-live-openrouter.py "${model_arguments[@]}"
      env -u OPENROUTER_API_KEY \
        "${repository_root}/scripts/local-development.sh" reset
      trap - EXIT
      ;;
    *)
      fail "Usage: ./scripts/local-development.sh {start|stop|reset|status|logs|live-openrouter|live-openrouter-mimo|live-openrouter-granite|live-openrouter-granite-stream}"
      ;;
  esac
}

main "$@"
