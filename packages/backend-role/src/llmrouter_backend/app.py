"""Create the one LLM Router web application."""
# ruff: noqa: B008, C901, EM101, FAST002, PLR0913, PLR0917, PLR2004, TC002, TC003

from __future__ import annotations

import hmac
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any

import httpx
import psycopg
from fastapi import Cookie, Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg.rows import dict_row
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHttpException

from llmrouter_backend.config import Settings
from llmrouter_backend.database import migration_plan
from llmrouter_backend.errors import (
    ApiError,
    authentication_required,
    conflict,
    invalid_request,
    not_found,
)
from llmrouter_backend.models import (
    ActivityEvent,
    ActivityPage,
    AdministratorSession,
    AdministratorSessionStart,
    PageInfo,
    Service,
    ServiceCreate,
    ServiceKey,
    ServiceKeyCreate,
    ServiceKeyCreated,
    ServiceKeyPage,
    ServicePage,
    ServiceUpdate,
    Workspace,
    WorkspaceCreate,
    WorkspacePage,
)
from llmrouter_backend.security import (
    AdministratorSecrets,
    ControlKeys,
    OidcClient,
    new_token,
    require_canonical_token,
    valid_return_path,
)
from llmrouter_backend.store import (
    AdministratorActor,
    ServiceActor,
    authenticate_administrator_session,
    authenticate_service_key,
    consume_oidc_flow,
    create_administrator_session,
    create_key,
    create_service,
    create_workspace,
    delete_administrator_session,
    delete_service,
    delete_workspace,
    list_activity,
    list_keys,
    list_services,
    list_workspaces,
    revoke_key,
    service_by_api_name,
    service_id,
    session_expiry,
    store_oidc_flow,
    update_service,
    workspace_by_api_name,
)

_DATABASE_CONNECT_TIMEOUT_SECONDS = 2
_ADMINISTRATOR_COOKIE = "llmrouter_admin_session"
_OIDC_FLOW_COOKIE = "llmrouter_admin_oidc_flow"
_OIDC_FLOW_MINUTES = 10
ApiNamePath = Annotated[str, Path(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")]


def create_app(  # noqa: PLR0915 - One factory owns the native HTTP map.
    *,
    database_url: str | None = None,
    settings: Settings | None = None,
    oidc_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    """Create one application with optional fixed runtime dependencies."""
    application = FastAPI(title="LLM Router", version="1.0.0")
    application.state.database_url = database_url
    application.state.settings = settings or Settings.from_environment()
    application.state.oidc_transport = oidc_transport
    _install_error_handlers(application)

    def connection(request: Request) -> Iterator[psycopg.Connection[Any]]:
        configured_url = request.app.state.database_url or os.environ.get(
            "LLMROUTER_DATABASE_URL"
        )
        if configured_url is None:
            raise ApiError(
                500,
                "internal_error",
                "The Router could not complete the operation.",
            )
        with psycopg.connect(
            configured_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
        ) as database:
            yield database

    def control_keys(request: Request) -> ControlKeys:
        return ControlKeys.load(request.app.state.settings)

    def service_actor(
        authorization: Annotated[str | None, Header()] = None,
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> ServiceActor:
        if authorization is None or not authorization.startswith("Bearer "):
            raise authentication_required()
        bearer = authorization.removeprefix("Bearer ")
        if not bearer or bearer.strip() != bearer or len(bearer) > 500:
            raise authentication_required()
        return authenticate_service_key(database, bearer, controls)

    def administrator_actor(
        session_token: Annotated[
            str | None, Cookie(alias=_ADMINISTRATOR_COOKIE)
        ] = None,
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> AdministratorActor:
        if session_token is None:
            raise authentication_required()
        require_canonical_token(session_token)
        return authenticate_administrator_session(
            database, session_token=session_token, control_keys=controls
        )

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Report that the web process can serve requests."""
        return {"status": "ok"}

    @application.get("/v1/health", response_model=None)
    async def native_health() -> dict[str, str]:
        """Report the public native health shape."""
        return {"status": "healthy", "checked_at": datetime.now(tz=UTC).isoformat()}

    @application.get("/ready", include_in_schema=False, response_model=None)
    def ready() -> dict[str, str] | JSONResponse:
        """Report readiness only when the complete clean schema is available."""
        configured_url = application.state.database_url or os.environ.get(
            "LLMROUTER_DATABASE_URL"
        )
        if configured_url is None:
            return _not_ready()
        try:
            expected_history = tuple(
                (migration.version, migration.name, migration.checksum)
                for migration in migration_plan()
            )
            with psycopg.connect(
                configured_url,
                connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            ) as database:
                schema_row = database.execute(
                    "SELECT to_regnamespace('router') IS NOT NULL"
                ).fetchone()
                history = tuple(
                    database.execute(
                        """SELECT version, name, checksum
                           FROM public.router_schema_migrations
                           ORDER BY version"""
                    ).fetchall()
                )
        except OSError, UnicodeError, psycopg.Error, RuntimeError:
            return _not_ready()
        if schema_row != (True,) or history != expected_history:
            return _not_ready()
        return {"status": "ready"}

    @application.get(
        "/v1/workspaces",
        response_model=WorkspacePage,
        response_model_exclude_none=True,
    )
    def service_list_workspaces(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> WorkspacePage:
        items, next_cursor = list_workspaces(
            database, service_id=actor.service_id, limit=limit, cursor=cursor
        )
        return WorkspacePage(
            items=[Workspace.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/workspaces",
        response_model=Workspace,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def service_create_workspace(
        body: WorkspaceCreate,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        return create_workspace(
            database,
            service_id=actor.service_id,
            api_name=body.api_name,
            display_name=body.display_name,
            actor_subject=actor.activity_subject,
        )

    @application.get(
        "/v1/workspaces/{workspace_api_name}",
        response_model=Workspace,
        response_model_exclude_none=True,
    )
    def service_get_workspace(
        workspace_api_name: ApiNamePath,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        row = workspace_by_api_name(database, actor.service_id, workspace_api_name)
        if row is None:
            raise not_found("workspace")
        return row

    @application.delete(
        "/v1/workspaces/{workspace_api_name}", status_code=HTTPStatus.NO_CONTENT
    )
    def service_delete_workspace(
        workspace_api_name: ApiNamePath,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> Response:
        delete_workspace(
            database,
            service_id=actor.service_id,
            api_name=workspace_api_name,
            actor_subject=actor.activity_subject,
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/service-keys",
        response_model=ServiceKeyPage,
        response_model_exclude_none=True,
    )
    def service_list_keys(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> ServiceKeyPage:
        items, next_cursor = list_keys(
            database, service_id=actor.service_id, limit=limit, cursor=cursor
        )
        return ServiceKeyPage(
            items=[ServiceKey.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/service-keys",
        response_model=ServiceKeyCreated,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def service_create_key(
        body: ServiceKeyCreate,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> ServiceKeyCreated:
        row, secret = create_key(
            database,
            service_id=actor.service_id,
            name=body.name,
            actor_subject=actor.activity_subject,
            control_keys=controls,
        )
        return ServiceKeyCreated(key=ServiceKey.model_validate(row), secret=secret)

    @application.delete("/v1/service-keys/{key_id}", status_code=HTTPStatus.NO_CONTENT)
    def service_revoke_key(
        key_id: str,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> Response:
        revoke_key(
            database,
            service_id=actor.service_id,
            key_id=_key_id(key_id),
            actor_subject=actor.activity_subject,
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.post("/v1/admin/session/start")
    def start_administrator_session(
        body: AdministratorSessionStart,
        request: Request,
        response: Response,
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, str]:
        if not valid_return_path(body.return_to):
            raise invalid_request(
                "return_to", "The return target must be a local absolute path."
            )
        settings_value: Settings = request.app.state.settings
        secrets_value = AdministratorSecrets.load(settings_value)
        oidc = OidcClient(
            settings_value,
            secrets_value,
            transport=request.app.state.oidc_transport,
        )
        metadata = oidc.metadata()
        state = new_token()
        nonce = new_token()
        verifier = new_token()
        browser_binding = new_token()
        flow_expires_at = datetime.now(tz=UTC) + timedelta(minutes=_OIDC_FLOW_MINUTES)
        authorization_url = oidc.authorization_url(
            metadata, state=state, nonce=nonce, verifier=verifier
        )
        store_oidc_flow(
            database,
            state_verifier=secrets_value.verifier(state),
            encrypted_control=secrets_value.encrypt(
                {
                    "binding_verifier": secrets_value.verifier(browser_binding).hex(),
                    "nonce": nonce,
                    "return_to": body.return_to,
                    "verifier": verifier,
                }
            ),
            expires_at=flow_expires_at,
        )
        response.set_cookie(
            _OIDC_FLOW_COOKIE,
            browser_binding,
            expires=flow_expires_at,
            max_age=_OIDC_FLOW_MINUTES * 60,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/v1/admin/oidc/callback",
        )
        return {"authorization_url": authorization_url}

    @application.get("/v1/admin/oidc/callback", response_model=None)
    def complete_administrator_session(
        request: Request,
        code: Annotated[str, Query(min_length=1, max_length=2_000)],
        state: Annotated[str, Query(min_length=1, max_length=2_000)],
        browser_binding: Annotated[str | None, Cookie(alias=_OIDC_FLOW_COOKIE)] = None,
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> RedirectResponse:
        settings_value: Settings = request.app.state.settings
        secrets_value = AdministratorSecrets.load(settings_value)
        require_canonical_token(state)
        encrypted_flow = consume_oidc_flow(database, secrets_value.verifier(state))
        flow = secrets_value.decrypt(encrypted_flow)
        if set(flow) != {
            "binding_verifier",
            "nonce",
            "return_to",
            "verifier",
        } or not valid_return_path(flow.get("return_to", "")):
            raise authentication_required()
        if browser_binding is None:
            raise authentication_required()
        require_canonical_token(browser_binding)
        try:
            expected_binding = bytes.fromhex(flow["binding_verifier"])
        except ValueError as error:
            raise authentication_required() from error
        if len(expected_binding) != 32 or not hmac.compare_digest(
            secrets_value.verifier(browser_binding), expected_binding
        ):
            raise authentication_required()
        oidc = OidcClient(
            settings_value,
            secrets_value,
            transport=request.app.state.oidc_transport,
        )
        metadata = oidc.metadata()
        token = oidc.exchange(metadata, code=code, verifier=flow["verifier"])
        identity = oidc.verify_identity(metadata, id_token=token, nonce=flow["nonce"])
        session_token = new_token()
        csrf_token = new_token()
        expires_at = session_expiry(settings_value.administrator_session_hours)
        create_administrator_session(
            database,
            session_verifier=secrets_value.verifier(session_token),
            csrf_verifier=secrets_value.verifier(csrf_token),
            encrypted_csrf_token=secrets_value.encrypt({"csrf_token": csrf_token}),
            issuer=identity.issuer,
            subject=identity.subject,
            display_name=identity.display_name,
            expires_at=expires_at,
        )
        response = RedirectResponse(flow["return_to"], status_code=HTTPStatus.SEE_OTHER)
        response.set_cookie(
            _ADMINISTRATOR_COOKIE,
            session_token,
            expires=expires_at,
            max_age=settings_value.administrator_session_hours * 3600,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(
            _OIDC_FLOW_COOKIE,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/v1/admin/oidc/callback",
        )
        return response

    @application.get(
        "/v1/admin/session",
        response_model=AdministratorSession,
        response_model_exclude_none=True,
    )
    def get_administrator_session(
        actor: AdministratorActor = Depends(administrator_actor),
    ) -> AdministratorSession:
        return _administrator_session(actor)

    @application.delete(
        "/v1/admin/session", status_code=HTTPStatus.NO_CONTENT, response_model=None
    )
    def logout_administrator(
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        delete_administrator_session(database, actor.session_verifier)
        response = Response(status_code=HTTPStatus.NO_CONTENT)
        response.delete_cookie(
            _ADMINISTRATOR_COOKIE,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @application.get(
        "/v1/admin/services",
        response_model=ServicePage,
        response_model_exclude_none=True,
    )
    def admin_list_services(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> ServicePage:
        items, next_cursor = list_services(database, limit=limit, cursor=cursor)
        return ServicePage(
            items=[Service.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/admin/services",
        response_model=Service,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def admin_create_service(
        body: ServiceCreate,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return create_service(
            database,
            api_name=body.api_name,
            display_name=body.display_name,
            parent_api_name=body.parent_service_api_name,
            actor=actor,
        )

    @application.get(
        "/v1/admin/services/{service_api_name}",
        response_model=Service,
        response_model_exclude_none=True,
    )
    def admin_get_service(
        service_api_name: ApiNamePath,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        row = service_by_api_name(database, service_api_name)
        if row is None:
            raise not_found("service")
        return row

    @application.put(
        "/v1/admin/services/{service_api_name}",
        response_model=Service,
        response_model_exclude_none=True,
    )
    def admin_update_service(
        service_api_name: ApiNamePath,
        body: ServiceUpdate,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return update_service(
            database,
            api_name=service_api_name,
            display_name=body.display_name,
            parent_api_name=body.parent_service_api_name,
            actor=actor,
        )

    @application.delete(
        "/v1/admin/services/{service_api_name}", status_code=HTTPStatus.NO_CONTENT
    )
    def admin_delete_service(
        service_api_name: ApiNamePath,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        delete_service(database, api_name=service_api_name, actor=actor)
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/admin/services/{service_api_name}/workspaces",
        response_model=WorkspacePage,
        response_model_exclude_none=True,
    )
    def admin_list_workspaces(
        service_api_name: ApiNamePath,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> WorkspacePage:
        owner_id = service_id(database, service_api_name)
        items, next_cursor = list_workspaces(
            database, service_id=owner_id, limit=limit, cursor=cursor
        )
        return WorkspacePage(
            items=[Workspace.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/admin/services/{service_api_name}/workspaces",
        response_model=Workspace,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def admin_create_workspace(
        service_api_name: ApiNamePath,
        body: WorkspaceCreate,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return create_workspace(
            database,
            service_id=service_id(database, service_api_name),
            api_name=body.api_name,
            display_name=body.display_name,
            actor_subject=actor.activity_subject,
        )

    @application.get(
        "/v1/admin/services/{service_api_name}/workspaces/{workspace_api_name}",
        response_model=Workspace,
        response_model_exclude_none=True,
    )
    def admin_get_workspace(
        service_api_name: ApiNamePath,
        workspace_api_name: ApiNamePath,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        row = workspace_by_api_name(
            database, service_id(database, service_api_name), workspace_api_name
        )
        if row is None:
            raise not_found("workspace")
        return row

    @application.delete(
        "/v1/admin/services/{service_api_name}/workspaces/{workspace_api_name}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def admin_delete_workspace(
        service_api_name: ApiNamePath,
        workspace_api_name: ApiNamePath,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        delete_workspace(
            database,
            service_id=service_id(database, service_api_name),
            api_name=workspace_api_name,
            actor_subject=actor.activity_subject,
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/admin/services/{service_api_name}/keys",
        response_model=ServiceKeyPage,
        response_model_exclude_none=True,
    )
    def admin_list_keys(
        service_api_name: ApiNamePath,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> ServiceKeyPage:
        items, next_cursor = list_keys(
            database,
            service_id=service_id(database, service_api_name),
            limit=limit,
            cursor=cursor,
        )
        return ServiceKeyPage(
            items=[ServiceKey.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/admin/services/{service_api_name}/keys",
        response_model=ServiceKeyCreated,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def admin_create_key(
        service_api_name: ApiNamePath,
        body: ServiceKeyCreate,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> ServiceKeyCreated:
        _require_browser_write(request, actor, controls)
        row, secret = create_key(
            database,
            service_id=service_id(database, service_api_name),
            name=body.name,
            actor_subject=actor.activity_subject,
            control_keys=controls,
        )
        return ServiceKeyCreated(key=ServiceKey.model_validate(row), secret=secret)

    @application.delete(
        "/v1/admin/services/{service_api_name}/keys/{key_id}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def admin_revoke_key(
        service_api_name: ApiNamePath,
        key_id: str,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        revoke_key(
            database,
            service_id=service_id(database, service_api_name),
            key_id=_key_id(key_id),
            actor_subject=actor.activity_subject,
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/admin/activity",
        response_model=ActivityPage,
        response_model_exclude_none=True,
    )
    def admin_list_activity(
        from_time: Annotated[datetime, Query(alias="from")],
        to_time: Annotated[datetime, Query(alias="to")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> ActivityPage:
        items, next_cursor = list_activity(
            database,
            from_time=from_time,
            to_time=to_time,
            limit=limit,
            cursor=cursor,
        )
        return ActivityPage(
            items=[ActivityEvent.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    return application


def _require_browser_write(
    request: Request, actor: AdministratorActor, control_keys: ControlKeys
) -> None:
    """Require exact origin and the session-bound CSRF token."""
    origin = request.headers.get("Origin")
    supplied_csrf = request.headers.get("X-CSRF-Token")
    settings: Settings = request.app.state.settings
    if (
        origin not in settings.allowed_origins
        or supplied_csrf is None
        or not 16 <= len(supplied_csrf) <= 500
    ):
        raise ApiError(403, "permission_denied", "The browser write is not permitted.")
    if not hmac.compare_digest(
        control_keys.verifier(supplied_csrf), actor.csrf_verifier
    ):
        raise ApiError(403, "permission_denied", "The browser write is not permitted.")


def _administrator_session(actor: AdministratorActor) -> AdministratorSession:
    return AdministratorSession(
        subject=actor.subject,
        display_name=actor.display_name,
        expires_at=actor.expires_at,
        csrf_token=actor.csrf_token,
    )


def _key_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise invalid_request(
            "key_id", "The service key identifier is invalid."
        ) from None


def _install_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(error.envelope(), status_code=error.status_code)

    @application.exception_handler(RequestValidationError)
    async def request_validation(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        field = None
        if error.errors():
            location = error.errors()[0].get("loc", ())
            if location:
                field = str(location[-1])[:200]
        public = invalid_request(field, "The value does not match the contract.")
        return JSONResponse(public.envelope(), status_code=public.status_code)

    @application.exception_handler(ValidationError)
    async def response_validation(
        _request: Request, _error: ValidationError
    ) -> JSONResponse:
        public = ApiError(
            500, "internal_error", "The Router could not complete the operation."
        )
        return JSONResponse(public.envelope(), status_code=public.status_code)

    @application.exception_handler(psycopg.IntegrityError)
    async def database_conflict(
        _request: Request, error: psycopg.IntegrityError
    ) -> JSONResponse:
        constraint_name = error.diag.constraint_name or ""
        if constraint_name == "services_parent_cycle":
            public = conflict("The service parent relationship would contain a cycle.")
        elif isinstance(error, psycopg.errors.ForeignKeyViolation):
            public = conflict("A current relationship blocks this change.")
        else:
            public = conflict("A resource with this identity already exists.")
        return JSONResponse(public.envelope(), status_code=public.status_code)

    @application.exception_handler(psycopg.Error)
    async def database_error(_request: Request, _error: psycopg.Error) -> JSONResponse:
        public = ApiError(
            500, "internal_error", "The Router could not complete the operation."
        )
        return JSONResponse(public.envelope(), status_code=public.status_code)

    @application.exception_handler(StarletteHttpException)
    async def route_error(
        _request: Request, error: StarletteHttpException
    ) -> JSONResponse:
        public = (
            not_found("resource")
            if error.status_code == HTTPStatus.NOT_FOUND
            else invalid_request()
        )
        return JSONResponse(public.envelope(), status_code=public.status_code)

    @application.exception_handler(Exception)
    async def internal_error(_request: Request, _error: Exception) -> JSONResponse:
        public = ApiError(
            500, "internal_error", "The Router could not complete the operation."
        )
        return JSONResponse(public.envelope(), status_code=public.status_code)


def _not_ready() -> JSONResponse:
    """Create the closed readiness failure response."""
    return JSONResponse(
        {"status": "not_ready"},
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
    )


app = create_app()
