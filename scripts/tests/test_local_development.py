"""Regression tests for the localhost development deployment."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = REPOSITORY_ROOT / "scripts/check-local-development.py"
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.dev.yml"
STORAGE_CONFIG_PATH = REPOSITORY_ROOT / "scripts/local-development-object-storage.toml"
VITE_CONFIG_PATH = REPOSITORY_ROOT / "apps/admin/vite.config.ts"
ADMIN_PATH = REPOSITORY_ROOT / "apps/admin"
LOCAL_JAVASCRIPT_IMPORT = re.compile(
    r'''(?:from\s+|import\s+)["']\.\.?/[^"']+\.js["']'''
)
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


def _local_javascript_imports(admin_path: Path) -> list[str]:
    offenders: list[str] = []
    for source_directory in (admin_path / "src", admin_path / "test"):
        for path in sorted(source_directory.rglob("*.ts*")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if LOCAL_JAVASCRIPT_IMPORT.search(line):
                    offenders.append(f"{path.relative_to(admin_path)}:{line_number}")
    return offenders


def test_local_development_contract_accepts_repository_compose() -> None:
    """Accept the immutable loopback deployment."""
    _module().main()


def test_admin_imports_use_typescript_source_paths() -> None:
    """Keep Vite imports on stable TypeScript source paths."""
    config = json.loads((ADMIN_PATH / "tsconfig.json").read_text(encoding="utf-8"))
    assert config["compilerOptions"]["allowImportingTsExtensions"] is True
    assert _local_javascript_imports(ADMIN_PATH) == []


def test_admin_import_check_rejects_javascript_source_path(tmp_path: Path) -> None:
    """Reject an emitted JavaScript path in a TypeScript source file."""
    source_directory = tmp_path / "src"
    source_directory.mkdir()
    (source_directory / "broken.ts").write_text(
        'import { value } from "./value.js";\n', encoding="utf-8"
    )
    assert _local_javascript_imports(tmp_path) == ["src/broken.ts:1"]


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
        (
            'LLMROUTER_MAXIMUM_REQUEST_BODY_BYTES: "73400320"',
            "LLMROUTER_MAXIMUM_REQUEST_BODY_BYTES: missing",
            "deployment operation limits",
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


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            'rpc_bind_addr = "127.0.0.1:3901"',
            'rpc_bind_addr = "0.0.0.0:3901"',
        ),
        (
            'rpc_public_addr = "127.0.0.1:3901"',
            'rpc_public_addr = "0.0.0.0:3901"',
        ),
        (
            'api_bind_addr = "127.0.0.1:3900"',
            'api_bind_addr = "0.0.0.0:3900"',
        ),
        (
            'api_bind_addr = "127.0.0.1:3903"',
            'api_bind_addr = "0.0.0.0:3903"',
        ),
        ('api_bind_addr = "127.0.0.1:3903"\n', ""),
    ],
)
def test_local_contract_rejects_unsafe_or_missing_storage_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    """Reject public or missing Garage bindings in each required section."""
    unsafe = tmp_path / "garage.toml"
    source = STORAGE_CONFIG_PATH.read_text(encoding="utf-8")
    assert old in source
    unsafe.write_text(source.replace(old, new, 1), encoding="utf-8")
    module = _module()
    monkeypatch.setattr(module, "STORAGE_CONFIG_PATH", unsafe)
    with pytest.raises(SystemExit, match="not isolated on loopback"):
        module.main()


def test_local_contract_rejects_duplicate_storage_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reject a duplicate Garage section instead of using substring matches."""
    unsafe = tmp_path / "garage.toml"
    source = STORAGE_CONFIG_PATH.read_text(encoding="utf-8")
    unsafe.write_text(
        f'{source}\n[admin]\napi_bind_addr = "127.0.0.1:3903"\n',
        encoding="utf-8",
    )
    module = _module()
    monkeypatch.setattr(module, "STORAGE_CONFIG_PATH", unsafe)
    with pytest.raises(SystemExit, match="not isolated on loopback"):
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


def test_public_administration_proxy_does_not_expose_metrics() -> None:
    """Keep the unauthenticated scrape route on the loopback backend port."""
    config = VITE_CONFIG_PATH.read_text(encoding="utf-8")
    bypass = config[config.index("bypass(") :]
    assert "decodeURIComponent(rawPath)" in bypass
    assert 'path !== "/v1/metrics"' in bypass
    assert "response.statusCode = 404" in bypass
    assert "return false" in bypass


def test_local_start_reruns_migrations_before_the_application() -> None:
    """Stop the old application before each migration and restart."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    migration_command = (
        "compose up --detach --remove-orphans --force-recreate \\\n"
        "        object-storage-init migrate node-dependencies"
    )
    stop = script.index("compose stop admin-dev object-storage backend")
    migration = script.index(migration_command, stop)
    jobs_complete = script.index(
        "compose wait object-storage-init migrate node-dependencies", migration
    )
    application_command = "compose up --detach --remove-orphans --no-deps backend"
    application = script.index(application_command, jobs_complete)
    storage = script.index(
        "compose up --detach --remove-orphans --no-deps object-storage admin-dev",
        application,
    )
    readiness = script.index("wait_until_ready", application)
    assert stop < migration < jobs_complete < application < storage < readiness
    readiness_function = script[
        script.index("wait_until_ready()") : script.index("main()")
    ]
    assert "compose ps --quiet object-storage" in readiness_function
    assert "State.Health.Status" in readiness_function
    assert readiness_function.index(
        "object_storage_container"
    ) < readiness_function.index("http://127.0.0.1:8010/ready")
