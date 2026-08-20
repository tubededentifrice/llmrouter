"""PostgreSQL storage for secure administration embed sessions."""
# ruff: noqa: EM101, PLR0913, TRY003

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg

from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    EmbedPrincipal,
    PrincipalKind,
    RequestContext,
)

from .model import (
    SENSITIVE_PERMISSIONS,
    CreatedSession,
    EmbedSessionError,
    EmbedSessionRequest,
    EmbedTheme,
    RedeemedSession,
    exact_web_origin,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from psycopg import Connection

SESSION_LIFETIME = timedelta(minutes=5)


class EmbedSessionRepository:
    """Create, redeem, authenticate, and revoke short-lived sessions."""

    def __init__(
        self,
        database_url: str,
        *,
        frame_origin: str,
        allowed_host_origins: Mapping[str, frozenset[str]],
    ) -> None:
        """Use one database and exact Router frame origin."""
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        self._database_url = database_url
        self._frame_origin = exact_web_origin(frame_origin)
        self._frame_url = f"{self._frame_origin}/service-administration"
        self._allowed_host_origins: dict[str, frozenset[str]] = {}
        for service_id, origins in allowed_host_origins.items():
            canonical_service_id = str(uuid.UUID(service_id))
            if canonical_service_id != service_id:
                raise ValueError("A configured service identity must be canonical.")
            self._allowed_host_origins[canonical_service_id] = frozenset(
                exact_web_origin(origin) for origin in origins
            )
        if not self._allowed_host_origins or any(
            not origins for origins in self._allowed_host_origins.values()
        ):
            raise ValueError("Each configured service needs a host origin.")

    def create(
        self,
        context: RequestContext,
        document: EmbedSessionRequest,
        *,
        now: datetime,
    ) -> CreatedSession:
        """Store one bounded session and return its bootstrap secret once."""
        _require_create_context(context)
        _require_aware(now)
        service_id = _uuid(context.scope.service_id, context.request_id)
        workspace_id = (
            None
            if document.workspace_id is None
            else _uuid(document.workspace_id, context.request_id)
        )
        if context.scope.workspace_id != document.workspace_id:
            raise EmbedSessionError("not_found", context.request_id)
        permissions = frozenset(document.permissions)
        recent = document.recent_auth_at
        if recent is not None and recent > now:
            raise EmbedSessionError("recent_auth_required", context.request_id)
        if permissions & SENSITIVE_PERMISSIONS:
            if recent is None or now - recent >= SESSION_LIFETIME:
                raise EmbedSessionError("recent_auth_required", context.request_id)
            expires_at = min(now + SESSION_LIFETIME, recent + SESSION_LIFETIME)
        else:
            expires_at = now + SESSION_LIFETIME
        if expires_at <= now:
            raise EmbedSessionError("recent_auth_required", context.request_id)
        session_id = uuid.uuid4()
        bootstrap_token = secrets.token_urlsafe(32)
        bootstrap_digest = _digest(bootstrap_token)
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                _require_active_scope(
                    connection,
                    service_id=service_id,
                    workspace_id=workspace_id,
                    request_id=context.request_id,
                )
                if document.allowed_origin not in self._allowed_host_origins.get(
                    str(service_id), frozenset()
                ):
                    raise EmbedSessionError("insufficient_scope", context.request_id)
                connection.execute(
                    """
                    INSERT INTO router.embed_sessions (
                        id, service_id, workspace_ids, host_subject,
                        permitted_actions, host_origin, frame_origin,
                        bootstrap_token_digest, expires_at, created_at,
                        recent_auth_at, theme_mode, theme_density,
                        theme_corner_style
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        session_id,
                        service_id,
                        [] if workspace_id is None else [workspace_id],
                        document.host_user_subject,
                        sorted(permissions),
                        document.allowed_origin,
                        self._frame_origin,
                        bootstrap_digest,
                        expires_at,
                        now,
                        recent,
                        document.theme.mode,
                        document.theme.density,
                        document.theme.corner_style,
                    ),
                )
                _audit(
                    connection,
                    event_id=uuid.uuid4(),
                    actor_id=context.actor_id,
                    service_id=service_id,
                    workspace_id=workspace_id,
                    action="embed_session.create",
                    permitted=True,
                    session_id=session_id,
                    now=now,
                )
        return CreatedSession(
            session_id=str(session_id),
            bootstrap_token=bootstrap_token,
            frame_url=self._frame_url,
            expires_at=expires_at,
        )

    def redeem(
        self,
        session_id: str,
        bootstrap_token: str,
        frame_nonce: str,
        host_origin: str,
        *,
        request_origin: str,
        request_id: str,
        now: datetime,
    ) -> RedeemedSession:
        """Atomically consume one bootstrap secret and issue a hidden cookie secret."""
        _require_aware(now)
        parsed_session_id = _uuid(session_id, request_id)
        session_token = secrets.token_urlsafe(32)
        row: tuple[Any, ...] | None = None
        principal: EmbedPrincipal | None = None
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE router.embed_sessions AS embed
                    SET redeemed_at = %s,
                        frame_nonce_digest = %s,
                        session_token_digest = %s
                    FROM router.services AS service
                    WHERE embed.id = %s
                      AND embed.bootstrap_token_digest = %s
                      AND embed.host_origin = %s
                      AND embed.frame_origin = %s
                      AND embed.frame_origin = %s
                      AND embed.redeemed_at IS NULL
                      AND embed.revoked_at IS NULL
                      AND embed.created_at <= %s
                      AND embed.expires_at > %s
                      AND service.id = embed.service_id
                      AND service.state = 'active'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM unnest(embed.workspace_ids) AS selected(workspace_id)
                          LEFT JOIN router.workspaces AS workspace
                            ON workspace.id = selected.workspace_id
                           AND workspace.service_id = embed.service_id
                           AND workspace.state = 'active'
                          WHERE workspace.id IS NULL
                      )
                    RETURNING embed.service_id::text, embed.workspace_ids,
                              embed.host_subject, embed.permitted_actions,
                              embed.created_at, embed.expires_at,
                              embed.recent_auth_at, embed.theme_mode,
                              embed.theme_density, embed.theme_corner_style
                    """,
                    (
                        now,
                        _digest(frame_nonce),
                        _digest(session_token),
                        parsed_session_id,
                        _digest(bootstrap_token),
                        host_origin,
                        self._frame_origin,
                        request_origin,
                        now,
                        now,
                    ),
                ).fetchone()
                if row is None:
                    _audit_denied_redemption(
                        connection,
                        session_id=parsed_session_id,
                        request_id=request_id,
                        now=now,
                    )
                else:
                    workspace_ids = frozenset(str(value) for value in row[1])
                    principal = EmbedPrincipal(
                        session_id=str(parsed_session_id),
                        host_subject=row[2],
                        service_id=row[0],
                        allowed_workspace_ids=workspace_ids,
                        operations=frozenset(row[3]),
                        issued_at=row[4],
                        expires_at=row[5],
                        recent_auth_at=row[6],
                    )
                    workspace_id = next(iter(row[1]), None)
                    _audit(
                        connection,
                        event_id=uuid.uuid4(),
                        actor_id=str(parsed_session_id),
                        service_id=uuid.UUID(row[0]),
                        workspace_id=workspace_id,
                        action="embed_session.bootstrap",
                        permitted=True,
                        session_id=parsed_session_id,
                        now=now,
                        system_actor=True,
                    )
        if row is None:
            raise EmbedSessionError("not_found", request_id)
        if principal is None:  # pragma: no cover - row and principal are one result.
            raise EmbedSessionError("not_found", request_id)
        return RedeemedSession(
            principal=principal,
            session_token=session_token,
            theme=EmbedTheme(mode=row[7], density=row[8], corner_style=row[9]),
            cookie_max_age=max(0, int((principal.expires_at - now).total_seconds())),
        )

    def authenticate_session(
        self,
        session_token: str,
        *,
        request_origin: str,
        request_id: str,
        now: datetime,
    ) -> EmbedPrincipal:
        """Resolve one redeemed cookie only for the exact frame origin."""
        _require_aware(now)
        if request_origin != self._frame_origin:
            raise EmbedSessionError("invalid_token", request_id)
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                """
                SELECT embed.id::text, embed.host_subject,
                       embed.service_id::text, embed.workspace_ids,
                       embed.permitted_actions, embed.created_at,
                       embed.expires_at, embed.recent_auth_at
                FROM router.embed_sessions AS embed
                JOIN router.services AS service ON service.id = embed.service_id
                WHERE embed.session_token_digest = %s
                  AND embed.redeemed_at IS NOT NULL
                  AND embed.revoked_at IS NULL
                  AND embed.created_at <= %s
                  AND embed.expires_at > %s
                  AND embed.frame_origin = %s
                  AND service.state = 'active'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM unnest(embed.workspace_ids) AS selected(workspace_id)
                      LEFT JOIN router.workspaces AS workspace
                        ON workspace.id = selected.workspace_id
                       AND workspace.service_id = embed.service_id
                       AND workspace.state = 'active'
                      WHERE workspace.id IS NULL
                  )
                """,
                (_digest(session_token), now, now, self._frame_origin),
            ).fetchone()
        if row is None:
            raise EmbedSessionError("invalid_token", request_id)
        return EmbedPrincipal(
            session_id=row[0],
            host_subject=row[1],
            service_id=row[2],
            allowed_workspace_ids=frozenset(str(value) for value in row[3]),
            operations=frozenset(row[4]),
            issued_at=row[5],
            expires_at=row[6],
            recent_auth_at=row[7],
        )

    def revoke(
        self,
        context: RequestContext,
        session_id: str,
        *,
        now: datetime,
        allowed_workspace_ids: frozenset[str] | None = None,
    ) -> None:
        """Revoke one session through its original host service authority."""
        _require_create_context(context)
        _require_aware(now)
        parsed_session_id = _uuid(session_id, context.request_id)
        service_id = _uuid(context.scope.service_id, context.request_id)
        parsed_workspaces = (
            None
            if allowed_workspace_ids is None
            else [_uuid(value, context.request_id) for value in allowed_workspace_ids]
        )
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE router.embed_sessions
                    SET revoked_at = %s
                    WHERE id = %s AND service_id = %s
                      AND revoked_at IS NULL
                      AND created_at <= %s
                      AND (
                          %s::uuid[] IS NULL
                          OR (
                              cardinality(workspace_ids) > 0
                              AND workspace_ids <@ %s::uuid[]
                          )
                      )
                    RETURNING workspace_ids
                    """,
                    (
                        now,
                        parsed_session_id,
                        service_id,
                        now,
                        parsed_workspaces,
                        parsed_workspaces,
                    ),
                ).fetchone()
                if row is None:
                    raise EmbedSessionError("not_found", context.request_id)
                workspace_id = next(iter(row[0]), None)
                _audit(
                    connection,
                    event_id=uuid.uuid4(),
                    actor_id=context.actor_id,
                    service_id=service_id,
                    workspace_id=workspace_id,
                    action="embed_session.revoke",
                    permitted=True,
                    session_id=parsed_session_id,
                    now=now,
                )


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _uuid(value: str | None, request_id: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise EmbedSessionError("not_found", request_id) from error
    if str(parsed) != value:
        raise EmbedSessionError("not_found", request_id)
    return parsed


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("The current time needs a time zone.")


def _require_create_context(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.HOST_BACKEND
        and context.operation == "admin_embed.create"
        and context.scope.service_id is not None
        and context.mutation
    ):
        raise EmbedSessionError("insufficient_scope", context.request_id)


def _require_active_scope(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    request_id: str,
) -> None:
    service = connection.execute(
        "SELECT 1 FROM router.services WHERE id = %s AND state = 'active' FOR SHARE",
        (service_id,),
    ).fetchone()
    if service is None:
        raise EmbedSessionError("not_found", request_id)
    if (
        workspace_id is not None
        and connection.execute(
            """
        SELECT 1 FROM router.workspaces
        WHERE id = %s AND service_id = %s AND state = 'active'
        FOR SHARE
        """,
            (workspace_id, service_id),
        ).fetchone()
        is None
    ):
        raise EmbedSessionError("not_found", request_id)


def _audit(
    connection: Connection[Any],
    *,
    event_id: uuid.UUID,
    actor_id: str,
    service_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    action: str,
    permitted: bool,
    session_id: uuid.UUID,
    now: datetime,
    system_actor: bool = False,
) -> None:
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            service_id, workspace_id, action, permission_result,
            safe_details, occurred_at
        ) VALUES (
            %s, 'security', %s, %s, %s, %s, %s, %s, %s,
            jsonb_build_object('resource_type', 'embed_session',
                               'resource_id', %s::text), %s
        )
        """,
        (
            event_id,
            "system" if system_actor else "service",
            actor_id,
            "system" if system_actor else "service",
            service_id,
            workspace_id,
            action,
            "permitted" if permitted else "denied",
            session_id,
            now,
        ),
    )


def _audit_denied_redemption(
    connection: Connection[Any],
    *,
    session_id: uuid.UUID,
    request_id: str,
    now: datetime,
) -> None:
    row = connection.execute(
        "SELECT service_id, workspace_ids FROM router.embed_sessions WHERE id = %s",
        (session_id,),
    ).fetchone()
    if row is None:
        return
    _audit(
        connection,
        event_id=uuid.uuid4(),
        actor_id=request_id,
        service_id=row[0],
        workspace_id=next(iter(row[1]), None),
        action="embed_session.bootstrap",
        permitted=False,
        session_id=session_id,
        now=now,
        system_actor=True,
    )
