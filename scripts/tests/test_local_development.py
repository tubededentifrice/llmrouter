"""Regression tests for the localhost development deployment."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = REPOSITORY_ROOT / "scripts/check-local-development.py"
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.dev.yml"
SUBJECTS_INPUT = (
    "LLMROUTER_ADMINISTRATOR_SUBJECTS_FILE: /run/secrets/administrator_subjects"
)


def _module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_local_development", CHECK_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_local_development_contract_accepts_repository_compose() -> None:
    """Accept the immutable loopback deployment."""
    _module().main()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("127.0.0.1:8010:8000", "8010:8000", "public port binding"),
        (
            "node:24.17.0-alpine@sha256:156b55f92e98ccd5ef49578a8cea0df4679826564bad1c9d4ef04462b9f0ded6",
            "node:24.17.0-alpine",
            "not immutable",
        ),
        (
            SUBJECTS_INPUT,
            "LLMROUTER_ADMINISTRATOR_SUBJECTS_FILE: missing",
            "Pocket ID deployment inputs",
        ),
    ],
)
def test_local_contract_rejects_unsafe_or_incomplete_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    """Reject one unsafe port, image, or identity input."""
    unsafe = tmp_path / "docker-compose.dev.yml"
    source = COMPOSE_PATH.read_text(encoding="utf-8")
    assert old in source
    unsafe.write_text(source.replace(old, new, 1), encoding="utf-8")
    module = _module()
    monkeypatch.setattr(module, "COMPOSE_PATH", unsafe)
    with pytest.raises(SystemExit, match=message):
        module.main()


def test_local_wrapper_preserves_identity_inputs_and_resets_database() -> None:
    """Keep Pocket ID inputs while the reset deletes database volumes."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    for name in (
        "pocket-id-client-id",
        "pocket-id-client-secret",
        "pocket-id-administrator-subjects",
    ):
        assert f'install_secret "${{state_directory}}/{name}"' in script
    reset = script[script.index("    reset)") : script.index("    status)")]
    assert "compose down --volumes --remove-orphans" in reset
    assert "state_directory" not in reset
    identity_check = script[script.index("  for target in") : script.index("  done")]
    assert "pocket-id-administrator-subjects" in identity_check
    assert 'if [[ "${configured}" == "3" ]]' in script
    assert script.index("prepare_secrets") < script.index('case "${action}" in')


def test_pocket_id_callback_matches_the_registered_exact_redirect() -> None:
    """Keep one callback that matches the existing Pocket ID client."""
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    contract = (REPOSITORY_ROOT / "docs/api/openapi.yaml").read_text(encoding="utf-8")
    callback = "/v1/admin/oidc/callback"
    assert f"https://llmrouter.opendle.dev{callback}" in compose
    assert f"  {callback}:" in contract
    assert "/v1/admin/session/callback" not in compose
    assert "/v1/admin/session/callback" not in contract


def test_local_wrapper_rejects_a_public_address_before_start() -> None:
    """Keep the wrapper loopback check explicit and early."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    assert script.index("validate_environment") < script.index("compose up")
    assert "LLMROUTER_BIND_ADDRESS:-127.0.0.1" in script
    assert "can bind only to 127.0.0.1" in script
    assert "flock --nonblock" in script


def test_local_start_reruns_migrations_before_the_application() -> None:
    """Stop the old application before each migration and restart."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    migration_command = (
        "compose up --detach --remove-orphans --force-recreate \\\n"
        "        object-storage migrate node-dependencies"
    )
    stop = script.index("compose stop admin-dev backend")
    migration = script.index(migration_command, stop)
    jobs_complete = script.index("compose wait migrate node-dependencies", migration)
    application_command = (
        "compose up --detach --remove-orphans --no-deps backend admin-dev"
    )
    application = script.index(application_command, jobs_complete)
    readiness = script.index("wait_until_ready", application)
    assert stop < migration < jobs_complete < application < readiness
