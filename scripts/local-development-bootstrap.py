"""Create deterministic local identities and a short-lived example token."""
# ruff: noqa: EM101, EM102, INP001, PLR2004, TRY003

from __future__ import annotations

import hashlib
import os
from base64 import urlsafe_b64decode
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.machine_identity import (
    BootstrapScope,
    MachineCredentialRepository,
    WorkspaceLimit,
)

SERVICE_ID = "0198a080-0000-7000-8000-000000000101"
WORKSPACE_IDS = (
    "0198a080-0000-7000-8000-000000000102",
    "0198a080-0000-7000-8000-000000000103",
)
STATE_DIRECTORY = Path("/local-state")


def main() -> None:
    """Seed stable scopes and refresh the example host token."""
    database_url = _required_environment("LLMROUTER_DATABASE_URL")
    digest_key = _secret_bytes(Path("/run/secrets/machine_digest_key"))
    current = datetime.now(UTC)
    with psycopg.connect(database_url) as connection:
        _seed_scopes(connection)
        has_generation = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM router.service_bootstrap_generations "
            "WHERE service_id = %s)",
            (SERVICE_ID,),
        ).fetchone() == (True,)
    machine = MachineCredentialRepository(
        database_url,
        issuer="llmrouter-local-development",
        digest_keys={"local-v1": digest_key},
        current_digest_key_id="local-v1",
    )
    bootstrap_path = STATE_DIRECTORY / "service-bootstrap"
    if not has_generation:
        created = machine.create_initial_bootstrap(
            _administrator_context(current),
            SERVICE_ID,
            BootstrapScope(
                audiences=frozenset({Audience.HOST_BACKEND}),
                operations=frozenset({"admin_embed.create"}),
                workspace_limit=WorkspaceLimit.EXPLICIT_ONLY,
            ),
            now=current,
        )
        _write_secret(bootstrap_path, created.secret.value)
    elif not bootstrap_path.is_file() or bootstrap_path.is_symlink():
        raise SystemExit(
            "The local bootstrap secret is unavailable. Run the reset command."
        )
    token = machine.exchange(
        request_id="local-example-token",
        service_id=SERVICE_ID,
        bootstrap_secret=bootstrap_path.read_text(encoding="ascii").strip(),
        audience=Audience.HOST_BACKEND,
        operations=frozenset({"admin_embed.create"}),
        workspace_ids=frozenset(WORKSPACE_IDS),
        now=current,
    )
    _write_secret(STATE_DIRECTORY / "example-host-token", token.access_token.value)
    print("Local development identities are ready.")


def _seed_scopes(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        """
        INSERT INTO router.services (id, stable_name, display_name)
        VALUES (%s, 'local-example-service', 'Local example service')
        ON CONFLICT (id) DO NOTHING
        """,
        (SERVICE_ID,),
    )
    for index, workspace_id in enumerate(WORKSPACE_IDS, start=1):
        connection.execute(
            """
            INSERT INTO router.workspaces (
                id, service_id, caller_reference, creation_idempotency_key,
                creation_fingerprint, display_name
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                workspace_id,
                SERVICE_ID,
                f"local-workspace-{index}",
                f"local-workspace-create-{index}",
                hashlib.sha256(f"local-workspace-{index}".encode()).digest(),
                f"Local workspace {index}",
            ),
        )


def _administrator_context(current: datetime) -> RequestContext:
    return RequestContext(
        request_id="local-bootstrap-create",
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="local-development-setup",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation="credential.manage",
        scope=Scope(),
        authorized_at=current,
        recent_authentication_at=current,
        mutation=True,
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise SystemExit(f"{name} is required.")
    return value


def _secret_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("A local deployment key is unavailable.")
    try:
        value = urlsafe_b64decode(path.read_text(encoding="ascii").strip() + "=")
    except ValueError as error:
        raise SystemExit("A local deployment key is invalid.") from error
    if len(value) != 32:
        raise SystemExit("A local deployment key is invalid.")
    return value


def _write_secret(path: Path, value: str) -> None:
    if path.is_symlink():
        raise SystemExit("A local secret path is unsafe.")
    path.write_text(value + "\n", encoding="ascii")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
