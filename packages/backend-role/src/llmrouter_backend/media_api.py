"""Closed asynchronous media-job API and durable worker composition."""
# ruff: noqa: BLE001, C901, D102, EM101, TRY003, TRY004, TRY300, TRY301

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import psycopg
from opendle import AssignmentSelector, ExactModelSelector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ConfigDict, Field, field_validator, model_validator

from llmrouter_backend import catalog
from llmrouter_backend.assignments import (
    ResolvedAssignment,
    resolve_assignment_for_call,
)
from llmrouter_backend.calls import (
    CallExecutionError,
    CallExecutor,
    CallRequest,
    CallRequirements,
    CallResult,
)
from llmrouter_backend.diagnostics import CapturedMedia
from llmrouter_backend.errors import (
    ApiError,
    content_unavailable,
    invalid_request,
    not_found,
)
from llmrouter_backend.model_api import (
    AssignmentModelSelector,
    ImageInputPart,
    ModelSelector,
    _validate_tags,
)
from llmrouter_backend.models import ClosedModel, SafeError
from llmrouter_backend.object_store import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    StoredObject,
)
from llmrouter_backend.store import ServiceActor

if TYPE_CHECKING:
    from collections.abc import Mapping

_DATABASE_OPTIONS = "-c statement_timeout=2000 -c lock_timeout=500"
_DATABASE_CONNECT_TIMEOUT_SECONDS = 2
_CATALOG_WRITE_LOCK = 4_993_044_345_823
_MAXIMUM_MEDIA_BYTES = 1024 * 1024 * 1024
_API_NAME_PATTERN = r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
_WORKER_DEPENDENCY_RETRY_SECONDS = 1.0
_WORKER_MAXIMUM_RETRY_SECONDS = 30.0
type MediaKind = Literal["image", "video", "audio"]
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _MediaAdmissionSnapshot:
    """Immutable scope and route facts checked on both sides of object I/O."""

    workspace_id: uuid.UUID
    provider_model_api_name: str
    route_state: tuple[object, ...]


class NativeMediaModel(ClosedModel):
    """Reject coercion and hide private media input in validation diagnostics."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class MediaJobRequest(NativeMediaModel):
    """One complete closed native media-job request."""

    workspace_api_name: str = Field(pattern=_API_NAME_PATTERN)
    selector: ModelSelector
    kind: MediaKind
    prompt: str = Field(min_length=1, max_length=1_000_000)
    input_images: list[ImageInputPart] | None = Field(default=None, max_length=8)
    tags: list[str] | None = Field(default=None, max_length=32)

    @field_validator("prompt")
    @classmethod
    def require_utf8_prompt(cls, value: str) -> str:
        value.encode("utf-8")
        return value

    @model_validator(mode="after")
    def validate_media_shape(self) -> MediaJobRequest:
        if self.kind == "audio" and "input_images" in self.model_fields_set:
            raise ValueError("Audio generation cannot contain input images.")
        if "input_images" in self.model_fields_set and self.input_images is None:
            raise ValueError("The optional input-images field cannot be null.")
        if "tags" in self.model_fields_set and self.tags is None:
            raise ValueError("The optional tags field cannot be null.")
        _validate_tags(self.tags or [])
        images = self.input_images or []
        if sum(len(item.decoded_body()) for item in images) > 50 * 1024 * 1024:
            raise ValueError("The total input-image byte size is invalid.")
        return self


class MediaContent(NativeMediaModel):
    """Safe metadata for one retained generated result."""

    media_type: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(strict=True, ge=0)


class MediaJob(NativeMediaModel):
    """One protected media job without object-store controls."""

    id: str
    workspace_api_name: str = Field(pattern=_API_NAME_PATTERN)
    provider_model_api_name: str = Field(pattern=_API_NAME_PATTERN)
    kind: MediaKind
    state: Literal["pending", "running", "succeeded", "failed"]
    content: MediaContent | None = None
    error: SafeError | None = None
    created_at: datetime
    completed_at: datetime | None = None


def internal_media_call(body: MediaJobRequest) -> CallRequest:
    """Translate one validated job without provider or storage fields."""
    images = tuple(_captured_image(item) for item in (body.input_images or []))
    selector = (
        AssignmentSelector(body.selector.assignment_api_name)
        if isinstance(body.selector, AssignmentModelSelector)
        else ExactModelSelector(body.selector.provider_model_api_name)
    )
    logged_body = body.model_dump(mode="json", exclude_none=True)
    logged_body.pop("input_images", None)
    return CallRequest(
        workspace_api_name=body.workspace_api_name,
        selector=selector,
        kind="media",
        requirements=CallRequirements(
            required_inputs=frozenset({"text", "image"} if images else {"text"}),
            required_output=body.kind,
            input_image_sizes=tuple(len(item.body) for item in images),
        ),
        request_json=json.dumps(
            logged_body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        tags=tuple(body.tags or ()),
        media=images,
    )


def create_media_job(
    *,
    database_url: str,
    object_store: ObjectStore | None,
    actor: ServiceActor,
    body: MediaJobRequest,
    deadline_seconds: int,
) -> dict[str, Any]:
    """Upload without locks, then admit through one short checked transaction."""
    connection: psycopg.Connection[Any] | None = None
    uploaded: list[str] = []
    try:
        call = _validated_internal_media_call(body)
        snapshot = _read_media_admission_snapshot(database_url, actor, call)
        retained_objects = _required_object_store(object_store)
        job_id = uuid.uuid4()
        created_at = datetime.now(tz=UTC)
        payload: dict[str, object] = body.model_dump(
            mode="json", exclude={"input_images"}, exclude_none=True
        )
        payload["input_media_ids"] = []
        media_rows: list[tuple[object, ...]] = []
        for image in call.media:
            media_id = uuid.uuid4()
            object_key = _object_key(
                created_at,
                actor.service_id,
                snapshot.workspace_id,
                job_id,
                media_id,
            )
            uploaded.append(object_key)
            retained_objects.put(object_key, image.body, image.media_type)
            media_rows.append(
                (
                    media_id,
                    actor.service_id,
                    snapshot.workspace_id,
                    job_id,
                    object_key,
                    image.media_type,
                    len(image.body),
                    hashlib.sha256(image.body).digest(),
                ),
            )
            cast("list[str]", payload["input_media_ids"]).append(str(media_id))
        connection = psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
            options=_DATABASE_OPTIONS,
        )
        current = _resolve_media_admission(connection, actor, call)
        if current != snapshot:
            raise invalid_request(
                "selector", "The media route changed during job admission."
            )
        connection.execute(
            """INSERT INTO router.media_jobs
                   (id, service_id, workspace_id, provider_model_api_name, kind,
                    payload, created_at, deadline_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                job_id,
                actor.service_id,
                snapshot.workspace_id,
                snapshot.provider_model_api_name,
                body.kind,
                Jsonb(payload),
                created_at,
                created_at + timedelta(seconds=deadline_seconds),
            ),
        )
        for media_row in media_rows:
            connection.execute(
                """INSERT INTO router.media_objects
                       (id, service_id, workspace_id, media_job_id, object_key,
                        media_type, role, size_bytes, content_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, 'input', %s, %s)""",
                media_row,
            )
        _commit_media_admission(connection)
    except ApiError:
        if connection is not None:
            _rollback_media_admission(connection)
        for object_key in uploaded:
            _delete_or_queue(database_url, object_store, object_key)
        raise
    except Exception as error:
        if connection is not None:
            _rollback_media_admission(connection)
        for object_key in uploaded:
            _delete_or_queue(database_url, object_store, object_key)
        raise ApiError(
            500,
            "internal_error",
            "The Router could not retain the uploaded media.",
        ) from error
    finally:
        if connection is not None:
            connection.close()
    return {
        "id": str(job_id),
        "workspace_api_name": body.workspace_api_name,
        "provider_model_api_name": snapshot.provider_model_api_name,
        "kind": body.kind,
        "state": "pending",
        "created_at": created_at,
    }


def _validated_internal_media_call(body: MediaJobRequest) -> CallRequest:
    try:
        return internal_media_call(body)
    except ValueError:
        raise invalid_request(
            "body", "The media job does not match the deployment bounds."
        ) from None


def _required_object_store(object_store: ObjectStore | None) -> ObjectStore:
    if object_store is None:
        raise ApiError(
            500,
            "internal_error",
            "The Router could not retain generated media.",
        )
    return object_store


def get_media_job(
    connection: psycopg.Connection[Any], *, actor: ServiceActor, job_id: uuid.UUID
) -> dict[str, Any]:
    """Read one job only through its owning service scope."""
    row = connection.execute(
        """SELECT media_jobs.id, workspaces.api_name AS workspace_api_name,
                  media_jobs.provider_model_api_name, media_jobs.kind,
                  media_jobs.state, media_jobs.error_code,
                  media_jobs.error_message, media_jobs.created_at,
                  media_jobs.completed_at,
                  media_objects.media_type, media_objects.size_bytes
           FROM router.media_jobs
           JOIN router.workspaces ON workspaces.id = media_jobs.workspace_id
           CROSS JOIN router.global_settings
           LEFT JOIN router.media_objects
             ON media_objects.media_job_id = media_jobs.id
            AND media_objects.role = 'output'
            AND media_objects.created_at >= statement_timestamp()
                - make_interval(days => global_settings.log_retention_days)
           WHERE media_jobs.id = %s AND media_jobs.service_id = %s""",
        (job_id, actor.service_id),
    ).fetchone()
    if row is None:
        raise not_found("media job")
    return _public_job(row)


def get_media_job_content(
    connection: psycopg.Connection[Any],
    object_store: ObjectStore | None,
    *,
    actor: ServiceActor,
    job_id: uuid.UUID,
) -> StoredObject:
    """Read and verify one retained result without exposing its storage key."""
    row = connection.execute(
        """SELECT media_jobs.state, media_objects.object_key,
                  media_objects.media_type, media_objects.size_bytes,
                  media_objects.content_sha256
           FROM router.media_jobs
           CROSS JOIN router.global_settings
           LEFT JOIN router.media_objects
             ON media_objects.media_job_id = media_jobs.id
            AND media_objects.role = 'output'
            AND media_objects.created_at >= statement_timestamp()
                - make_interval(days => global_settings.log_retention_days)
           WHERE media_jobs.id = %s AND media_jobs.service_id = %s""",
        (job_id, actor.service_id),
    ).fetchone()
    if row is None:
        raise not_found("media job")
    if row["state"] != "succeeded" or row["object_key"] is None or object_store is None:
        raise content_unavailable()
    try:
        stored = object_store.get(
            cast("str", row["object_key"]), maximum_bytes=row["size_bytes"]
        )
    except ObjectNotFoundError, ObjectStoreError:
        raise content_unavailable() from None
    if (
        stored.content_type != row["media_type"]
        or len(stored.body) != row["size_bytes"]
        or hashlib.sha256(stored.body).digest() != row["content_sha256"]
    ):
        raise content_unavailable()
    return stored


async def run_media_worker_once(
    database_url: str,
    executor: CallExecutor,
    object_store: ObjectStore | None,
) -> bool:
    """Claim and finish at most one durable job without a public lease state."""
    claimed = await asyncio.to_thread(_claim_job, database_url)
    if claimed is None:
        return False
    job_id = cast("uuid.UUID", claimed["id"])
    try:
        call = await asyncio.to_thread(
            _call_from_claimed_job, database_url, object_store, claimed
        )
        actor = ServiceActor(
            cast("uuid.UUID", claimed["service_id"]),
            cast("str", claimed["service_api_name"]),
            uuid.UUID(int=0),
        )
        remaining = (
            cast("datetime", claimed["deadline_at"]) - datetime.now(tz=UTC)
        ).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        async with asyncio.timeout(remaining):
            result = await executor.execute(actor, call)
        await asyncio.to_thread(
            _complete_job, database_url, object_store, claimed, result
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        await asyncio.to_thread(
            _fail_job,
            database_url,
            job_id,
            "upstream_failed",
            "The media-job deadline expired.",
        )
    except CallExecutionError as error:
        await asyncio.to_thread(_fail_job, database_url, job_id, error.code, str(error))
    except Exception:
        await asyncio.to_thread(
            _fail_job,
            database_url,
            job_id,
            "internal_error",
            "The Router could not complete the media job.",
        )
    return True


async def media_worker_loop(
    database_url: str,
    executor: CallExecutor,
    object_store: ObjectStore | None,
) -> None:
    """Run bounded durable media work in the one normal application."""
    retry_seconds = _WORKER_DEPENDENCY_RETRY_SECONDS
    while True:
        try:
            worked = await run_media_worker_once(database_url, executor, object_store)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning("The media worker dependency failed; it will retry.")
            await asyncio.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, _WORKER_MAXIMUM_RETRY_SECONDS)
            continue
        retry_seconds = _WORKER_DEPENDENCY_RETRY_SECONDS
        if not worked:
            await asyncio.sleep(0.1)


def _commit_media_admission(connection: psycopg.Connection[Any]) -> None:
    """Commit one admission at the object-cleanup boundary."""
    connection.commit()


def _rollback_media_admission(connection: psycopg.Connection[Any]) -> None:
    """Keep object cleanup available when the failed connection cannot roll back."""
    try:
        connection.rollback()
    except Exception:
        _LOGGER.warning("The failed media admission could not roll back cleanly.")


def _read_media_admission_snapshot(
    database_url: str, actor: ServiceActor, call: CallRequest
) -> _MediaAdmissionSnapshot:
    with psycopg.connect(
        database_url,
        connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
        row_factory=dict_row,
        options=_DATABASE_OPTIONS,
    ) as connection:
        try:
            return _resolve_media_admission(
                connection, actor, call, commit_assignment_evidence=True
            )
        finally:
            connection.rollback()


def _resolve_media_admission(
    connection: psycopg.Connection[Any],
    actor: ServiceActor,
    call: CallRequest,
    *,
    commit_assignment_evidence: bool = False,
) -> _MediaAdmissionSnapshot:
    workspace = connection.execute(
        """SELECT id FROM router.workspaces
           WHERE service_id = %s AND api_name = %s FOR KEY SHARE""",
        (actor.service_id, call.workspace_api_name),
    ).fetchone()
    if workspace is None:
        raise not_found("workspace")
    requirements = call.requirements
    if isinstance(call.selector, AssignmentSelector):
        resolved, routes = resolve_assignment_for_call(
            connection,
            service_id=actor.service_id,
            workspace_api_name=call.workspace_api_name,
            assignment_api_name=call.selector.assignment_api_name,
            required_inputs=requirements.required_inputs,
            required_output=requirements.required_output,
            required_capabilities=requirements.required_capabilities,
            actor_subject=actor.activity_subject,
            embedding_dimension=None,
            input_image_sizes=requirements.input_image_sizes,
            output_duration_seconds=None,
            commit_evidence=commit_assignment_evidence,
        )
        route_state: tuple[object, ...] = (
            "assignment",
            _assignment_route_state(resolved),
            routes,
        )
    else:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_CATALOG_WRITE_LOCK,))
        route = catalog.resolve_provider_route(
            connection,
            call.selector.provider_model_api_name,
            required_inputs=requirements.required_inputs,
            required_output=requirements.required_output,
            required_capabilities=requirements.required_capabilities,
            reasoning_level=None,
        )
        catalog.validate_route_constraints(
            route,
            embedding_dimension=None,
            input_image_sizes=requirements.input_image_sizes,
            output_duration_seconds=None,
        )
        routes = (route,)
        route_state = ("exact", routes)
    return _MediaAdmissionSnapshot(
        cast("uuid.UUID", workspace["id"]),
        routes[0].provider_model_api_name,
        route_state,
    )


def _assignment_route_state(resolved: ResolvedAssignment) -> tuple[object, ...]:
    return (
        resolved.definition_kind,
        resolved.defined_by_service_api_name,
        resolved.inherits_assignment_api_name,
        resolved.direct_chain,
        resolved.effective_chain,
        resolved.reasoning_level,
    )


def _claim_job(database_url: str) -> dict[str, Any] | None:
    with psycopg.connect(
        database_url,
        connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
        row_factory=dict_row,
        options=_DATABASE_OPTIONS,
    ) as connection:
        connection.execute(
            """UPDATE router.media_jobs
               SET state = 'failed', payload = '{}'::jsonb,
                   error_code = 'upstream_failed',
                   error_message = 'The media-job deadline expired.',
                   completed_at = statement_timestamp()
               WHERE id IN (
                   SELECT id FROM router.media_jobs
                   WHERE state IN ('pending', 'running')
                     AND deadline_at <= statement_timestamp()
                   ORDER BY deadline_at, id LIMIT 100
                   FOR UPDATE SKIP LOCKED
               )"""
        )
        row = connection.execute(
            """SELECT media_jobs.id, media_jobs.service_id,
                      media_jobs.workspace_id, media_jobs.payload,
                      media_jobs.deadline_at,
                      services.api_name AS service_api_name
               FROM router.media_jobs
               JOIN router.services ON services.id = media_jobs.service_id
               WHERE media_jobs.id = (
                   SELECT id FROM router.media_jobs
                   WHERE state = 'pending' AND deadline_at > statement_timestamp()
                   ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
               ) FOR UPDATE OF media_jobs"""
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            """UPDATE router.media_jobs SET state = 'running'
               WHERE id = %s AND state = 'pending' RETURNING id""",
            (row["id"],),
        ).fetchone()
        return row if updated is not None else None


def _call_from_claimed_job(
    database_url: str,
    object_store: ObjectStore | None,
    claimed: Mapping[str, object],
) -> CallRequest:
    payload = cast("dict[str, object]", claimed["payload"])
    document = {
        name: value for name, value in payload.items() if name != "input_media_ids"
    }
    images: list[dict[str, str]] = []
    media_ids = payload.get("input_media_ids", [])
    if not isinstance(media_ids, list):
        raise ValueError
    if media_ids:
        if object_store is None:
            raise ObjectStoreError
        parsed_ids = [uuid.UUID(cast("str", raw_id)) for raw_id in media_ids]
        if len(parsed_ids) != len(set(parsed_ids)):
            raise ValueError
        with psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
            options=_DATABASE_OPTIONS,
        ) as connection:
            rows = connection.execute(
                """SELECT media_objects.id, object_key, media_type, size_bytes,
                          content_sha256
                   FROM router.media_objects, router.global_settings
                   WHERE media_objects.id = ANY(%s)
                     AND media_job_id = %s AND role = 'input'
                     AND created_at >= statement_timestamp()
                         - make_interval(days => global_settings.log_retention_days)""",
                (parsed_ids, claimed["id"]),
            ).fetchall()
        retained = {cast("uuid.UUID", row["id"]): row for row in rows}
        if len(retained) != len(parsed_ids):
            raise ObjectNotFoundError
        for media_id in parsed_ids:
            row = retained[media_id]
            stored = object_store.get(row["object_key"], row["size_bytes"])
            if (
                stored.content_type != row["media_type"]
                or len(stored.body) != row["size_bytes"]
                or hashlib.sha256(stored.body).digest() != row["content_sha256"]
            ):
                raise ObjectStoreError
            images.append(
                {
                    "type": "image",
                    "media_type": stored.content_type,
                    "data_base64": base64.b64encode(stored.body).decode("ascii"),
                }
            )
    if images:
        document["input_images"] = images
    return internal_media_call(MediaJobRequest.model_validate(document))


def _complete_job(
    database_url: str,
    object_store: ObjectStore | None,
    claimed: Mapping[str, object],
    result: CallResult,
) -> None:
    if (
        object_store is None
        or len(result.outputs) != 1
        or result.outputs[0].kind != "media"
        or result.outputs[0].media_body is None
    ):
        _fail_job(
            database_url,
            cast("uuid.UUID", claimed["id"]),
            "content_unavailable",
            "The generated media could not be retained.",
        )
        return
    output = result.outputs[0]
    metadata = json.loads(output.content_json)
    body = cast("bytes", output.media_body)
    media_id = uuid.uuid4()
    created_at = datetime.now(tz=UTC)
    object_key = _object_key(
        created_at,
        cast("uuid.UUID", claimed["service_id"]),
        cast("uuid.UUID", claimed["workspace_id"]),
        cast("uuid.UUID", claimed["id"]),
        media_id,
    )
    try:
        object_store.put(object_key, body, cast("str", metadata["media_type"]))
        with psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
            options=_DATABASE_OPTIONS,
        ) as connection:
            row = connection.execute(
                """UPDATE router.media_jobs
                   SET state = 'succeeded', provider_model_api_name = %s,
                       payload = '{}'::jsonb,
                       completed_at = statement_timestamp()
                   WHERE id = %s AND state = 'running'
                   RETURNING service_id, workspace_id""",
                (result.provider_model_api_name, claimed["id"]),
            ).fetchone()
            if row is None:
                raise LookupError
            connection.execute(
                """INSERT INTO router.media_objects
                       (id, service_id, workspace_id, media_job_id, object_key,
                        media_type, role, size_bytes, content_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, 'output', %s, %s)""",
                (
                    media_id,
                    row["service_id"],
                    row["workspace_id"],
                    claimed["id"],
                    object_key,
                    metadata["media_type"],
                    len(body),
                    hashlib.sha256(body).digest(),
                ),
            )
    except Exception:
        _delete_or_queue(database_url, object_store, object_key)
        _fail_job(
            database_url,
            cast("uuid.UUID", claimed["id"]),
            "content_unavailable",
            "The generated media could not be retained.",
        )


def _fail_job(database_url: str, job_id: uuid.UUID, code: str, message: str) -> None:
    safe_codes = {
        "provider_unavailable",
        "upstream_failed",
        "rate_limited",
        "internal_error",
        "content_unavailable",
    }
    safe_code = code if code in safe_codes else "upstream_failed"
    safe_message = message[:1000] if code in safe_codes else "The media job failed."
    with psycopg.connect(
        database_url,
        connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
        options=_DATABASE_OPTIONS,
    ) as connection:
        connection.execute(
            """UPDATE router.media_jobs
               SET state = 'failed', payload = '{}'::jsonb,
                   error_code = %s, error_message = %s,
                   completed_at = statement_timestamp()
               WHERE id = %s AND state = 'running'""",
            (safe_code, safe_message, job_id),
        )


def _public_job(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        name: row[name]
        for name in (
            "workspace_api_name",
            "provider_model_api_name",
            "kind",
            "state",
            "created_at",
            "completed_at",
        )
        if row.get(name) is not None
    }
    result["id"] = str(row["id"])
    if row.get("media_type") is not None:
        result["content"] = {
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
        }
    if row.get("error_code") is not None:
        result["error"] = {
            "code": row["error_code"],
            "message": row["error_message"],
        }
    return result


def _captured_image(value: ImageInputPart) -> CapturedMedia:
    return CapturedMedia(value.decoded_body(), value.media_type, "input")


def _object_key(
    created_at: datetime,
    service_id: uuid.UUID,
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    media_id: uuid.UUID,
) -> str:
    return (
        f"{created_at.date().isoformat()}/{service_id}/{workspace_id}/"
        f"media-jobs/{job_id}/{media_id}"
    )


def _delete_or_queue(
    database_url: str, object_store: ObjectStore | None, object_key: str
) -> None:
    if object_store is None:
        return
    try:
        object_store.delete(object_key)
        return
    except Exception:
        _LOGGER.warning("A retained media object needs queued deletion.")
    try:
        with psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            options=_DATABASE_OPTIONS,
        ) as connection:
            connection.execute(
                """INSERT INTO router.object_deletion_queue
                       (object_key, failure_count, failure_class, last_attempt_at)
                   VALUES (%s, 1, 'media_cleanup_failed', statement_timestamp())
                   ON CONFLICT (object_key) DO UPDATE
                   SET failure_count = router.object_deletion_queue.failure_count + 1,
                       failure_class = 'media_cleanup_failed',
                       last_attempt_at = statement_timestamp()""",
                (object_key,),
            )
    except Exception:
        return
