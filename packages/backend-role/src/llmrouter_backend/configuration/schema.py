"""Closed registered-schema and structured-field validation."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .errors import ValidationIssue

if TYPE_CHECKING:
    from .model import RegisteredDocument

_SCHEMA_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,99}$")
_STRUCTURED_SECRET_FIELDS = frozenset(
    {"api_key", "authorization", "credential", "password", "secret", "token"}
)
_MAXIMUM_ENDPOINT_CHARACTERS = 2_048


@dataclass(frozen=True, slots=True)
class RegisteredSchema:
    """One closed settings schema major version."""

    schema_name: str
    major_version: int
    field_types: dict[str, type | tuple[type, ...]]
    required_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Reject an invalid registry definition."""
        if not _SCHEMA_NAME.fullmatch(self.schema_name) or self.major_version < 1:
            msg = "A registered schema needs a valid name and major version."
            raise ValueError(msg)
        if not self.required_fields <= self.field_types.keys():
            msg = "Each required field must occur in the registered schema."
            raise ValueError(msg)
        forbidden = _STRUCTURED_SECRET_FIELDS.intersection(self.field_types)
        if forbidden:
            msg = "A non-secret settings schema must not register secret fields."
            raise ValueError(msg)


class SettingsSchemaRegistry:
    """A bounded process-local registry for closed settings documents."""

    def __init__(self, schemas: tuple[RegisteredSchema, ...]) -> None:
        """Index unique schema major versions."""
        self._schemas: dict[tuple[str, int], RegisteredSchema] = {}
        for schema in schemas:
            key = (schema.schema_name, schema.major_version)
            if key in self._schemas:
                msg = "A schema major version must be unique."
                raise ValueError(msg)
            self._schemas[key] = schema

    def validate(
        self, value: RegisteredDocument, *, field_path: str
    ) -> tuple[ValidationIssue, ...]:
        """Return safe issues for one closed registered document."""
        schema = self._schemas.get((value.schema_name, value.major_version))
        if schema is None:
            return (ValidationIssue(field_path, "The registered schema is unknown."),)
        issues: list[ValidationIssue] = []
        fields = set(value.document)
        issues.extend(
            ValidationIssue(f"{field_path}.document.{field}", "The field is unknown.")
            for field in sorted(fields - schema.field_types.keys())
        )
        issues.extend(
            ValidationIssue(f"{field_path}.document.{field}", "The field is required.")
            for field in sorted(schema.required_fields - fields)
        )
        for field in sorted(fields & schema.field_types.keys()):
            item = value.document[field]
            expected = schema.field_types[field]
            if not _matches_type(item, expected) or (
                isinstance(item, bool) and expected is int
            ):
                issues.append(
                    ValidationIssue(
                        f"{field_path}.document.{field}", "The field type is invalid."
                    )
                )
        issues.extend(
            _structured_secret_issues(value.document, f"{field_path}.document")
        )
        return tuple(issues)


def _matches_type(value: object, expected: type | tuple[type, ...]) -> bool:
    expected_types = expected if isinstance(expected, tuple) else (expected,)
    return any(
        (item is dict and isinstance(value, Mapping))
        or (item is list and isinstance(value, (list, tuple)))
        or isinstance(value, item)
        for item in expected_types
    )


def validate_endpoint(  # noqa: PLR0911
    value: str, *, field_path: str
) -> tuple[ValidationIssue, ...]:
    """Enforce HTTPS, with plaintext HTTP only on explicit loopback hosts."""
    if len(value) > _MAXIMUM_ENDPOINT_CHARACTERS:
        return (ValidationIssue(field_path, "The endpoint is too long."),)
    try:
        endpoint = urlsplit(value)
        host = endpoint.hostname
        port = endpoint.port
    except ValueError:
        return (ValidationIssue(field_path, "The endpoint is invalid."),)
    if not host or endpoint.username is not None or endpoint.password is not None:
        return (ValidationIssue(field_path, "The endpoint is invalid."),)
    if endpoint.query or endpoint.fragment:
        return (
            ValidationIssue(
                field_path, "The endpoint must not contain query or fragment data."
            ),
        )
    if endpoint.scheme == "https":
        return ()
    if endpoint.scheme != "http" or not _is_loopback(host) or port is None:
        return (
            ValidationIssue(
                field_path,
                "Plaintext HTTP is permitted only on an explicit loopback endpoint.",
            ),
        )
    return ()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _structured_secret_issues(
    value: object, field_path: str
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{field_path}.{key}"
            if key.casefold() in _STRUCTURED_SECRET_FIELDS:
                issues.append(
                    ValidationIssue(
                        child, "Secret material is not permitted in this field."
                    )
                )
            elif isinstance(item, (Mapping, list, tuple)):
                issues.extend(_structured_secret_issues(item, child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            if isinstance(item, (Mapping, list, tuple)):
                issues.extend(_structured_secret_issues(item, f"{field_path}[{index}]"))
    return tuple(issues)
