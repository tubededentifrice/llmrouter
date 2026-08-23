#!/usr/bin/env python3
"""Check exact dependency pins and the repository release cutoff."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

EXACT_PYTHON = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*(?:\s*;.+)?$"
)
EXACT_NPM = re.compile(r"^(?:[A-Za-z0-9_.-]+@)?[0-9]+\.[0-9]+\.[0-9]+$")
SHARED_BACKEND_SPEC = (
    "opendle-lib @ git+https://github.com/opendle/opendle-lib.git@main"
)
SHARED_UI_PACKAGE = "@opendle/ui"
SHARED_UI_SPEC = "git+https://github.com/tubededentifrice/opendle-ui.git#main"
CUTOFF = "2026-07-30T06:00:00Z"
MINIMUM_RELEASE_AGE = timedelta(days=14)
APPROVED_PYTHON_OVERRIDES = {
    "cryptography": {
        "version": "50.0.0",
        "released": "2026-07-31T14:25:10.110000Z",
        "exclude_newer": "2026-08-09T00:00:00Z",
    }
}
NODE_VERSION = "24.17.0"
NPM_VERSION = "11.18.0"
PYTHON_VERSION = "3.14.6"
UV_VERSION = "0.12.0"
APPROVED_NPM_EXCEPTION_DOCUMENT: dict[str, Any] = {
    "approved_at": None,
    "decision": None,
    "exceptions": [],
}
APPROVED_NPM_OVERRIDES = {
    "brace-expansion": "5.0.9",
    "nanoid": "5.1.16",
}


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def check_pyproject(path: Path) -> list[str]:
    """Return policy errors for one Python manifest."""
    document = _load_toml(path)
    dependencies = list(document.get("project", {}).get("dependencies", []))
    for group in document.get("project", {}).get("optional-dependencies", {}).values():
        dependencies.extend(group)
    for group in document.get("dependency-groups", {}).values():
        dependencies.extend(group)
    build_dependencies = document.get("build-system", {}).get("requires", [])
    dependencies.extend(build_dependencies)
    return [
        f"{path}: Python dependency is not exact: {dependency}"
        for dependency in dependencies
        if dependency != SHARED_BACKEND_SPEC and not EXACT_PYTHON.fullmatch(dependency)
    ]


def check_package_json(path: Path) -> list[str]:
    """Return policy errors for one Node manifest."""
    document = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, version in document.get(field, {}).items():
            if name == SHARED_UI_PACKAGE and version == SHARED_UI_SPEC:
                continue
            if not EXACT_NPM.fullmatch(version):
                errors.append(f"{path}: Node dependency is not exact: {name}@{version}")
    if path == Path("package.json"):
        overrides = document.get("overrides", {})
        if overrides != APPROVED_NPM_OVERRIDES:
            errors.append(
                "package.json: overrides must equal the approved security fixes"
            )
    return errors


def check_python_security_overrides(
    uv_policy: dict[str, Any],
    dependencies: list[str],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Return errors for the exact approved Python security update."""
    errors: list[str] = []
    configured = uv_policy.get("exclude-newer-package")
    expected = {
        package: approval["exclude_newer"]
        for package, approval in APPROVED_PYTHON_OVERRIDES.items()
    }
    if configured != expected:
        return [
            (
                "pyproject.toml: tool.uv.exclude-newer-package must equal the "
                "approved security update cutoffs"
            )
        ]

    checked_at = now or datetime.now(tz=UTC)
    for package, approval in APPROVED_PYTHON_OVERRIDES.items():
        version = approval["version"]
        if f"{package}=={version}" not in dependencies:
            errors.append(
                f"packages/backend-role/pyproject.toml: {package} must be pinned "
                f"to the approved security version {version}"
            )
        try:
            released = datetime.fromisoformat(approval["released"])
            cutoff = datetime.fromisoformat(approval["exclude_newer"])
        except AttributeError, ValueError:
            errors.append(f"pyproject.toml: approved {package} dates are malformed")
            continue
        if checked_at - released < MINIMUM_RELEASE_AGE:
            errors.append(
                f"pyproject.toml: approved {package} release is less than 14 days old"
            )
        if cutoff > checked_at - MINIMUM_RELEASE_AGE:
            errors.append(
                f"pyproject.toml: {package} exclude-newer cutoff is less than "
                "14 days old"
            )
    return errors


def check_root_policy() -> list[str]:
    """Return policy errors for pinned root tools and resolver settings."""
    errors: list[str] = []
    root = _load_toml(Path("pyproject.toml"))
    uv_policy = root.get("tool", {}).get("uv", {})
    if uv_policy.get("exclude-newer") != CUTOFF:
        errors.append(f"pyproject.toml: tool.uv.exclude-newer must be {CUTOFF}")
    backend = _load_toml(Path("packages/backend-role/pyproject.toml"))
    errors.extend(
        check_python_security_overrides(
            uv_policy, list(backend.get("project", {}).get("dependencies", []))
        )
    )
    if uv_policy.get("required-version") != f"=={UV_VERSION}":
        errors.append(
            f"pyproject.toml: tool.uv.required-version must be =={UV_VERSION}"
        )

    package = json.loads(Path("package.json").read_text(encoding="utf-8"))
    if package.get("packageManager") != f"npm@{NPM_VERSION}":
        errors.append(f"package.json: packageManager must be npm@{NPM_VERSION}")
    engines = package.get("engines", {})
    if engines.get("node") != NODE_VERSION:
        errors.append(f"package.json: engines.node must be {NODE_VERSION}")
    if engines.get("npm") != NPM_VERSION:
        errors.append(f"package.json: engines.npm must be {NPM_VERSION}")

    exception_document = json.loads(
        Path("dependency-age-exceptions.json").read_text(encoding="utf-8")
    )
    if exception_document != APPROVED_NPM_EXCEPTION_DOCUMENT:
        errors.append(
            "dependency-age-exceptions.json must equal the approved decision 0053 "
            "record"
        )

    if Path(".node-version").read_text(encoding="utf-8").strip() != NODE_VERSION:
        errors.append(f".node-version must be {NODE_VERSION}")
    if Path(".python-version").read_text(encoding="utf-8").strip() != PYTHON_VERSION:
        errors.append(f".python-version must be {PYTHON_VERSION}")

    npm_policy = Path(".npmrc").read_text(encoding="utf-8").splitlines()
    required_npm_policy = {
        "min-release-age=14",
        "save-exact=true",
        "engine-strict=true",
        "audit=true",
    }
    errors.extend(
        f".npmrc: required policy is missing: {missing_policy}"
        for missing_policy in sorted(required_npm_policy - set(npm_policy))
    )
    declared_exclusions = {
        line.removeprefix("min-release-age-exclude[]=")
        for line in npm_policy
        if line.startswith("min-release-age-exclude[]=")
    }
    if declared_exclusions:
        errors.append(".npmrc: release-age exclusions must be empty")
    return errors


def main() -> int:
    """Check manifests supplied by the caller."""
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--require-root-policy", action="store_true")
    arguments = parser.parse_args()
    errors: list[str] = []
    for path in arguments.paths:
        if path.name == "pyproject.toml":
            errors.extend(check_pyproject(path))
        elif path.name == "package.json":
            errors.extend(check_package_json(path))
        else:
            errors.append(f"Unsupported manifest: {path}")
    if arguments.require_root_policy:
        errors.extend(check_root_policy())
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Dependency policy checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
