"""Check the localhost development deployment contract."""
# ruff: noqa: EM101, INP001, TRY003

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.dev.yml"
STORAGE_CONFIG_PATH = REPOSITORY_ROOT / "scripts/local-development-object-storage.toml"
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
EXPECTED_IMAGE_COUNT = 4


def _check_storage_isolation() -> None:
    """Require each Garage listener to use its exact loopback address."""
    try:
        with STORAGE_CONFIG_PATH.open("rb") as stream:
            config = tomllib.load(stream)
    except OSError, tomllib.TOMLDecodeError:
        raise SystemExit(
            "The object-store endpoint is not isolated on loopback."
        ) from None
    expected = {
        ("rpc_bind_addr",): "127.0.0.1:3901",
        ("rpc_public_addr",): "127.0.0.1:3901",
        ("s3_api", "api_bind_addr"): "127.0.0.1:3900",
        ("admin", "api_bind_addr"): "127.0.0.1:3903",
    }
    for path, value in expected.items():
        current: object = config
        for name in path:
            if not isinstance(current, dict) or name not in current:
                raise SystemExit(
                    "The object-store endpoint is not isolated on loopback."
                )
            current = current[name]
        if current != value:
            raise SystemExit("The object-store endpoint is not isolated on loopback.")


def main() -> None:
    """Reject public ports, floating images, and obsolete runtime roles."""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    required = {
        "postgres",
        "object-storage-init",
        "object-storage",
        "migrate",
        "backend",
        "node-dependencies",
        "admin-dev",
    }
    missing = sorted(name for name in required if f"\n  {name}:\n" not in text)
    if missing:
        raise SystemExit("The local Compose service map is incomplete.")
    forbidden_services = {"token-worker", "control", "data-plane", "worker"}
    if any(f"\n  {name}:\n" in text for name in forbidden_services):
        raise SystemExit("The local Compose file contains an obsolete runtime role.")

    images = re.findall(r"^\s+image:\s+(\S+)$", text, re.MULTILINE)
    if len(images) != EXPECTED_IMAGE_COUNT or any(
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

    required_identity_inputs = {
        "LLMROUTER_PUBLIC_ADMIN_AUTH: ${LLMROUTER_PUBLIC_ADMIN_AUTH:-0}",
        "LLMROUTER_OIDC_ISSUER: https://auth.opendle.dev",
        (
            "LLMROUTER_OIDC_REDIRECT_URI: "
            "https://llmrouter.opendle.dev/v1/admin/oidc/callback"
        ),
        'LLMROUTER_ADMIN_SESSION_HOURS: "24"',
        (
            "LLMROUTER_ADMIN_ALLOWED_ORIGINS: "
            "http://127.0.0.1:5174,https://llmrouter.opendle.dev"
        ),
        "LLMROUTER_OIDC_CLIENT_ID_FILE: /run/secrets/oidc_client_id",
        "LLMROUTER_OIDC_CLIENT_SECRET_FILE: /run/secrets/oidc_client_secret",
        "LLMROUTER_ADMINISTRATOR_SUBJECTS_FILE: /run/secrets/administrator_subjects",
        "LLMROUTER_ADMIN_DIGEST_KEY_FILE: /run/secrets/administrator_digest_key",
        (
            "LLMROUTER_ADMIN_ENCRYPTION_KEY_FILE: "
            "/run/secrets/administrator_encryption_key"
        ),
        "file: .local-development/pocket-id-client-id",
        "file: .local-development/pocket-id-client-secret",
        "file: .local-development/pocket-id-administrator-subjects",
        "file: .local-development/administrator-digest-key",
        "file: .local-development/administrator-encryption-key",
    }
    if any(value not in text for value in required_identity_inputs):
        raise SystemExit("The Pocket ID deployment inputs are not preserved.")
    if (
        "UV_PROJECT_ENVIRONMENT: /python-environment/.venv" not in text
        or "llmrouter-python-environment:/python-environment" not in text
        or "llmrouter-python-environment:/workspace/.venv" in text
    ):
        raise SystemExit("The container Python environment is not isolated.")
    required_storage_inputs = {
        "GARAGE_DEFAULT_BUCKET: llmrouter-local",
        "LLMROUTER_OBJECT_STORE_ENDPOINT: http://127.0.0.1:3900",
        "LLMROUTER_OBJECT_STORE_BUCKET: llmrouter-local",
        "LLMROUTER_OBJECT_STORE_ACCESS_KEY_FILE: /run/secrets/object_store_access_key",
        "LLMROUTER_OBJECT_STORE_SECRET_KEY_FILE: /run/secrets/object_store_secret_key",
        "file: .local-development/object-store-access-key",
        "file: .local-development/object-store-secret-key",
    }
    if any(value not in text for value in required_storage_inputs):
        raise SystemExit("The private object-store deployment inputs are incomplete.")
    if 'network_mode: "service:backend"' not in text:
        raise SystemExit("The object-store endpoint is not isolated on loopback.")
    _check_storage_isolation()

    required_proxy = {
        "networks:\n      - default\n      - traefik-proxy\n    labels:",
        "traefik.enable=true",
        "traefik.docker.network=traefik-proxy",
        "traefik.http.routers.llmrouter-dev.rule=Host(`llmrouter.opendle.dev`)",
        "traefik.http.routers.llmrouter-dev.middlewares=badger@file",
        "traefik-proxy:\n    external: true",
    }
    if any(value not in text for value in required_proxy):
        raise SystemExit("The protected development route is not configured.")
    print("Local development deployment checks passed.")


if __name__ == "__main__":
    main()
