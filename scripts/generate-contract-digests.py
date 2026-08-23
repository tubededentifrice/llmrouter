# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "ruamel.yaml==0.18.15",
# ]
# ///
"""Generate the native API contract digest manifest."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML  # type: ignore[import-not-found]

CONTRACT_ARTIFACT_FILES = (
    "docs/api/README.md",
    "docs/api/errors.md",
    "docs/api/openapi.yaml",
    "docs/api/stream-protocol.md",
)
GENERATOR_VERSION = 3


def load_spec(root: Path) -> dict[str, Any]:
    """Load the accepted OpenAPI source."""
    yaml = YAML(typ="safe")
    value = yaml.load((root / "docs/api/openapi.yaml").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        message = "The OpenAPI source must be an object."
        raise TypeError(message)
    return value


def render_digest_manifest(root: Path, spec: dict[str, Any]) -> str:
    """Render the materialized SHA-256 digest manifest."""
    info = spec["info"]
    manifest = {
        "contract_set": "llmrouter-v1",
        "generator_version": GENERATOR_VERSION,
        "source_sha256": hashlib.sha256(
            (root / "docs/api/openapi.yaml").read_bytes()
        ).hexdigest(),
        "version": info["version"],
        "artifacts": [
            {
                "path": path,
                "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
            }
            for path in CONTRACT_ARTIFACT_FILES
        ],
        "component_schemas": {
            name: hashlib.sha256(
                json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            for name, schema in spec["components"]["schemas"].items()
        },
    }
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def expected_output(root: Path) -> str:
    """Return the expected digest manifest."""
    return render_digest_manifest(root, load_spec(root))


def output_is_current(path: Path, expected: str) -> bool:
    """Return true when one generated file has its exact expected content."""
    return path.is_file() and path.read_text(encoding="utf-8") == expected


def self_test(root: Path) -> None:
    """Prove that stale output and source changes are observable."""
    expected = expected_output(root)
    with tempfile.TemporaryDirectory() as temporary_directory:
        stale = Path(temporary_directory) / "contract-digests.json"
        stale.write_text("stale\n", encoding="utf-8")
        if output_is_current(stale, expected):
            message = "The digest drift expected-failure test did not fail."
            raise RuntimeError(message)

    spec = load_spec(root)
    mutated = copy.deepcopy(spec)
    mutated["components"]["schemas"]["OpaqueId"]["maxLength"] = 201
    if render_digest_manifest(root, mutated) == render_digest_manifest(root, spec):
        message = "An OpenAPI schema mutation did not change its digest."
        raise RuntimeError(message)


def main() -> int:
    """Generate or check the digest manifest."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.self_test:
        self_test(root)
        print("Contract digest generation self-test passed.")
        return 0
    output_path = root / "docs/api/contract-digests.json"
    expected = expected_output(root)
    if args.check:
        if not output_is_current(output_path, expected):
            print(
                "Generated contract digest drift: docs/api/contract-digests.json",
                file=sys.stderr,
            )
            return 1
    else:
        output_path.write_text(expected, encoding="utf-8")
    print("Contract digest generation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
