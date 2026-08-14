"""PostgreSQL content capture, retention, export, and lifecycle custody."""
# ruff: noqa: D107, E501, EM101, PLC0415, PLR0913, PLR0915, PLR2004, S101, TC001, TRY003

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import secrets
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import psycopg
from psycopg.rows import dict_row

from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
)
from llmrouter_backend.credential_store.crypto import (
    EncryptedEnvelope,
    EnvelopeCipher,
    EnvelopeDecryptionError,
)

from .errors import ContentError, ContentErrorCode
from .model import (
    MAXIMUM_DISCOVERY_ITEMS,
    MAXIMUM_REDEMPTION_AGE,
    CapturedContent,
    CapturedContentMetadata,
    CapturePolicy,
    CaptureReason,
    EffectiveCapture,
    ExportDataClass,
    ExportOperation,
    ExportRequest,
    ExportState,
    LifecycleLease,
    ObjectManifest,
    ObjectSegment,
    RedeemedExport,
    RetentionDataClass,
    RetentionEffect,
    RetentionLimit,
    RetentionPreview,
    RetentionSelection,
)
from .object_store import ObjectStore
from .security import redact_authenticated_values, reject_structured_control_fields

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg import Connection

    from llmrouter_backend.authority import RequestContext

    from .model import JsonValue

_CAPTURE_ORDER = {
    CapturePolicy.DISABLED: 0,
    CapturePolicy.METADATA_ONLY: 1,
    CapturePolicy.COMPLETE: 2,
}
_PREVIEW_LIFETIME = timedelta(minutes=5)
_DEFAULT_EXPORT_LIFETIME = timedelta(hours=1)
_DEFAULT_LEASE_LIFETIME = timedelta(minutes=2)


class PostgresContentRepository:
    """Keep captured values outside PostgreSQL and lifecycle state inside it."""

    def __init__(
        self,
        database_url: str,
        *,
        cipher: EnvelopeCipher,
        object_store: ObjectStore,
        token_digest_key: bytes,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        if len(token_digest_key) < 32:
            raise ValueError("The export token digest key must contain 32 bytes.")
        self._database_url = database_url
        self._cipher = cipher
        self._object_store = object_store
        self._token_digest_key = token_digest_key
        self._identity_factory = identity_factory
        self._random_bytes = random_bytes

    def resolve_capture(
        self,
        service_id: str,
        workspace_id: str | None,
        *,
        admitted_at: datetime,
        spool_pressure: bool = False,
    ) -> EffectiveCapture:
        """Resolve nearest replacement and snapshot its admission-time expiry."""
        _require_aware(admitted_at)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ON (scope_kind) scope_kind, policy,
                       minimum_policy, maximum_policy
                FROM router.capture_policies
                WHERE effective_at <= %s
                  AND (
                    scope_kind = 'global'
                    OR (scope_kind = 'service' AND service_id = %s)
                    OR (scope_kind = 'workspace' AND service_id = %s
                        AND workspace_id = %s)
                  )
                ORDER BY scope_kind, revision DESC
                """,
                (admitted_at, service_id, service_id, workspace_id),
            ).fetchall()
            by_scope = {row["scope_kind"]: row for row in rows}
            global_row = by_scope.get("global")
            if global_row is None:
                raise RuntimeError("The global capture policy is missing.")
            selected_scope = (
                "workspace"
                if workspace_id is not None and "workspace" in by_scope
                else "service"
                if "service" in by_scope
                else "global"
            )
            configured = CapturePolicy(by_scope[selected_scope]["policy"])
            minimum = CapturePolicy(global_row["minimum_policy"])
            maximum = CapturePolicy(global_row["maximum_policy"])
            if (
                not _CAPTURE_ORDER[minimum]
                <= _CAPTURE_ORDER[configured]
                <= _CAPTURE_ORDER[maximum]
            ):
                raise RuntimeError(
                    "A configured capture policy is outside global limits."
                )
            if spool_pressure:
                return EffectiveCapture(
                    CapturePolicy.DISABLED,
                    CaptureReason.SPOOL_PRESSURE,
                    selected_scope,
                    None,
                )
            expiry = None
            if configured is not CapturePolicy.DISABLED:
                retention = self.resolve_retention(
                    service_id,
                    workspace_id,
                    RetentionDataClass.CAPTURED_CONTENT,
                    effective_at=admitted_at,
                )
                expiry = admitted_at + timedelta(days=retention.days)
            return EffectiveCapture(
                configured, CaptureReason.CONFIGURED, selected_scope, expiry
            )

    def put_capture_policy(
        self,
        context: RequestContext,
        policy: CapturePolicy,
        *,
        now: datetime,
        minimum_policy: CapturePolicy | None = None,
        maximum_policy: CapturePolicy | None = None,
    ) -> int:
        """Publish one capture replacement at the authorized exact scope."""
        _require_configuration_mutation(context)
        _require_aware(now)
        is_global = context.scope.service_id is None
        if is_global != (minimum_policy is not None and maximum_policy is not None):
            raise ContentError(ContentErrorCode.INVALID, context.request_id)
        if (
            minimum_policy is not None
            and maximum_policy is not None
            and not _CAPTURE_ORDER[minimum_policy]
            <= _CAPTURE_ORDER[policy]
            <= _CAPTURE_ORDER[maximum_policy]
        ):
            raise ContentError(ContentErrorCode.INVALID, context.request_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_configuration_scope(connection, context, "capture-policy")
            if is_global:
                assert minimum_policy is not None
                assert maximum_policy is not None
                configured_rows = connection.execute(
                    """
                    SELECT DISTINCT ON (scope_kind, service_id, workspace_id) policy
                    FROM router.capture_policies
                    WHERE scope_kind <> 'global'
                    ORDER BY scope_kind, service_id, workspace_id, revision DESC
                    """,
                ).fetchall()
                if any(
                    not _CAPTURE_ORDER[minimum_policy]
                    <= _CAPTURE_ORDER[CapturePolicy(row["policy"])]
                    <= _CAPTURE_ORDER[maximum_policy]
                    for row in configured_rows
                ):
                    raise ContentError(ContentErrorCode.CONFLICT, context.request_id)
            else:
                global_row = connection.execute(
                    """
                    SELECT minimum_policy, maximum_policy
                    FROM router.capture_policies
                    WHERE scope_kind = 'global' AND effective_at <= %s
                    ORDER BY revision DESC LIMIT 1 FOR SHARE
                    """,
                    (now,),
                ).fetchone()
                if global_row is None or not (
                    _CAPTURE_ORDER[CapturePolicy(global_row["minimum_policy"])]
                    <= _CAPTURE_ORDER[policy]
                    <= _CAPTURE_ORDER[CapturePolicy(global_row["maximum_policy"])]
                ):
                    raise ContentError(ContentErrorCode.INVALID, context.request_id)
            next_row = connection.execute(
                """
                SELECT coalesce(max(revision), 0) + 1 AS next_revision
                FROM router.capture_policies
                WHERE scope_kind = %s AND service_id IS NOT DISTINCT FROM %s
                  AND workspace_id IS NOT DISTINCT FROM %s
                """,
                (
                    context.scope.kind.value,
                    context.scope.service_id,
                    context.scope.workspace_id,
                ),
            ).fetchone()
            assert next_row is not None
            revision = int(next_row["next_revision"])
            connection.execute(
                """
                INSERT INTO router.capture_policies (
                    id, scope_kind, service_id, workspace_id, policy,
                    minimum_policy, maximum_policy, revision, effective_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._identity_factory(),
                    context.scope.kind.value,
                    context.scope.service_id,
                    context.scope.workspace_id,
                    policy.value,
                    None if minimum_policy is None else minimum_policy.value,
                    None if maximum_policy is None else maximum_policy.value,
                    revision,
                    now,
                ),
            )
            _audit(
                connection,
                context,
                action="capture_policy.write",
                resource_type="capture_policy",
                resource_id=str(revision),
                now=now,
            )
        return revision

    def put_retention_limits(
        self,
        context: RequestContext,
        limits: Sequence[RetentionLimit],
        *,
        now: datetime,
    ) -> None:
        """Change fleet limits without leaving an invalid current selection."""
        _require_global_retention_mutation(context)
        _require_aware(now)
        if not limits or len({item.data_class for item in limits}) != len(limits):
            raise ContentError(ContentErrorCode.INVALID, context.request_id)
        for limit in limits:
            if not 1 <= limit.minimum_days <= limit.maximum_days <= 36500:
                raise ContentError(ContentErrorCode.INVALID, context.request_id)
            if limit.data_class is RetentionDataClass.AGENT_TOOL_AUDIT and (
                limit.minimum_days < 7 or limit.maximum_days > 365
            ):
                raise ContentError(ContentErrorCode.INVALID, context.request_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_configuration_family(connection, "retention-policy")
            for limit in limits:
                latest = connection.execute(
                    """
                    SELECT DISTINCT ON (scope_kind, service_id, workspace_id)
                           retention_days, minimum_revision_count
                    FROM router.retention_policies
                    WHERE data_class = %s
                    ORDER BY scope_kind, service_id, workspace_id, revision DESC
                    """,
                    (limit.data_class.value,),
                ).fetchall()
                for row in latest:
                    if not limit.permits(
                        RetentionSelection(
                            limit.data_class,
                            row["retention_days"],
                            row["minimum_revision_count"],
                        )
                    ):
                        raise ContentError(
                            ContentErrorCode.CONFLICT, context.request_id
                        )
                connection.execute(
                    """
                    UPDATE router.retention_limits SET
                        minimum_days = %s, maximum_days = %s,
                        allowed_minimum_count = %s, allowed_maximum_count = %s,
                        revision = revision + 1, updated_at = %s
                    WHERE data_class = %s
                    """,
                    (
                        limit.minimum_days,
                        limit.maximum_days,
                        limit.allowed_minimum_count,
                        limit.allowed_maximum_count,
                        now,
                        limit.data_class.value,
                    ),
                )
            _audit(
                connection,
                context,
                action="retention_limits.write",
                resource_type="retention_limits",
                resource_id=context.request_id,
                now=now,
            )

    def resolve_retention(
        self,
        service_id: str,
        workspace_id: str | None,
        data_class: RetentionDataClass,
        *,
        effective_at: datetime,
    ) -> RetentionSelection:
        """Return the nearest effective replacement for one separate data class."""
        _require_aware(effective_at)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = _effective_retention_row(
                connection,
                service_id,
                workspace_id,
                data_class,
                effective_at=effective_at,
            )
        if row is None:
            raise RuntimeError("The global retention configuration is missing.")
        return RetentionSelection(
            data_class, row["retention_days"], row["minimum_revision_count"]
        )

    def preview_retention(
        self,
        context: RequestContext,
        selections: Sequence[RetentionSelection],
        *,
        expected_revision: str,
        now: datetime,
    ) -> RetentionPreview:
        """Store a bounded no-effect preview for later exact confirmation."""
        _require_retention_context(
            context, mutation=False, operation="retention.preview"
        )
        _require_aware(now)
        normalized = _normalize_selections(selections)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_configuration_scope(connection, context, "retention-policy")
            _require_retention_limits(connection, normalized)
            actual_revision = _scope_retention_revision(connection, context)
            if actual_revision != expected_revision:
                raise ContentError(ContentErrorCode.CONFLICT, context.request_id)
            effects = _estimate_effects(connection, context, normalized, now=now)
            preview_id = self._identity_factory()
            expires_at = now + _PREVIEW_LIFETIME
            connection.execute(
                """
                INSERT INTO router.retention_previews (
                    id, actor_id, scope_kind, service_id, workspace_id,
                    expected_revision, selection_fingerprint, effects,
                    created_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    preview_id,
                    context.actor_id,
                    context.scope.kind.value,
                    context.scope.service_id,
                    context.scope.workspace_id,
                    expected_revision,
                    _selection_fingerprint(normalized),
                    json.dumps([_effect_document(item) for item in effects]),
                    now,
                    expires_at,
                ),
            )
        return RetentionPreview(str(preview_id), expected_revision, expires_at, effects)

    def put_retention(
        self,
        context: RequestContext,
        selections: Sequence[RetentionSelection],
        *,
        expected_revision: str,
        confirmed_preview_id: str,
        now: datetime,
    ) -> str:
        """Publish values only after an exact, live, one-use preview."""
        _require_retention_context(context, mutation=True, operation="retention.write")
        _require_aware(now)
        normalized = _normalize_selections(selections)
        try:
            preview_id = uuid.UUID(confirmed_preview_id)
        except ValueError as error:
            raise ContentError(ContentErrorCode.INVALID, context.request_id) from error
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_configuration_scope(connection, context, "retention-policy")
            _require_retention_limits(connection, normalized)
            actual_revision = _scope_retention_revision(connection, context, lock=True)
            preview = connection.execute(
                """
                SELECT * FROM router.retention_previews
                WHERE id = %s FOR UPDATE
                """,
                (preview_id,),
            ).fetchone()
            if (
                actual_revision != expected_revision
                or preview is None
                or preview["actor_id"] != context.actor_id
                or preview["scope_kind"] != context.scope.kind.value
                or preview["service_id"] != _uuid_or_none(context.scope.service_id)
                or preview["workspace_id"] != _uuid_or_none(context.scope.workspace_id)
                or preview["expected_revision"] != expected_revision
                or preview["selection_fingerprint"]
                != _selection_fingerprint(normalized)
                or preview["expires_at"] <= now
                or preview["confirmed_at"] is not None
            ):
                raise ContentError(ContentErrorCode.CONFLICT, context.request_id)
            for selection in normalized:
                next_revision_row = connection.execute(
                    """
                    SELECT coalesce(max(revision), 0) + 1 AS next_revision
                    FROM router.retention_policies
                    WHERE scope_kind = %s AND service_id IS NOT DISTINCT FROM %s
                      AND workspace_id IS NOT DISTINCT FROM %s AND data_class = %s
                    """,
                    (
                        context.scope.kind.value,
                        context.scope.service_id,
                        context.scope.workspace_id,
                        selection.data_class.value,
                    ),
                ).fetchone()
                assert next_revision_row is not None
                next_revision = next_revision_row["next_revision"]
                connection.execute(
                    """
                    INSERT INTO router.retention_policies (
                        id, scope_kind, service_id, workspace_id, data_class,
                        retention_days, minimum_revision_count, revision, effective_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self._identity_factory(),
                        context.scope.kind.value,
                        context.scope.service_id,
                        context.scope.workspace_id,
                        selection.data_class.value,
                        selection.days,
                        selection.minimum_count,
                        next_revision,
                        now,
                    ),
                )
            connection.execute(
                "UPDATE router.retention_previews SET confirmed_at = %s WHERE id = %s",
                (now, preview_id),
            )
            revision = _scope_retention_revision(connection, context)
            _audit(
                connection,
                context,
                action="retention.manage",
                resource_type="retention_configuration",
                resource_id=revision,
                now=now,
            )
        return revision

    def revision_retention_evidence(
        self,
        revision_times: Sequence[tuple[str, datetime]],
        selection: RetentionSelection,
        *,
        now: datetime,
    ) -> tuple[str, str]:
        """Return the oldest revision kept by count, days, or both."""
        if selection.data_class is not RetentionDataClass.CONFIGURATION_REVISIONS:
            raise ValueError("Revision evidence needs revision retention.")
        _require_aware(now)
        ordered = sorted(
            revision_times, key=lambda item: (item[1], item[0]), reverse=True
        )
        if not ordered:
            raise ValueError("Revision evidence needs at least one revision.")
        count = selection.minimum_count
        assert count is not None
        age_boundary = now - timedelta(days=selection.days)
        kept = [
            item
            for index, item in enumerate(ordered)
            if index < count or item[1] >= age_boundary
        ]
        oldest = kept[-1]
        by_count = ordered.index(oldest) < count
        by_age = oldest[1] >= age_boundary
        return (
            oldest[0],
            "both"
            if by_count and by_age
            else "minimum_count"
            if by_count
            else "minimum_age",
        )

    def enqueue_retention_execution(
        self,
        data_class: RetentionDataClass,
        *,
        service_id: str | None,
        workspace_id: str | None,
        now: datetime,
        limit: int = 1000,
    ) -> str:
        """Queue one bounded, fenced retention execution for one exact scope."""
        _require_aware(now)
        if (workspace_id is not None and service_id is None) or not 1 <= limit <= 1000:
            raise ValueError("The retention execution scope is invalid.")
        scope_key = ":".join(
            (
                data_class.value,
                service_id or "global",
                workspace_id or "-",
                now.isoformat(),
            )
        )
        return self.enqueue_lifecycle_job(
            "retention",
            scope_key,
            {
                "data_class": data_class.value,
                "service_id": service_id,
                "workspace_id": workspace_id,
                "limit": limit,
            },
            now=now,
        )

    def capture(
        self,
        request_row_id: str,
        content_type: str,
        value: JsonValue,
        *,
        content_id: str,
        authenticated_control_values: Sequence[str],
        now: datetime,
    ) -> CapturedContentMetadata:
        """Store one segment for one durable admitted request row exactly once."""
        _require_aware(now)
        if not content_type or len(content_type) > 200:
            raise ContentError(ContentErrorCode.INVALID, request_row_id)
        try:
            parsed_content_id = uuid.UUID(content_id)
            parsed_request_row_id = uuid.UUID(request_row_id)
            reject_structured_control_fields(value)
            safe_value = redact_authenticated_values(
                value, authenticated_control_values
            )
            plaintext = json.dumps(
                safe_value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        except (TypeError, ValueError) as error:
            raise ContentError(ContentErrorCode.INVALID, request_row_id) from error
        uploaded: tuple[str, str] | None = None
        try:
            with (
                psycopg.connect(self._database_url, row_factory=dict_row) as connection,
                connection.transaction(),
            ):
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"captured-content:{parsed_content_id}",),
                )
                request = connection.execute(
                    """
                    SELECT row_id, request_id, service_id, workspace_id,
                           capture_policy, admitted_at, captured_content_expires_at
                    FROM router.logical_requests
                    WHERE row_id = %s FOR SHARE
                    """,
                    (parsed_request_row_id,),
                ).fetchone()
                if request is None:
                    raise ContentError(ContentErrorCode.NOT_FOUND, request_row_id)
                if now < request["admitted_at"]:
                    raise ContentError(ContentErrorCode.INVALID, request_row_id)
                policy = CapturePolicy(request["capture_policy"])
                expires_at = request["captured_content_expires_at"]
                if (
                    policy is CapturePolicy.DISABLED
                    or expires_at is None
                    or expires_at <= now
                ):
                    raise ContentError(ContentErrorCode.EXPIRED, request_row_id)
                existing = connection.execute(
                    "SELECT * FROM router.captured_content WHERE id = %s FOR SHARE",
                    (parsed_content_id,),
                ).fetchone()
                plaintext_digest = hashlib.sha256(plaintext).digest()
                if existing is not None:
                    if (
                        existing["request_row_id"] != parsed_request_row_id
                        or existing["content_type"] != content_type
                        or (
                            policy is CapturePolicy.COMPLETE
                            and existing["plaintext_sha256"] != plaintext_digest
                        )
                    ):
                        raise ContentError(ContentErrorCode.CONFLICT, request_row_id)
                    return _metadata(existing)
                manifest_id: uuid.UUID | None = None
                if policy is CapturePolicy.COMPLETE:
                    manifest_id = self._identity_factory()
                    context = _encryption_context(
                        content_id=parsed_content_id,
                        request=request,
                        content_type=content_type,
                        expires_at=expires_at,
                        ordinal=1,
                        plaintext_sha256=plaintext_digest.hex(),
                    )
                    envelope = self._cipher.encrypt(plaintext, context=context)
                    ciphertext_digest = hashlib.sha256(envelope.ciphertext).hexdigest()
                    object_key = (
                        f"capture/{request['service_id']}/{manifest_id}/000001.bin"
                    )
                    self._object_store.put(
                        object_key, envelope.ciphertext, sha256=ciphertext_digest
                    )
                    uploaded = (object_key, ciphertext_digest)
                    segment = ObjectSegment(
                        1,
                        object_key,
                        len(envelope.ciphertext),
                        ciphertext_digest,
                        envelope.encrypted_data_key,
                        envelope.wrapping_key_id,
                    )
                    manifest = ObjectManifest.build(str(manifest_id), (segment,))
                    connection.execute(
                        """
                        INSERT INTO router.content_manifests (
                            id, segment_count, ciphertext_bytes, manifest_sha256, created_at
                        ) VALUES (%s, 1, %s, %s, %s)
                        """,
                        (
                            manifest_id,
                            segment.ciphertext_bytes,
                            bytes.fromhex(manifest.sha256),
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO router.content_segments (
                            manifest_id, ordinal, object_key, ciphertext_bytes,
                            ciphertext_sha256, encrypted_data_key, wrapping_key_id
                        ) VALUES (%s, 1, %s, %s, %s, %s, %s)
                        """,
                        (
                            manifest_id,
                            object_key,
                            segment.ciphertext_bytes,
                            bytes.fromhex(ciphertext_digest),
                            envelope.encrypted_data_key,
                            envelope.wrapping_key_id,
                        ),
                    )
                row = connection.execute(
                    """
                    INSERT INTO router.captured_content (
                        id, service_id, workspace_id, request_row_id, request_id, capture_policy,
                        content_type, manifest_id, plaintext_sha256, plaintext_bytes,
                        admitted_at, expires_at, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        parsed_content_id,
                        request["service_id"],
                        request["workspace_id"],
                        parsed_request_row_id,
                        request["request_id"],
                        policy.value,
                        content_type,
                        manifest_id,
                        plaintext_digest if manifest_id is not None else None,
                        len(plaintext) if manifest_id is not None else None,
                        request["admitted_at"],
                        expires_at,
                        now,
                    ),
                ).fetchone()
                assert row is not None
                result = _metadata(row)
            uploaded = None
            return result
        finally:
            if uploaded is not None:
                self._object_store.delete(uploaded[0], sha256=uploaded[1])

    def discover(
        self,
        context: RequestContext,
        *,
        now: datetime,
        limit: int = 100,
        before: tuple[datetime, str] | None = None,
    ) -> tuple[CapturedContentMetadata, ...]:
        """List protected metadata for a global content reader only."""
        _require_aware(now)
        _require_content_context(context, mutation=False, now=now)
        if not 1 <= limit <= MAXIMUM_DISCOVERY_ITEMS:
            raise ContentError(ContentErrorCode.INVALID, context.request_id)
        values: list[Any] = [now]
        cursor = ""
        if before is not None:
            _require_aware(before[0])
            cursor = "AND (created_at, id) < (%s, %s)"
            values.extend([before[0], before[1]])
        values.append(limit)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            rows = connection.execute(
                f"""
                SELECT * FROM router.captured_content
                WHERE lifecycle_state = 'live' AND expires_at > %s {cursor}
                ORDER BY created_at DESC, id DESC LIMIT %s
                """,  # noqa: S608 - cursor is a closed local constant.
                tuple(values),
            ).fetchall()
            _audit(
                connection,
                context,
                action="captured_content.discover",
                resource_type="captured_content_page",
                resource_id=context.request_id,
                now=now,
            )
        return tuple(_metadata(row) for row in rows)

    def read(
        self, context: RequestContext, content_id: str, *, now: datetime
    ) -> CapturedContent:
        """Read one complete captured value and audit the protected read."""
        _require_aware(now)
        _require_content_context(context, mutation=False, now=now)
        try:
            parsed_id = uuid.UUID(content_id)
        except ValueError as error:
            raise ContentError(
                ContentErrorCode.NOT_FOUND, context.request_id
            ) from error
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = connection.execute(
                """
                SELECT * FROM router.captured_content
                WHERE id = %s AND lifecycle_state = 'live'
                  AND expires_at > %s FOR SHARE
                """,
                (parsed_id, now),
            ).fetchone()
            if row is None or row["capture_policy"] != CapturePolicy.COMPLETE.value:
                raise ContentError(ContentErrorCode.NOT_FOUND, context.request_id)
            value = self._read_manifest(connection, row, request_id=context.request_id)
            _audit(
                connection,
                context,
                action="captured_content.read",
                resource_type="captured_content",
                resource_id=content_id,
                now=now,
            )
        return CapturedContent(_metadata(row), value)

    def create_export(
        self,
        export_context: RequestContext,
        request: ExportRequest,
        *,
        idempotency_key: str,
        administrator_session_id: str,
        now: datetime,
        content_context: RequestContext | None = None,
    ) -> ExportOperation:
        """Create one global export with exact grants and a stable replay."""
        _require_aware(now)
        _require_export_context(export_context, mutation=True, now=now)
        if request.data_class is ExportDataClass.CAPTURED_CONTENT:
            if content_context is None:
                raise ContentError(
                    ContentErrorCode.INSUFFICIENT_SCOPE, export_context.request_id
                )
            _require_content_context(content_context, mutation=False, now=now)
            _require_matching_actor(export_context, content_context)
        if not 16 <= len(idempotency_key) <= 200 or not administrator_session_id:
            raise ContentError(ContentErrorCode.INVALID, export_context.request_id)
        fingerprint = request.fingerprint()
        idempotency_digest = self._idempotency_digest(idempotency_key)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"export:{export_context.actor_id}:{idempotency_digest.hex()}",),
            )
            existing = connection.execute(
                """
                SELECT * FROM router.protected_exports
                WHERE actor_id = %s AND idempotency_key_digest = %s FOR SHARE
                """,
                (export_context.actor_id, idempotency_digest),
            ).fetchone()
            if existing is not None:
                if existing[
                    "administrator_session_id"
                ] != administrator_session_id or not hmac.compare_digest(
                    bytes(existing["request_fingerprint"]), fingerprint
                ):
                    raise ContentError(
                        ContentErrorCode.CONFLICT, export_context.request_id
                    )
                return _export_operation(existing)
            operation_id = self._identity_factory()
            expires_at = now + _DEFAULT_EXPORT_LIFETIME
            row = connection.execute(
                """
                INSERT INTO router.protected_exports (
                    id, actor_id, administrator_session_id, data_class,
                    service_id, workspace_id, range_start, range_end,
                    export_format, idempotency_key_digest, request_fingerprint,
                    created_at, expires_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    operation_id,
                    export_context.actor_id,
                    administrator_session_id,
                    request.data_class.value,
                    request.service_id,
                    request.workspace_id,
                    request.range_start,
                    request.range_end,
                    request.export_format,
                    idempotency_digest,
                    fingerprint,
                    now,
                    expires_at,
                    now,
                ),
            ).fetchone()
            assert row is not None
            connection.execute(
                """
                INSERT INTO router.content_lifecycle_jobs (
                    id, job_kind, scope_key, payload, available_at, created_at, updated_at
                ) VALUES (%s, 'export', %s, %s, %s, %s, %s)
                ON CONFLICT (job_kind, scope_key) DO NOTHING
                """,
                (
                    self._identity_factory(),
                    str(operation_id),
                    json.dumps({"export_id": str(operation_id)}),
                    now,
                    now,
                    now,
                ),
            )
            _audit(
                connection,
                export_context,
                action="export.create",
                resource_type="protected_export",
                resource_id=str(operation_id),
                now=now,
            )
        return _export_operation(row)

    def export_status(
        self,
        export_context: RequestContext,
        operation_id: str,
        *,
        administrator_session_id: str,
        now: datetime,
    ) -> ExportOperation:
        """Read status and issue a new short-lived one-use token when complete."""
        _require_aware(now)
        _require_export_status_context(export_context)
        try:
            parsed_id = uuid.UUID(operation_id)
        except ValueError as error:
            raise ContentError(
                ContentErrorCode.NOT_FOUND, export_context.request_id
            ) from error
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = connection.execute(
                "SELECT * FROM router.protected_exports WHERE id = %s FOR UPDATE",
                (parsed_id,),
            ).fetchone()
            if (
                row is None
                or row["actor_id"] != export_context.actor_id
                or row["administrator_session_id"] != administrator_session_id
            ):
                raise ContentError(
                    ContentErrorCode.NOT_FOUND, export_context.request_id
                )
            if row["expires_at"] <= now and row["state"] != ExportState.EXPIRED.value:
                connection.execute(
                    "UPDATE router.protected_exports SET state = 'expired', updated_at = %s WHERE id = %s",
                    (now, parsed_id),
                )
                _enqueue_export_expiry(
                    connection, self._identity_factory, parsed_id, now
                )
                row["state"] = ExportState.EXPIRED.value
            token: str | None = None
            token_expires_at: datetime | None = None
            if row["state"] == ExportState.COMPLETED.value:
                token = _token(self._random_bytes)
                token_expires_at = min(now + MAXIMUM_REDEMPTION_AGE, row["expires_at"])
                connection.execute(
                    """
                    INSERT INTO router.export_redemptions (
                        export_id, token_digest, administrator_session_id,
                        issued_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (export_id) DO UPDATE SET
                        token_digest = EXCLUDED.token_digest,
                        administrator_session_id = EXCLUDED.administrator_session_id,
                        issued_at = EXCLUDED.issued_at,
                        expires_at = EXCLUDED.expires_at,
                        redeemed_at = NULL
                    """,
                    (
                        parsed_id,
                        self._token_digest(token),
                        administrator_session_id,
                        now,
                        token_expires_at,
                    ),
                )
                _audit(
                    connection,
                    export_context,
                    action="export.location.issue",
                    resource_type="protected_export",
                    resource_id=operation_id,
                    now=now,
                )
            _audit(
                connection,
                export_context,
                action="export.status.read",
                resource_type="protected_export",
                resource_id=operation_id,
                now=now,
            )
        return _export_operation(row, token=token, token_expires_at=token_expires_at)

    def redeem_export(
        self,
        export_context: RequestContext,
        operation_id: str,
        redemption_token: str,
        *,
        administrator_session_id: str,
        now: datetime,
        content_context: RequestContext | None = None,
    ) -> RedeemedExport:
        """Proxy bytes once through the Router after current authorization."""
        _require_aware(now)
        _require_export_context(export_context, mutation=True, now=now)
        try:
            parsed_id = uuid.UUID(operation_id)
        except ValueError as error:
            raise ContentError(
                ContentErrorCode.NOT_FOUND, export_context.request_id
            ) from error
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = connection.execute(
                """
                SELECT export.*, redemption.token_digest, redemption.expires_at AS token_expires_at,
                       redemption.redeemed_at, redemption.administrator_session_id AS token_session_id
                FROM router.protected_exports AS export
                JOIN router.export_redemptions AS redemption ON redemption.export_id = export.id
                WHERE export.id = %s FOR UPDATE OF export, redemption
                """,
                (parsed_id,),
            ).fetchone()
            if (
                row is None
                or row["actor_id"] != export_context.actor_id
                or row["administrator_session_id"] != administrator_session_id
                or row["token_session_id"] != administrator_session_id
                or row["state"] != ExportState.COMPLETED.value
                or row["expires_at"] <= now
                or row["token_expires_at"] <= now
                or row["redeemed_at"] is not None
                or not hmac.compare_digest(
                    row["token_digest"], self._token_digest(redemption_token)
                )
            ):
                raise ContentError(
                    ContentErrorCode.NOT_FOUND, export_context.request_id
                )
            if row["data_class"] == ExportDataClass.CAPTURED_CONTENT.value:
                if content_context is None:
                    raise ContentError(
                        ContentErrorCode.INSUFFICIENT_SCOPE, export_context.request_id
                    )
                _require_content_context(content_context, mutation=False, now=now)
                _require_matching_actor(export_context, content_context)
            value = self._read_export_manifest(
                connection, row, export_context.request_id
            )
            updated = connection.execute(
                """
                UPDATE router.export_redemptions SET redeemed_at = %s
                WHERE export_id = %s AND redeemed_at IS NULL
                RETURNING export_id
                """,
                (now, parsed_id),
            ).fetchone()
            if updated is None:
                raise ContentError(
                    ContentErrorCode.NOT_FOUND, export_context.request_id
                )
            _audit(
                connection,
                export_context,
                action="export.location.redeem",
                resource_type="protected_export",
                resource_id=operation_id,
                now=now,
            )
        return RedeemedExport(value)

    def enqueue_lifecycle_job(
        self,
        job_kind: str,
        scope_key: str,
        payload: Mapping[str, object],
        *,
        now: datetime,
    ) -> str:
        """Create one retry-safe lifecycle job identity."""
        _require_aware(now)
        if (
            job_kind
            not in {
                "expiry",
                "delete",
                "export",
                "export_expiry",
                "archive",
                "retention",
            }
            or not scope_key
        ):
            raise ValueError("The content lifecycle job is invalid.")
        job_id = self._identity_factory()
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = connection.execute(
                """
                INSERT INTO router.content_lifecycle_jobs (
                    id, job_kind, scope_key, payload, available_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_kind, scope_key) DO NOTHING
                RETURNING id, payload
                """,
                (job_id, job_kind, scope_key, json.dumps(payload), now, now, now),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT id, payload FROM router.content_lifecycle_jobs
                    WHERE job_kind = %s AND scope_key = %s FOR SHARE
                    """,
                    (job_kind, scope_key),
                ).fetchone()
            assert row is not None
            if row["payload"] != payload:
                raise ContentError(ContentErrorCode.CONFLICT, "lifecycle-job")
            return str(row["id"])

    def claim_lifecycle_job(
        self,
        owner_node_id: str,
        *,
        now: datetime,
        lease_lifetime: timedelta = _DEFAULT_LEASE_LIFETIME,
    ) -> LifecycleLease | None:
        """Claim one due job or take over one expired owner with fencing."""
        _require_aware(now)
        if lease_lifetime <= timedelta(0):
            raise ValueError("A lifecycle lease must be positive.")
        try:
            owner = uuid.UUID(owner_node_id)
        except ValueError as error:
            raise ValueError("The owner node identity must be a UUID.") from error
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = connection.execute(
                """
                SELECT * FROM router.content_lifecycle_jobs
                WHERE (
                    state IN ('ready', 'retry_wait') AND available_at <= %s
                ) OR (
                    state = 'running' AND lease_expires_at <= %s
                )
                ORDER BY available_at, id
                LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            generation = row["lease_generation"] + 1
            expires_at = now + lease_lifetime
            claimed = connection.execute(
                """
                UPDATE router.content_lifecycle_jobs SET
                    state = 'running', owner_node_id = %s, lease_generation = %s,
                    lease_expires_at = %s, attempt_count = attempt_count + 1,
                    updated_at = %s
                WHERE id = %s RETURNING *
                """,
                (owner, generation, expires_at, now, row["id"]),
            ).fetchone()
            assert claimed is not None
        return LifecycleLease(
            str(claimed["id"]),
            claimed["job_kind"],
            claimed["scope_key"],
            dict(claimed["payload"]),
            owner_node_id,
            generation,
            expires_at,
        )

    def run_lifecycle_job(self, lease: LifecycleLease, *, now: datetime) -> None:
        """Run one idempotent effect and complete it only with the live fence."""
        _require_aware(now)
        self._require_live_lease(lease, now=now)
        if lease.job_kind == "export":
            self._build_export(lease, now=now)
        elif lease.job_kind in {"expiry", "delete"}:
            content_id = str(lease.payload.get("content_id", lease.scope_key))
            self._delete_content_objects(content_id, lease=lease, now=now)
        elif lease.job_kind == "export_expiry":
            self._delete_export_objects(lease, now=now)
        elif lease.job_kind == "retention":
            self._execute_retention(lease, now=now)
        elif lease.job_kind == "archive":
            self._verify_archive(lease, now=now)
        else:
            raise ContentError(ContentErrorCode.INVALID, lease.job_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = connection.execute(
                """
                UPDATE router.content_lifecycle_jobs SET
                    state = 'succeeded', owner_node_id = NULL, lease_expires_at = NULL,
                    lease_generation = lease_generation + 1, updated_at = %s
                WHERE id = %s AND state = 'running' AND owner_node_id = %s
                  AND lease_generation = %s AND lease_expires_at > %s
                RETURNING id
                """,
                (now, lease.job_id, lease.owner_node_id, lease.generation, now),
            ).fetchone()
            if row is None:
                raise ContentError(ContentErrorCode.STALE_LEASE, lease.job_id)

    def retry_lifecycle_job(
        self,
        lease: LifecycleLease,
        *,
        now: datetime,
        retry_at: datetime,
        safe_error: str,
    ) -> None:
        """Return one failed live lease to bounded retry without losing its fence."""
        _require_aware(now)
        _require_aware(retry_at)
        if retry_at < now or not safe_error or len(safe_error) > 500:
            raise ValueError("The lifecycle retry is invalid.")
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = connection.execute(
                """
                UPDATE router.content_lifecycle_jobs SET
                    state = 'retry_wait', owner_node_id = NULL,
                    lease_expires_at = NULL,
                    lease_generation = lease_generation + 1,
                    available_at = %s, safe_error = %s, updated_at = %s
                WHERE id = %s AND state = 'running' AND owner_node_id = %s
                  AND lease_generation = %s AND lease_expires_at > %s
                RETURNING id
                """,
                (
                    retry_at,
                    safe_error,
                    now,
                    lease.job_id,
                    lease.owner_node_id,
                    lease.generation,
                    now,
                ),
            ).fetchone()
            if row is None:
                raise ContentError(ContentErrorCode.STALE_LEASE, lease.job_id)

    def _require_live_lease(self, lease: LifecycleLease, *, now: datetime) -> None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT id FROM router.content_lifecycle_jobs
                WHERE id = %s AND state = 'running' AND owner_node_id = %s
                  AND lease_generation = %s AND lease_expires_at > %s
                """,
                (lease.job_id, lease.owner_node_id, lease.generation, now),
            ).fetchone()
        if row is None:
            raise ContentError(ContentErrorCode.STALE_LEASE, lease.job_id)

    def expire_due_content(self, *, now: datetime, limit: int = 1000) -> int:
        """Queue bounded expiry work without deleting content in the scanner."""
        _require_aware(now)
        if not 1 <= limit <= 1000:
            raise ValueError("The expiry batch limit is invalid.")
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            rows = connection.execute(
                """
                SELECT id FROM router.captured_content
                WHERE lifecycle_state = 'live' AND expires_at <= %s
                ORDER BY expires_at, id LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                content_id = str(row["id"])
                connection.execute(
                    """
                    INSERT INTO router.content_lifecycle_jobs (
                        id, job_kind, scope_key, payload, available_at, created_at, updated_at
                    ) VALUES (%s, 'expiry', %s, %s, %s, %s, %s)
                    ON CONFLICT (job_kind, scope_key) DO NOTHING
                    """,
                    (
                        self._identity_factory(),
                        content_id,
                        json.dumps({"content_id": content_id}),
                        now,
                        now,
                        now,
                    ),
                )
        return len(rows)

    def expire_due_exports(self, *, now: datetime, limit: int = 1000) -> int:
        """Make expired exports unreadable and queue fenced object cleanup."""
        _require_aware(now)
        if not 1 <= limit <= 1000:
            raise ValueError("The export expiry batch limit is invalid.")
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            rows = connection.execute(
                """
                SELECT id FROM router.protected_exports
                WHERE expires_at <= %s AND deletion_started_at IS NULL
                ORDER BY expires_at, id LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                export_id = row["id"]
                connection.execute(
                    """
                    UPDATE router.protected_exports
                    SET state = 'expired', updated_at = %s
                    WHERE id = %s AND state <> 'expired'
                    """,
                    (now, export_id),
                )
                _enqueue_export_expiry(
                    connection, self._identity_factory, export_id, now
                )
        return len(rows)

    def _build_export(self, lease: LifecycleLease, *, now: datetime) -> None:
        export_id = uuid.UUID(str(lease.payload.get("export_id", lease.scope_key)))
        uploaded: tuple[str, str] | None = None
        try:
            with (
                psycopg.connect(self._database_url, row_factory=dict_row) as connection,
                connection.transaction(),
            ):
                _lock_live_lease(connection, lease, now=now)
                row = connection.execute(
                    "SELECT * FROM router.protected_exports WHERE id = %s FOR UPDATE",
                    (export_id,),
                ).fetchone()
                if row is None:
                    return
                if row["state"] == ExportState.COMPLETED.value:
                    return
                if row["state"] == ExportState.EXPIRED.value:
                    _enqueue_export_expiry(
                        connection, self._identity_factory, export_id, now
                    )
                    return
                if row["expires_at"] <= now:
                    connection.execute(
                        "UPDATE router.protected_exports SET state = 'expired', updated_at = %s WHERE id = %s",
                        (now, export_id),
                    )
                    _enqueue_export_expiry(
                        connection, self._identity_factory, export_id, now
                    )
                    return
                connection.execute(
                    "UPDATE router.protected_exports SET state = 'running', updated_at = %s WHERE id = %s",
                    (now, export_id),
                )
                payload = self._export_bytes(connection, row, now=now)
                plaintext_digest = hashlib.sha256(payload).digest()
                manifest_id = self._identity_factory()
                encryption_context = {
                    "content_kind": "protected_export",
                    "export_id": str(export_id),
                    "manifest_id": str(manifest_id),
                    "sha256": plaintext_digest.hex(),
                    "expires_at": row["expires_at"].isoformat(),
                }
                envelope = self._cipher.encrypt(payload, context=encryption_context)
                ciphertext_digest = hashlib.sha256(envelope.ciphertext).hexdigest()
                object_key = f"export/{export_id}/{manifest_id}/000001.bin"
                self._object_store.put(
                    object_key, envelope.ciphertext, sha256=ciphertext_digest
                )
                uploaded = (object_key, ciphertext_digest)
                segment = ObjectSegment(
                    1,
                    object_key,
                    len(envelope.ciphertext),
                    ciphertext_digest,
                    envelope.encrypted_data_key,
                    envelope.wrapping_key_id,
                )
                manifest = ObjectManifest.build(str(manifest_id), (segment,))
                connection.execute(
                    """
                    INSERT INTO router.content_manifests (
                        id, segment_count, ciphertext_bytes, manifest_sha256, created_at
                    ) VALUES (%s, 1, %s, %s, %s)
                    """,
                    (
                        manifest_id,
                        len(envelope.ciphertext),
                        bytes.fromhex(manifest.sha256),
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO router.content_segments (
                        manifest_id, ordinal, object_key, ciphertext_bytes,
                        ciphertext_sha256, encrypted_data_key, wrapping_key_id
                    ) VALUES (%s, 1, %s, %s, %s, %s, %s)
                    """,
                    (
                        manifest_id,
                        object_key,
                        len(envelope.ciphertext),
                        bytes.fromhex(ciphertext_digest),
                        envelope.encrypted_data_key,
                        envelope.wrapping_key_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE router.protected_exports SET
                        state = 'completed', manifest_id = %s, content_sha256 = %s,
                        updated_at = %s WHERE id = %s
                    """,
                    (manifest_id, plaintext_digest, now, export_id),
                )
            uploaded = None
        finally:
            if uploaded is not None:
                self._object_store.delete(uploaded[0], sha256=uploaded[1])

    def _export_bytes(
        self, connection: Connection[Any], row: Mapping[str, Any], *, now: datetime
    ) -> bytes:
        documents = self._export_documents(connection, row, now=now)
        if row["export_format"] == "jsonl":
            return b"".join(
                json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                for item in documents
            )
        return _csv_bytes(documents)

    def _export_documents(
        self, connection: Connection[Any], row: Mapping[str, Any], *, now: datetime
    ) -> list[dict[str, Any]]:
        data_class = ExportDataClass(row["data_class"])
        parameters = (
            row["range_start"],
            row["range_end"],
            row["service_id"],
            row["service_id"],
            row["workspace_id"],
            row["workspace_id"],
        )
        if data_class is ExportDataClass.ACCOUNTING:
            records = connection.execute(
                """
                SELECT event.event_id, request.request_id, request.service_id,
                       request.workspace_id, event.attempt_id, event.budget_scope_id,
                       event.currency, event.event_kind, event.quantity, event.amount,
                       event.price_version_id, event.source_event_id, event.occurred_at
                FROM router.accounting_events AS event
                JOIN router.logical_requests AS request
                  ON request.row_id = event.request_row_id
                WHERE event.occurred_at >= %s AND event.occurred_at < %s
                  AND (%s::uuid IS NULL OR request.service_id = %s)
                  AND (%s::uuid IS NULL OR request.workspace_id = %s)
                ORDER BY event.occurred_at, event.event_id
                """,
                parameters,
            ).fetchall()
            return [_json_document(item) for item in records]
        if data_class is ExportDataClass.AUDIT:
            records = connection.execute(
                """
                SELECT event_id, audit_class, actor_kind, actor_id, authority_class,
                       service_id, workspace_id, action, permission_result,
                       safe_details, occurred_at
                FROM router.audit_events
                WHERE occurred_at >= %s AND occurred_at < %s
                  AND (%s::uuid IS NULL OR service_id = %s)
                  AND (%s::uuid IS NULL OR workspace_id = %s)
                ORDER BY occurred_at, event_id
                """,
                parameters,
            ).fetchall()
            return [_json_document(item) for item in records]
        if data_class is ExportDataClass.CONFIGURATION:
            records = connection.execute(
                """
                SELECT id, scope_kind, service_id, workspace_id, revision_number,
                       restored_from_revision_id, content, content_sha256, created_at,
                       created_by_kind, created_by_id
                FROM router.configuration_revisions
                WHERE created_at >= %s AND created_at < %s
                  AND (%s::uuid IS NULL OR service_id = %s)
                  AND (%s::uuid IS NULL OR workspace_id = %s)
                ORDER BY created_at, id
                """,
                parameters,
            ).fetchall()
            return [_json_document(item) for item in records]
        records = connection.execute(
            """
            SELECT * FROM router.captured_content
            WHERE lifecycle_state = 'live' AND expires_at > %s
              AND created_at >= %s AND created_at < %s
              AND (%s::uuid IS NULL OR service_id = %s)
              AND (%s::uuid IS NULL OR workspace_id = %s)
            ORDER BY created_at, id
            """,
            (now, *parameters),
        ).fetchall()
        documents: list[dict[str, Any]] = []
        for record in records:
            if record["capture_policy"] != CapturePolicy.COMPLETE.value:
                continue
            documents.append(
                {
                    "capture_policy": record["capture_policy"],
                    "content_id": str(record["id"]),
                    "content_type": record["content_type"],
                    "expires_at": record["expires_at"].isoformat(),
                    "request_id": str(record["request_id"]),
                    "service_id": str(record["service_id"]),
                    "value": self._read_manifest(
                        connection, record, request_id="export-worker"
                    ),
                    "workspace_id": None
                    if record["workspace_id"] is None
                    else str(record["workspace_id"]),
                }
            )
        return documents

    def _read_manifest(
        self,
        connection: Connection[Any],
        row: Mapping[str, Any],
        *,
        request_id: str,
    ) -> JsonValue:
        manifest, segments = _load_manifest(connection, row["manifest_id"], request_id)
        plaintext = bytearray()
        try:
            for segment in segments:
                ciphertext = self._object_store.get(
                    segment.object_key, sha256=segment.ciphertext_sha256
                )
                context = _encryption_context(
                    content_id=row["id"],
                    request=row,
                    content_type=row["content_type"],
                    expires_at=row["expires_at"],
                    ordinal=segment.ordinal,
                    plaintext_sha256=bytes(row["plaintext_sha256"]).hex(),
                )
                plaintext.extend(
                    self._cipher.decrypt(
                        EncryptedEnvelope(
                            ciphertext,
                            segment.encrypted_data_key,
                            segment.wrapping_key_id,
                        ),
                        context=context,
                    )
                )
            if len(plaintext) != row["plaintext_bytes"] or not hmac.compare_digest(
                hashlib.sha256(plaintext).digest(), bytes(row["plaintext_sha256"])
            ):
                raise ContentError(ContentErrorCode.INTEGRITY, request_id)
            expected = ObjectManifest.build(manifest.manifest_id, manifest.segments)
            if not hmac.compare_digest(expected.sha256, manifest.sha256):
                raise ContentError(ContentErrorCode.INTEGRITY, request_id)
            return cast("JsonValue", json.loads(plaintext))
        except (EnvelopeDecryptionError, json.JSONDecodeError) as error:
            raise ContentError(ContentErrorCode.INTEGRITY, request_id) from error
        finally:
            plaintext[:] = bytes(len(plaintext))

    def _read_export_manifest(
        self,
        connection: Connection[Any],
        row: Mapping[str, Any],
        request_id: str,
    ) -> bytes:
        manifest, segments = _load_manifest(connection, row["manifest_id"], request_id)
        plaintext = bytearray()
        try:
            for segment in segments:
                ciphertext = self._object_store.get(
                    segment.object_key, sha256=segment.ciphertext_sha256
                )
                context = {
                    "content_kind": "protected_export",
                    "export_id": str(row["id"]),
                    "manifest_id": manifest.manifest_id,
                    "sha256": bytes(row["content_sha256"]).hex(),
                    "expires_at": row["expires_at"].isoformat(),
                }
                plaintext.extend(
                    self._cipher.decrypt(
                        EncryptedEnvelope(
                            ciphertext,
                            segment.encrypted_data_key,
                            segment.wrapping_key_id,
                        ),
                        context=context,
                    )
                )
            if not hmac.compare_digest(
                hashlib.sha256(plaintext).digest(), bytes(row["content_sha256"])
            ):
                raise ContentError(ContentErrorCode.INTEGRITY, request_id)
            expected = ObjectManifest.build(manifest.manifest_id, manifest.segments)
            if not hmac.compare_digest(expected.sha256, manifest.sha256):
                raise ContentError(ContentErrorCode.INTEGRITY, request_id)
            return bytes(plaintext)
        except EnvelopeDecryptionError as error:
            raise ContentError(ContentErrorCode.INTEGRITY, request_id) from error
        finally:
            plaintext[:] = bytes(len(plaintext))

    def _delete_content_objects(
        self, content_id: str, *, lease: LifecycleLease, now: datetime
    ) -> None:
        parsed_id = uuid.UUID(content_id)
        manifest_id: uuid.UUID | None = None
        segments: list[dict[str, Any]] = []
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_live_lease(connection, lease, now=now)
            _set_worker_fence(connection, lease)
            row = connection.execute(
                "SELECT * FROM router.captured_content WHERE id = %s FOR UPDATE",
                (parsed_id,),
            ).fetchone()
            if row is None:
                return
            if row["lifecycle_state"] == "live":
                connection.execute(
                    """
                    UPDATE router.captured_content SET
                        lifecycle_state = 'deleting', deletion_started_at = %s
                    WHERE id = %s
                    """,
                    (now, parsed_id),
                )
            manifest_id = row["manifest_id"]
            if row["manifest_id"] is not None:
                segments = connection.execute(
                    "SELECT * FROM router.content_segments WHERE manifest_id = %s ORDER BY ordinal",
                    (row["manifest_id"],),
                ).fetchall()
        for segment in segments:
            self._object_store.delete(
                segment["object_key"],
                sha256=bytes(segment["ciphertext_sha256"]).hex(),
            )
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_live_lease(connection, lease, now=now)
            _set_worker_fence(connection, lease, expected_manifest_id=manifest_id)
            if manifest_id is not None:
                connection.execute(
                    """
                    INSERT INTO router.content_manifest_cleanup_authorizations (
                        manifest_id, job_id, lease_generation, scope_key, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (manifest_id, lease.job_id, lease.generation, lease.scope_key, now),
                )
                connection.execute(
                    "DELETE FROM router.content_segments WHERE manifest_id = %s",
                    (manifest_id,),
                )
            connection.execute(
                "DELETE FROM router.captured_content WHERE id = %s AND lifecycle_state = 'deleting'",
                (parsed_id,),
            )
            if manifest_id is not None:
                connection.execute(
                    "DELETE FROM router.content_manifests WHERE id = %s",
                    (manifest_id,),
                )
                connection.execute(
                    "DELETE FROM router.content_manifest_cleanup_authorizations WHERE manifest_id = %s",
                    (manifest_id,),
                )

    def _delete_export_objects(self, lease: LifecycleLease, *, now: datetime) -> None:
        export_id = uuid.UUID(str(lease.payload.get("export_id", lease.scope_key)))
        manifest_id: uuid.UUID | None = None
        segments: list[dict[str, Any]] = []
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_live_lease(connection, lease, now=now)
            _set_worker_fence(connection, lease)
            row = connection.execute(
                "SELECT * FROM router.protected_exports WHERE id = %s FOR UPDATE",
                (export_id,),
            ).fetchone()
            if row is None:
                return
            if row["state"] != ExportState.EXPIRED.value:
                raise ContentError(ContentErrorCode.CONFLICT, lease.job_id)
            if row["deletion_started_at"] is None:
                connection.execute(
                    "UPDATE router.protected_exports SET deletion_started_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, export_id),
                )
            manifest_id = row["manifest_id"]
            if manifest_id is not None:
                segments = connection.execute(
                    "SELECT * FROM router.content_segments WHERE manifest_id = %s ORDER BY ordinal",
                    (manifest_id,),
                ).fetchall()
        for segment in segments:
            self._object_store.delete(
                segment["object_key"],
                sha256=bytes(segment["ciphertext_sha256"]).hex(),
            )
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_live_lease(connection, lease, now=now)
            _set_worker_fence(connection, lease, expected_manifest_id=manifest_id)
            if manifest_id is not None:
                connection.execute(
                    """
                    INSERT INTO router.content_manifest_cleanup_authorizations (
                        manifest_id, job_id, lease_generation, scope_key, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (manifest_id, lease.job_id, lease.generation, lease.scope_key, now),
                )
                connection.execute(
                    "DELETE FROM router.content_segments WHERE manifest_id = %s",
                    (manifest_id,),
                )
            connection.execute(
                "DELETE FROM router.export_redemptions WHERE export_id = %s",
                (export_id,),
            )
            connection.execute(
                "DELETE FROM router.protected_exports WHERE id = %s AND deletion_started_at IS NOT NULL",
                (export_id,),
            )
            if manifest_id is not None:
                connection.execute(
                    "DELETE FROM router.content_manifests WHERE id = %s",
                    (manifest_id,),
                )
                connection.execute(
                    "DELETE FROM router.content_manifest_cleanup_authorizations WHERE manifest_id = %s",
                    (manifest_id,),
                )

    def _execute_retention(self, lease: LifecycleLease, *, now: datetime) -> None:
        try:
            data_class = RetentionDataClass(str(lease.payload["data_class"]))
        except (KeyError, ValueError) as error:
            raise ContentError(ContentErrorCode.INVALID, lease.job_id) from error
        service_value = lease.payload.get("service_id")
        workspace_value = lease.payload.get("workspace_id")
        service_id = service_value if isinstance(service_value, str) else None
        workspace_id = workspace_value if isinstance(workspace_value, str) else None
        limit_value = lease.payload.get("limit", 1000)
        if not isinstance(limit_value, int) or not 1 <= limit_value <= 1000:
            raise ContentError(ContentErrorCode.INVALID, lease.job_id)
        if data_class in {
            RetentionDataClass.DIAGNOSTIC_LOGS,
            RetentionDataClass.CAPTURED_CONTENT,
        }:
            return
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_live_lease(connection, lease, now=now)
            row = _effective_retention_row(
                connection,
                service_id,
                workspace_id,
                data_class,
                effective_at=now,
            )
            if row is None:
                raise ContentError(ContentErrorCode.CONFLICT, lease.job_id)
            selection = RetentionSelection(
                data_class, row["retention_days"], row["minimum_revision_count"]
            )
            _set_worker_fence(connection, lease)
            _delete_retained_rows(
                connection,
                selection,
                service_id=service_id,
                workspace_id=workspace_id,
                now=now,
                limit=limit_value,
            )

    def _verify_archive(self, lease: LifecycleLease, *, now: datetime) -> None:
        key = lease.payload.get("object_key")
        digest = lease.payload.get("sha256")
        if not isinstance(key, str) or not isinstance(digest, str):
            raise ContentError(ContentErrorCode.INVALID, lease.job_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_live_lease(connection, lease, now=now)
            self._object_store.get(key, sha256=digest)

    def _token_digest(self, token: str) -> bytes:
        return hmac.digest(self._token_digest_key, token.encode(), "sha256")

    def _idempotency_digest(self, key: str) -> bytes:
        return hmac.digest(
            self._token_digest_key,
            b"protected-export-idempotency\0" + key.encode(),
            "sha256",
        )


def _require_content_context(
    context: RequestContext, *, mutation: bool, now: datetime
) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.operation == "content.read"
        and context.scope.service_id is None
        and context.recent_authentication_at is not None
        and context.recent_authentication_at <= context.authorized_at <= now
        and now - context.recent_authentication_at <= MAXIMUM_REDEMPTION_AGE
        and context.mutation is mutation
    ):
        raise ContentError(ContentErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_export_context(
    context: RequestContext, *, mutation: bool, now: datetime
) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.operation == "export.create"
        and context.scope.service_id is None
        and context.recent_authentication_at is not None
        and context.recent_authentication_at <= context.authorized_at <= now
        and now - context.recent_authentication_at <= MAXIMUM_REDEMPTION_AGE
        and context.mutation is mutation
    ):
        raise ContentError(ContentErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_export_status_context(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.operation == "export.create"
        and context.scope.service_id is None
        and not context.mutation
    ):
        raise ContentError(ContentErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_retention_context(
    context: RequestContext, *, mutation: bool, operation: str
) -> None:
    global_access = (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.operation == "retention.manage"
        and context.scope.service_id is None
        and context.mutation is mutation
    )
    service_access = (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.CONFIGURATION
        and context.operation == operation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
        and context.mutation is mutation
    )
    if not (global_access or service_access):
        raise ContentError(ContentErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_configuration_mutation(context: RequestContext) -> None:
    _require_retention_context(context, mutation=True, operation="retention.write")


def _require_global_retention_mutation(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.operation == "retention.manage"
        and context.scope.service_id is None
        and context.mutation
    ):
        raise ContentError(ContentErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_matching_actor(first: RequestContext, second: RequestContext) -> None:
    if first.actor_id != second.actor_id:
        raise ContentError(ContentErrorCode.INSUFFICIENT_SCOPE, first.request_id)


def _lock_configuration_scope(
    connection: Connection[Any], context: RequestContext, namespace: str
) -> None:
    _lock_configuration_family(connection, namespace)
    scope_key = ":".join(
        (
            namespace,
            context.scope.kind.value,
            context.scope.service_id or "-",
            context.scope.workspace_id or "-",
        )
    )
    if scope_key != f"{namespace}:global:-:-":
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (scope_key,)
        )


def _lock_configuration_family(connection: Connection[Any], namespace: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{namespace}:global:-:-",),
    )


def _lock_live_lease(
    connection: Connection[Any], lease: LifecycleLease, *, now: datetime
) -> None:
    row = connection.execute(
        """
        SELECT id FROM router.content_lifecycle_jobs
        WHERE id = %s AND state = 'running' AND owner_node_id = %s
          AND lease_generation = %s AND lease_expires_at > %s
        FOR UPDATE
        """,
        (lease.job_id, lease.owner_node_id, lease.generation, now),
    ).fetchone()
    if row is None:
        raise ContentError(ContentErrorCode.STALE_LEASE, lease.job_id)


def _set_worker_fence(
    connection: Connection[Any],
    lease: LifecycleLease,
    *,
    expected_manifest_id: uuid.UUID | None = None,
) -> None:
    connection.execute(
        "SELECT set_config('llmrouter.lifecycle_job_id', %s, true)",
        (lease.job_id,),
    )
    connection.execute(
        "SELECT set_config('llmrouter.lifecycle_owner_node_id', %s, true)",
        (lease.owner_node_id,),
    )
    connection.execute(
        "SELECT set_config('llmrouter.lifecycle_generation', %s, true)",
        (str(lease.generation),),
    )
    connection.execute(
        "SELECT set_config('llmrouter.lifecycle_manifest_id', %s, true)",
        ("" if expected_manifest_id is None else str(expected_manifest_id),),
    )


def _enqueue_export_expiry(
    connection: Connection[Any],
    identity_factory: Callable[[], uuid.UUID],
    export_id: uuid.UUID,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO router.content_lifecycle_jobs (
            id, job_kind, scope_key, payload, available_at, created_at, updated_at
        ) VALUES (%s, 'export_expiry', %s, %s, %s, %s, %s)
        ON CONFLICT (job_kind, scope_key) DO NOTHING
        """,
        (
            identity_factory(),
            str(export_id),
            json.dumps({"export_id": str(export_id)}),
            now,
            now,
            now,
        ),
    )


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _json_document(row: Mapping[str, Any]) -> dict[str, object]:
    return {key: _json_value(value) for key, value in row.items()}


def _csv_bytes(documents: Sequence[Mapping[str, object]]) -> bytes:
    if not documents:
        return b""
    fieldnames = sorted({key for item in documents for key in item})
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for item in documents:
        writer.writerow(
            {
                key: json.dumps(value, sort_keys=True, separators=(",", ":"))
                if isinstance(value, (Mapping, list))
                else value
                for key, value in item.items()
            }
        )
    return output.getvalue().encode()


def _normalize_selections(
    selections: Sequence[RetentionSelection],
) -> tuple[RetentionSelection, ...]:
    if not selections or len(selections) > len(RetentionDataClass):
        raise ValueError("A retention update must contain a bounded value set.")
    result = tuple(sorted(selections, key=lambda item: item.data_class.value))
    if len({item.data_class for item in result}) != len(result):
        raise ValueError("A retention update contains a duplicate data class.")
    return result


def _selection_fingerprint(selections: Sequence[RetentionSelection]) -> bytes:
    document = [
        {
            "data_class": item.data_class.value,
            "days": item.days,
            "minimum_count": item.minimum_count,
        }
        for item in selections
    ]
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _require_retention_limits(
    connection: Connection[Any], selections: Sequence[RetentionSelection]
) -> None:
    rows = connection.execute(
        "SELECT * FROM router.retention_limits FOR SHARE"
    ).fetchall()
    limits = {
        RetentionDataClass(row["data_class"]): RetentionLimit(
            RetentionDataClass(row["data_class"]),
            row["minimum_days"],
            row["maximum_days"],
            row["allowed_minimum_count"],
            row["allowed_maximum_count"],
        )
        for row in rows
    }
    if any(not limits[item.data_class].permits(item) for item in selections):
        raise ContentError(ContentErrorCode.INVALID, "retention")


def _scope_retention_revision(
    connection: Connection[Any], context: RequestContext, *, lock: bool = False
) -> str:
    lock_clause = "FOR SHARE" if lock else ""
    rows = connection.execute(
        f"""
        SELECT data_class, revision FROM router.retention_policies
        WHERE scope_kind = %s AND service_id IS NOT DISTINCT FROM %s
          AND workspace_id IS NOT DISTINCT FROM %s
        ORDER BY data_class, revision {lock_clause}
        """,  # noqa: S608 - lock clause is a closed local constant.
        (
            context.scope.kind.value,
            context.scope.service_id,
            context.scope.workspace_id,
        ),
    ).fetchall()
    source = json.dumps(
        [(row["data_class"], row["revision"]) for row in rows], separators=(",", ":")
    ).encode()
    return hashlib.sha256(source).hexdigest()


def _effective_retention_row(
    connection: Connection[Any],
    service_id: str | None,
    workspace_id: str | None,
    data_class: RetentionDataClass,
    *,
    effective_at: datetime,
) -> dict[str, Any] | None:
    return connection.execute(
        """
        SELECT data_class, retention_days, minimum_revision_count
        FROM router.retention_policies
        WHERE data_class = %s AND effective_at <= %s AND (
            scope_kind = 'global'
            OR (scope_kind = 'service' AND service_id = %s)
            OR (scope_kind = 'workspace' AND service_id = %s AND workspace_id = %s)
        )
        ORDER BY CASE scope_kind
            WHEN 'workspace' THEN 3 WHEN 'service' THEN 2 ELSE 1 END DESC,
            revision DESC
        LIMIT 1
        """,
        (data_class.value, effective_at, service_id, service_id, workspace_id),
    ).fetchone()


def _estimate_effects(
    connection: Connection[Any],
    context: RequestContext,
    selections: Sequence[RetentionSelection],
    *,
    now: datetime,
) -> tuple[RetentionEffect, ...]:
    effects: list[RetentionEffect] = []
    for selection in selections:
        current_row = _effective_retention_row(
            connection,
            context.scope.service_id,
            context.scope.workspace_id,
            selection.data_class,
            effective_at=now,
        )
        if current_row is None:
            raise RuntimeError("The global retention configuration is missing.")
        current_days = current_row["retention_days"]
        direction = (
            "delete_sooner"
            if selection.days < current_days
            else "retain_longer"
            if selection.days > current_days
            else "no_change"
        )
        if selection.data_class is RetentionDataClass.DIAGNOSTIC_LOGS:
            effects.append(
                RetentionEffect(selection.data_class, direction, 0, 0, "not_stored")
            )
            continue
        if selection.data_class is RetentionDataClass.CAPTURED_CONTENT:
            effects.append(
                RetentionEffect(
                    selection.data_class,
                    direction,
                    0,
                    0,
                    "admission_snapshot_unchanged",
                )
            )
            continue
        if direction != "delete_sooner":
            effects.append(
                RetentionEffect(
                    selection.data_class,
                    direction,
                    0,
                    0,
                    "deleted_rows_cannot_return"
                    if direction == "retain_longer"
                    else "no_change",
                )
            )
            continue
        records, size = _retention_effect_count(
            connection,
            selection,
            current_days=current_days,
            service_id=context.scope.service_id,
            workspace_id=context.scope.workspace_id,
            now=now,
        )
        effects.append(RetentionEffect(selection.data_class, direction, records, size))
    return tuple(effects)


def _retention_effect_count(
    connection: Connection[Any],
    selection: RetentionSelection,
    *,
    current_days: int,
    service_id: str | None,
    workspace_id: str | None,
    now: datetime,
) -> tuple[int, int]:
    old_cutoff = now - timedelta(days=current_days)
    new_cutoff = now - timedelta(days=selection.days)
    common = (
        old_cutoff,
        new_cutoff,
        service_id,
        service_id,
        workspace_id,
        workspace_id,
    )
    if selection.data_class is RetentionDataClass.RAW_ACCOUNTING:
        row = connection.execute(
            """
            SELECT count(*) AS records,
                   coalesce(sum(pg_column_size(event)), 0) AS bytes
            FROM router.accounting_events AS event
            JOIN router.logical_requests AS request
              ON request.row_id = event.request_row_id
            WHERE event.occurred_at >= %s AND event.occurred_at < %s
              AND (%s::uuid IS NULL OR request.service_id = %s)
              AND (%s::uuid IS NULL OR request.workspace_id = %s)
            """,
            common,
        ).fetchone()
    elif selection.data_class in {
        RetentionDataClass.AGENT_TOOL_AUDIT,
        RetentionDataClass.SECURITY_AUDIT,
    }:
        classes = (
            ["agent_run", "business_tool"]
            if selection.data_class is RetentionDataClass.AGENT_TOOL_AUDIT
            else ["security", "global_administration"]
        )
        row = connection.execute(
            """
            SELECT count(*) AS records,
                   coalesce(sum(pg_column_size(event)), 0) AS bytes
            FROM router.audit_events AS event
            WHERE event.audit_class = ANY(%s)
              AND event.occurred_at >= %s AND event.occurred_at < %s
              AND (%s::uuid IS NULL OR event.service_id = %s)
              AND (%s::uuid IS NULL OR event.workspace_id = %s)
            """,
            (classes, *common),
        ).fetchone()
    elif selection.data_class is RetentionDataClass.DAILY_ACCOUNTING:
        row = connection.execute(
            """
            SELECT count(*) AS records,
                   coalesce(sum(pg_column_size(item)), 0) AS bytes
            FROM router.daily_accounting_aggregates AS item
            WHERE item.accounting_day >= %s::date AND item.accounting_day < %s::date
              AND (%s::uuid IS NULL OR item.service_id = %s)
              AND (%s::uuid IS NULL OR item.workspace_id = %s)
            """,
            common,
        ).fetchone()
    else:
        minimum_count = selection.minimum_count
        assert minimum_count is not None
        row = connection.execute(
            """
            WITH ranked AS (
                SELECT revision.*, row_number() OVER (
                    PARTITION BY scope_kind, service_id, workspace_id
                    ORDER BY revision_number DESC
                ) AS retained_rank
                FROM router.configuration_revisions AS revision
                WHERE (%s::uuid IS NULL OR service_id = %s)
                  AND (%s::uuid IS NULL OR workspace_id = %s)
            )
            SELECT count(*) AS records,
                   coalesce(sum(pg_column_size(ranked)), 0) AS bytes
            FROM ranked
            WHERE retained_rank > %s AND created_at >= %s AND created_at < %s
            """,
            (
                service_id,
                service_id,
                workspace_id,
                workspace_id,
                minimum_count,
                old_cutoff,
                new_cutoff,
            ),
        ).fetchone()
    assert row is not None
    return int(row["records"]), int(row["bytes"])


def _delete_retained_rows(
    connection: Connection[Any],
    selection: RetentionSelection,
    *,
    service_id: str | None,
    workspace_id: str | None,
    now: datetime,
    limit: int,
) -> int:
    cutoff = now - timedelta(days=selection.days)
    if selection.data_class is RetentionDataClass.RAW_ACCOUNTING:
        rows = connection.execute(
            """
            SELECT event.event_id
            FROM router.accounting_events AS event
            JOIN router.logical_requests AS request
              ON request.row_id = event.request_row_id
            WHERE event.occurred_at < %s
              AND (%s::uuid IS NULL OR request.service_id = %s)
              AND (%s::uuid IS NULL OR request.workspace_id = %s)
              AND NOT EXISTS (
                  SELECT 1 FROM router.accounting_events AS child
                  WHERE child.source_event_id = event.event_id
              )
            ORDER BY event.occurred_at, event.event_id
            LIMIT %s FOR UPDATE OF event SKIP LOCKED
            """,
            (cutoff, service_id, service_id, workspace_id, workspace_id, limit),
        ).fetchall()
        identities = [row["event_id"] for row in rows]
        return _delete_unreferenced_identities(
            connection,
            table="accounting_events",
            identity_column="event_id",
            identities=identities,
        )
    if selection.data_class in {
        RetentionDataClass.AGENT_TOOL_AUDIT,
        RetentionDataClass.SECURITY_AUDIT,
    }:
        classes = (
            ["agent_run", "business_tool"]
            if selection.data_class is RetentionDataClass.AGENT_TOOL_AUDIT
            else ["security", "global_administration"]
        )
        rows = connection.execute(
            """
            SELECT event_id FROM router.audit_events
            WHERE audit_class = ANY(%s) AND occurred_at < %s
              AND (%s::uuid IS NULL OR service_id = %s)
              AND (%s::uuid IS NULL OR workspace_id = %s)
            ORDER BY occurred_at, event_id LIMIT %s FOR UPDATE SKIP LOCKED
            """,
            (
                classes,
                cutoff,
                service_id,
                service_id,
                workspace_id,
                workspace_id,
                limit,
            ),
        ).fetchall()
        identities = [row["event_id"] for row in rows]
        return _delete_unreferenced_identities(
            connection,
            table="audit_events",
            identity_column="event_id",
            identities=identities,
        )
    if selection.data_class is RetentionDataClass.DAILY_ACCOUNTING:
        rows = connection.execute(
            """
            SELECT id FROM router.daily_accounting_aggregates
            WHERE accounting_day < %s::date
              AND (%s::uuid IS NULL OR service_id = %s)
              AND (%s::uuid IS NULL OR workspace_id = %s)
            ORDER BY accounting_day, id LIMIT %s FOR UPDATE SKIP LOCKED
            """,
            (cutoff, service_id, service_id, workspace_id, workspace_id, limit),
        ).fetchall()
        identities = [row["id"] for row in rows]
        if identities:
            connection.execute(
                "DELETE FROM router.daily_accounting_aggregates WHERE id = ANY(%s)",
                (identities,),
            )
        return len(identities)
    if selection.data_class is RetentionDataClass.CONFIGURATION_REVISIONS:
        count = selection.minimum_count
        assert count is not None
        rows = connection.execute(
            """
            WITH ranked AS (
                SELECT id, created_at, revision_number,
                       row_number() OVER (
                           PARTITION BY scope_kind, service_id, workspace_id
                           ORDER BY revision_number DESC
                       ) AS retained_rank
                FROM router.configuration_revisions
                WHERE (%s::uuid IS NULL OR service_id = %s)
                  AND (%s::uuid IS NULL OR workspace_id = %s)
            )
            SELECT id FROM ranked
            WHERE retained_rank > %s AND created_at < %s
            ORDER BY created_at, id LIMIT %s
            """,
            (service_id, service_id, workspace_id, workspace_id, count, cutoff, limit),
        ).fetchall()
        deleted = 0
        for row in rows:
            try:
                with connection.transaction():
                    result = connection.execute(
                        "DELETE FROM router.configuration_revisions WHERE id = %s RETURNING id",
                        (row["id"],),
                    ).fetchone()
                deleted += result is not None
            except psycopg.errors.ForeignKeyViolation:
                continue
        return deleted
    return 0


def _delete_unreferenced_identities(
    connection: Connection[Any],
    *,
    table: str,
    identity_column: str,
    identities: Sequence[uuid.UUID],
) -> int:
    """Delete eligible rows and skip each row that still has a live reference."""
    if (table, identity_column) not in {
        ("accounting_events", "event_id"),
        ("audit_events", "event_id"),
    }:
        raise ValueError("The retained table identity is invalid.")
    deleted = 0
    for identity in identities:
        try:
            with connection.transaction():
                row = connection.execute(
                    psycopg.sql.SQL(
                        "DELETE FROM router.{table} "
                        "WHERE {identity} = %s RETURNING {identity}"
                    ).format(
                        table=psycopg.sql.Identifier(table),
                        identity=psycopg.sql.Identifier(identity_column),
                    ),
                    (identity,),
                ).fetchone()
            deleted += row is not None
        except psycopg.errors.ForeignKeyViolation:
            continue
    return deleted


def _effect_document(effect: RetentionEffect) -> dict[str, object]:
    return {
        "data_class": effect.data_class.value,
        "direction": effect.direction,
        "estimated_records": effect.estimated_records,
        "estimated_bytes": effect.estimated_bytes,
        "evidence": effect.evidence,
    }


def _metadata(row: Mapping[str, Any]) -> CapturedContentMetadata:
    return CapturedContentMetadata(
        str(row["id"]),
        str(row["service_id"]),
        None if row["workspace_id"] is None else str(row["workspace_id"]),
        str(row["request_id"]),
        CapturePolicy(row["capture_policy"]),
        row["expires_at"],
        row["content_type"],
    )


def _load_manifest(
    connection: Connection[Any], manifest_id: uuid.UUID, request_id: str
) -> tuple[ObjectManifest, tuple[ObjectSegment, ...]]:
    manifest_row = connection.execute(
        "SELECT * FROM router.content_manifests WHERE id = %s", (manifest_id,)
    ).fetchone()
    segment_rows = connection.execute(
        "SELECT * FROM router.content_segments WHERE manifest_id = %s ORDER BY ordinal",
        (manifest_id,),
    ).fetchall()
    if manifest_row is None or len(segment_rows) != manifest_row["segment_count"]:
        raise ContentError(ContentErrorCode.INTEGRITY, request_id)
    segments = tuple(
        ObjectSegment(
            row["ordinal"],
            row["object_key"],
            row["ciphertext_bytes"],
            bytes(row["ciphertext_sha256"]).hex(),
            bytes(row["encrypted_data_key"]),
            row["wrapping_key_id"],
        )
        for row in segment_rows
    )
    manifest = ObjectManifest(
        str(manifest_id), segments, bytes(manifest_row["manifest_sha256"]).hex()
    )
    if (
        sum(item.ciphertext_bytes for item in segments)
        != manifest_row["ciphertext_bytes"]
    ):
        raise ContentError(ContentErrorCode.INTEGRITY, request_id)
    return manifest, segments


def _encryption_context(
    *,
    content_id: uuid.UUID,
    request: Mapping[str, Any],
    content_type: str,
    expires_at: datetime,
    ordinal: int,
    plaintext_sha256: str,
) -> dict[str, str]:
    return {
        "content_kind": "captured_content",
        "content_id": str(content_id),
        "request_id": str(request["request_id"]),
        "service_id": str(request["service_id"]),
        "workspace_id": "-"
        if request["workspace_id"] is None
        else str(request["workspace_id"]),
        "content_type": content_type,
        "capture_policy": str(request["capture_policy"]),
        "expires_at": expires_at.isoformat(),
        "ordinal": str(ordinal),
        "plaintext_sha256": plaintext_sha256,
    }


def _export_operation(
    row: Mapping[str, Any],
    *,
    token: str | None = None,
    token_expires_at: datetime | None = None,
) -> ExportOperation:
    operation_id = str(row["id"])
    return ExportOperation(
        operation_id,
        ExportState(row["state"]),
        row["created_at"],
        row["expires_at"],
        f"/v1/admin/exports/{operation_id}/redeem" if token is not None else None,
        token,
        token_expires_at,
        None if row["content_sha256"] is None else bytes(row["content_sha256"]).hex(),
        row["safe_error"],
    )


def _audit(
    connection: Connection[Any],
    context: RequestContext,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    now: datetime,
) -> None:
    global_action = context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
    audit_class = "global_administration" if global_action else "security"
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            service_id, workspace_id, action, permission_result, safe_details, occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'permitted', %s, %s)
        """,
        (
            uuid.uuid4(),
            audit_class,
            context.actor_kind.value,
            context.actor_id,
            context.authority_class.value,
            context.scope.service_id,
            context.scope.workspace_id,
            action,
            json.dumps({"resource_type": resource_type, "resource_id": resource_id}),
            now,
        ),
    )


def _token(random_bytes: Callable[[int], bytes]) -> str:
    import base64

    raw = random_bytes(32)
    if len(raw) != 32:
        raise ValueError("The random source did not return 32 bytes.")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return None if value is None else uuid.UUID(value)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A content lifecycle time must include a time zone.")
