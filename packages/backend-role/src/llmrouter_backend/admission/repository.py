"""Fleet-wide PostgreSQL request admission and scoped status reads."""
# ruff: noqa: ARG001, D107, E501, EM101, S101, TRY003

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
)

from .errors import AdmissionError, AdmissionErrorCode
from .model import (
    DEFAULT_MAXIMUM_FUTURE_SKEW,
    DEFAULT_MAXIMUM_INITIAL_AGE,
    FINGERPRINT_VERSION,
    AdmissionReceipt,
    AdmissionRequest,
    AdmissionResult,
    RequestKind,
    RequestState,
    RequestStatus,
    uuidv7_time,
)

if TYPE_CHECKING:
    from datetime import datetime

    from psycopg import Connection

    from llmrouter_backend.authority import RequestContext


class PostgresAdmissionRepository:
    """Atomically create or find scoped logical request bindings."""

    def __init__(
        self,
        database_url: str,
        *,
        maximum_initial_age: timedelta = DEFAULT_MAXIMUM_INITIAL_AGE,
        maximum_future_skew: timedelta = DEFAULT_MAXIMUM_FUTURE_SKEW,
    ) -> None:
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        if maximum_initial_age <= timedelta(0) or maximum_future_skew < timedelta(0):
            raise ValueError("The UUIDv7 time limits are invalid.")
        self._database_url = database_url
        self._maximum_initial_age = maximum_initial_age
        self._maximum_future_skew = maximum_future_skew

    def admit(
        self,
        context: RequestContext,
        request: AdmissionRequest,
        *,
        now: datetime,
    ) -> AdmissionResult:
        """Return one committed create-or-equal-replay admission."""
        _require_create_authority(context, request)
        _require_aware(now)
        if request.fingerprint.service_id != context.scope.service_id or (
            request.fingerprint.workspace_id != context.scope.workspace_id
        ):
            raise AdmissionError(
                AdmissionErrorCode.INSUFFICIENT_SCOPE, context.request_id
            )
        digest = request.fingerprint.sha256()
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                _lock_identity(connection, context, request.request_id)
                existing = _select_binding(connection, context, request.request_id)
                if existing is not None:
                    if (
                        existing["terminal_at"] is not None
                        and existing["expires_at"] is not None
                        and existing["expires_at"] <= now
                    ):
                        raise AdmissionError(
                            AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED,
                            context.request_id,
                        )
                    if (
                        existing["fingerprint_version"] != FINGERPRINT_VERSION
                        or bytes(existing["fingerprint_sha256"]) != digest
                    ):
                        raise AdmissionError(
                            AdmissionErrorCode.REQUEST_IDENTITY_CONFLICT,
                            context.request_id,
                        )
                    result = AdmissionResult(_receipt(existing), created=False)
                else:
                    request_time = uuidv7_time(request.request_id)
                    if (
                        request_time < now - self._maximum_initial_age
                        or request_time > now + self._maximum_future_skew
                    ):
                        raise AdmissionError(
                            AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED,
                            context.request_id,
                        )
                    _require_active_scope(connection, context)
                    target = _resolve_target(connection, context, request)
                    attachments = _validated_attachments(
                        connection, context, request, now=now
                    )
                    row_id = uuid.uuid4()
                    locations = _locations(request.kind, request.request_id, context)
                    row = connection.execute(
                        """
                        INSERT INTO router.logical_requests (
                            row_id, request_id, request_kind, service_id, workspace_id,
                            assignment_id, exact_route_id, configuration_revision_id,
                            operation_name, contract_major, fingerprint_version,
                            fingerprint_sha256, data_profile, capture_enabled,
                            capture_pressure_reason, admitted_at, last_transition_at,
                            status_location, cancel_location, events_location
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s
                        ) RETURNING *
                        """,
                        (
                            row_id,
                            request.request_id,
                            request.kind.value,
                            context.scope.service_id,
                            context.scope.workspace_id,
                            target[1],
                            target[2],
                            target[0],
                            request.fingerprint.operation,
                            request.fingerprint.contract_major,
                            FINGERPRINT_VERSION,
                            digest,
                            request.fingerprint.data_profile,
                            request.capture_enabled,
                            None if request.capture_enabled else request.capture_reason,
                            now,
                            now,
                            *locations,
                        ),
                    ).fetchone()
                    assert row is not None
                    for ordinal, attachment in enumerate(attachments, start=1):
                        connection.execute(
                            """
                            INSERT INTO router.request_attachments (
                                request_row_id, service_id, workspace_id, attachment_id,
                                ordinal, content_sha256, byte_length
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                row_id,
                                context.scope.service_id,
                                context.scope.workspace_id,
                                attachment[0],
                                ordinal,
                                attachment[1],
                                attachment[2],
                            ),
                        )
                    result = AdmissionResult(_receipt(row), created=True)
            if connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                raise RuntimeError("The durable admission transaction did not commit.")
        return result

    def status(
        self, context: RequestContext, request_id: str, *, now: datetime
    ) -> RequestStatus:
        """Return status only for the exact original service and workspace."""
        _require_read_authority(context)
        _require_aware(now)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = _select_binding(connection, context, request_id)
        if row is None or (
            row["terminal_at"] is not None
            and row["expires_at"] is not None
            and row["expires_at"] <= now
        ):
            raise AdmissionError(
                AdmissionErrorCode.REQUEST_NOT_FOUND, context.request_id
            )
        expected_kind = {
            "model.read": RequestKind.MODEL.value,
            "tool.read": RequestKind.SHARED_TOOL.value,
        }[context.operation]
        if row["request_kind"] != expected_kind:
            raise AdmissionError(
                AdmissionErrorCode.REQUEST_NOT_FOUND, context.request_id
            )
        return RequestStatus(
            receipt=_receipt(row),
            last_transition_at=row["last_transition_at"],
            terminal_at=row["terminal_at"],
            configuration_revision_id=str(row["configuration_revision_id"]),
            assignment_id=(
                None if row["assignment_id"] is None else str(row["assignment_id"])
            ),
            exact_route_id=(
                None if row["exact_route_id"] is None else str(row["exact_route_id"])
            ),
        )


def _require_create_authority(
    context: RequestContext, request: AdmissionRequest
) -> None:
    operations = {
        RequestKind.MODEL: "model.create",
        RequestKind.SHARED_TOOL: "tool.create",
    }
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation == operations[request.kind]
        and context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise AdmissionError(AdmissionErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_read_authority(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation in {"model.read", "tool.read"}
        and not context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise AdmissionError(AdmissionErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _lock_identity(
    connection: Connection[Any], context: RequestContext, request_id: str
) -> None:
    key = f"admission:{context.scope.service_id}:{context.scope.workspace_id or '-'}:{request_id}"
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def _select_binding(
    connection: Connection[Any], context: RequestContext, request_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM router.logical_requests
        WHERE service_id = %s AND workspace_id IS NOT DISTINCT FROM %s
          AND request_id = %s
        """,
        (context.scope.service_id, context.scope.workspace_id, request_id),
    ).fetchone()
    return None if row is None else dict(row)


def _resolve_target(
    connection: Connection[Any], context: RequestContext, request: AdmissionRequest
) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]:
    if request.exact_route_id is not None:
        row = connection.execute(
            """
            WITH RECURSIVE service_chain AS (
                SELECT id, parent_service_id, 1 AS depth
                FROM router.services WHERE id = %s
              UNION ALL
                SELECT parent.id, parent.parent_service_id, child.depth + 1
                FROM router.services AS parent
                JOIN service_chain AS child ON child.parent_service_id = parent.id
            ), scope_revisions AS (
                SELECT active.revision_id, revision.content, 0 AS priority
                FROM router.active_configurations AS active
                JOIN router.configuration_revisions AS revision
                  ON revision.id = active.revision_id
                WHERE active.scope_kind = 'workspace' AND active.service_id = %s
                  AND active.workspace_id = %s
              UNION ALL
                SELECT active.revision_id, revision.content, chain.depth
                FROM service_chain AS chain
                JOIN router.active_configurations AS active
                  ON active.scope_kind = 'service' AND active.service_id = chain.id
                JOIN router.configuration_revisions AS revision
                  ON revision.id = active.revision_id
              UNION ALL
                SELECT active.revision_id, revision.content, 1000000
                FROM router.active_configurations AS active
                JOIN router.configuration_revisions AS revision
                  ON revision.id = active.revision_id
                WHERE active.scope_kind = 'global'
            )
            SELECT route.current_revision, route.id
            FROM router.provider_model_routes AS route
            JOIN scope_revisions AS owner
              ON owner.revision_id = route.current_revision
            WHERE route.id = %s AND route.state = 'active'
              AND (route.owner_kind = 'global' OR route.owner_service_id IN (
                  SELECT id FROM service_chain
              ))
              AND (route.eligible_service_ids = '{}'::uuid[] OR EXISTS (
                  SELECT 1 FROM service_chain
                  WHERE id = ANY(route.eligible_service_ids)
              ))
              AND NOT EXISTS (
                  SELECT 1 FROM scope_revisions AS child
                  WHERE child.priority < owner.priority
                    AND child.content->'inherited_disables' @>
                        jsonb_build_array(jsonb_build_object(
                            'resource_kind', 'provider_model_route',
                            'resource_id', route.id::text
                        ))
              )
            FOR SHARE OF route
            """,
            (
                context.scope.service_id,
                context.scope.service_id,
                context.scope.workspace_id,
                request.exact_route_id,
            ),
        ).fetchone()
        if row is None or row["current_revision"] is None:
            raise AdmissionError(
                AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE, context.request_id
            )
        return row["current_revision"], None, row["id"]
    row = connection.execute(
        """
        WITH RECURSIVE service_chain AS (
            SELECT id, parent_service_id, 1 AS depth FROM router.services WHERE id = %s
          UNION ALL
            SELECT parent.id, parent.parent_service_id, child.depth + 1
            FROM router.services AS parent
            JOIN service_chain AS child ON child.parent_service_id = parent.id
        ), scope_revisions AS (
            SELECT active.revision_id, 0 AS priority
            FROM router.active_configurations AS active
            WHERE active.scope_kind = 'workspace' AND active.service_id = %s
              AND active.workspace_id = %s
          UNION ALL
            SELECT active.revision_id, chain.depth
            FROM service_chain AS chain
            JOIN router.active_configurations AS active
              ON active.scope_kind = 'service' AND active.service_id = chain.id
          UNION ALL
            SELECT active.revision_id, 1000000
            FROM router.active_configurations AS active WHERE active.scope_kind = 'global'
        )
        SELECT assignment.configuration_revision_id, assignment.id,
               assignment.state
        FROM scope_revisions AS scope
        JOIN router.assignment_definitions AS assignment
          ON assignment.configuration_revision_id = scope.revision_id
        WHERE assignment.stable_name = %s
        ORDER BY scope.priority LIMIT 1
        """,
        (
            context.scope.service_id,
            context.scope.service_id,
            context.scope.workspace_id,
            request.assignment,
        ),
    ).fetchone()
    if row is None or row["state"] != "active":
        raise AdmissionError(
            AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE, context.request_id
        )
    return row["configuration_revision_id"], row["id"], None


def _require_active_scope(connection: Connection[Any], context: RequestContext) -> None:
    service = connection.execute(
        "SELECT state FROM router.services WHERE id = %s FOR SHARE",
        (context.scope.service_id,),
    ).fetchone()
    if service is None or service["state"] != "active":
        raise AdmissionError(
            AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE, context.request_id
        )
    if context.scope.workspace_id is None:
        return
    workspace = connection.execute(
        """SELECT state FROM router.workspaces
           WHERE id = %s AND service_id = %s FOR SHARE""",
        (context.scope.workspace_id, context.scope.service_id),
    ).fetchone()
    if workspace is None or workspace["state"] != "active":
        raise AdmissionError(
            AdmissionErrorCode.WORKSPACE_UNAVAILABLE, context.request_id
        )


def _validated_attachments(
    connection: Connection[Any],
    context: RequestContext,
    request: AdmissionRequest,
    *,
    now: datetime,
) -> list[tuple[uuid.UUID, bytes, int]]:
    result: list[tuple[uuid.UUID, bytes, int]] = []
    for reference in request.fingerprint.attachments:
        row = connection.execute(
            """
            SELECT attachment.id, attachment.content_sha256, attachment.byte_length
            FROM router.attachments AS attachment
            JOIN router.attachment_status AS status
              ON status.attachment_id = attachment.id
            WHERE attachment.id = %s AND attachment.service_id = %s
              AND attachment.workspace_id IS NOT DISTINCT FROM %s
              AND attachment.content_sha256 = %s AND attachment.media_type = %s
              AND attachment.byte_length = %s AND attachment.expires_at > %s
              AND status.state = 'ready'
            FOR SHARE OF attachment, status
            """,
            (
                reference.attachment_id,
                context.scope.service_id,
                context.scope.workspace_id,
                bytes.fromhex(reference.sha256),
                reference.media_type,
                reference.byte_length,
                now,
            ),
        ).fetchone()
        if row is None:
            raise AdmissionError(
                AdmissionErrorCode.ATTACHMENT_INVALID, context.request_id
            )
        result.append(
            (
                row["id"],
                bytes(row["content_sha256"]),
                int(row["byte_length"]),
            )
        )
    return result


def _locations(
    kind: RequestKind, request_id: str, context: RequestContext
) -> tuple[str, str | None, str | None]:
    collection = {
        RequestKind.MODEL: "model-requests",
        RequestKind.SHARED_TOOL: "shared-tool-requests",
    }[kind]
    base = f"/v1/{collection}/{request_id}"
    return (
        base,
        f"{base}/cancel",
        None if kind is RequestKind.SHARED_TOOL else f"{base}/events",
    )


def _receipt(row: dict[str, Any]) -> AdmissionReceipt:
    enabled = bool(row["capture_enabled"])
    return AdmissionReceipt(
        request_id=str(row["request_id"]),
        admitted_at=row["admitted_at"],
        state=RequestState(row["state"]),
        state_revision=int(row["state_revision"]),
        status_url=row["status_location"],
        cancel_url=row["cancel_location"],
        events_url=row["events_location"],
        capture_enabled=enabled,
        capture_reason="configured" if enabled else "spool_pressure",
    )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("The admission time must include a time zone.")
