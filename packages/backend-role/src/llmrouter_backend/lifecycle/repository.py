"""Transactional PostgreSQL service and workspace lifecycle operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.types.json import Jsonb

from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    ScopeKind,
)
from llmrouter_backend.lifecycle.errors import LifecycleError, LifecycleErrorCode
from llmrouter_backend.lifecycle.model import (
    LifecycleResult,
    LifecycleState,
    ServiceAction,
    ServiceRecord,
    WorkspaceAction,
    WorkspaceRecord,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg import Connection


_MINIMUM_IDEMPOTENCY_LENGTH = 16
_MAXIMUM_IDEMPOTENCY_LENGTH = 200
_MAXIMUM_DISPLAY_NAME_LENGTH = 200
_MAXIMUM_CALLER_REFERENCE_LENGTH = 200
_MAXIMUM_REASON_LENGTH = 500


class PostgresLifecycleRepository:
    """Keep lifecycle state, receipts, and audit in one PostgreSQL transaction."""

    def __init__(
        self,
        database_url: str,
        *,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        workspace_restore_is_eligible: Callable[[str, str], bool] | None = None,
    ) -> None:
        """Use one database authority and one collision-resistant identity source."""
        if not database_url:
            msg = "The database URL must not be empty."
            raise ValueError(msg)
        self._database_url = database_url
        self._identity_factory = identity_factory
        self._workspace_restore_is_eligible = (
            workspace_restore_is_eligible
            if workspace_restore_is_eligible is not None
            else lambda _service_id, _workspace_id: True
        )

    def create_service(
        self,
        context: RequestContext,
        *,
        idempotency_key: str,
        display_name: str,
        parent_service_id: str | None = None,
    ) -> LifecycleResult[ServiceRecord]:
        """Create one active service or return its equal durable replay."""
        _require_global_change(context, "service.manage")
        parent_id = (
            None
            if parent_service_id is None
            else _parse_uuid(
                parent_service_id,
                LifecycleErrorCode.NOT_FOUND,
                request_id=context.request_id,
            )
        )
        _require_idempotency_key(idempotency_key)
        _require_bounded(display_name, _MAXIMUM_DISPLAY_NAME_LENGTH, "display name")
        fingerprint = _fingerprint(
            {
                "display_name": display_name,
                "parent_service_id": None if parent_id is None else str(parent_id),
            }
        )
        with (
            psycopg.connect(self._database_url) as connection,
            connection.transaction(),
        ):
            _lock(connection, "service-parent-tree")
            _lock(
                connection,
                f"service-operation:{context.actor_id}:{idempotency_key}",
            )
            replay = _find_service_replay(
                connection,
                actor_id=context.actor_id,
                idempotency_key=idempotency_key,
                action="service.create",
                fingerprint=fingerprint,
                request_id=context.request_id,
            )
            if replay is not None:
                return LifecycleResult(replay, replayed=True, changed=True)
            if parent_id is not None:
                _require_service_exists(
                    connection, parent_id, request_id=context.request_id
                )

            service_id = self._identity_factory()
            operation_id = self._identity_factory()
            connection.execute(
                """
                    INSERT INTO router.services (
                        id, stable_name, display_name, parent_service_id
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                (service_id, str(service_id), display_name, parent_id),
            )
            _insert_service_operation(
                connection,
                operation_id=operation_id,
                service_id=service_id,
                actor_id=context.actor_id,
                action="service.create",
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                display_name=display_name,
                state=LifecycleState.ACTIVE,
                revision=1,
                parent_service_id=None if parent_id is None else str(parent_id),
                changed=True,
            )
            _insert_audit(
                connection,
                context,
                operation_id=operation_id,
                action="service.create",
                service_id=service_id,
                workspace_id=None,
                reason=None,
            )
            record = ServiceRecord(
                service_id=str(service_id),
                display_name=display_name,
                parent_service_id=None if parent_id is None else str(parent_id),
                state=LifecycleState.ACTIVE,
                revision="1",
                operation_id=str(operation_id),
            )
        return LifecycleResult(record, replayed=False, changed=True)

    def get_service(self, context: RequestContext, service_id: str) -> ServiceRecord:
        """Read one retained service through global administration."""
        _require_global_read(context)
        parsed_service_id = _parse_uuid(
            service_id,
            LifecycleErrorCode.NOT_FOUND,
            request_id=context.request_id,
        )
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                """
                SELECT service.id::text, service.display_name,
                       service.parent_service_id::text, service.state,
                       service.state_revision::text,
                       COALESCE(operation.operation_id::text, service.id::text)
                FROM router.services AS service
                LEFT JOIN LATERAL (
                    SELECT operation_id
                    FROM router.service_lifecycle_operations
                    WHERE service_id = service.id
                    ORDER BY created_at DESC, operation_id DESC
                    LIMIT 1
                ) AS operation ON true
                WHERE service.id = %s
                """,
                (parsed_service_id,),
            ).fetchone()
        if row is None:
            raise LifecycleError(LifecycleErrorCode.NOT_FOUND, context.request_id)
        return _service_record(row)

    def change_service_state(  # noqa: PLR0913
        self,
        context: RequestContext,
        service_id: str,
        action: ServiceAction,
        *,
        expected_revision: str,
        idempotency_key: str,
        reason: str,
    ) -> LifecycleResult[ServiceRecord]:
        """Apply one exact service state transition with a stable receipt."""
        _require_global_change(context, "service.manage")
        parsed_service_id = _parse_uuid(
            service_id,
            LifecycleErrorCode.NOT_FOUND,
            request_id=context.request_id,
        )
        _require_idempotency_key(idempotency_key)
        _require_bounded(reason, _MAXIMUM_REASON_LENGTH, "reason")
        operation_action = f"service.{action.value}"
        fingerprint = _fingerprint(
            {
                "action": operation_action,
                "expected_revision": expected_revision,
                "reason": reason,
                "service_id": str(parsed_service_id),
            }
        )
        with (
            psycopg.connect(self._database_url) as connection,
            connection.transaction(),
        ):
            _lock(connection, "service-parent-tree")
            _lock(
                connection,
                f"service-operation:{context.actor_id}:{idempotency_key}",
            )
            replay = _find_service_replay(
                connection,
                actor_id=context.actor_id,
                idempotency_key=idempotency_key,
                action=operation_action,
                fingerprint=fingerprint,
                request_id=context.request_id,
            )
            if replay is not None:
                changed_row = connection.execute(
                    """
                        SELECT changed
                        FROM router.service_lifecycle_operations
                        WHERE operation_id = %s
                        """,
                    (replay.operation_id,),
                ).fetchone()
                changed = changed_row is not None and bool(changed_row[0])
                return LifecycleResult(replay, replayed=True, changed=changed)

            row = connection.execute(
                """
                    SELECT id, display_name, parent_service_id::text, state,
                           state_revision
                    FROM router.services
                    WHERE id = %s
                    FOR UPDATE
                    """,
                (parsed_service_id,),
            ).fetchone()
            if row is None:
                raise LifecycleError(LifecycleErrorCode.NOT_FOUND, context.request_id)
            state = LifecycleState(row[3])
            revision = int(row[4])
            if state is LifecycleState.RETIRED:
                raise LifecycleError(
                    LifecycleErrorCode.TERMINAL_STATE,
                    context.request_id,
                    current_state=state,
                    current_revision=str(revision),
                )
            _require_revision(
                expected_revision, state, revision, request_id=context.request_id
            )
            changed, next_state = _service_transition(state, action)
            next_revision = revision + 1 if changed else revision
            operation_id = self._identity_factory()
            if changed:
                connection.execute(
                    """
                        UPDATE router.services
                        SET state = %s, state_revision = %s,
                            retired_at = CASE WHEN %s = 'retired'
                                THEN transaction_timestamp() ELSE NULL END
                        WHERE id = %s
                        """,
                    (
                        next_state.value,
                        next_revision,
                        next_state.value,
                        parsed_service_id,
                    ),
                )
            _insert_service_operation(
                connection,
                operation_id=operation_id,
                service_id=parsed_service_id,
                actor_id=context.actor_id,
                action=operation_action,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                display_name=str(row[1]),
                state=next_state,
                revision=next_revision,
                parent_service_id=row[2],
                changed=changed,
            )
            _insert_audit(
                connection,
                context,
                operation_id=operation_id,
                action=operation_action,
                service_id=parsed_service_id,
                workspace_id=None,
                reason=reason,
            )
            record = ServiceRecord(
                service_id=str(row[0]),
                display_name=str(row[1]),
                parent_service_id=row[2],
                state=next_state,
                revision=str(next_revision),
                operation_id=str(operation_id),
            )
        return LifecycleResult(record, replayed=False, changed=changed)

    def change_service_parent(
        self,
        context: RequestContext,
        service_id: str,
        *,
        expected_revision: str,
        new_parent_service_id: str | None,
        reason: str,
    ) -> LifecycleResult[ServiceRecord]:
        """Replace one parent link after a serialized cycle check."""
        _require_global_change(context, "service_parent.manage")
        parsed_service_id = _parse_uuid(
            service_id,
            LifecycleErrorCode.NOT_FOUND,
            request_id=context.request_id,
        )
        parsed_parent_id = (
            None
            if new_parent_service_id is None
            else _parse_uuid(
                new_parent_service_id,
                LifecycleErrorCode.NOT_FOUND,
                request_id=context.request_id,
            )
        )
        _require_bounded(reason, _MAXIMUM_REASON_LENGTH, "reason")
        fingerprint = _fingerprint(
            {
                "expected_revision": expected_revision,
                "new_parent_service_id": (
                    None if parsed_parent_id is None else str(parsed_parent_id)
                ),
                "reason": reason,
                "service_id": str(parsed_service_id),
            }
        )
        with (
            psycopg.connect(self._database_url) as connection,
            connection.transaction(),
        ):
            _lock(connection, "service-parent-tree")
            row = connection.execute(
                """
                    SELECT id, display_name, parent_service_id::text, state,
                           state_revision
                    FROM router.services
                    WHERE id = %s
                    FOR UPDATE
                    """,
                (parsed_service_id,),
            ).fetchone()
            if row is None:
                raise LifecycleError(LifecycleErrorCode.NOT_FOUND, context.request_id)
            state = LifecycleState(row[3])
            revision = int(row[4])
            if state is LifecycleState.RETIRED:
                raise LifecycleError(
                    LifecycleErrorCode.TERMINAL_STATE,
                    context.request_id,
                    current_state=state,
                    current_revision=str(revision),
                )
            _require_revision(
                expected_revision, state, revision, request_id=context.request_id
            )
            if parsed_parent_id is not None:
                _require_service_exists(
                    connection,
                    parsed_parent_id,
                    request_id=context.request_id,
                )
                cycle = connection.execute(
                    """
                        WITH RECURSIVE descendants AS (
                            SELECT id FROM router.services WHERE id = %s
                          UNION ALL
                            SELECT child.id
                            FROM router.services AS child
                            JOIN descendants AS parent
                              ON child.parent_service_id = parent.id
                        )
                        SELECT EXISTS (
                            SELECT 1 FROM descendants WHERE id = %s
                        )
                        """,
                    (parsed_service_id, parsed_parent_id),
                ).fetchone()
                if cycle is not None and cycle[0]:
                    raise LifecycleError(
                        LifecycleErrorCode.INVALID_REQUEST, context.request_id
                    )
            next_parent = None if parsed_parent_id is None else str(parsed_parent_id)
            changed = row[2] != next_parent
            next_revision = revision + 1 if changed else revision
            operation_id = self._identity_factory()
            if changed:
                connection.execute(
                    """
                        UPDATE router.services
                        SET parent_service_id = %s, state_revision = %s
                        WHERE id = %s
                        """,
                    (parsed_parent_id, next_revision, parsed_service_id),
                )
            _insert_service_operation(
                connection,
                operation_id=operation_id,
                service_id=parsed_service_id,
                actor_id=context.actor_id,
                action="service.parent",
                idempotency_key=None,
                fingerprint=fingerprint,
                display_name=str(row[1]),
                state=state,
                revision=next_revision,
                parent_service_id=next_parent,
                changed=changed,
            )
            _insert_audit(
                connection,
                context,
                operation_id=operation_id,
                action="service.parent",
                service_id=parsed_service_id,
                workspace_id=None,
                reason=reason,
            )
            record = ServiceRecord(
                service_id=str(row[0]),
                display_name=str(row[1]),
                parent_service_id=next_parent,
                state=state,
                revision=str(next_revision),
                operation_id=str(operation_id),
            )
        return LifecycleResult(record, replayed=False, changed=changed)

    def create_workspace(
        self,
        context: RequestContext,
        *,
        idempotency_key: str,
        caller_reference: str,
        display_name: str,
    ) -> LifecycleResult[WorkspaceRecord]:
        """Create one active workspace with two conflict-bound identities."""
        service_id = _require_workspace_context(context, "workspace.create", None)
        parsed_service_id = _parse_uuid(
            service_id,
            LifecycleErrorCode.WORKSPACE_NOT_FOUND,
            request_id=context.request_id,
        )
        _require_idempotency_key(idempotency_key)
        _require_bounded(
            caller_reference,
            _MAXIMUM_CALLER_REFERENCE_LENGTH,
            "caller reference",
        )
        _require_bounded(display_name, _MAXIMUM_DISPLAY_NAME_LENGTH, "display name")
        fingerprint = _fingerprint(
            {"caller_reference": caller_reference, "display_name": display_name}
        )
        with (
            psycopg.connect(self._database_url) as connection,
            connection.transaction(),
        ):
            _lock_shared(connection, "service-parent-tree")
            lock_names = sorted(
                (
                    f"workspace-key:{service_id}:{idempotency_key}",
                    f"workspace-reference:{service_id}:{caller_reference}",
                )
            )
            for lock_name in lock_names:
                _lock(connection, lock_name)
            replay = _find_workspace_create_replay(
                connection,
                service_id=str(parsed_service_id),
                idempotency_key=idempotency_key,
                caller_reference=caller_reference,
                fingerprint=fingerprint,
                request_id=context.request_id,
            )
            if replay is not None:
                return LifecycleResult(replay, replayed=True, changed=True)
            if not _service_tree_is_active(connection, parsed_service_id):
                raise LifecycleError(
                    LifecycleErrorCode.WORKSPACE_UNAVAILABLE, context.request_id
                )
            workspace_id = self._identity_factory()
            operation_id = self._identity_factory()
            connection.execute(
                """
                    INSERT INTO router.workspaces (
                        id, service_id, caller_reference, creation_idempotency_key,
                        creation_fingerprint, display_name
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                (
                    workspace_id,
                    parsed_service_id,
                    caller_reference,
                    idempotency_key,
                    fingerprint,
                    display_name,
                ),
            )
            _insert_workspace_operation(
                connection,
                operation_id=operation_id,
                service_id=str(parsed_service_id),
                workspace_id=workspace_id,
                actor_id=context.actor_id,
                action="workspace.create",
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                caller_reference=caller_reference,
                display_name=display_name,
                state=LifecycleState.ACTIVE,
                revision=1,
                changed=True,
            )
            _insert_audit(
                connection,
                context,
                operation_id=operation_id,
                action="workspace.create",
                service_id=parsed_service_id,
                workspace_id=workspace_id,
                reason=None,
            )
            record = WorkspaceRecord(
                workspace_id=str(workspace_id),
                caller_reference=caller_reference,
                display_name=display_name,
                state=LifecycleState.ACTIVE,
                state_revision="1",
                operation_id=str(operation_id),
            )
        return LifecycleResult(record, replayed=False, changed=True)

    def get_workspace(
        self,
        context: RequestContext,
        workspace_id: str,
    ) -> WorkspaceRecord:
        """Read a workspace only through its owning service scope."""
        service_id = _require_workspace_context(context, "workspace.read", workspace_id)
        parsed_service_id = _parse_uuid(
            service_id,
            LifecycleErrorCode.WORKSPACE_NOT_FOUND,
            request_id=context.request_id,
        )
        parsed_workspace_id = _parse_uuid(
            workspace_id,
            LifecycleErrorCode.WORKSPACE_NOT_FOUND,
            request_id=context.request_id,
        )
        with psycopg.connect(self._database_url) as connection:
            row = _select_workspace(connection, parsed_service_id, parsed_workspace_id)
        if row is None:
            raise LifecycleError(
                LifecycleErrorCode.WORKSPACE_NOT_FOUND, context.request_id
            )
        return _workspace_record(row)

    def change_workspace_state(  # noqa: PLR0913
        self,
        context: RequestContext,
        workspace_id: str,
        action: WorkspaceAction,
        *,
        expected_revision: str,
        idempotency_key: str,
        reason: str,
    ) -> LifecycleResult[WorkspaceRecord]:
        """Apply one exact workspace transition or stable equal replay."""
        service_id = _require_workspace_context(
            context, f"workspace.{action.value}", workspace_id
        )
        parsed_service_id = _parse_uuid(
            service_id,
            LifecycleErrorCode.WORKSPACE_NOT_FOUND,
            request_id=context.request_id,
        )
        parsed_workspace_id = _parse_uuid(
            workspace_id,
            LifecycleErrorCode.WORKSPACE_NOT_FOUND,
            request_id=context.request_id,
        )
        _require_idempotency_key(idempotency_key)
        _require_bounded(reason, _MAXIMUM_REASON_LENGTH, "reason")
        operation_action = f"workspace.{action.value}"
        fingerprint = _fingerprint(
            {
                "action": operation_action,
                "expected_revision": expected_revision,
                "reason": reason,
                "workspace_id": str(parsed_workspace_id),
            }
        )
        with (
            psycopg.connect(self._database_url) as connection,
            connection.transaction(),
        ):
            _lock_shared(connection, "service-parent-tree")
            _lock(connection, f"workspace-key:{service_id}:{idempotency_key}")
            replay = _find_workspace_replay(
                connection,
                service_id=service_id,
                idempotency_key=idempotency_key,
                action=operation_action,
                fingerprint=fingerprint,
                request_id=context.request_id,
            )
            if replay is not None:
                changed_row = connection.execute(
                    """
                        SELECT changed
                        FROM router.workspace_lifecycle_operations
                        WHERE operation_id = %s
                        """,
                    (replay.operation_id,),
                ).fetchone()
                return LifecycleResult(
                    replay,
                    replayed=True,
                    changed=changed_row is not None and bool(changed_row[0]),
                )
            row = connection.execute(
                """
                    SELECT id::text, caller_reference, display_name, state,
                           state_revision
                    FROM router.workspaces
                    WHERE id = %s AND service_id = %s
                    FOR UPDATE
                    """,
                (parsed_workspace_id, parsed_service_id),
            ).fetchone()
            if row is None:
                raise LifecycleError(
                    LifecycleErrorCode.WORKSPACE_NOT_FOUND, context.request_id
                )
            state = LifecycleState(row[3])
            revision = int(row[4])
            if state is LifecycleState.RETIRED:
                raise LifecycleError(
                    LifecycleErrorCode.WORKSPACE_RETIRED,
                    context.request_id,
                    current_state=state,
                    current_revision=str(revision),
                )
            _require_revision(
                expected_revision, state, revision, request_id=context.request_id
            )
            changed, next_state = _workspace_transition(state, action)
            if (
                action is WorkspaceAction.RESTORE
                and changed
                and (
                    not _service_tree_is_active(connection, parsed_service_id)
                    or not self._workspace_restore_is_eligible(
                        str(parsed_service_id), str(parsed_workspace_id)
                    )
                )
            ):
                raise LifecycleError(
                    LifecycleErrorCode.WORKSPACE_UNAVAILABLE,
                    context.request_id,
                )
            next_revision = revision + 1 if changed else revision
            operation_id = self._identity_factory()
            if changed:
                connection.execute(
                    """
                        UPDATE router.workspaces
                        SET state = %s, state_revision = %s,
                            retired_at = CASE WHEN %s = 'retired'
                                THEN transaction_timestamp() ELSE NULL END
                        WHERE id = %s AND service_id = %s
                        """,
                    (
                        next_state.value,
                        next_revision,
                        next_state.value,
                        parsed_workspace_id,
                        parsed_service_id,
                    ),
                )
            _insert_workspace_operation(
                connection,
                operation_id=operation_id,
                service_id=str(parsed_service_id),
                workspace_id=parsed_workspace_id,
                actor_id=context.actor_id,
                action=operation_action,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                caller_reference=str(row[1]),
                display_name=str(row[2]),
                state=next_state,
                revision=next_revision,
                changed=changed,
            )
            _insert_audit(
                connection,
                context,
                operation_id=operation_id,
                action=operation_action,
                service_id=parsed_service_id,
                workspace_id=parsed_workspace_id,
                reason=reason,
            )
            record = WorkspaceRecord(
                workspace_id=str(row[0]),
                caller_reference=str(row[1]),
                display_name=str(row[2]),
                state=next_state,
                state_revision=str(next_revision),
                operation_id=str(operation_id),
            )
        return LifecycleResult(record, replayed=False, changed=changed)

    def admission_is_allowed(
        self,
        service_id: str,
        workspace_id: str | None = None,
    ) -> bool:
        """Report whether all ancestors and the optional workspace are active."""
        try:
            parsed_service_id = uuid.UUID(service_id)
            parsed_workspace_id = (
                None if workspace_id is None else uuid.UUID(workspace_id)
            )
        except (TypeError, ValueError, AttributeError):
            return False
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                """
                SELECT router.lifecycle_admission_is_allowed(%s, %s)
                """,
                (parsed_service_id, parsed_workspace_id),
            ).fetchone()
        return row is not None and bool(row[0])


def _require_global_change(context: RequestContext, operation: str) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.scope.kind is ScopeKind.GLOBAL
        and context.operation == operation
        and context.mutation
    ):
        raise LifecycleError(LifecycleErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_global_read(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.scope.kind is ScopeKind.GLOBAL
        and context.operation == "service.manage"
    ):
        raise LifecycleError(LifecycleErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_workspace_context(
    context: RequestContext,
    operation: str,
    workspace_id: str | None,
) -> str:
    service_id = context.scope.service_id
    correct_scope = (
        context.scope.kind is ScopeKind.SERVICE
        if workspace_id is None
        else context.scope.kind is ScopeKind.WORKSPACE
        and context.scope.workspace_id == workspace_id
    )
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.SERVICE_MANAGEMENT
        and service_id is not None
        and context.actor_id == service_id
        and context.operation == operation
        and correct_scope
        and (operation.endswith(".read") or context.mutation)
    ):
        raise LifecycleError(LifecycleErrorCode.INSUFFICIENT_SCOPE, context.request_id)
    return service_id


def _require_idempotency_key(value: str) -> None:
    if not _MINIMUM_IDEMPOTENCY_LENGTH <= len(value) <= _MAXIMUM_IDEMPOTENCY_LENGTH:
        msg = "The idempotency key must contain 16 to 200 characters."
        raise ValueError(msg)


def _parse_uuid(
    value: str,
    code: LifecycleErrorCode,
    *,
    request_id: str,
) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise LifecycleError(code, request_id) from error


def _require_bounded(value: str, limit: int, label: str) -> None:
    if not 1 <= len(value) <= limit:
        msg = f"The {label} must contain 1 to {limit} characters."
        raise ValueError(msg)


def _fingerprint(value: dict[str, object]) -> bytes:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).digest()


def _lock(connection: Connection[Any], name: str) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))


def _lock_shared(connection: Connection[Any], name: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))", (name,)
    )


def _require_service_exists(
    connection: Connection[Any], service_id: uuid.UUID, *, request_id: str
) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM router.services WHERE id = %s", (service_id,)
        ).fetchone()
        is None
    ):
        raise LifecycleError(LifecycleErrorCode.NOT_FOUND, request_id)


def _require_revision(
    expected: str,
    state: LifecycleState,
    current: int,
    *,
    request_id: str,
) -> None:
    if expected != str(current):
        raise LifecycleError(
            LifecycleErrorCode.STATE_REVISION_CONFLICT,
            request_id,
            current_state=state,
            current_revision=str(current),
        )


def _service_transition(
    state: LifecycleState, action: ServiceAction
) -> tuple[bool, LifecycleState]:
    desired = {
        ServiceAction.DISABLE: LifecycleState.DISABLED,
        ServiceAction.RESTORE: LifecycleState.ACTIVE,
        ServiceAction.RETIRE: LifecycleState.RETIRED,
    }[action]
    if action is ServiceAction.RESTORE and state is not LifecycleState.DISABLED:
        return False, state
    return state is not desired, desired


def _workspace_transition(
    state: LifecycleState, action: WorkspaceAction
) -> tuple[bool, LifecycleState]:
    desired = {
        WorkspaceAction.DISABLE: LifecycleState.DISABLED,
        WorkspaceAction.RESTORE: LifecycleState.ACTIVE,
        WorkspaceAction.RETIRE: LifecycleState.RETIRED,
    }[action]
    if action is WorkspaceAction.RESTORE and state is not LifecycleState.DISABLED:
        return False, state
    return state is not desired, desired


def _service_tree_is_active(connection: Connection[Any], service_id: uuid.UUID) -> bool:
    row = connection.execute(
        """
        WITH RECURSIVE ancestors AS (
            SELECT id, parent_service_id, state
            FROM router.services
            WHERE id = %s
          UNION ALL
            SELECT parent.id, parent.parent_service_id, parent.state
            FROM router.services AS parent
            JOIN ancestors AS child ON parent.id = child.parent_service_id
        )
        SELECT count(*) > 0 AND bool_and(state = 'active')
        FROM ancestors
        """,
        (service_id,),
    ).fetchone()
    return row is not None and bool(row[0])


def _find_service_replay(  # noqa: PLR0913
    connection: Connection[Any],
    *,
    actor_id: str,
    idempotency_key: str,
    action: str,
    fingerprint: bytes,
    request_id: str,
) -> ServiceRecord | None:
    row = connection.execute(
        """
        SELECT operation.action, operation.request_fingerprint,
               service.id::text, operation.resulting_display_name,
               operation.resulting_parent_service_id::text,
               operation.resulting_state, operation.resulting_revision::text,
               operation.operation_id::text
        FROM router.service_lifecycle_operations AS operation
        JOIN router.services AS service ON service.id = operation.service_id
        WHERE operation.actor_id = %s AND operation.idempotency_key = %s
        """,
        (actor_id, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if row[0] != action or bytes(row[1]) != fingerprint:
        raise LifecycleError(LifecycleErrorCode.IDEMPOTENCY_CONFLICT, request_id)
    return ServiceRecord(
        service_id=row[2],
        display_name=row[3],
        parent_service_id=row[4],
        state=LifecycleState(row[5]),
        revision=row[6],
        operation_id=row[7],
    )


def _find_workspace_create_replay(  # noqa: PLR0913
    connection: Connection[Any],
    *,
    service_id: str,
    idempotency_key: str,
    caller_reference: str,
    fingerprint: bytes,
    request_id: str,
) -> WorkspaceRecord | None:
    binding = connection.execute(
        """
        SELECT binding.request_fingerprint, operation.operation_id::text,
               workspace.id::text, operation.resulting_caller_reference,
               operation.resulting_display_name, operation.resulting_state,
               operation.resulting_revision::text
        FROM router.workspace_lifecycle_idempotency_bindings AS binding
        JOIN router.workspace_lifecycle_operations AS operation
          ON operation.operation_id = binding.operation_id
        JOIN router.workspaces AS workspace
          ON workspace.id = operation.workspace_id
         AND workspace.service_id = operation.service_id
        WHERE binding.service_id = %s AND binding.idempotency_key = %s
        """,
        (service_id, idempotency_key),
    ).fetchone()
    by_reference = connection.execute(
        """
        SELECT id::text, creation_idempotency_key
        FROM router.workspaces
        WHERE service_id = %s AND caller_reference = %s
        """,
        (service_id, caller_reference),
    ).fetchone()
    if binding is not None:
        if bytes(binding[0]) != fingerprint:
            raise LifecycleError(LifecycleErrorCode.IDEMPOTENCY_CONFLICT, request_id)
        return WorkspaceRecord(
            workspace_id=binding[2],
            caller_reference=binding[3],
            display_name=binding[4],
            state=LifecycleState(binding[5]),
            state_revision=binding[6],
            operation_id=binding[1],
        )
    if by_reference is None:
        return None
    stored = connection.execute(
        """
        SELECT operation.operation_id::text, operation.request_fingerprint,
               workspace.id::text, operation.resulting_caller_reference,
               operation.resulting_display_name, operation.resulting_state,
               operation.resulting_revision::text
        FROM router.workspace_lifecycle_operations AS operation
        JOIN router.workspaces AS workspace
          ON workspace.id = operation.workspace_id
         AND workspace.service_id = operation.service_id
        WHERE operation.service_id = %s
          AND operation.workspace_id = %s
          AND operation.action = 'workspace.create'
        """,
        (service_id, by_reference[0]),
    ).fetchone()
    if stored is None or bytes(stored[1]) != fingerprint:
        raise LifecycleError(LifecycleErrorCode.IDEMPOTENCY_CONFLICT, request_id)
    connection.execute(
        """
        INSERT INTO router.workspace_lifecycle_idempotency_bindings (
            service_id, idempotency_key, request_fingerprint, operation_id
        ) VALUES (%s, %s, %s, %s)
        """,
        (service_id, idempotency_key, fingerprint, stored[0]),
    )
    return WorkspaceRecord(
        workspace_id=stored[2],
        caller_reference=stored[3],
        display_name=stored[4],
        state=LifecycleState(stored[5]),
        state_revision=stored[6],
        operation_id=stored[0],
    )


def _find_workspace_replay(  # noqa: PLR0913
    connection: Connection[Any],
    *,
    service_id: str,
    idempotency_key: str,
    action: str,
    fingerprint: bytes,
    request_id: str,
) -> WorkspaceRecord | None:
    row = connection.execute(
        """
        SELECT operation.action, operation.request_fingerprint,
               workspace.id::text, operation.resulting_caller_reference,
               operation.resulting_display_name, operation.resulting_state,
               operation.resulting_revision::text, operation.operation_id::text
        FROM router.workspace_lifecycle_idempotency_bindings AS binding
        JOIN router.workspace_lifecycle_operations AS operation
          ON operation.operation_id = binding.operation_id
        JOIN router.workspaces AS workspace
          ON workspace.id = operation.workspace_id
         AND workspace.service_id = operation.service_id
        WHERE binding.service_id = %s AND binding.idempotency_key = %s
        """,
        (service_id, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if row[0] != action or bytes(row[1]) != fingerprint:
        raise LifecycleError(LifecycleErrorCode.IDEMPOTENCY_CONFLICT, request_id)
    return WorkspaceRecord(
        workspace_id=row[2],
        caller_reference=row[3],
        display_name=row[4],
        state=LifecycleState(row[5]),
        state_revision=row[6],
        operation_id=row[7],
    )


def _insert_service_operation(  # noqa: PLR0913
    connection: Connection[Any],
    *,
    operation_id: uuid.UUID,
    service_id: uuid.UUID | str,
    actor_id: str,
    action: str,
    idempotency_key: str | None,
    fingerprint: bytes,
    display_name: str,
    state: LifecycleState,
    revision: int,
    parent_service_id: str | None,
    changed: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO router.service_lifecycle_operations (
            operation_id, service_id, actor_id, action, idempotency_key,
            request_fingerprint, resulting_display_name, resulting_state,
            resulting_revision,
            resulting_parent_service_id, changed, audit_event_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            operation_id,
            service_id,
            actor_id,
            action,
            idempotency_key,
            fingerprint,
            display_name,
            state.value,
            revision,
            parent_service_id,
            changed,
            operation_id,
        ),
    )


def _insert_workspace_operation(  # noqa: PLR0913
    connection: Connection[Any],
    *,
    operation_id: uuid.UUID,
    service_id: str,
    workspace_id: uuid.UUID | str,
    actor_id: str,
    action: str,
    idempotency_key: str,
    fingerprint: bytes,
    caller_reference: str,
    display_name: str,
    state: LifecycleState,
    revision: int,
    changed: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO router.workspace_lifecycle_operations (
            operation_id, service_id, workspace_id, actor_id, action,
            idempotency_key, request_fingerprint, resulting_caller_reference,
            resulting_display_name, resulting_state, resulting_revision,
            changed, audit_event_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            operation_id,
            service_id,
            workspace_id,
            actor_id,
            action,
            idempotency_key,
            fingerprint,
            caller_reference,
            display_name,
            state.value,
            revision,
            changed,
            operation_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO router.workspace_lifecycle_idempotency_bindings (
            service_id, idempotency_key, request_fingerprint, operation_id
        ) VALUES (%s, %s, %s, %s)
        """,
        (service_id, idempotency_key, fingerprint, operation_id),
    )


def _insert_audit(  # noqa: PLR0913
    connection: Connection[Any],
    context: RequestContext,
    *,
    operation_id: uuid.UUID,
    action: str,
    service_id: uuid.UUID | str,
    workspace_id: uuid.UUID | str | None,
    reason: str | None,
) -> None:
    detail: dict[str, str] = {
        "resource_type": "workspace" if workspace_id is not None else "service",
        "resource_id": str(workspace_id if workspace_id is not None else service_id),
    }
    if reason is not None:
        detail["reason"] = reason
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            service_id, workspace_id, action, permission_result, safe_details,
            occurred_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, 'permitted', %s, %s
        )
        """,
        (
            operation_id,
            (
                "global_administration"
                if context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
                else "security"
            ),
            context.actor_kind.value,
            context.actor_id,
            context.authority_class.value,
            service_id,
            workspace_id,
            action,
            Jsonb(detail),
            context.authorized_at,
        ),
    )


def _select_workspace(
    connection: Connection[Any], service_id: uuid.UUID, workspace_id: uuid.UUID
) -> tuple[Any, ...] | None:
    return connection.execute(
        """
        SELECT workspace.id::text, workspace.caller_reference,
               workspace.display_name, workspace.state,
               workspace.state_revision::text,
               COALESCE(operation.operation_id::text, workspace.id::text)
        FROM router.workspaces AS workspace
        LEFT JOIN LATERAL (
            SELECT operation_id
            FROM router.workspace_lifecycle_operations
            WHERE service_id = workspace.service_id
              AND workspace_id = workspace.id
            ORDER BY created_at DESC, operation_id DESC
            LIMIT 1
        ) AS operation ON true
        WHERE workspace.id = %s AND workspace.service_id = %s
        """,
        (workspace_id, service_id),
    ).fetchone()


def _service_record(row: tuple[Any, ...]) -> ServiceRecord:
    return ServiceRecord(
        service_id=str(row[0]),
        display_name=str(row[1]),
        parent_service_id=None if row[2] is None else str(row[2]),
        state=LifecycleState(row[3]),
        revision=str(row[4]),
        operation_id=str(row[5]),
    )


def _workspace_record(row: tuple[Any, ...]) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=str(row[0]),
        caller_reference=str(row[1]),
        display_name=str(row[2]),
        state=LifecycleState(row[3]),
        state_revision=str(row[4]),
        operation_id=str(row[5]),
    )
