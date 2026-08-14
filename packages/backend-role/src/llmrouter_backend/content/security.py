"""Deterministic control-secret rejection and authenticated-value redaction."""
# ruff: noqa: EM101, TRY003

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import JsonValue

REDACTED_VALUE = "[REDACTED]"

# These are structured control fields. This is not a content pattern scanner.
_CONTROL_FIELD_NAMES = frozenset(
    {
        "authorization",
        "authorization_header",
        "access_token",
        "api_key",
        "bootstrap_secret",
        "client_secret",
        "cookie",
        "credential",
        "passkey_secret",
        "pkce_verifier",
        "private_key",
        "refresh_token",
        "secret",
        "session_cookie",
        "token",
    }
)
_CONTROL_CONTAINERS = frozenset({"authentication", "control", "credentials", "headers"})


def reject_structured_control_fields(document: JsonValue) -> None:
    """Reject a declared control field without scanning arbitrary text."""
    _reject(document, ())


def redact_authenticated_values(
    document: JsonValue, authenticated_control_values: Sequence[str]
) -> JsonValue:
    """Remove only exact known control values from the captured document."""
    values = tuple(
        sorted(
            {value for value in authenticated_control_values if value},
            key=lambda item: (-len(item), item),
        )
    )
    return _redact(document, values)


def _reject(value: JsonValue, path: tuple[str, ...]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = key.casefold().replace("-", "_")
            parent = "" if not path else path[-1]
            if normalized in _CONTROL_FIELD_NAMES and (
                not path or parent in _CONTROL_CONTAINERS
            ):
                raise ValueError(
                    "Captured content contains a structured control field."
                )
            _reject(item, (*path, normalized))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject(item, path)


def _redact(value: JsonValue, controls: tuple[str, ...]) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _redact(item, controls) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact(item, controls) for item in value]
    if isinstance(value, str):
        result = value
        for control in controls:
            result = result.replace(control, REDACTED_VALUE)
        return result
    return value
