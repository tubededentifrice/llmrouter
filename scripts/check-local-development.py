"""Check the localhost development deployment contract."""
# ruff: noqa: EM101, INP001, TRY003

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
EXPECTED_IMAGE_COUNT = 3


def main() -> None:
    """Reject public ports, floating images, and obsolete runtime roles."""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    required = {"postgres", "migrate", "backend", "node-dependencies", "admin-dev"}
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
        "LLMROUTER_OIDC_CLIENT_ID_FILE: /run/secrets/oidc_client_id",
        "LLMROUTER_OIDC_CLIENT_SECRET_FILE: /run/secrets/oidc_client_secret",
        "LLMROUTER_ADMINISTRATOR_SUBJECTS_FILE: /run/secrets/administrator_subjects",
        "file: .local-development/pocket-id-client-id",
        "file: .local-development/pocket-id-client-secret",
        "file: .local-development/pocket-id-administrator-subjects",
    }
    if any(value not in text for value in required_identity_inputs):
        raise SystemExit("The Pocket ID deployment inputs are not preserved.")
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
        "traefik.http.routers.llmrouter-dev.middlewares=badger@file",
        "traefik-proxy:\n    external: true",
    }
    if any(value not in text for value in required_proxy):
        raise SystemExit("The protected development route is not configured.")
    print("Local development deployment checks passed.")


if __name__ == "__main__":
    main()
