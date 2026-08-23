"""Check the localhost development deployment contract."""
# ruff: noqa: EM101, INP001, PLR2004, TRY003

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.dev.yml"
IMMUTABLE_IMAGE = re.compile(r"^[^\s@]+(?:[:][^\s@]+)?@sha256:[0-9a-f]{64}$")
PORT_BINDING = re.compile(
    r"^\s+-\s+['\"]?(?P<binding>[^'\"\s#]*\d+:\d+(?:/tcp)?)['\"]?\s*$",
    re.MULTILINE,
)
EXPECTED_PORT_BINDINGS = {
    "127.0.0.1:5434:5432",
    "127.0.0.1:8010:8000",
    "127.0.0.1:5174:5173",
}


def main() -> None:
    """Reject public ports, floating images, and embedded secret values."""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    required = {
        "postgres",
        "migrate",
        "token-worker",
        "backend",
        "node-dependencies",
        "admin-dev",
    }
    missing = sorted(name for name in required if f"\n  {name}:\n" not in text)
    if missing:
        raise SystemExit("The local Compose service map is incomplete.")
    images = re.findall(r"^\s+image:\s+(\S+)$", text, re.MULTILINE)
    if len(images) != 3 or any(
        IMMUTABLE_IMAGE.fullmatch(image) is None for image in images
    ):
        raise SystemExit("A local development image is not immutable.")
    port_bindings = {
        match.group("binding").removesuffix("/tcp")
        for match in PORT_BINDING.finditer(text)
    }
    if port_bindings != EXPECTED_PORT_BINDINGS or re.search(
        r"^\s+published:\s*", text, re.MULTILINE
    ):
        raise SystemExit("A local development service has a public port binding.")
    forbidden = ("LLMROUTER_OPENROUTER_API_KEY:", "sk-or-", "OPENROUTER_API_KEY=")
    if any(value in text for value in forbidden):
        raise SystemExit("The local Compose file contains provider secret material.")
    required_runtime = {
        'LLMROUTER_LOCAL_RUNTIME: "1"',
        "LLMROUTER_LOCAL_OPENROUTER_LIVE: ${LLMROUTER_LOCAL_OPENROUTER_LIVE:-0}",
        "LLMROUTER_WRAPPING_KEY_FILE:",
        "LLMROUTER_IDEMPOTENCY_KEY_FILE:",
        "LLMROUTER_DISTRIBUTION_KEY_FILE:",
        "LLMROUTER_REPLAY_KEY_FILE:",
        "LLMROUTER_ADMIN_SESSION_FILE:",
        "LLMROUTER_ADMIN_CSRF_FILE:",
        "LLMROUTER_PUBLIC_ADMIN_AUTH:",
        "LLMROUTER_OIDC_CLIENT_ID_FILE:",
        "LLMROUTER_OIDC_CLIENT_SECRET_FILE:",
        "LLMROUTER_ADMIN_DIGEST_KEY_FILE:",
        "LLMROUTER_ADMIN_ENCRYPTION_KEY_FILE:",
    }
    if any(value not in text for value in required_runtime):
        raise SystemExit("The complete local runtime is not configured.")
    if (
        "UV_PROJECT_ENVIRONMENT: /python-environment/.venv" not in text
        or "llmrouter-python-environment:/python-environment" not in text
        or "llmrouter-python-environment:/workspace/.venv" in text
    ):
        raise SystemExit("The container Python environment is not isolated.")
    required_proxy = {
        "networks:\n      - default\n      - traefik-proxy\n    labels:",
        "traefik.enable=true",
        "traefik.docker.network=traefik-proxy",
        "traefik.http.routers.llmrouter-dev.rule=Host(`llmrouter.opendle.dev`)",
        "traefik.http.routers.llmrouter-dev.entrypoints=websecure",
        "traefik.http.routers.llmrouter-dev.tls=true",
        "traefik.http.routers.llmrouter-dev.tls.certresolver=letsencrypt",
        "traefik.http.routers.llmrouter-dev.middlewares=badger@file",
        "traefik.http.routers.llmrouter-dev.service=llmrouter-dev",
        "traefik.http.services.llmrouter-dev.loadbalancer.server.port=5173",
        "traefik-proxy:\n    external: true",
    }
    if any(value not in text for value in required_proxy):
        raise SystemExit("The protected development route is not configured.")
    required_secrets = {
        "credential-wrapping-key",
        "idempotency-digest-key",
        "distribution-key",
        "canonical-replay-key",
        "administrator-session",
        "administrator-csrf",
        "administrator-digest-key",
        "administrator-encryption-key",
        "pocket-id-client-id",
        "pocket-id-client-secret",
    }
    if any(f"file: .local-development/{name}" not in text for name in required_secrets):
        raise SystemExit("A generated local runtime secret is not configured.")
    print("Local development deployment checks passed.")


if __name__ == "__main__":
    main()
