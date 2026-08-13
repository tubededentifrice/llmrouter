"""Runtime validation for generated public contract models."""
# ruff: noqa: EM102, TRY003, UP047

from __future__ import annotations

from datetime import datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from referencing import Registry, Resource

from llmrouter_client.generated_models import CONTRACT_SCHEMAS

ContractValue = TypeVar("ContractValue")
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time")  # type: ignore[untyped-decorator]
def _is_date_time(value: object) -> bool:
    """Check the date-time format without an optional package."""
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


@FORMAT_CHECKER.checks("uuid")  # type: ignore[untyped-decorator]
def _is_uuid(value: object) -> bool:
    """Check a canonical UUID string."""
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


@FORMAT_CHECKER.checks("uri")  # type: ignore[untyped-decorator]
def _is_uri(value: object) -> bool:
    """Check an absolute URI."""
    return isinstance(value, str) and bool(urlsplit(value).scheme)


class ContractValidationError(ValueError):
    """A value does not match its selected public contract schema."""


def validate_contract(schema_name: str, value: ContractValue) -> ContractValue:
    """Validate and return one value against a closed generated schema."""
    schema = CONTRACT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ContractValidationError(f"Unknown contract schema: {schema_name}")
    root_schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:llmrouter:contracts",
        "components": {"schemas": CONTRACT_SCHEMAS},
    }
    registry = Registry().with_resource(
        "urn:llmrouter:contracts", Resource.from_contents(root_schema)
    )
    validator = Draft202012Validator(
        {"$ref": f"urn:llmrouter:contracts#/components/schemas/{schema_name}"},
        registry=registry,
        format_checker=FORMAT_CHECKER,
    )
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ContractValidationError(f"{schema_name}.{path}: {error.message}")
    return value


__all__ = ["ContractValidationError", "validate_contract"]
