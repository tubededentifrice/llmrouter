#!/usr/bin/env python3
"""Check exact dependency pins and the repository release cutoff."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

EXACT_PYTHON = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*(?:\s*;.+)?$"
)
EXACT_NPM = re.compile(r"^(?:[A-Za-z0-9_.-]+@)?[0-9]+\.[0-9]+\.[0-9]+$")
CUTOFF = "2026-07-30T06:00:00Z"
NODE_VERSION = "24.17.0"
NPM_VERSION = "11.18.0"
PYTHON_VERSION = "3.13.12"
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
        if not EXACT_PYTHON.fullmatch(dependency)
    ]


def check_package_json(path: Path) -> list[str]:
    """Return policy errors for one Node manifest."""
    document = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, version in document.get(field, {}).items():
            if not EXACT_NPM.fullmatch(version):
                errors.append(f"{path}: Node dependency is not exact: {name}@{version}")
    if path == Path("package.json"):
        overrides = document.get("overrides", {})
        if overrides != APPROVED_NPM_OVERRIDES:
            errors.append(
                "package.json: overrides must equal the approved security fixes"
            )
    return errors


def check_root_policy() -> list[str]:
    """Return policy errors for pinned root tools and resolver settings."""
    errors: list[str] = []
    root = _load_toml(Path("pyproject.toml"))
    uv_policy = root.get("tool", {}).get("uv", {})
    if uv_policy.get("exclude-newer") != CUTOFF:
        errors.append(f"pyproject.toml: tool.uv.exclude-newer must be {CUTOFF}")
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
