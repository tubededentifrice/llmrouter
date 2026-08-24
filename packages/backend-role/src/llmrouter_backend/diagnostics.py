"""Best-effort detailed logs, media retention, and cleanup."""
# ruff: noqa: EM101, TRY003

from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from llmrouter_backend.errors import content_unavailable, invalid_request, not_found
from llmrouter_backend.object_store import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    StoredObject,
)
from llmrouter_backend.store import AdministratorActor, record_activity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from psycopg import Connection

    from llmrouter_backend.models import RequestAttempt

_MAX_LOG_RANGE = timedelta(days=31)
_MAX_INPUT_IMAGES = 8
_MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_TOTAL_INPUT_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_RETAINED_OBJECT_BYTES = 1024 * 1024 * 1024
_MAX_TAGS = 32
_MAX_TAG_BYTES = 128
_MAX_TAG_SET_BYTES = 2048
_MAX_LOG_REQUEST_CHARACTERS = 5_000_000
_MAX_LOG_RESPONSE_CHARACTERS = 10_000_000
_MAX_ATTEMPTS = 16
_MAX_MEDIA_ITEMS = 16
_DATABASE_CONNECT_TIMEOUT_SECONDS = 2
_DATABASE_TIMEOUT_OPTIONS = "-c statement_timeout=2000 -c lock_timeout=500"
_SAFE_MEDIA_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/webm",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
    }
)
_INPUT_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


@dataclass(frozen=True, slots=True)
class CapturedMedia:
    """Model media bytes separate from structured storage controls."""

    body: bytes
    media_type: str
    role: Literal["input", "output"]


@dataclass(frozen=True, slots=True)
class DetailedLogWrite:
    """Typed log input with no field for credentials or HTTP controls."""

    service_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: Literal["model", "embedding", "media"]
    outcome: Literal["succeeded", "failed"]
    request_json: str
    response_json: str | None
    attempts: tuple[RequestAttempt, ...]
    started_at: datetime
    assignment_api_name: str | None = None
    provider_model_api_name: str | None = None
    tags: tuple[str, ...] = ()
    media: tuple[CapturedMedia, ...] = ()
    accounting_call_id: uuid.UUID | None = None


def write_detailed_log_best_effort(
    database_url: str,
    object_store: ObjectStore | None,
    value: DetailedLogWrite,
) -> uuid.UUID | None:
    """Write diagnostic data separately so call success never depends on it."""
    uploaded: list[str] = []
    try:
        _validate_write(value)
        with psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
            options=_DATABASE_TIMEOUT_OPTIONS,
        ) as connection:
            row = connection.execute(
                """SELECT services.api_name AS service_api_name,
                          workspaces.api_name AS workspace_api_name
                   FROM router.workspaces
                   JOIN router.services ON services.id = workspaces.service_id
                   WHERE workspaces.service_id = %s AND workspaces.id = %s""",
                (value.service_id, value.workspace_id),
            ).fetchone()
            if row is None:
                return None
            log_id = value.accounting_call_id or uuid.uuid4()
            connection.execute(
                """INSERT INTO router.request_logs
                       (id, service_id, workspace_id, assignment_api_name,
                        provider_model_api_name, kind, outcome, tags, request_json,
                        response_json, attempts, started_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                           %s::jsonb, %s)""",
                (
                    log_id,
                    value.service_id,
                    value.workspace_id,
                    value.assignment_api_name,
                    value.provider_model_api_name,
                    value.kind,
                    value.outcome,
                    Jsonb(list(_normalized_tags(value.tags))),
                    value.request_json,
                    value.response_json,
                    Jsonb(
                        [
                            attempt.model_dump(mode="json", exclude_none=True)
                            for attempt in value.attempts
                        ]
                    ),
                    value.started_at,
                ),
            )
            if object_store is not None:
                for media in value.media:
                    media_id = uuid.uuid4()
                    object_key = _object_key(
                        value.started_at, value.service_id, value.workspace_id, media_id
                    )
                    try:
                        object_store.put(object_key, media.body, media.media_type)
                    except Exception:  # noqa: BLE001 - Diagnostic media is best-effort.
                        _delete_or_queue_best_effort(
                            database_url, object_store, object_key
                        )
                        break
                    uploaded.append(object_key)
                    connection.execute(
                        """INSERT INTO router.media_objects
                               (id, service_id, workspace_id, request_log_id,
                                object_key, media_type, role, size_bytes,
                                content_sha256)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            media_id,
                            value.service_id,
                            value.workspace_id,
                            log_id,
                            object_key,
                            media.media_type,
                            media.role,
                            len(media.body),
                            hashlib.sha256(media.body).digest(),
                        ),
                    )
    except Exception:  # noqa: BLE001 - Diagnostics must not change the call result.
        if object_store is not None:
            for object_key in uploaded:
                _delete_or_queue_best_effort(database_url, object_store, object_key)
        return None
    else:
        return log_id


def list_request_logs(
    connection: Connection[Any],
    *,
    from_time: datetime,
    to_time: datetime,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read one bounded newest-first administrator log page."""
    _validate_time_range(from_time, to_time)
    cursor_id = _uuid_cursor(cursor)
    cursor_time: datetime | None = None
    if cursor_id is not None:
        cursor_row = connection.execute(
            """SELECT started_at
               FROM router.request_logs, router.global_settings
               WHERE request_logs.id = %s
                 AND request_logs.started_at >= statement_timestamp()
                     - make_interval(days => global_settings.log_retention_days)""",
            (cursor_id,),
        ).fetchone()
        if cursor_row is None:
            raise invalid_request("cursor", "The cursor is invalid.")
        cursor_time = cast("datetime", cursor_row["started_at"])
    rows = connection.execute(
        """SELECT request_logs.id, services.api_name AS service_api_name,
                  workspaces.api_name AS workspace_api_name,
                  request_logs.assignment_api_name,
                  request_logs.provider_model_api_name, request_logs.kind,
                  request_logs.outcome, request_logs.tags, request_logs.started_at
           FROM router.request_logs
           JOIN router.services ON services.id = request_logs.service_id
           JOIN router.workspaces ON workspaces.id = request_logs.workspace_id
           CROSS JOIN router.global_settings
           WHERE request_logs.started_at >= %s AND request_logs.started_at < %s
             AND request_logs.started_at >= statement_timestamp()
                 - make_interval(days => global_settings.log_retention_days)
             AND (%s::timestamptz IS NULL OR
                  (request_logs.started_at, request_logs.id) < (%s, %s))
           ORDER BY request_logs.started_at DESC, request_logs.id DESC
           LIMIT %s""",
        (from_time, to_time, cursor_time, cursor_time, cursor_id, limit + 1),
    ).fetchall()
    return _page(rows, limit)


def get_request_log(
    connection: Connection[Any], request_log_id: uuid.UUID
) -> dict[str, Any]:
    """Read one complete administrator-only log without storage identifiers."""
    row = connection.execute(
        """SELECT request_logs.id, services.api_name AS service_api_name,
                  workspaces.api_name AS workspace_api_name,
                  request_logs.assignment_api_name,
                  request_logs.provider_model_api_name, request_logs.kind,
                  request_logs.outcome, request_logs.tags, request_logs.started_at,
                  request_logs.request_json, request_logs.response_json,
                  request_logs.attempts
           FROM router.request_logs
           JOIN router.services ON services.id = request_logs.service_id
           JOIN router.workspaces ON workspaces.id = request_logs.workspace_id
           CROSS JOIN router.global_settings
           WHERE request_logs.id = %s
             AND request_logs.started_at >= statement_timestamp()
                 - make_interval(days => global_settings.log_retention_days)""",
        (request_log_id,),
    ).fetchone()
    if row is None:
        raise not_found("request log")
    media = connection.execute(
        """SELECT id, media_type, role, size_bytes
           FROM router.media_objects
           WHERE request_log_id = %s ORDER BY id""",
        (request_log_id,),
    ).fetchall()
    summary = {
        name: row.pop(name)
        for name in (
            "id",
            "service_api_name",
            "workspace_api_name",
            "assignment_api_name",
            "provider_model_api_name",
            "kind",
            "outcome",
            "tags",
            "started_at",
        )
    }
    summary["id"] = str(summary["id"])
    for item in media:
        item["id"] = str(item["id"])
    result = {"summary": summary, **row}
    if media:
        result["media"] = media
    return result


def get_request_log_media(
    connection: Connection[Any],
    object_store: ObjectStore | None,
    *,
    request_log_id: uuid.UUID,
    media_id: uuid.UUID,
) -> StoredObject:
    """Read and verify one retained object behind the administrator route."""
    row = connection.execute(
        """SELECT media_objects.object_key, media_objects.media_type,
                  media_objects.size_bytes, media_objects.content_sha256
           FROM router.media_objects
           JOIN router.request_logs ON request_logs.id = media_objects.request_log_id
           CROSS JOIN router.global_settings
           WHERE media_objects.request_log_id = %s AND media_objects.id = %s
             AND request_logs.started_at >= statement_timestamp()
                 - make_interval(days => global_settings.log_retention_days)""",
        (request_log_id, media_id),
    ).fetchone()
    if row is None:
        if (
            connection.execute(
                """SELECT 1 FROM router.request_logs, router.global_settings
                   WHERE request_logs.id = %s
                     AND request_logs.started_at >= statement_timestamp()
                         - make_interval(
                             days => global_settings.log_retention_days
                           )""",
                (request_log_id,),
            ).fetchone()
            is None
        ):
            raise not_found("request log")
        raise not_found("request log media")
    if object_store is None:
        raise content_unavailable()
    try:
        stored = object_store.get(row["object_key"], maximum_bytes=row["size_bytes"])
    except ObjectNotFoundError, ObjectStoreError:
        raise content_unavailable() from None
    if (
        stored.content_type != row["media_type"]
        or len(stored.body) != row["size_bytes"]
        or not hashlib.sha256(stored.body).digest() == row["content_sha256"]
    ):
        raise content_unavailable()
    return stored


def get_log_retention(connection: Connection[Any]) -> int:
    """Read the one global whole-day retention value."""
    row = connection.execute(
        "SELECT log_retention_days FROM router.global_settings WHERE singleton"
    ).fetchone()
    if row is None:
        raise RuntimeError("The global retention row is missing.")
    return cast("int", row["log_retention_days"])


def put_log_retention(
    connection: Connection[Any], *, duration_days: int, actor: AdministratorActor
) -> int:
    """Replace retention and record one current-state activity event."""
    row = connection.execute(
        """UPDATE router.global_settings
           SET log_retention_days = %s, updated_at = transaction_timestamp()
           WHERE singleton RETURNING log_retention_days""",
        (duration_days,),
    ).fetchone()
    if row is None:
        raise RuntimeError("The global retention row is missing.")
    record_activity(
        connection,
        actor.activity_subject,
        "log_retention.update",
        "log_retention",
        resource_id=uuid.UUID(int=0),
    )
    return cast("int", row["log_retention_days"])


def apply_retention_and_cleanup(
    connection: Connection[Any], object_store: ObjectStore | None, *, batch: int = 200
) -> tuple[int, int]:
    """Expire one bounded batch, then drain one bounded object-delete batch."""
    duration = get_log_retention(connection)
    cutoff = datetime.now(tz=UTC) - timedelta(days=duration)
    expired_media = connection.execute(
        """DELETE FROM router.media_objects WHERE id IN (
               SELECT id FROM router.media_objects
               WHERE created_at < %s ORDER BY created_at, id LIMIT %s
               FOR UPDATE SKIP LOCKED
           ) RETURNING id""",
        (cutoff, batch),
    ).fetchall()
    expired_logs = connection.execute(
        """DELETE FROM router.request_logs WHERE id IN (
               SELECT id FROM router.request_logs
               WHERE started_at < %s ORDER BY started_at, id LIMIT %s
               FOR UPDATE SKIP LOCKED
           )""",
        (cutoff, batch),
    ).rowcount
    connection.execute(
        """DELETE FROM router.activity_events WHERE id IN (
               SELECT id FROM router.activity_events
               WHERE occurred_at < %s ORDER BY occurred_at, id LIMIT %s
               FOR UPDATE SKIP LOCKED
           )""",
        (cutoff, batch),
    )
    # Commit database expiry before an object-store timeout. This releases all
    # row locks and makes the public retention boundary effective at once.
    connection.commit()
    deleted_objects = _drain_object_deletions(connection, object_store, batch=batch)
    return len(expired_media) + expired_logs, deleted_objects


def cleanup_health(
    connection: Connection[Any],
) -> Literal["healthy", "degraded", "unavailable"]:
    """Expose queued or overdue object cleanup without public resource states."""
    row = connection.execute(
        """SELECT count(*) AS pending_objects,
                  count(*) FILTER (
                    WHERE queued_at <= statement_timestamp() - interval '24 hours'
                  ) AS overdue_objects,
                  count(*) FILTER (WHERE failure_count > 0) AS failed_objects,
                  EXISTS (
                    SELECT 1 FROM router.request_logs, router.global_settings
                    WHERE request_logs.started_at < statement_timestamp()
                          - make_interval(days => global_settings.log_retention_days)
                  ) OR EXISTS (
                    SELECT 1 FROM router.activity_events, router.global_settings
                    WHERE activity_events.occurred_at < statement_timestamp()
                          - make_interval(days => global_settings.log_retention_days)
                  ) OR EXISTS (
                    SELECT 1 FROM router.media_objects, router.global_settings
                    WHERE media_objects.created_at < statement_timestamp()
                          - make_interval(days => global_settings.log_retention_days)
                  ) AS expired_diagnostics,
                  EXISTS (
                    SELECT 1 FROM router.request_logs, router.global_settings
                    WHERE request_logs.started_at < statement_timestamp()
                          - make_interval(
                              days => global_settings.log_retention_days + 1
                            )
                  ) OR EXISTS (
                    SELECT 1 FROM router.activity_events, router.global_settings
                    WHERE activity_events.occurred_at < statement_timestamp()
                          - make_interval(
                              days => global_settings.log_retention_days + 1
                            )
                  ) OR EXISTS (
                    SELECT 1 FROM router.media_objects, router.global_settings
                    WHERE media_objects.created_at < statement_timestamp()
                          - make_interval(
                              days => global_settings.log_retention_days + 1
                            )
                  ) AS overdue_diagnostics
           FROM router.object_deletion_queue"""
    ).fetchone()
    if row is None:
        raise RuntimeError("The cleanup health query failed.")
    if row["overdue_objects"] or row["overdue_diagnostics"]:
        return "unavailable"
    if row["pending_objects"] or row["failed_objects"] or row["expired_diagnostics"]:
        return "degraded"
    return "healthy"


def _drain_object_deletions(
    connection: Connection[Any], object_store: ObjectStore | None, *, batch: int
) -> int:
    if object_store is None:
        return 0
    rows = connection.execute(
        """SELECT object_key FROM router.object_deletion_queue
           ORDER BY queued_at, object_key LIMIT %s""",
        (min(batch, 4),),
    ).fetchall()
    connection.commit()
    if not rows:
        return 0

    def delete_one(object_key: str) -> tuple[str, bool]:
        try:
            object_store.delete(object_key)
        except Exception:  # noqa: BLE001 - Record all dependency failure classes safely.
            return object_key, False
        return object_key, True

    # One bounded concurrent group avoids a sequence of object-store timeouts.
    with ThreadPoolExecutor(max_workers=len(rows)) as executor:
        results = dict(executor.map(delete_one, (row["object_key"] for row in rows)))
    deleted = 0
    for object_key, succeeded in results.items():
        if not succeeded:
            connection.execute(
                """UPDATE router.object_deletion_queue
                   SET last_attempt_at = statement_timestamp(),
                       failure_count = failure_count + 1,
                       failure_class = 'object_store_unavailable'
                   WHERE object_key = %s""",
                (object_key,),
            )
        else:
            connection.execute(
                "DELETE FROM router.object_deletion_queue WHERE object_key = %s",
                (object_key,),
            )
            deleted += 1
    return deleted


def _validate_write(value: DetailedLogWrite) -> None:
    if value.started_at.tzinfo is None:
        raise ValueError
    if len(value.request_json) > _MAX_LOG_REQUEST_CHARACTERS or (
        value.response_json is not None
        and len(value.response_json) > _MAX_LOG_RESPONSE_CHARACTERS
    ):
        raise ValueError
    _normalized_tags(value.tags)
    if len(value.attempts) > _MAX_ATTEMPTS or len(value.media) > _MAX_MEDIA_ITEMS:
        raise ValueError
    input_media = [media for media in value.media if media.role == "input"]
    if len(input_media) > _MAX_INPUT_IMAGES:
        raise ValueError
    if sum(len(media.body) for media in input_media) > _MAX_TOTAL_INPUT_IMAGE_BYTES:
        raise ValueError
    for media in value.media:
        if media.media_type not in _SAFE_MEDIA_TYPES:
            raise ValueError
        if not 1 <= len(media.body) <= _MAX_RETAINED_OBJECT_BYTES:
            raise ValueError
        if media.role == "input" and (
            media.media_type not in _INPUT_IMAGE_TYPES
            or len(media.body) > _MAX_INPUT_IMAGE_BYTES
        ):
            raise ValueError


def _normalized_tags(tags: Sequence[str]) -> tuple[str, ...]:
    if len(tags) > _MAX_TAGS:
        raise ValueError
    normalized = tuple(sorted(set(tags), key=lambda tag: tag.encode("utf-8")))
    encoded = [tag.encode("utf-8") for tag in normalized]
    if any(not 1 <= len(tag) <= _MAX_TAG_BYTES for tag in encoded):
        raise ValueError
    if sum(map(len, encoded)) > _MAX_TAG_SET_BYTES:
        raise ValueError
    return normalized


def _validate_time_range(from_time: datetime, to_time: datetime) -> None:
    if (
        from_time.tzinfo is None
        or to_time.tzinfo is None
        or from_time >= to_time
        or to_time - from_time > _MAX_LOG_RANGE
    ):
        raise invalid_request("from", "The request-log time range is invalid.")


def _uuid_cursor(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise invalid_request("cursor", "The cursor is invalid.") from None


def _page(
    rows: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    selected = rows[:limit]
    for row in selected:
        row["id"] = str(row["id"])
    if len(rows) <= limit:
        return selected, None
    return selected, str(selected[-1]["id"])


def _object_key(
    started_at: datetime,
    service_id: uuid.UUID,
    workspace_id: uuid.UUID,
    media_id: uuid.UUID,
) -> str:
    day = started_at.astimezone(UTC).date().isoformat()
    return f"{day}/{service_id}/{workspace_id}/{media_id}"


def _queue_orphaned_object_best_effort(database_url: str, object_key: str) -> None:
    """Keep cleanup intent when upload rollback cannot delete an object."""
    try:
        with psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            options=_DATABASE_TIMEOUT_OPTIONS,
        ) as connection:
            connection.execute(
                """INSERT INTO router.object_deletion_queue
                       (object_key, failure_count, failure_class, last_attempt_at)
                   VALUES (%s, 1, 'upload_rollback_failed', statement_timestamp())
                   ON CONFLICT (object_key) DO UPDATE
                   SET failure_count = router.object_deletion_queue.failure_count + 1,
                       failure_class = 'upload_rollback_failed',
                       last_attempt_at = statement_timestamp()""",
                (object_key,),
            )
    except Exception:  # noqa: BLE001 - Diagnostic cleanup stays best-effort.
        return


def _delete_or_queue_best_effort(
    database_url: str, object_store: ObjectStore, object_key: str
) -> None:
    """Delete one uncertain upload or keep durable cleanup intent."""
    try:
        object_store.delete(object_key)
    except Exception:  # noqa: BLE001 - Convert dependency failures to cleanup intent.
        _queue_orphaned_object_best_effort(database_url, object_key)
