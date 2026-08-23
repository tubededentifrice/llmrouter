"""Transactional PostgreSQL access for identity and ownership records."""
# ruff: noqa: EM101, PLR0913, TRY003

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from llmrouter_backend.errors import authentication_required, invalid_request, not_found
from llmrouter_backend.security import ControlKeys, create_service_key, service_key_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg import Connection


@dataclass(frozen=True, slots=True)
class ServiceActor:
    """Authority from one valid direct service key."""

    service_id: uuid.UUID
    service_api_name: str
    key_id: uuid.UUID

    @property
    def activity_subject(self) -> str:
        """Return a safe service actor identity."""
        return f"service:{self.service_api_name}:key:{self.key_id}"


@dataclass(frozen=True, slots=True)
class AdministratorActor:
    """Authority from one unexpired allowlisted local session."""

    session_verifier: bytes
    issuer: str
    subject: str
    display_name: str
    expires_at: datetime
    csrf_token: str
    csrf_verifier: bytes

    @property
    def activity_subject(self) -> str:
        """Bind administrator authority to the immutable issuer and subject."""
        authority = f"{self.issuer}\0{self.subject}".encode()
        return f"oidc:{hashlib.sha256(authority).hexdigest()}"


def service_by_api_name(
    connection: Connection[Any], api_name: str
) -> dict[str, Any] | None:
    """Read one current service with its readable parent identity."""
    return connection.execute(
        """SELECT service.id, service.api_name, service.display_name,
                  parent.api_name AS parent_service_api_name, service.created_at
           FROM router.services AS service
           LEFT JOIN router.services AS parent ON parent.id = service.parent_service_id
           WHERE service.api_name = %s""",
        (api_name,),
    ).fetchone()


def create_service(
    connection: Connection[Any],
    *,
    api_name: str,
    display_name: str,
    parent_api_name: str | None,
    actor: AdministratorActor,
) -> dict[str, Any]:
    """Create one service and record the configuration result atomically."""
    parent_id = _service_id(connection, parent_api_name) if parent_api_name else None
    row = connection.execute(
        """INSERT INTO router.services (api_name, display_name, parent_service_id)
           VALUES (%s, %s, %s)
           RETURNING id, api_name, display_name, created_at""",
        (api_name, display_name, parent_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("The service insert did not return its row.")
    row["parent_service_api_name"] = parent_api_name
    record_activity(
        connection,
        actor.activity_subject,
        "service.create",
        "service",
        resource_api_name=api_name,
        resource_id=row["id"],
    )
    return cast("dict[str, Any]", row)


def update_service(
    connection: Connection[Any],
    *,
    api_name: str,
    display_name: str,
    parent_api_name: str | None,
    actor: AdministratorActor,
    validate_dependents: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Replace service fields and validate dependent assignment graphs."""
    try:
        parent_id = (
            _service_id(connection, parent_api_name) if parent_api_name else None
        )
        row = connection.execute(
            """UPDATE router.services
               SET display_name = %s, parent_service_id = %s
               WHERE api_name = %s
               RETURNING id, api_name, display_name, created_at""",
            (display_name, parent_id, api_name),
        ).fetchone()
        if row is None:
            raise not_found("service")  # noqa: TRY301
        if validate_dependents is not None:
            validate_dependents()
        row["parent_service_api_name"] = parent_api_name
        record_activity(
            connection,
            actor.activity_subject,
            "service.update",
            "service",
            resource_api_name=api_name,
            resource_id=row["id"],
        )
    except Exception:
        connection.rollback()
        current = connection.execute(
            "SELECT id FROM router.services WHERE api_name = %s", (api_name,)
        ).fetchone()
        record_activity(
            connection,
            actor.activity_subject,
            "service.update",
            "service",
            resource_api_name=api_name,
            resource_id=current["id"] if current is not None else None,
            result="failed",
        )
        connection.commit()
        raise
    return cast("dict[str, Any]", row)


def delete_service(
    connection: Connection[Any], *, api_name: str, actor: AdministratorActor
) -> None:
    """Delete one childless service and all of its owned records."""
    deleted = connection.execute(
        "DELETE FROM router.services WHERE api_name = %s RETURNING id", (api_name,)
    ).fetchone()
    if deleted is None:
        raise not_found("service")
    record_activity(
        connection,
        actor.activity_subject,
        "service.delete",
        "service",
        resource_api_name=api_name,
        resource_id=deleted["id"],
    )


def list_services(
    connection: Connection[Any], *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Read one stable api-name-ordered service page."""
    rows = connection.execute(
        """SELECT service.api_name, service.display_name,
                  parent.api_name AS parent_service_api_name, service.created_at
           FROM router.services AS service
           LEFT JOIN router.services AS parent ON parent.id = service.parent_service_id
           WHERE (%s::text IS NULL OR service.api_name > %s)
           ORDER BY service.api_name
           LIMIT %s""",
        (cursor, cursor, limit + 1),
    ).fetchall()
    return _page(rows, limit, "api_name")


def workspace_by_api_name(
    connection: Connection[Any], service_id: uuid.UUID, api_name: str
) -> dict[str, Any] | None:
    """Read one workspace only through its owning service."""
    return connection.execute(
        """SELECT api_name, display_name, created_at
           FROM router.workspaces
           WHERE service_id = %s AND api_name = %s""",
        (service_id, api_name),
    ).fetchone()


def create_workspace(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    api_name: str,
    display_name: str,
    actor_subject: str,
) -> dict[str, Any]:
    """Create a workspace within one exact service boundary."""
    row = connection.execute(
        """INSERT INTO router.workspaces (service_id, api_name, display_name)
           VALUES (%s, %s, %s)
           RETURNING id, api_name, display_name, created_at""",
        (service_id, api_name, display_name),
    ).fetchone()
    if row is None:
        raise RuntimeError("The workspace insert did not return its row.")
    service_name = _service_api_name(connection, service_id)
    record_activity(
        connection,
        actor_subject,
        "workspace.create",
        "workspace",
        service_api_name=service_name,
        resource_api_name=api_name,
        resource_id=row["id"],
    )
    del row["id"]
    return cast("dict[str, Any]", row)


def delete_workspace(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    api_name: str,
    actor_subject: str,
) -> None:
    """Delete one exact service-owned workspace and all dependent records."""
    service_name = _service_api_name(connection, service_id)
    deleted = connection.execute(
        """DELETE FROM router.workspaces
           WHERE service_id = %s AND api_name = %s RETURNING id""",
        (service_id, api_name),
    ).fetchone()
    if deleted is None:
        raise not_found("workspace")
    record_activity(
        connection,
        actor_subject,
        "workspace.delete",
        "workspace",
        service_api_name=service_name,
        resource_api_name=api_name,
        resource_id=deleted["id"],
    )


def list_workspaces(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read workspaces only within one exact service boundary."""
    rows = connection.execute(
        """SELECT api_name, display_name, created_at
           FROM router.workspaces
           WHERE service_id = %s AND (%s::text IS NULL OR api_name > %s)
           ORDER BY api_name
           LIMIT %s""",
        (service_id, cursor, cursor, limit + 1),
    ).fetchall()
    return _page(rows, limit, "api_name")


def create_key(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    name: str,
    actor_subject: str,
    control_keys: ControlKeys,
) -> tuple[dict[str, Any], str]:
    """Create one key, store only its verifier, and return the secret once."""
    key_id = uuid.uuid4()
    secret = create_service_key(str(key_id))
    row = connection.execute(
        """INSERT INTO router.service_api_keys (id, service_id, name, verifier)
           VALUES (%s, %s, %s, %s)
           RETURNING id, name, created_at, last_used_at""",
        (key_id, service_id, name, control_keys.verifier(secret)),
    ).fetchone()
    if row is None:
        raise RuntimeError("The service-key insert did not return its row.")
    row["id"] = str(row["id"])
    service_name = _service_api_name(connection, service_id)
    record_activity(
        connection,
        actor_subject,
        "service_key.create",
        "service_key",
        service_api_name=service_name,
        resource_id=key_id,
    )
    return row, secret


def revoke_key(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    key_id: uuid.UUID,
    actor_subject: str,
) -> None:
    """Remove one exact key so it cannot authenticate another request."""
    deleted = connection.execute(
        """DELETE FROM router.service_api_keys
           WHERE service_id = %s AND id = %s RETURNING id""",
        (service_id, key_id),
    ).fetchone()
    if deleted is None:
        raise not_found("service key")
    service_name = _service_api_name(connection, service_id)
    record_activity(
        connection,
        actor_subject,
        "service_key.revoke",
        "service_key",
        service_api_name=service_name,
        resource_id=key_id,
    )


def list_keys(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """List verifier-free key records for one service."""
    cursor_id = _uuid_cursor(cursor)
    rows = connection.execute(
        """SELECT id, name, created_at, last_used_at
           FROM router.service_api_keys
           WHERE service_id = %s AND (%s::uuid IS NULL OR id > %s)
           ORDER BY id
           LIMIT %s""",
        (service_id, cursor_id, cursor_id, limit + 1),
    ).fetchall()
    for row in rows:
        row["id"] = str(row["id"])
    return _page(rows, limit, "id")


def authenticate_service_key(
    connection: Connection[Any], bearer: str, control_keys: ControlKeys
) -> ServiceActor:
    """Authenticate one direct key and update its use evidence."""
    raw_id = service_key_id(bearer)
    if raw_id is None:
        raise authentication_required()
    row = connection.execute(
        """SELECT service_api_keys.id, service_api_keys.service_id,
                  service_api_keys.verifier, services.api_name
           FROM router.service_api_keys
           JOIN router.services ON services.id = service_api_keys.service_id
           WHERE service_api_keys.id = %s""",
        (raw_id,),
    ).fetchone()
    supplied = control_keys.verifier(bearer)
    if row is None or not hmac.compare_digest(row["verifier"], supplied):
        raise authentication_required()
    connection.execute(
        """UPDATE router.service_api_keys
           SET last_used_at = statement_timestamp()
           WHERE id = %s""",
        (row["id"],),
    )
    return ServiceActor(row["service_id"], row["api_name"], row["id"])


def store_oidc_flow(
    connection: Connection[Any],
    *,
    state_verifier: bytes,
    encrypted_control: bytes,
    expires_at: datetime,
) -> None:
    """Store one short-lived one-time authorization flow."""
    connection.execute(
        """DELETE FROM router.administrator_oidc_flows
           WHERE expires_at <= statement_timestamp()"""
    )
    connection.execute(
        """INSERT INTO router.administrator_oidc_flows
               (state_verifier, encrypted_control, expires_at)
           VALUES (%s, %s, %s)""",
        (state_verifier, encrypted_control, expires_at),
    )


def lock_oidc_flow(connection: Connection[Any], state_verifier: bytes) -> bytes:
    """Lock and read one unexpired flow before browser-binding validation."""
    row = connection.execute(
        """SELECT encrypted_control FROM router.administrator_oidc_flows
           WHERE state_verifier = %s AND expires_at > statement_timestamp()
           FOR UPDATE""",
        (state_verifier,),
    ).fetchone()
    if row is None:
        raise authentication_required()
    return cast("bytes", row["encrypted_control"])


def consume_oidc_flow(connection: Connection[Any], state_verifier: bytes) -> None:
    """Consume one locked state value before external token work."""
    deleted = connection.execute(
        """DELETE FROM router.administrator_oidc_flows
           WHERE state_verifier = %s AND expires_at > statement_timestamp()
           RETURNING state_verifier""",
        (state_verifier,),
    ).fetchone()
    if deleted is None:
        raise authentication_required()
    # State remains consumed if discovery, authorization-code work, or validation fails.
    # A later session insert starts a new transaction on this connection.
    connection.commit()


def create_administrator_session(
    connection: Connection[Any],
    *,
    session_verifier: bytes,
    csrf_verifier: bytes,
    encrypted_csrf_token: bytes,
    issuer: str,
    subject: str,
    display_name: str,
    expires_at: datetime,
) -> None:
    """Store one absolute-expiry administrator session."""
    connection.execute(
        """DELETE FROM router.administrator_sessions
           WHERE expires_at <= statement_timestamp()"""
    )
    connection.execute(
        """INSERT INTO router.administrator_sessions
               (session_verifier, csrf_verifier, encrypted_csrf_token,
                issuer, subject, display_name, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            session_verifier,
            csrf_verifier,
            encrypted_csrf_token,
            issuer,
            subject,
            display_name,
            expires_at,
        ),
    )


def authenticate_administrator_session(
    connection: Connection[Any],
    *,
    session_token: str,
    control_keys: ControlKeys,
) -> AdministratorActor:
    """Authenticate one local session without extending its absolute expiry."""
    session_verifier = control_keys.verifier(session_token)
    row = connection.execute(
        """SELECT session_verifier, csrf_verifier, encrypted_csrf_token,
                  issuer, subject, display_name, expires_at
           FROM router.administrator_sessions
           WHERE session_verifier = %s AND expires_at > statement_timestamp()""",
        (session_verifier,),
    ).fetchone()
    if row is None:
        raise authentication_required()
    values = control_keys.decrypt(row["encrypted_csrf_token"])
    csrf_token = values.get("csrf_token")
    if csrf_token is None or not hmac.compare_digest(
        control_keys.verifier(csrf_token), row["csrf_verifier"]
    ):
        raise authentication_required()
    return AdministratorActor(
        session_verifier=row["session_verifier"],
        csrf_verifier=row["csrf_verifier"],
        issuer=row["issuer"],
        subject=row["subject"],
        display_name=row["display_name"],
        expires_at=row["expires_at"],
        csrf_token=csrf_token,
    )


def delete_administrator_session(
    connection: Connection[Any], session_verifier: bytes
) -> None:
    """Invalidate one local administrator session immediately."""
    connection.execute(
        "DELETE FROM router.administrator_sessions WHERE session_verifier = %s",
        (session_verifier,),
    )


def record_activity(
    connection: Connection[Any],
    actor_subject: str,
    action: str,
    resource_type: str,
    *,
    service_api_name: str | None = None,
    resource_api_name: str | None = None,
    resource_id: uuid.UUID | None = None,
    result: str = "succeeded",
) -> None:
    """Record a basic activity result without old values or control data."""
    connection.execute(
        """INSERT INTO router.activity_events
               (actor_subject, action, resource_type, service_api_name,
                resource_api_name, resource_id, result)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            actor_subject,
            action,
            resource_type,
            service_api_name,
            resource_api_name,
            resource_id,
            result,
        ),
    )


def list_activity(
    connection: Connection[Any],
    *,
    from_time: datetime,
    to_time: datetime,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read one bounded newest-first activity page."""
    if from_time.tzinfo is None or to_time.tzinfo is None or from_time >= to_time:
        raise invalid_request("from", "The activity time range is invalid.")
    cursor_id = _uuid_cursor(cursor)
    cursor_time: datetime | None = None
    if cursor_id is not None:
        cursor_row = connection.execute(
            """SELECT occurred_at
               FROM router.activity_events, router.global_settings
               WHERE activity_events.id = %s
                 AND activity_events.occurred_at >= statement_timestamp()
                     - make_interval(days => global_settings.log_retention_days)""",
            (cursor_id,),
        ).fetchone()
        if cursor_row is None:
            raise invalid_request("cursor", "The cursor is invalid.")
        cursor_time = cast("datetime", cursor_row["occurred_at"])
    rows = connection.execute(
        """SELECT id, actor_subject, action, resource_type, service_api_name,
                  resource_api_name, resource_id, result, occurred_at
           FROM router.activity_events
           CROSS JOIN router.global_settings
           WHERE occurred_at >= %s AND occurred_at < %s
             AND occurred_at >= statement_timestamp()
                 - make_interval(days => global_settings.log_retention_days)
             AND (
                 %s::timestamptz IS NULL
                 OR (occurred_at, id) < (%s, %s)
             )
           ORDER BY occurred_at DESC, id DESC
           LIMIT %s""",
        (
            from_time,
            to_time,
            cursor_time,
            cursor_time,
            cursor_id,
            limit + 1,
        ),
    ).fetchall()
    for row in rows:
        row["id"] = str(row["id"])
        if row["resource_id"] is not None:
            row["resource_id"] = str(row["resource_id"])
    return _page(rows, limit, "id")


def session_expiry(hours: int) -> datetime:
    """Create one absolute UTC session expiry."""
    return datetime.now(tz=UTC) + timedelta(hours=hours)


def _service_id(connection: Connection[Any], api_name: str) -> uuid.UUID:
    row = connection.execute(
        "SELECT id FROM router.services WHERE api_name = %s", (api_name,)
    ).fetchone()
    if row is None:
        raise not_found("service")
    return cast("uuid.UUID", row["id"])


def service_id(connection: Connection[Any], api_name: str) -> uuid.UUID:
    """Resolve one administrator-selected current service."""
    return _service_id(connection, api_name)


def _service_api_name(connection: Connection[Any], service_id: uuid.UUID) -> str:
    row = connection.execute(
        "SELECT api_name FROM router.services WHERE id = %s", (service_id,)
    ).fetchone()
    if row is None:
        raise not_found("service")
    return cast("str", row["api_name"])


def _uuid_cursor(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise invalid_request("cursor", "The cursor is invalid.") from None


def _page(
    rows: list[dict[str, Any]], limit: int, cursor_field: str
) -> tuple[list[dict[str, Any]], str | None]:
    if len(rows) <= limit:
        return rows, None
    selected = rows[:limit]
    return selected, str(selected[-1][cursor_field])
