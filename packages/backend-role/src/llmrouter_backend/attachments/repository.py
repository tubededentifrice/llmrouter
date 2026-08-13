"""PostgreSQL custody for scoped immutable attachment content."""
# ruff: noqa: D107, EM101, TRY003

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from llmrouter_backend.admission import AttachmentReference
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

from .errors import AttachmentError, AttachmentErrorCode
from .model import (
    MAXIMUM_ATTACHMENT_BYTES,
    AttachmentContent,
    AttachmentCreateResult,
    AttachmentMetadata,
    AttachmentState,
    CreateAttachment,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from psycopg import Connection

    from llmrouter_backend.authority import RequestContext


class PostgresAttachmentRepository:
    """Create, fill, expire, and read exact-scope attachment objects."""

    def __init__(
        self,
        database_url: str,
        *,
        cipher: EnvelopeCipher,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        maximum_create_replay_age: timedelta = timedelta(minutes=15),
    ) -> None:
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        if maximum_create_replay_age <= timedelta(0):
            raise ValueError("The create replay age must be positive.")
        self._database_url = database_url
        self._cipher = cipher
        self._identity_factory = identity_factory
        self._maximum_create_replay_age = maximum_create_replay_age

    def create(
        self,
        context: RequestContext,
        declaration: CreateAttachment,
        *,
        expires_at: datetime,
        now: datetime,
    ) -> AttachmentCreateResult:
        """Create metadata or recover an equal unexpired create response.

        The public create route has no caller identity field. The repository
        therefore serializes equal declarations in one authenticated scope and
        returns a recent existing object after response loss. The short replay
        window does not prevent an intentional later create of equal content.
        """
        _require_authority(context, mutation=True)
        _require_aware(expires_at)
        _require_aware(now)
        if expires_at <= now:
            raise AttachmentError(AttachmentErrorCode.INVALID, context.request_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock(
                connection,
                "attachment-create:"
                f"{context.scope.service_id}:{context.scope.workspace_id or '-'}:"
                f"{declaration.media_type}:{declaration.byte_length}:{declaration.sha256}",
            )
            _require_active_scope(connection, context)
            row = connection.execute(
                """
                SELECT attachment.*, status.state
                FROM router.attachments AS attachment
                JOIN router.attachment_status AS status
                  ON status.attachment_id = attachment.id
                WHERE attachment.service_id = %s
                  AND attachment.workspace_id IS NOT DISTINCT FROM %s
                  AND attachment.media_type = %s
                  AND attachment.byte_length = %s
                  AND attachment.content_sha256 = %s
                  AND attachment.expires_at > %s
                  AND attachment.created_at >= %s
                  AND status.state IN ('pending', 'ready')
                ORDER BY attachment.created_at DESC, attachment.id DESC
                LIMIT 1 FOR UPDATE OF attachment, status
                """,
                (
                    context.scope.service_id,
                    context.scope.workspace_id,
                    declaration.media_type,
                    declaration.byte_length,
                    bytes.fromhex(declaration.sha256),
                    now,
                    now - self._maximum_create_replay_age,
                ),
            ).fetchone()
            if row is not None:
                return AttachmentCreateResult(_metadata(row, now=now), replayed=True)
            attachment_id = self._identity_factory()
            connection.execute(
                """
                INSERT INTO router.attachments (
                    id, service_id, workspace_id, media_type, byte_length,
                    content_sha256, object_manifest_id, expires_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attachment_id,
                    context.scope.service_id,
                    context.scope.workspace_id,
                    declaration.media_type,
                    declaration.byte_length,
                    bytes.fromhex(declaration.sha256),
                    attachment_id,
                    expires_at,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO router.attachment_status (
                       attachment_id, state, updated_at
                   ) VALUES (%s, 'pending', %s)""",
                (attachment_id, now),
            )
            row = connection.execute(
                """SELECT attachment.*, status.state
                   FROM router.attachments AS attachment
                   JOIN router.attachment_status AS status
                     ON status.attachment_id = attachment.id
                   WHERE attachment.id = %s""",
                (attachment_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("The created attachment receipt is missing.")
            return AttachmentCreateResult(_metadata(row, now=now), replayed=False)

    def upload(
        self,
        context: RequestContext,
        attachment_id: str,
        content: bytes,
        *,
        now: datetime,
    ) -> AttachmentMetadata:
        """Verify and encrypt one upload exactly once."""
        _require_authority(context, mutation=True)
        _require_aware(now)
        parsed_id = _parse_id(attachment_id, context.request_id)
        if (
            not isinstance(content, bytes)
            or not 1 <= len(content) <= MAXIMUM_ATTACHMENT_BYTES
        ):
            raise AttachmentError(AttachmentErrorCode.INVALID, context.request_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _require_active_scope(connection, context)
            row = _select_scoped(connection, context, parsed_id, for_update=True)
            if row is None:
                raise AttachmentError(AttachmentErrorCode.NOT_FOUND, context.request_id)
            if _expire_locked(connection, row, now=now):
                raise AttachmentError(AttachmentErrorCode.NOT_FOUND, context.request_id)
            digest = hashlib.sha256(content).digest()
            matches_declaration = len(content) == row[
                "byte_length"
            ] and hmac.compare_digest(digest, bytes(row["content_sha256"]))
            if row["state"] == "ready" and matches_declaration:
                return _metadata(row, now=now)
            if row["state"] == "ready":
                raise AttachmentError(
                    AttachmentErrorCode.ALREADY_COMPLETE, context.request_id
                )
            if row["state"] != "pending":
                raise AttachmentError(AttachmentErrorCode.INVALID, context.request_id)
            if not matches_declaration:
                raise AttachmentError(AttachmentErrorCode.INVALID, context.request_id)
            envelope = self._cipher.encrypt(content, context=_encryption_context(row))
            connection.execute(
                """
                INSERT INTO router.attachment_content (
                    attachment_id, ciphertext, encrypted_data_key,
                    wrapping_key_id, stored_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    parsed_id,
                    envelope.ciphertext,
                    envelope.encrypted_data_key,
                    envelope.wrapping_key_id,
                    now,
                ),
            )
            connection.execute(
                """UPDATE router.attachment_status
                   SET state = 'ready', revision = revision + 1,
                       verified_at = %s, updated_at = %s
                   WHERE attachment_id = %s""",
                (now, now, parsed_id),
            )
            row["state"] = "ready"
            return _metadata(row, now=now)

    def metadata(
        self, context: RequestContext, attachment_id: str, *, now: datetime
    ) -> AttachmentMetadata:
        """Read metadata only in the authenticated service and workspace."""
        _require_authority(context, mutation=False)
        _require_aware(now)
        parsed_id = _parse_id(attachment_id, context.request_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _require_active_scope(connection, context)
            row = _select_scoped(connection, context, parsed_id, for_update=True)
            if row is None:
                raise AttachmentError(AttachmentErrorCode.NOT_FOUND, context.request_id)
            _expire_locked(connection, row, now=now)
            return _metadata(row, now=now)

    def content(
        self, context: RequestContext, attachment_id: str, *, now: datetime
    ) -> AttachmentContent:
        """Decrypt ready bytes only in the authenticated exact scope."""
        _require_authority(context, mutation=False)
        _require_aware(now)
        parsed_id = _parse_id(attachment_id, context.request_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _require_active_scope(connection, context)
            row = _select_scoped(connection, context, parsed_id, for_update=True)
            if row is None or _expire_locked(connection, row, now=now):
                raise AttachmentError(AttachmentErrorCode.NOT_FOUND, context.request_id)
            if row["state"] != "ready":
                raise AttachmentError(AttachmentErrorCode.NOT_FOUND, context.request_id)
            stored = connection.execute(
                """SELECT ciphertext, encrypted_data_key, wrapping_key_id
                   FROM router.attachment_content WHERE attachment_id = %s""",
                (parsed_id,),
            ).fetchone()
            if stored is None:
                raise AttachmentError(AttachmentErrorCode.INTERNAL, context.request_id)
            try:
                plaintext = self._cipher.decrypt(
                    EncryptedEnvelope(
                        bytes(stored["ciphertext"]),
                        bytes(stored["encrypted_data_key"]),
                        stored["wrapping_key_id"],
                    ),
                    context=_encryption_context(row),
                )
            except EnvelopeDecryptionError as error:
                raise AttachmentError(
                    AttachmentErrorCode.INTERNAL, context.request_id
                ) from error
            try:
                if len(plaintext) != row["byte_length"] or not hmac.compare_digest(
                    hashlib.sha256(plaintext).digest(), bytes(row["content_sha256"])
                ):
                    raise AttachmentError(
                        AttachmentErrorCode.INTERNAL, context.request_id
                    )
                return AttachmentContent(
                    bytes(plaintext),
                    row["media_type"],
                    bytes(row["content_sha256"]).hex(),
                )
            finally:
                plaintext[:] = bytes(len(plaintext))

    def admission_reference(
        self, context: RequestContext, attachment_id: str, *, now: datetime
    ) -> AttachmentReference:
        """Return one verified closed reference without aggregate request checks."""
        _require_admission_authority(context)
        _require_aware(now)
        parsed_id = _parse_id(attachment_id, context.request_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _require_active_scope(connection, context)
            row = _select_scoped(connection, context, parsed_id, for_update=True)
            if (
                row is None
                or _expire_locked(connection, row, now=now)
                or row["state"] != "ready"
            ):
                raise AttachmentError(AttachmentErrorCode.INVALID, context.request_id)
            metadata = _metadata(row, now=now)
            metadata.require_ready()
            return AttachmentReference(
                metadata.attachment_id,
                metadata.sha256,
                metadata.media_type,
                metadata.byte_length,
            )


def _require_authority(context: RequestContext, *, mutation: bool) -> None:
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation
        == ("attachment.create" if mutation else "attachment.read")
        and context.mutation is mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise AttachmentError(
            AttachmentErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )


def _require_admission_authority(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation in {"model.create", "run.create", "tool.create"}
        and context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise AttachmentError(
            AttachmentErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )


def _require_active_scope(connection: Connection[Any], context: RequestContext) -> None:
    services = connection.execute(
        """WITH RECURSIVE service_chain AS (
               SELECT id, parent_service_id
               FROM router.services WHERE id = %s
             UNION ALL
               SELECT parent.id, parent.parent_service_id
               FROM router.services AS parent
               JOIN service_chain AS child
                 ON child.parent_service_id = parent.id
           )
           SELECT service.state
           FROM router.services AS service
           JOIN service_chain AS chain ON chain.id = service.id
           FOR SHARE OF service""",
        (context.scope.service_id,),
    ).fetchall()
    if not services or any(service["state"] != "active" for service in services):
        raise AttachmentError(
            AttachmentErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )
    if context.scope.workspace_id is None:
        return
    workspace = connection.execute(
        """SELECT state FROM router.workspaces
           WHERE id = %s AND service_id = %s FOR SHARE""",
        (context.scope.workspace_id, context.scope.service_id),
    ).fetchone()
    if workspace is None or workspace["state"] != "active":
        raise AttachmentError(
            AttachmentErrorCode.WORKSPACE_UNAVAILABLE, context.request_id
        )


def _select_scoped(
    connection: Connection[Any],
    context: RequestContext,
    attachment_id: uuid.UUID,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    lock = "FOR UPDATE OF attachment, status" if for_update else ""
    row = connection.execute(
        f"""SELECT attachment.*, status.state
            FROM router.attachments AS attachment
            JOIN router.attachment_status AS status
              ON status.attachment_id = attachment.id
            WHERE attachment.id = %s AND attachment.service_id = %s
              AND attachment.workspace_id IS NOT DISTINCT FROM %s
            {lock}""",  # noqa: S608 - lock is a closed local constant.
        (attachment_id, context.scope.service_id, context.scope.workspace_id),
    ).fetchone()
    return None if row is None else dict(row)


def _expire_locked(
    connection: Connection[Any], row: dict[str, Any], *, now: datetime
) -> bool:
    if row["expires_at"] > now:
        return False
    if row["state"] != "expired":
        connection.execute(
            """UPDATE router.attachment_status
               SET state = 'expired', revision = revision + 1,
                   verified_at = NULL, updated_at = %s
               WHERE attachment_id = %s""",
            (now, row["id"]),
        )
        connection.execute(
            "DELETE FROM router.attachment_content WHERE attachment_id = %s",
            (row["id"],),
        )
        row["state"] = "expired"
    return True


def _metadata(row: dict[str, Any], *, now: datetime) -> AttachmentMetadata:
    state = "expired" if row["expires_at"] <= now else row["state"]
    public_state = {
        "pending": AttachmentState.AWAITING_CONTENT,
        "ready": AttachmentState.READY,
        "expired": AttachmentState.EXPIRED,
    }.get(state)
    if public_state is None:
        public_state = AttachmentState.EXPIRED
    return AttachmentMetadata(
        attachment_id=str(row["id"]),
        service_id=str(row["service_id"]),
        workspace_id=(
            None if row["workspace_id"] is None else str(row["workspace_id"])
        ),
        media_type=row["media_type"],
        byte_length=int(row["byte_length"]),
        sha256=bytes(row["content_sha256"]).hex(),
        state=public_state,
        expires_at=row["expires_at"],
    )


def _encryption_context(row: dict[str, Any]) -> dict[str, str]:
    return {
        "attachment_id": str(row["id"]),
        "service_id": str(row["service_id"]),
        "workspace_id": "-"
        if row["workspace_id"] is None
        else str(row["workspace_id"]),
        "media_type": str(row["media_type"]),
        "byte_length": str(row["byte_length"]),
        "sha256": bytes(row["content_sha256"]).hex(),
        "expires_at": row["expires_at"].isoformat(),
    }


def _parse_id(value: str, request_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise AttachmentError(AttachmentErrorCode.NOT_FOUND, request_id) from error


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An attachment time must include a time zone.")


def _lock(connection: Connection[Any], name: str) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))
