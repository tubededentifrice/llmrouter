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
        "adminCreatePlaygroundEmbedding",
        "adminCreatePlaygroundMediaJob",
        "adminCreatePlaygroundModelCall",
        "adminCreatePlaygroundModelStream",
        "adminGetPlaygroundMediaJob",
        "adminGetPlaygroundMediaJobContent",
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
        if actor == "administrator" and permission != "administrator.unrestricted":
            raise ContractError(
                f"{operation_id} does not use unrestricted administrator authority"
            )
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


def validation_errors(
    spec: dict[str, Any], schema_name: str, instance: dict[str, Any]
) -> list[Any]:
    """Return deterministic validation errors for one component instance."""
    schema = spec["components"]["schemas"][schema_name]
    resolver = RefResolver.from_schema(spec)
    return sorted(
        Draft202012Validator(schema, resolver=resolver).iter_errors(instance),
        key=lambda error: (list(error.path), error.message),
    )


def require_valid_instances(
    spec: dict[str, Any], schema_name: str, instances: tuple[dict[str, Any], ...]
) -> None:
    """Require each positive contract form to validate."""
    errors = [
        error
        for instance in instances
        for error in validation_errors(spec, schema_name, instance)
    ]
    if errors:
        detail = "; ".join(error.message for error in errors[:5])
        raise ContractError(f"A valid {schema_name} form does not validate: {detail}")


def require_invalid_instances(
    spec: dict[str, Any], schema_name: str, instances: tuple[dict[str, Any], ...]
) -> None:
    """Require each unsafe or contradictory contract form to fail."""
    valid_indexes = [
        index
        for index, instance in enumerate(instances)
        if not validation_errors(spec, schema_name, instance)
    ]
    if valid_indexes:
        raise ContractError(
            f"Invalid {schema_name} forms validate at indexes {valid_indexes}"
        )


def check_administrator_playground(spec: dict[str, Any]) -> None:
    """Check unrestricted administrator calls and their isolated records."""
    schemas = spec["components"]["schemas"]
    expected_paths = {
        "adminCreatePlaygroundModelCall": (
            "/v1/admin/playground/model-calls",
            "post",
        ),
        "adminCreatePlaygroundModelStream": (
            "/v1/admin/playground/model-streams",
            "post",
        ),
        "adminCreatePlaygroundEmbedding": (
            "/v1/admin/playground/embeddings",
            "post",
        ),
        "adminCreatePlaygroundMediaJob": (
            "/v1/admin/playground/media-jobs",
            "post",
        ),
        "adminGetPlaygroundMediaJob": (
            "/v1/admin/playground/media-jobs/{media_job_id}",
            "get",
        ),
        "adminGetPlaygroundMediaJobContent": (
            "/v1/admin/playground/media-jobs/{media_job_id}/content",
            "get",
        ),
    }
    spec_operations = operations(spec)
    for operation_id, (expected_path, expected_method) in expected_paths.items():
        item = spec_operations[operation_id]
        if item["path"] != expected_path or item["method"] != expected_method:
            raise ContractError(f"{operation_id} uses an unexpected path or method")
        success_status = (
            "202" if operation_id == "adminCreatePlaygroundMediaJob" else "200"
        )
        success = item["operation"]["responses"][success_status]
        no_store = success.get("headers", {}).get("Cache-Control", {})
        if (
            no_store.get("required") is not True
            or no_store.get("schema", {}).get("const") != "no-store"
        ):
            raise ContractError(f"{operation_id} does not require no-store responses")

    stream_headers = spec_operations["adminCreatePlaygroundModelStream"]["operation"][
        "responses"
    ]["200"]["headers"]
    call_id_header = stream_headers.get("X-LLMRouter-Logical-Call-Id", {})
    if (
        call_id_header.get("required") is not True
        or call_id_header.get("schema", {}).get("$ref")
        != "#/components/schemas/OpaqueId"
    ):
        raise ContractError("Administrator streams lack the logical-call header")

    administrator_requests = {
        "AdministratorModelCallRequest": "ModelCallRequest",
        "AdministratorEmbeddingRequest": "EmbeddingRequest",
        "AdministratorMediaJobRequest": "MediaJobRequest",
    }
    for administrator_name, service_name in administrator_requests.items():
        administrator_schema = schemas[administrator_name]
        administrator_fields = set(administrator_schema.get("properties", {}))
        if administrator_fields & {
            "workspace_api_name",
            "service_key",
            "service_api_key",
        }:
            raise ContractError(
                f"{administrator_name} contains service authority or workspace fields"
            )
        service_fields = set(schemas[service_name].get("properties", {}))
        expected_shared_fields = service_fields - {"workspace_api_name", "selector"}
        if not expected_shared_fields.issubset(administrator_fields):
            raise ContractError(
                f"{administrator_name} lacks service-call bounds for "
                f"{sorted(expected_shared_fields - administrator_fields)}"
            )
        for field in expected_shared_fields:
            if (
                administrator_schema["properties"][field]
                != schemas[service_name]["properties"][field]
            ):
                raise ContractError(
                    f"{administrator_name}.{field} differs from {service_name}.{field}"
                )
        for bound in ("x-max-json-bytes", "x-max-image-bytes"):
            if (
                bound in schemas[service_name]
                and administrator_schema.get(bound) != schemas[service_name][bound]
            ):
                raise ContractError(
                    f"{administrator_name}.{bound} differs from {service_name}.{bound}"
                )

    assignment_selector = {
        "assignment_api_name": "summarize",
        "service_api_name": "billing",
    }
    exact_selector = {"provider_model_api_name": "primary-text"}
    base_messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Summarize this."}],
        }
    ]
    image = {
        "type": "image",
        "media_type": "image/png",
        "data_base64": "aW1hZ2U=",
    }
    require_valid_instances(
        spec,
        "AdministratorModelCallRequest",
        (
            {"selector": assignment_selector, "messages": base_messages},
            {
                "selector": assignment_selector,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Summarize this."},
                            image,
                            {
                                "type": "tool_result",
                                "tool_call_id": "tool-call",
                                "result_json": '{"value":1}',
                            },
                        ],
                    }
                ],
                "excluded_provider_model_api_names": ["secondary-text"],
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Find one record.",
                        "input_schema_json": '{"type":"object"}',
                    }
                ],
                "output_format": {
                    "type": "json_schema",
                    "schema_json": '{"type":"object"}',
                },
                "output_limit": 1000000,
                "temperature": 2,
                "tags": ["playground"],
            },
            {"selector": exact_selector, "messages": base_messages},
        ),
    )
    require_invalid_instances(
        spec,
        "AdministratorModelCallRequest",
        (
            {
                "selector": {"assignment_api_name": "summarize"},
                "messages": base_messages,
            },
            {
                "selector": {
                    "provider_model_api_name": "primary-text",
                    "service_api_name": "billing",
                },
                "messages": base_messages,
            },
            {
                "selector": exact_selector,
                "messages": base_messages,
                "excluded_provider_model_api_names": ["secondary-text"],
            },
            {
                "workspace_api_name": "billing-production",
                "selector": exact_selector,
                "messages": base_messages,
            },
            {
                "service_api_key": "secret",
                "selector": exact_selector,
                "messages": base_messages,
            },
            {
                "selector": assignment_selector,
                "messages": base_messages,
                "excluded_provider_model_api_names": [
                    f"route-{index}" for index in range(17)
                ],
            },
            {
                "selector": exact_selector,
                "messages": base_messages,
                "tools": [
                    {
                        "name": f"tool-{index}",
                        "description": "One tool.",
                        "input_schema_json": '{"type":"object"}',
                    }
                    for index in range(129)
                ],
            },
            {
                "selector": exact_selector,
                "messages": base_messages,
                "output_limit": 1000001,
            },
            {
                "selector": exact_selector,
                "messages": base_messages,
                "temperature": 2.01,
            },
        ),
    )
    usage = {
        "units": [
            {"unit": "input_token", "quantity": "10"},
            {"unit": "output_token", "quantity": "5"},
        ],
        "cost": "0.001",
        "currency": "USD",
    }
    succeeded_attempt = {
        "provider_model_api_name": "primary-text",
        "outcome": "succeeded",
        "elapsed_ms": 900,
        "usage": usage,
    }
    model_result = {
        "logical_call_id": "call-model",
        "selector": exact_selector,
        "elapsed_ms": 1000,
        "attempts": [succeeded_attempt],
        "result": {
            "output_type": "standard",
            "provider_model_api_name": "primary-text",
            "content": [{"type": "text", "text": "Complete."}],
            "usage": usage,
        },
    }
    require_valid_instances(
        spec,
        "AdministratorModelCallResult",
        (
            model_result,
            {
                **model_result,
                "selector": assignment_selector,
                "attempts": [
                    {
                        "provider_model_api_name": "primary-text",
                        "outcome": "failed",
                        "elapsed_ms": 900,
                        "usage": usage,
                        "error": {
                            "code": "upstream_failed",
                            "message": "First route failed.",
                        },
                    },
                    {
                        **succeeded_attempt,
                        "provider_model_api_name": "secondary-text",
                    },
                ],
                "result": {
                    **model_result["result"],
                    "provider_model_api_name": "secondary-text",
                },
            },
            {
                **model_result,
                "result": {
                    "output_type": "standard",
                    "provider_model_api_name": "primary-text",
                    "content": [
                        {
                            "type": "tool_call",
                            "id": "tool-call",
                            "name": "lookup",
                            "arguments_json": '{"id":1}',
                        }
                    ],
                    "usage": usage,
                },
            },
            {
                **model_result,
                "result": {
                    "output_type": "structured_json",
                    "provider_model_api_name": "primary-text",
                    "structured_output_json": '{"summary":"Complete."}',
                    "usage": usage,
                },
            },
        ),
    )
    require_invalid_instances(
        spec,
        "AdministratorModelCallResult",
        (
            {**model_result, "workspace_api_name": "production"},
            {**model_result, "elapsed_ms": 900001},
            {**model_result, "attempts": [succeeded_attempt, succeeded_attempt]},
        ),
    )
    require_valid_instances(
        spec,
        "AdministratorStreamStart",
        (
            {
                "logical_call_id": "call-stream",
                "selector": assignment_selector,
                "provider_model_api_name": "primary-text",
            },
        ),
    )
    require_valid_instances(
        spec,
        "AdministratorStreamCompleted",
        (
            {
                "logical_call_id": "call-stream",
                "provider_model_api_name": "primary-text",
                "selector": exact_selector,
                "elapsed_ms": 1000,
                "attempts": [succeeded_attempt],
                "usage": usage,
            },
        ),
    )
    require_valid_instances(
        spec,
        "AdministratorEmbeddingRequest",
        (
            {"selector": assignment_selector, "inputs": ["one"] * 32},
            {"selector": exact_selector, "inputs": ["one"]},
        ),
    )
    require_invalid_instances(
        spec,
        "AdministratorEmbeddingRequest",
        (
            {
                "workspace_api_name": "billing-production",
                "selector": exact_selector,
                "inputs": ["one"],
            },
            {"selector": {"assignment_api_name": "embed"}, "inputs": ["one"]},
            {"selector": exact_selector, "inputs": []},
            {"selector": exact_selector, "inputs": ["one"] * 33},
            {"selector": exact_selector, "inputs": ["x" * 32769]},
        ),
    )
    embedding_result = {
        "logical_call_id": "call-embedding",
        "selector": assignment_selector,
        "elapsed_ms": 1000,
        "attempts": [
            {**succeeded_attempt, "provider_model_api_name": "primary-embedding"}
        ],
        "result": {
            "provider_model_api_name": "primary-embedding",
            "embeddings": [{"index": 0, "values": [0.1, 0.2]}],
            "usage": usage,
        },
    }
    require_valid_instances(
        spec,
        "AdministratorEmbeddingResult",
        (embedding_result,),
    )
    require_invalid_instances(
        spec,
        "AdministratorEmbeddingResult",
        (
            {**embedding_result, "workspace_api_name": "production"},
            {**embedding_result, "elapsed_ms": 900001},
        ),
    )
    require_valid_instances(
        spec,
        "AdministratorMediaJobRequest",
        (
            {
                "selector": assignment_selector,
                "kind": "image",
                "prompt": "Create an image.",
                "input_images": [image],
            },
            {
                "selector": exact_selector,
                "kind": "video",
                "prompt": "Create a video.",
            },
            {
                "selector": exact_selector,
                "kind": "audio",
                "prompt": "Create audio.",
            },
        ),
    )
    require_invalid_instances(
        spec,
        "AdministratorMediaJobRequest",
        (
            {
                "selector": exact_selector,
                "kind": "audio",
                "prompt": "Create audio.",
                "input_images": [image],
            },
            {
                "selector": {"assignment_api_name": "image"},
                "kind": "image",
                "prompt": "Create an image.",
            },
            {
                "workspace_api_name": "billing-production",
                "selector": exact_selector,
                "kind": "image",
                "prompt": "Create an image.",
            },
            {
                "selector": exact_selector,
                "kind": "image",
                "prompt": "Create an image.",
                "input_images": [image] * 9,
            },
        ),
    )
    pending_job = {
        "id": "media-admin",
        "logical_call_id": "call-admin",
        "selector": exact_selector,
        "provider_model_api_name": "primary-image",
        "kind": "image",
        "state": "pending",
        "attempts": [],
        "created_at": "2026-08-24T00:00:00Z",
    }
    require_valid_instances(
        spec,
        "AdministratorMediaJob",
        (
            pending_job,
            {
                **pending_job,
                "state": "succeeded",
                "elapsed_ms": 1000,
                "attempts": [
                    {
                        **succeeded_attempt,
                        "provider_model_api_name": "primary-image",
                    }
                ],
                "usage": usage,
                "content": {"media_type": "image/png", "size_bytes": 1024},
                "completed_at": "2026-08-24T00:00:01Z",
            },
            {
                **pending_job,
                "state": "failed",
                "elapsed_ms": 1000,
                "attempts": [
                    {
                        "provider_model_api_name": "primary-image",
                        "outcome": "failed",
                        "elapsed_ms": 900,
                        "error": {
                            "code": "upstream_failed",
                            "message": "Call failed.",
                        },
                    }
                ],
                "error": {"code": "upstream_failed", "message": "Call failed."},
                "completed_at": "2026-08-24T00:00:01Z",
            },
            {
                **pending_job,
                "selector": assignment_selector,
                "state": "succeeded",
                "elapsed_ms": 1000,
                "attempts": [
                    {
                        "provider_model_api_name": "primary-image",
                        "outcome": "failed",
                        "elapsed_ms": 500,
                        "error": {
                            "code": "upstream_failed",
                            "message": "First route failed.",
                        },
                    },
                    {
                        **succeeded_attempt,
                        "provider_model_api_name": "secondary-image",
                    },
                ],
                "provider_model_api_name": "secondary-image",
                "usage": usage,
                "content": {"media_type": "image/png", "size_bytes": 1024},
                "completed_at": "2026-08-24T00:00:01Z",
            },
        ),
    )
    require_invalid_instances(
        spec,
        "AdministratorMediaJob",
        (
            {
                **pending_job,
                "content": {"media_type": "image/png", "size_bytes": 1024},
            },
            {**pending_job, "state": "succeeded"},
            {
                **pending_job,
                "state": "succeeded",
                "elapsed_ms": 1000,
                "content": {"media_type": "image/png", "size_bytes": 1024},
                "completed_at": "2026-08-24T00:00:01Z",
            },
            {
                **pending_job,
                "state": "succeeded",
                "elapsed_ms": 1000,
                "attempts": [succeeded_attempt, succeeded_attempt],
                "content": {"media_type": "image/png", "size_bytes": 1024},
                "completed_at": "2026-08-24T00:00:01Z",
            },
            {
                **pending_job,
                "state": "failed",
                "elapsed_ms": 1000,
                "content": {"media_type": "image/png", "size_bytes": 1024},
                "error": {"code": "upstream_failed", "message": "Call failed."},
                "completed_at": "2026-08-24T00:00:01Z",
            },
        ),
    )

    require_valid_instances(
        spec,
        "RequestLogSummary",
        (
            {
                "id": "log-service",
                "logical_call_id": "call-service",
                "call_actor": "service",
                "service_api_name": "billing",
                "workspace_api_name": "production",
                "kind": "model",
                "outcome": "succeeded",
                "started_at": "2026-08-24T00:00:00Z",
            },
            {
                "id": "log-admin-exact",
                "logical_call_id": "call-admin-exact",
                "call_actor": "administrator",
                "administrator_subject": "issuer|subject",
                "provider_model_api_name": "primary-text",
                "kind": "model",
                "outcome": "succeeded",
                "started_at": "2026-08-24T00:00:00Z",
            },
            {
                "id": "log-admin-assignment",
                "logical_call_id": "call-admin-assignment",
                "call_actor": "administrator",
                "administrator_subject": "issuer|subject",
                "configuration_service_api_name": "billing",
                "assignment_api_name": "summarize",
                "kind": "model",
                "outcome": "failed",
                "started_at": "2026-08-24T00:00:00Z",
            },
        ),
    )
    require_invalid_instances(
        spec,
        "RequestLogSummary",
        (
            {
                "id": "log-admin-owned",
                "logical_call_id": "call-admin-owned",
                "call_actor": "administrator",
                "administrator_subject": "issuer|subject",
                "service_api_name": "billing",
                "workspace_api_name": "production",
                "kind": "model",
                "outcome": "succeeded",
                "started_at": "2026-08-24T00:00:00Z",
            },
            {
                "id": "log-service-with-admin",
                "logical_call_id": "call-service-with-admin",
                "call_actor": "service",
                "service_api_name": "billing",
                "workspace_api_name": "production",
                "administrator_subject": "issuer|subject",
                "kind": "model",
                "outcome": "succeeded",
                "started_at": "2026-08-24T00:00:00Z",
            },
        ),
    )

    dimensions = set(schemas["StatisticsDimension"].get("enum", []))
    required_dimensions = {
        "call_actor",
        "administrator",
        "configuration_service",
    }
    if not required_dimensions.issubset(dimensions):
        raise ContractError(
            "Statistics cannot separate administrator playground records"
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
    check_administrator_playground(spec)
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
