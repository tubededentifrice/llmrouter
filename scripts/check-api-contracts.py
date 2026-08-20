# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "jsonschema==4.25.1",
#   "openapi-spec-validator==0.7.2",
#   "ruamel.yaml==0.18.15",
# ]
# ///
"""Check the complete first-release API contract."""

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
FINGERPRINT_SCHEMAS = {
    "EmbeddingRequest",
    "ModelRequest",
    "AgentRunRequest",
    "SharedToolRequest",
    "CompatibleChatRequest",
    "CompatibleResponsesRequest",
}
ADMIN_WRITE_METHODS = {"post", "put", "patch", "delete"}
PUBLIC_OPERATION_IDS = {
    "bootstrapAdministrationEmbedSession",
    "completeAdministratorSession",
    "exchangeServiceToken",
    "getContractManifest",
    "getHealth",
    "startAdministratorSession",
}
CONTRACT_ARTIFACT_FILES = {
    "docs/api/README.md",
    "docs/api/business-tool-gateway.md",
    "docs/api/cross-service-conformance.md",
    "docs/api/embed-protocol.md",
    "docs/api/embedding-protocol.md",
    "docs/api/errors.md",
    "docs/api/openapi.yaml",
    "docs/api/request-fingerprint.md",
    "docs/api/service-management.md",
    "docs/api/stream-protocol.md",
}


class ContractError(RuntimeError):
    """One deterministic contract check failed."""


def strict_yaml(text: str, source: str) -> Any:
    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    try:
        return yaml.load(text)
    except Exception as exc:
        raise ContractError(f"Strict YAML parse failed for {source}: {exc}") from exc


def load_yaml(path: Path) -> Any:
    return strict_yaml(path.read_text(encoding="utf-8"), str(path))


def strict_json(text: str, source: str) -> Any:
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


def resolve_local_ref(spec: dict[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        raise ContractError(
            f"Only local contract references are supported: {reference}"
        )
    node: Any = spec
    for part in reference[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path, path_item in spec.get("paths", {}).items():
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


def check_conditional_rule(
    spec: dict[str, Any],
    operation_id: str,
    operation: dict[str, Any],
    rule_name: str,
    rule: Any,
    value_type: type,
) -> None:
    if isinstance(rule, value_type):
        if value_type is str and not rule:
            raise ContractError(f"{operation_id} has an empty {rule_name} rule")
        return
    if not isinstance(rule, dict) or set(rule) != {"field", "values"}:
        raise ContractError(f"{operation_id} has an invalid {rule_name} rule")
    field = rule["field"]
    values = rule["values"]
    if not isinstance(field, str) or not isinstance(values, dict) or not values:
        raise ContractError(
            f"{operation_id} has an invalid conditional {rule_name} rule"
        )
    if any(
        not isinstance(value, value_type) or (value_type is str and not value)
        for value in values.values()
    ):
        raise ContractError(
            f"{operation_id} has an invalid conditional {rule_name} value"
        )

    request_schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    if "$ref" in request_schema:
        request_schema = resolve_local_ref(spec, request_schema["$ref"])
    field_schema = request_schema.get("properties", {}).get(field, {})
    accepted_values = set(field_schema.get("enum", []))
    if not accepted_values or set(values) != accepted_values:
        raise ContractError(
            f"{operation_id} conditional {rule_name} values do not equal "
            f"the {field} enum"
        )


def parameter_references(operation: dict[str, Any]) -> set[str]:
    return {
        parameter["$ref"]
        for parameter in operation.get("parameters", [])
        if isinstance(parameter, dict) and "$ref" in parameter
    }


def check_operation_contracts(
    spec: dict[str, Any],
    spec_operations: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> None:
    policy_operations = policy.get("operations", {})
    if set(spec_operations) != set(policy_operations):
        missing = sorted(set(spec_operations) - set(policy_operations))
        extra = sorted(set(policy_operations) - set(spec_operations))
        raise ContractError(f"Operation policy drift. Missing={missing}; extra={extra}")

    fixture_cases = policy.get("conformance_cases", {})
    for operation_id, item in spec_operations.items():
        responses = item["operation"].get("responses", {})
        if not any(
            str(status) == "default" or str(status)[0] in "45" for status in responses
        ):
            raise ContractError(f"{operation_id} has no declared error response")

        operation_policy = policy_operations[operation_id]
        check_conditional_rule(
            spec,
            operation_id,
            item["operation"],
            "permission",
            operation_policy.get("permission"),
            str,
        )
        check_conditional_rule(
            spec,
            operation_id,
            item["operation"],
            "recent-authentication",
            operation_policy.get("recent_auth"),
            bool,
        )
        case = operation_policy.get("conformance_case")
        if case not in fixture_cases:
            raise ContractError(
                f"{operation_id} names unknown conformance case {case!r}"
            )

        path = item["path"]
        method = item["method"]
        security = item["operation"].get("security", spec.get("security", []))
        security_names = {name for requirement in security for name in requirement}
        if operation_id in PUBLIC_OPERATION_IDS:
            expected_security: set[str] = set()
        elif path.startswith("/v1/admin/"):
            expected_security = {"administratorSession"}
        else:
            expected_security = {"bearerToken"}
        if security_names != expected_security:
            raise ContractError(
                f"{operation_id} security {sorted(security_names)} does not equal "
                f"{sorted(expected_security)}"
            )
        uses_administrator_session = any(
            "administratorSession" in requirement for requirement in security
        )
        if (
            path.startswith("/v1/admin/")
            and uses_administrator_session
            and method in ADMIN_WRITE_METHODS
        ):
            parameters = parameter_references(item["operation"])
            required = {
                "#/components/parameters/CsrfToken",
                "#/components/parameters/Origin",
            }
            missing_controls = sorted(required - parameters)
            if missing_controls:
                raise ContractError(
                    f"{operation_id} lacks administrator write controls {missing_controls}"
                )


def check_fingerprints(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    for schema_name in FINGERPRINT_SCHEMAS:
        properties = schemas[schema_name].get("properties", {})
        for property_name, property_schema in properties.items():
            if not isinstance(property_schema.get("x-router-fingerprint"), bool):
                raise ContractError(
                    f"{schema_name}.{property_name} has no x-router-fingerprint annotation"
                )


def check_embedding_contract(root: Path, spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    request = schemas["EmbeddingRequest"]
    properties = request["properties"]
    inputs = properties["inputs"]
    text = schemas["EmbeddingInput"]["properties"]["text"]
    vector_values = schemas["EmbeddingVector"]["properties"]["vector"]["items"]

    expected_request_rules = {
        "data_profile": {
            "type": "string",
            "const": "service-data",
            "x-router-fingerprint": True,
        },
        "timeout_ms": {
            "type": "integer",
            "const": 120000,
            "x-router-fingerprint": True,
        },
    }
    for property_name, expected in expected_request_rules.items():
        if properties.get(property_name) != expected:
            raise ContractError(
                f"EmbeddingRequest.{property_name} does not equal {expected}"
            )
    if (
        inputs.get("minItems") != 1
        or inputs.get("maxItems") != 32
        or inputs.get("x-max-total-utf8-bytes") != 262144
        or inputs.get("x-unique-property") != "input_id"
    ):
        raise ContractError("EmbeddingRequest.inputs lacks the fixed batch rules")
    if text.get("x-max-utf8-bytes") != 32768:
        raise ContractError("EmbeddingInput.text lacks the fixed UTF-8 byte limit")
    if vector_values.get("x-finite") is not True:
        raise ContractError("EmbeddingVector values do not require finite numbers")

    manifest = strict_json(
        (root / "docs/api/fixtures/contract-manifest.json").read_text(encoding="utf-8"),
        "docs/api/fixtures/contract-manifest.json",
    )
    if "embedding_requests_v1" not in manifest.get("capabilities", []):
        raise ContractError("Contract manifest lacks embedding_requests_v1")
    majors = {
        artifact.get("name"): artifact.get("major_version")
        for artifact in manifest.get("artifacts", [])
    }
    if majors.get("embedding_protocol") != 1 or majors.get("openapi") != 1:
        raise ContractError(
            "Contract manifest does not publish embedding_protocol and openapi major 1"
        )


def walk_public_schemas(node: Any, location: str = "openapi") -> None:
    if isinstance(node, list):
        for index, child in enumerate(node):
            walk_public_schemas(child, f"{location}[{index}]")
        return
    if not isinstance(node, dict):
        return

    is_object = node.get("type") == "object"
    if is_object and "$ref" not in node:
        registered = node.get("x-registered-schema") is True
        if node.get("additionalProperties") is not False and not registered:
            raise ContractError(
                f"Public object {location} is open and has no registered-schema marker"
            )

    for key, child in node.items():
        walk_public_schemas(child, f"{location}.{key}")


def check_error_drift(spec: dict[str, Any], errors_path: Path) -> None:
    markdown_codes = set(
        re.findall(r"^\|\s*\d{3}\s*\|\s*`([^`]+)`\s*\|", errors_path.read_text(), re.M)
    )
    openapi_codes = set(
        spec["components"]["schemas"]["ErrorEnvelope"]["properties"]["error"][
            "properties"
        ]["code"]["enum"]
    )
    if markdown_codes != openapi_codes:
        raise ContractError(
            "Error catalog drift. "
            f"Markdown-only={sorted(markdown_codes - openapi_codes)}; "
            f"OpenAPI-only={sorted(openapi_codes - markdown_codes)}"
        )


def check_readable_contracts(
    root: Path, policy: dict[str, Any], spec_ops: dict[str, Any]
) -> None:
    for relative_path, contract in policy.get("readable_contracts", {}).items():
        text = (root / relative_path).read_text(encoding="utf-8")
        for marker in contract.get("required_markers", []):
            if marker not in text:
                raise ContractError(f"{relative_path} lacks required marker {marker!r}")
        for operation_id in contract.get("operation_ids", []):
            if operation_id not in spec_ops:
                raise ContractError(
                    f"{relative_path} names unknown operation {operation_id}"
                )
            path = spec_ops[operation_id]["path"]
            if path not in text:
                raise ContractError(
                    f"{relative_path} does not name {path} for {operation_id}"
                )


def check_fixtures(root: Path, spec: dict[str, Any], policy: dict[str, Any]) -> None:
    resolver = RefResolver.from_schema(spec)
    cases = policy.get("conformance_cases", {})
    for case_name, case in cases.items():
        fixture_path = root / case["fixture"]
        if not fixture_path.is_file():
            raise ContractError(
                f"Fixture {case_name} does not exist: {case['fixture']}"
            )
        instance = strict_json(
            fixture_path.read_text(encoding="utf-8"), str(fixture_path)
        )
        schema_name = case["schema"]
        schema = spec["components"]["schemas"].get(schema_name)
        if schema is None:
            raise ContractError(
                f"Fixture {case_name} names unknown schema {schema_name}"
            )
        errors = sorted(
            Draft202012Validator(schema, resolver=resolver).iter_errors(instance),
            key=lambda error: list(error.path),
        )
        if errors:
            detail = "; ".join(error.message for error in errors[:5])
            raise ContractError(
                f"Fixture {case_name} does not match {schema_name}: {detail}"
            )


def check_artifact_digests(root: Path, policy: dict[str, Any]) -> None:
    declared = policy.get("artifact_sha256", {})
    if set(declared) != CONTRACT_ARTIFACT_FILES:
        raise ContractError(
            "Contract artifact set drift. "
            f"Missing={sorted(CONTRACT_ARTIFACT_FILES - set(declared))}; "
            f"extra={sorted(set(declared) - CONTRACT_ARTIFACT_FILES)}"
        )
    for relative_path, expected in declared.items():
        actual = hashlib.sha256((root / relative_path).read_bytes()).hexdigest()
        if actual != expected:
            raise ContractError(
                f"Contract artifact digest drift for {relative_path}: expected {expected}, got {actual}"
            )


def run(root: Path) -> None:
    spec_path = root / "docs/api/openapi.yaml"
    policy_path = root / "docs/api/contract-policy.yaml"
    spec = load_yaml(spec_path)
    policy = load_yaml(policy_path)
    validate_spec(spec)
    spec_operations = operations(spec)
    check_operation_contracts(spec, spec_operations, policy)
    check_fingerprints(spec)
    check_embedding_contract(root, spec)
    walk_public_schemas(spec["components"]["schemas"], "components.schemas")
    check_error_drift(spec, root / "docs/api/errors.md")
    check_readable_contracts(root, policy, spec_operations)
    check_fixtures(root, spec, policy)
    check_artifact_digests(root, policy)


def self_test() -> None:
    duplicate_yaml = "a: 1\na: 2\n"
    try:
        strict_yaml(duplicate_yaml, "duplicate-key self-test")
    except ContractError:
        pass
    else:
        raise ContractError("Strict YAML duplicate-key self-test did not fail")

    duplicate_operations = {
        "paths": {
            "/one": {"get": {"operationId": "same"}},
            "/two": {"post": {"operationId": "same"}},
        }
    }
    try:
        operations(duplicate_operations)
    except ContractError:
        pass
    else:
        raise ContractError("Duplicate operationId self-test did not fail")

    try:
        strict_json('{"a": 1, "a": 2}', "duplicate-key self-test")
    except ContractError:
        pass
    else:
        raise ContractError("Strict JSON duplicate-key self-test did not fail")

    try:
        strict_json('{"a": NaN}', "non-finite-number self-test")
    except ContractError:
        pass
    else:
        raise ContractError("Strict JSON non-finite-number self-test did not fail")

    try:
        walk_public_schemas(
            {"type": "object", "properties": {"value": {"type": "string"}}}
        )
    except ContractError:
        pass
    else:
        raise ContractError("Open public-object self-test did not fail")


def main() -> int:
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
