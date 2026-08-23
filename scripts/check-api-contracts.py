# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "jsonschema==4.25.1",
#   "openapi-spec-validator==0.7.2",
#   "ruamel.yaml==0.18.15",
# ]
# ///
"""Check the native version 1 API contract."""
# ruff: noqa: ANN401, C901, E402, E501, EM101, EM102, FURB167, TRY003

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message="jsonschema.RefResolver is deprecated.*")

from jsonschema import Draft202012Validator, RefResolver
from openapi_spec_validator import validate_spec
from ruamel.yaml import YAML

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
ADMIN_WRITE_METHODS = {"post", "put", "patch", "delete"}
CONTRACT_ARTIFACT_FILES = {
    "docs/api/README.md",
    "docs/api/errors.md",
    "docs/api/openapi.yaml",
    "docs/api/stream-protocol.md",
}
FORBIDDEN_PATH_PARTS = {
    "/openai",
    "/token-exchange",
    "/requests/",
    "/agent-runs",
    "/shared-tools",
    "/embed-sessions",
    "/budgets",
    "/revisions",
    "/drafts",
    "/rollouts",
    "/rollback",
}
FORBIDDEN_SCHEMA_NAMES = {
    "AgentRun",
    "Budget",
    "ConfigurationRevision",
    "EmbedSession",
    "RequestAdmission",
    "SharedTool",
    "TokenExchange",
}
UNVERSIONED_RESOURCE_SCHEMAS = {
    "AvailableProviderModel",
    "Assignment",
    "AssignmentWrite",
    "Credential",
    "CredentialWrite",
    "Model",
    "ModelWrite",
    "Provider",
    "ProviderModel",
    "ProviderModelWrite",
    "ProviderWrite",
    "Service",
    "ServiceCreate",
    "ServiceUpdate",
    "Workspace",
    "WorkspaceCreate",
}


class ContractError(RuntimeError):
    """One deterministic contract check failed."""


def strict_yaml(text: str, source: str) -> Any:
    """Parse YAML and reject duplicate keys."""
    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    try:
        return yaml.load(text)
    except Exception as exc:
        raise ContractError(f"Strict YAML parse failed for {source}: {exc}") from exc


def load_yaml(path: Path) -> Any:
    """Load one strict YAML file."""
    return strict_yaml(path.read_text(encoding="utf-8"), str(path))


def strict_json(text: str, source: str) -> Any:
    """Parse JSON and reject duplicate keys and non-finite numbers."""

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"Duplicate JSON key {key!r} in {source}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ContractError(f"Non-finite JSON number {value!r} in {source}")

    try:
        return json.loads(
            text,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError(f"Strict JSON parse failed for {source}: {exc}") from exc


def operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect each HTTP operation by its stable operation ID."""
    result: dict[str, dict[str, Any]] = {}
    for path, path_item in spec.get("paths", {}).items():
        if not path.startswith("/v1/"):
            raise ContractError(f"API path is not versioned: {path}")
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation.get("operationId")
            if not operation_id:
                raise ContractError(f"{method.upper()} {path} has no operationId")
            if operation_id in result:
                raise ContractError(f"Duplicate operationId: {operation_id}")
            result[operation_id] = {
                "path": path,
                "method": method,
                "operation": operation,
            }
    return result


def parameter_references(operation: dict[str, Any]) -> set[str]:
    """Get parameter component references from one operation."""
    return {
        parameter["$ref"]
        for parameter in operation.get("parameters", [])
        if isinstance(parameter, dict) and "$ref" in parameter
    }


def check_operations(
    spec: dict[str, Any],
    spec_operations: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> None:
    """Check operation access, errors, and browser write controls."""
    policy_operations = policy.get("operations", {})
    if set(spec_operations) != set(policy_operations):
        missing = sorted(set(spec_operations) - set(policy_operations))
        extra = sorted(set(policy_operations) - set(spec_operations))
        raise ContractError(f"Operation policy drift. Missing={missing}; extra={extra}")

    required_reset_operations = {
        "adminGetLogRetention",
        "adminPutLogRetention",
        "adminRemoveObservedAssignmentRequirement",
        "getMetrics",
        "listAvailableProviderModels",
        "removeObservedAssignmentRequirement",
    }
    if not required_reset_operations.issubset(spec_operations):
        raise ContractError(
            "Required simplified operations are missing: "
            f"{sorted(required_reset_operations - set(spec_operations))}"
        )

    expected_security = {
        "public": set(),
        "service": {"serviceKey"},
        "administrator": {"administratorSession"},
    }
    for operation_id, item in spec_operations.items():
        operation = item["operation"]
        responses = operation.get("responses", {})
        if "default" not in {str(status) for status in responses}:
            raise ContractError(f"{operation_id} has no default error response")

        rule = policy_operations[operation_id]
        actor = rule.get("actor")
        permission = rule.get("permission")
        if actor not in expected_security:
            raise ContractError(f"{operation_id} has invalid actor {actor!r}")
        if not isinstance(permission, str) or not permission:
            raise ContractError(f"{operation_id} has no permission")
        security = operation.get("security", spec.get("security", []))
        names = {name for requirement in security for name in requirement}
        if names != expected_security[actor]:
            raise ContractError(
                f"{operation_id} security {sorted(names)} does not equal "
                f"{sorted(expected_security[actor])}"
            )

        if actor == "administrator" and item["method"] in ADMIN_WRITE_METHODS:
            required = {
                "#/components/parameters/CsrfToken",
                "#/components/parameters/Origin",
            }
            missing_controls = sorted(required - parameter_references(operation))
            if missing_controls:
                raise ContractError(
                    f"{operation_id} lacks administrator write controls {missing_controls}"
                )


def walk_closed_objects(node: Any, location: str = "openapi") -> None:
    """Require each declared object schema to reject unknown fields."""
    if isinstance(node, list):
        for index, child in enumerate(node):
            walk_closed_objects(child, f"{location}[{index}]")
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" and node.get("additionalProperties") is not False:
        raise ContractError(f"Public object {location} is open")
    if "additionalProperties" in node and node["additionalProperties"] is not False:
        raise ContractError(f"Public map {location} permits unknown fields")
    for key, child in node.items():
        walk_closed_objects(child, f"{location}.{key}")


def check_reset_boundaries(spec: dict[str, Any]) -> None:
    """Reject the removed product surfaces and resource concurrency fields."""
    paths = set(spec.get("paths", {}))
    required_paths = {
        "/v1/metrics",
        "/v1/provider-models",
        "/v1/admin/settings/log-retention",
    }
    if not required_paths.issubset(paths):
        raise ContractError(
            f"Required simplified API paths are missing: {sorted(required_paths - paths)}"
        )
    callback = spec.get("paths", {}).get("/v1/admin/oidc/callback", {})
    if "get" not in callback or "post" in callback:
        raise ContractError(
            "The OpenID Connect callback is not a browser GET operation"
        )
    for path in paths:
        for forbidden in FORBIDDEN_PATH_PARTS:
            if forbidden in path:
                raise ContractError(f"Removed API surface remains at {path}")

    schemas = spec["components"]["schemas"]
    stale_schemas = sorted(set(schemas) & FORBIDDEN_SCHEMA_NAMES)
    if stale_schemas:
        raise ContractError(f"Removed schemas remain: {stale_schemas}")
    for name in UNVERSIONED_RESOURCE_SCHEMAS:
        fields = set(schemas[name].get("properties", {}))
        stale_fields = sorted(fields & {"revision", "version", "state"})
        if stale_fields:
            raise ContractError(f"{name} has removed fields {stale_fields}")

    selector = schemas.get("ModelSelector", {})
    refs = {
        item.get("$ref") for item in selector.get("oneOf", []) if isinstance(item, dict)
    }
    expected_refs = {
        "#/components/schemas/AssignmentSelector",
        "#/components/schemas/ExactProviderModelSelector",
    }
    if refs != expected_refs:
        raise ContractError(
            "ModelSelector must select one assignment or exact provider-model"
        )
    for name in ("ModelCallRequest", "EmbeddingRequest", "MediaJobRequest"):
        required = set(schemas[name].get("required", []))
        if not {"workspace_api_name", "selector"}.issubset(required):
            raise ContractError(f"{name} does not require a workspace and selector")

    model_call = schemas["ModelCallRequest"]
    exclusions = model_call.get("properties", {}).get(
        "excluded_provider_model_api_names", {}
    )
    if (
        exclusions.get("type") != "array"
        or exclusions.get("maxItems") != 16
        or exclusions.get("uniqueItems") is not True
        or exclusions.get("items", {}).get("$ref") != "#/components/schemas/ApiName"
    ):
        raise ContractError(
            "Model-call provider-model exclusions are not bounded and unique"
        )
    resolver = RefResolver.from_schema(spec)
    model_call_validator = Draft202012Validator(model_call, resolver=resolver)
    base_call = {
        "workspace_api_name": "workspace",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Run the workflow."}],
            }
        ],
    }
    valid_calls = (
        {
            **base_call,
            "selector": {"assignment_api_name": "workflow"},
            "excluded_provider_model_api_names": [],
        },
        {
            **base_call,
            "selector": {"assignment_api_name": "workflow"},
            "excluded_provider_model_api_names": ["route-a"],
        },
        {
            **base_call,
            "selector": {"provider_model_api_name": "route-a"},
        },
    )
    valid_errors = [
        list(model_call_validator.iter_errors(call)) for call in valid_calls
    ]
    if any(valid_errors):
        detail = "; ".join(error.message for errors in valid_errors for error in errors)
        raise ContractError(
            f"A valid model-call exclusion form does not validate: {detail}"
        )
    invalid_calls = (
        {
            **base_call,
            "selector": {"provider_model_api_name": "route-a"},
            "excluded_provider_model_api_names": ["route-b"],
        },
        {
            **base_call,
            "selector": {"assignment_api_name": "workflow"},
            "excluded_provider_model_api_names": ["route-a", "route-a"],
        },
        {
            **base_call,
            "selector": {"assignment_api_name": "workflow"},
            "excluded_provider_model_api_names": [
                f"route-{index}" for index in range(17)
            ],
        },
        {
            **base_call,
            "selector": {"assignment_api_name": "workflow"},
            "excluded_provider_model_api_names": "route-a",
        },
    )
    if any(not list(model_call_validator.iter_errors(call)) for call in invalid_calls):
        raise ContractError("An invalid model-call exclusion form validates")

    effective_chain = schemas.get("EffectiveAssignmentChain", {})
    if effective_chain.get("minItems") != 0:
        raise ContractError(
            "The effective assignment chain cannot represent empty default"
        )
    assignment_fields = set(schemas.get("Assignment", {}).get("properties", {}))
    if "observed_requirements" not in assignment_fields:
        raise ContractError("Assignments do not expose observed call requirements")

    model_fields = set(schemas.get("ModelWrite", {}).get("properties", {}))
    required_model_fields = {
        "input_modalities",
        "output_modalities",
        "capabilities",
        "constraints",
    }
    if not required_model_fields.issubset(model_fields):
        raise ContractError(
            f"Model configuration lacks capability fields: {sorted(required_model_fields - model_fields)}"
        )
    provider_model_fields = set(
        schemas.get("ProviderModelWrite", {}).get("properties", {})
    )
    required_provider_model_fields = {
        "input_modalities",
        "output_modalities",
        "capabilities",
        "constraints",
        "reasoning_mappings",
        "price_source",
        "price_lookup_key",
        "manual_price",
    }
    if not required_provider_model_fields.issubset(provider_model_fields):
        raise ContractError(
            "Provider-model configuration lacks required narrowing, reasoning, or price fields"
        )

    model_result_refs = {
        item.get("$ref")
        for item in schemas.get("ModelCallResult", {}).get("oneOf", [])
        if isinstance(item, dict)
    }
    expected_result_refs = {
        "#/components/schemas/StandardModelCallResult",
        "#/components/schemas/StructuredModelCallResult",
    }
    if model_result_refs != expected_result_refs:
        raise ContractError("Model-call results are not discriminated by output form")

    retention = (
        schemas.get("LogRetentionSettings", {})
        .get("properties", {})
        .get("duration_days", {})
    )
    if retention.get("minimum") != 1 or retention.get("maximum") != 30:
        raise ContractError("Log retention is not bounded from 1 through 30 days")
    return_to = (
        schemas.get("AdministratorSessionStart", {})
        .get("properties", {})
        .get("return_to", {})
    )
    return_to_pattern = str(return_to.get("pattern", ""))
    if (
        not return_to_pattern.startswith("^/")
        or re.fullmatch(return_to_pattern, "//example.com") is not None
        or re.fullmatch(return_to_pattern, "/admin") is None
    ):
        raise ContractError("The administrator return target is not a local path")

    for page_name in (
        "AssignmentPage",
        "AvailableProviderModelPage",
        "CredentialPage",
        "ModelPage",
        "ProviderModelPage",
        "ProviderPage",
        "ServiceKeyPage",
        "ServicePage",
        "WorkspacePage",
    ):
        if "page" not in schemas.get(page_name, {}).get("properties", {}):
            raise ContractError(f"{page_name} has no cursor page metadata")
    media_fields = set(schemas["MediaJob"].get("properties", {})) | set(
        schemas["MediaContent"].get("properties", {})
    )
    if media_fields & {"url", "storage_url", "provider_url"}:
        raise ContractError("Media resources expose a storage or provider URL")

    service_key = spec["components"]["securitySchemes"].get("serviceKey", {})
    if service_key.get("type") != "http" or service_key.get("scheme") != "bearer":
        raise ContractError(
            "Service authentication is not direct bearer service-key authentication"
        )


def check_error_drift(spec: dict[str, Any], errors_path: Path) -> None:
    """Keep stable error names equal in OpenAPI and readable documentation."""
    markdown_codes = set(
        re.findall(r"^- `([^`]+)`:", errors_path.read_text(encoding="utf-8"), re.M)
    )
    openapi_codes = set(spec["components"]["schemas"]["ErrorCode"]["enum"])
    if markdown_codes != openapi_codes:
        raise ContractError(
            "Error catalog drift. "
            f"Markdown-only={sorted(markdown_codes - openapi_codes)}; "
            f"OpenAPI-only={sorted(openapi_codes - markdown_codes)}"
        )


def check_readable_contracts(root: Path, policy: dict[str, Any]) -> None:
    """Check stable markers in the small readable contract set."""
    readable = policy.get("readable_contracts", {})
    for relative_path, marker in readable.items():
        path = root / "docs/api" / relative_path
        if not path.is_file() or marker not in path.read_text(encoding="utf-8"):
            raise ContractError(f"{relative_path} lacks required marker {marker!r}")


def check_fixtures(root: Path, spec: dict[str, Any], policy: dict[str, Any]) -> None:
    """Validate each declared strict JSON fixture against its component schema."""
    resolver = RefResolver.from_schema(spec)
    declared = policy.get("fixtures", {})
    fixture_directory = root / "docs/api/fixtures"
    actual = {
        str(path.relative_to(root / "docs/api"))
        for path in fixture_directory.glob("*.json")
    }
    if set(declared) != actual:
        raise ContractError(
            f"Fixture set drift. Missing={sorted(set(declared) - actual)}; "
            f"extra={sorted(actual - set(declared))}"
        )
    for relative_path, schema_name in declared.items():
        fixture_path = root / "docs/api" / relative_path
        instance = strict_json(
            fixture_path.read_text(encoding="utf-8"), str(fixture_path)
        )
        schema = spec["components"]["schemas"].get(schema_name)
        if schema is None:
            raise ContractError(f"{relative_path} names unknown schema {schema_name}")
        errors = sorted(
            Draft202012Validator(schema, resolver=resolver).iter_errors(instance),
            key=lambda error: list(error.path),
        )
        if errors:
            detail = "; ".join(error.message for error in errors[:5])
            raise ContractError(
                f"{relative_path} does not match {schema_name}: {detail}"
            )


def check_artifact_digests(root: Path) -> None:
    """Check the generated digest manifest and its bounded artifact set."""
    path = root / "docs/api/contract-digests.json"
    manifest = strict_json(path.read_text(encoding="utf-8"), str(path))
    declared = {
        item.get("path"): item.get("sha256") for item in manifest.get("artifacts", [])
    }
    if set(declared) != CONTRACT_ARTIFACT_FILES:
        raise ContractError(
            f"Contract artifact set drift. Missing={sorted(CONTRACT_ARTIFACT_FILES - set(declared))}; "
            f"extra={sorted(set(declared) - CONTRACT_ARTIFACT_FILES)}"
        )
    for relative_path, expected in declared.items():
        actual = hashlib.sha256((root / relative_path).read_bytes()).hexdigest()
        if expected != actual:
            raise ContractError(f"Contract artifact digest drift for {relative_path}")


def run(root: Path) -> None:
    """Run all contract checks."""
    spec = load_yaml(root / "docs/api/openapi.yaml")
    policy = load_yaml(root / "docs/api/contract-policy.yaml")
    validate_spec(spec)
    spec_operations = operations(spec)
    check_operations(spec, spec_operations, policy)
    walk_closed_objects(spec["components"]["schemas"], "components.schemas")
    check_reset_boundaries(spec)
    check_error_drift(spec, root / "docs/api/errors.md")
    check_readable_contracts(root, policy)
    check_fixtures(root, spec, policy)
    check_artifact_digests(root)


def expect_failure(function: Any, message: str) -> None:
    """Require one unsafe-input self-test to fail."""
    try:
        function()
    except ContractError:
        return
    raise ContractError(message)


def self_test() -> None:
    """Prove success helpers and expected failures for unsafe contract input."""
    expect_failure(
        lambda: strict_yaml("a: 1\na: 2\n", "duplicate-key self-test"),
        "Strict YAML duplicate-key self-test did not fail",
    )
    expect_failure(
        lambda: strict_json('{"a": 1, "a": 2}', "duplicate-key self-test"),
        "Strict JSON duplicate-key self-test did not fail",
    )
    expect_failure(
        lambda: strict_json('{"a": NaN}', "non-finite-number self-test"),
        "Strict JSON non-finite-number self-test did not fail",
    )
    expect_failure(
        lambda: walk_closed_objects(
            {"type": "object", "properties": {"value": {"type": "string"}}}
        ),
        "Open public-object self-test did not fail",
    )
    duplicate_operations = {
        "paths": {
            "/v1/one": {"get": {"operationId": "same"}},
            "/v1/two": {"post": {"operationId": "same"}},
        }
    }
    expect_failure(
        lambda: operations(duplicate_operations),
        "Duplicate operationId self-test did not fail",
    )


def main() -> int:
    """Run the checker or its deterministic self-test."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        if args.self_test:
            self_test()
        else:
            run(root)
    except ContractError as exc:
        print(f"API contract check failed: {exc}", file=sys.stderr)
        return 1
    print("API contract checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
