"""Stable and content-free global audit discovery."""
# ruff: noqa: D107, EM101, PLR0913, PLR0917, PLR2004, TRY003

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from llmrouter_backend.admin_auth import AdministratorAuthError
from llmrouter_backend.authority import (
    ADMINISTRATOR_OPERATIONS,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    ScopeKind,
)

if TYPE_CHECKING:
    from llmrouter_backend.authority import RequestContext

_PAGE_SIZE = 100
_CURSOR_VERSION = 3
_MAX_CURSOR_LENGTH = 1_000
_CURSOR_SIGNATURE_LENGTH = 32
_MAX_UNSIGNED = (1 << 64) - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ACTOR_PSEUDONYM_DOMAIN = b"llmrouter-audit-actor-v1\0"
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")
_SAFE_ACTIONS = ADMINISTRATOR_OPERATIONS | frozenset(
    {
        "administrator.grant.create",
        "administrator.grant.list",
        "administrator.grant.revoke",
        "administrator.session.complete",
        "administrator.session.invalidate",
        "administrator.session.logout",
        "administrator.session.start",
        "administrator.trusted_grant.create",
        "administrator.trusted_grant.failure",
        "administrator.trusted_grant.redeem",
        "administrator.trusted_grant.success",
        "budget_ceiling.write",
        "capture_policy.write",
        "captured_content.discover",
        "captured_content.read",
        "configuration.publish",
        "configuration.rollback",
        "credential.create",
        "credential.disable",
        "credential.retire",
        "credential.revoke",
        "credential.rotate",
        "credential.tls_identity",
        "credential.tls_policy",
        "credential.tls_revoke",
        "credential.wrapping_key.rotate",
        "diagnostic.grant.create",
        "diagnostic.route.use",
        "embed_session.bootstrap",
        "embed_session.create",
        "embed_session.revoke",
        "export.location.issue",
        "export.location.redeem",
        "export.status.read",
        "price.publish",
        "price.synchronize",
        "retention_limits.write",
        "service.create",
        "service.disable",
        "service.metadata",
        "service.parent",
        "service.restore",
        "service.retire",
        "workspace.create",
        "workspace.disable",
        "workspace.restore",
        "workspace.retire",
    }
)
_SAFE_RESOURCE_TYPES = frozenset(
    {
        "budget_limit",
        "capture_policy",
        "captured_content",
        "captured_content_page",
        "configuration_revision",
        "embed_session",
        "protected_export",
        "retention_configuration",
        "retention_limits",
        "service",
        "workspace",
        "workspace_budget_ceiling",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
        "allowance_unavailable",
        "assignment_unavailable",
        "attachment_already_complete",
        "attachment_invalid",
        "attachment_not_found",
        "budget_ceiling_conflict",
        "budget_exhausted",
        "capability_mismatch",
        "configuration_revision_conflict",
        "diagnostic_permission_required",
        "embedding_space_mismatch",
        "idempotency_conflict",
        "internal_error",
        "insufficient_scope",
        "invalid_request",
        "invalid_token",
        "not_found",
        "policy_denied",
        "rate_limited",
        "recent_auth_required",
        "request_identity_conflict",
        "request_identity_expired",
        "request_not_found",
        "secret_detected",
        "service_scope_mismatch",
        "spool_capacity_exhausted",
        "stale_configuration",
        "state_revision_conflict",
        "stream_replay_unavailable",
        "temporarily_unavailable",
        "terminal_state",
        "unsupported_capability",
        "unsupported_contract",
        "workspace_not_found",
        "workspace_retired",
        "workspace_scope_mismatch",
        "workspace_unavailable",
    }
)


class AuditDiscoveryError(ValueError):
    """One safe invalid audit filter or cursor error."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.safe_message = message


class PostgresAuditRepository:
    """Read immutable audit rows through one global administrator path."""

    def __init__(self, database_url: str, *, cursor_key: bytes) -> None:
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        if len(cursor_key) < 32:
            raise ValueError("The audit cursor key must contain at least 32 bytes.")
        self._database_url = database_url
        self._cursor_key = cursor_key

    def list_events(
        self,
        context: RequestContext,
        *,
        start: datetime,
        end: datetime,
        cursor: str | None = None,
    ) -> tuple[tuple[dict[str, object], ...], str | None]:
        """Return one stable keyset page with closed safe fields."""
        _require_global_audit_context(context)
        _require_range(start, end)
        parsed = (
            None
            if cursor is None
            else _decode_cursor(self._cursor_key, cursor, start, end)
        )
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            _record_read(connection, context)
            if parsed is None:
                snapshot = connection.execute(
                    "SELECT pg_current_snapshot()::text AS value"
                ).fetchone()
                snapshot_value = None if snapshot is None else snapshot.get("value")
                if not isinstance(snapshot_value, str):
                    raise RuntimeError("The audit database snapshot is unavailable.")
                database_snapshot = snapshot_value
            else:
                database_snapshot = parsed[0]
            parameters: list[object] = [start, end, database_snapshot]
            keyset = ""
            if parsed is not None:
                keyset = "AND (occurred_at, event_id) < (%s, %s)"
                parameters.extend((parsed[1], parsed[2]))
            parameters.append(_PAGE_SIZE + 1)
            rows = connection.execute(
                f"""
                SELECT event_id, actor_kind, actor_id, authority_class,
                       service_id, workspace_id, action, permission_result,
                       safe_details, occurred_at
                FROM router.audit_events
                WHERE occurred_at >= %s AND occurred_at < %s
                  AND pg_visible_in_snapshot(
                      xmin::text::xid8, %s::pg_snapshot
                  )
                  {keyset}
                ORDER BY occurred_at DESC, event_id DESC
                LIMIT %s
                """,  # noqa: S608 - keyset is one closed local SQL clause.  # nosec B608
                parameters,
            ).fetchall()
        visible = rows[:_PAGE_SIZE]
        items = tuple(_event_document(self._cursor_key, row) for row in visible)
        next_cursor = None
        if len(rows) > _PAGE_SIZE and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                self._cursor_key,
                start,
                end,
                database_snapshot,
                last["occurred_at"],
                last["event_id"],
            )
        return items, next_cursor


def _require_global_audit_context(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.operation == "audit.read"
        and context.scope.kind is ScopeKind.GLOBAL
    ):
        raise AdministratorAuthError("insufficient_scope", context.request_id)


def _require_range(start: datetime, end: datetime) -> None:
    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
        or start >= end
    ):
        raise AuditDiscoveryError(
            "from,to",
            "The audit time range must contain an aware start before its end.",
        )


def _encode_cursor(
    cursor_key: bytes,
    start: datetime,
    end: datetime,
    database_snapshot: str,
    occurred_at: datetime,
    event_id: uuid.UUID,
) -> str:
    snapshot_minimum, snapshot_maximum, in_progress = _snapshot_parts(database_snapshot)
    payload = bytearray([_CURSOR_VERSION])
    payload.extend(
        struct.pack(
            ">qq",
            _timestamp_microseconds(start),
            _timestamp_microseconds(end),
        )
    )
    payload.extend(_encode_unsigned(snapshot_minimum))
    payload.extend(_encode_unsigned(snapshot_maximum - snapshot_minimum))
    payload.extend(_encode_unsigned(len(in_progress)))
    previous = snapshot_minimum
    for identity in in_progress:
        payload.extend(_encode_unsigned(identity - previous))
        previous = identity
    payload.extend(struct.pack(">q", _timestamp_microseconds(occurred_at)))
    payload.extend(event_id.bytes)
    signature = hmac.digest(cursor_key, payload, hashlib.sha256)
    value = base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode()
    if len(value) > _MAX_CURSOR_LENGTH:
        raise RuntimeError("The audit page cursor exceeds the contract limit.")
    return value


def _decode_cursor(
    cursor_key: bytes, value: str, start: datetime, end: datetime
) -> tuple[str, datetime, uuid.UUID]:
    try:
        snapshot, occurred_at, event_id = _parse_cursor(cursor_key, value, start, end)
    except OverflowError, TypeError, ValueError, struct.error:
        raise AuditDiscoveryError(
            "cursor", "The audit page cursor is invalid for this time range."
        ) from None
    return snapshot, occurred_at, event_id


def _parse_cursor(
    cursor_key: bytes, value: str, start: datetime, end: datetime
) -> tuple[str, datetime, uuid.UUID]:
    if not value or len(value) > _MAX_CURSOR_LENGTH:
        raise ValueError
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    padded = value + "=" * (-len(value) % 4)
    signed = base64.urlsafe_b64decode(padded)
    if base64.urlsafe_b64encode(signed).rstrip(b"=").decode() != value:
        raise ValueError
    if len(signed) <= _CURSOR_SIGNATURE_LENGTH:
        raise ValueError
    payload = signed[:-_CURSOR_SIGNATURE_LENGTH]
    signature = signed[-_CURSOR_SIGNATURE_LENGTH:]
    expected = hmac.digest(cursor_key, payload, hashlib.sha256)
    if not hmac.compare_digest(signature, expected):
        raise ValueError
    return _parse_cursor_payload(payload, start, end)


def _parse_cursor_payload(
    payload: bytes, start: datetime, end: datetime
) -> tuple[str, datetime, uuid.UUID]:
    if payload[0] != _CURSOR_VERSION:
        raise ValueError
    range_start, range_end = struct.unpack_from(">qq", payload, 1)
    if range_start != _timestamp_microseconds(
        start
    ) or range_end != _timestamp_microseconds(end):
        raise ValueError
    offset = 17
    snapshot_minimum, offset = _decode_unsigned(payload, offset)
    snapshot_span, offset = _decode_unsigned(payload, offset)
    snapshot_maximum = snapshot_minimum + snapshot_span
    if snapshot_maximum > _MAX_UNSIGNED:
        raise ValueError
    count, offset = _decode_unsigned(payload, offset)
    in_progress: list[int] = []
    previous = snapshot_minimum
    for index in range(count):
        delta, offset = _decode_unsigned(payload, offset)
        identity = previous + delta
        if identity >= snapshot_maximum or (index > 0 and identity <= previous):
            raise ValueError
        in_progress.append(identity)
        previous = identity
    occurred_value = struct.unpack_from(">q", payload, offset)[0]
    offset += 8
    if len(payload) != offset + 16:
        raise ValueError
    event_id = uuid.UUID(bytes=payload[offset:])
    occurred_at = _from_microseconds(occurred_value)
    if not start <= occurred_at < end:
        raise ValueError
    identities = ",".join(str(identity) for identity in in_progress)
    snapshot = f"{snapshot_minimum}:{snapshot_maximum}:{identities}"
    return snapshot, occurred_at, event_id


def _snapshot_parts(value: str) -> tuple[int, int, tuple[int, ...]]:
    minimum_text, separator, remainder = value.partition(":")
    maximum_text, second_separator, identities_text = remainder.partition(":")
    if not separator or not second_separator:
        raise ValueError
    minimum = int(minimum_text)
    maximum = int(maximum_text)
    identities = tuple(
        int(identity) for identity in identities_text.split(",") if identity
    )
    if (
        minimum < 0
        or maximum < minimum
        or maximum > _MAX_UNSIGNED
        or any(
            identity < minimum
            or identity >= maximum
            or (index > 0 and identity <= identities[index - 1])
            for index, identity in enumerate(identities)
        )
    ):
        raise ValueError
    return minimum, maximum, identities


def _encode_unsigned(value: int) -> bytes:
    if value < 0 or value > _MAX_UNSIGNED:
        raise ValueError
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _decode_unsigned(payload: bytes, offset: int) -> tuple[int, int]:
    start = offset
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(payload):
            raise ValueError
        item = payload[offset]
        offset += 1
        value |= (item & 0x7F) << shift
        if item < 0x80:
            if value > _MAX_UNSIGNED or payload[start:offset] != _encode_unsigned(
                value
            ):
                raise ValueError
            return value, offset
    raise ValueError


def _timestamp_microseconds(value: datetime) -> int:
    delta = value.astimezone(UTC) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _from_microseconds(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _event_document(cursor_key: bytes, row: dict[str, Any]) -> dict[str, object]:
    actor_kind = str(row["actor_kind"])
    scope: dict[str, object] = {"authority_class": str(row["authority_class"])}
    if row["service_id"] is not None:
        scope["service_id"] = str(row["service_id"])
    if row["workspace_id"] is not None:
        scope["workspace_id"] = str(row["workspace_id"])
    result: dict[str, object] = {
        "event_id": str(row["event_id"]),
        "occurred_at": row["occurred_at"].isoformat(),
        "actor": _safe_actor(cursor_key, actor_kind, str(row["actor_id"])),
        "action": safe_audit_action(str(row["action"])),
        "outcome": str(row["permission_result"]),
        "scope": scope,
    }
    detail = _safe_detail(row["safe_details"])
    if detail:
        result["safe_detail"] = detail
    return result


def _safe_actor(cursor_key: bytes, actor_kind: str, actor_id: str) -> str:
    fallback = _safe_name(actor_kind, "unknown")
    digest = hmac.digest(
        cursor_key,
        _ACTOR_PSEUDONYM_DOMAIN + f"{fallback}\0{actor_id}".encode(),
        hashlib.sha256,
    ).hex()[:16]
    return f"{fallback}:{digest}"


def _safe_detail(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    resource_type = value.get("resource_type")
    if isinstance(resource_type, str) and resource_type in _SAFE_RESOURCE_TYPES:
        result["resource_type"] = resource_type
    resource_id = value.get("resource_id")
    if isinstance(resource_id, str) and _canonical_uuid(resource_id):
        result["resource_id"] = resource_id
    error_code = value.get("safe_error_code")
    if isinstance(error_code, str) and error_code in _SAFE_ERROR_CODES:
        result["safe_error_code"] = error_code
    return result


def _safe_name(value: str, fallback: str) -> str:
    return value if _SAFE_NAME.fullmatch(value) else fallback


def safe_audit_action(value: str) -> str:
    """Return one published action or the closed unknown value."""
    return value if value in _SAFE_ACTIONS else "unknown"


def _record_read(connection: psycopg.Connection[Any], context: RequestContext) -> None:
    """Append the permitted global audit read before its stable snapshot."""
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            action, permission_result, safe_details, occurred_at
        ) VALUES (
            %s, 'global_administration', %s, %s, 'global_administrator',
            'audit.read', 'permitted', '{}'::jsonb, %s
        )
        """,
        (
            uuid.uuid4(),
            context.actor_kind.value,
            context.actor_id,
            context.authorized_at,
        ),
    )


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False
