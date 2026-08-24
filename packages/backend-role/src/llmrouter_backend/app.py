"""Create the one LLM Router web application."""
# ruff: noqa: B008, C901, EM101, FAST002, PLR0913, PLR0917, PLR2004, TC002, TC003, TRY003

from __future__ import annotations

import asyncio
import hmac
import json
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import httpx
import psycopg
from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from psycopg.rows import dict_row
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHttpException
from starlette.middleware.base import RequestResponseEndpoint

from llmrouter_backend import accounting, assignments, catalog
from llmrouter_backend.adapters import (
    CompositeProviderAdapter,
    FakeAdapter,
    LocalEmbeddingAdapter,
    LocalEmbeddingConfiguration,
    OllamaEmbeddingAdapter,
    OllamaTextAdapter,
    OpenAIEmbeddingAdapter,
    OpenAITextAdapter,
)
from llmrouter_backend.calls import (
    CallExecutionError,
    CallExecutor,
    CallResult,
    ProviderOutput,
)
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migration_plan
from llmrouter_backend.diagnostics import (
    apply_retention_and_cleanup,
    cleanup_health,
    get_log_retention,
    get_request_log,
    get_request_log_media,
    list_request_logs,
    put_log_retention,
)
from llmrouter_backend.embedding_api import (
    EmbeddingRequest,
    EmbeddingResult,
    embedding_result,
    internal_embedding_call,
)
from llmrouter_backend.errors import (
    ApiError,
    authentication_required,
    conflict,
    invalid_request,
    not_found,
)
from llmrouter_backend.model_api import (
    ModelCallRequest,
    ModelCallResult,
    internal_model_call,
    model_call_result,
    result_usage,
)
from llmrouter_backend.models import (
    ActivityEvent,
    ActivityPage,
    AdministratorHealth,
    AdministratorSession,
    AdministratorSessionStart,
    Assignment,
    AssignmentPage,
    AssignmentWrite,
    AvailableProviderModel,
    AvailableProviderModelPage,
    Credential,
    CredentialPage,
    CredentialWrite,
    HealthComponent,
    LogRetentionSettings,
    Model,
    ModelImportPreview,
    ModelImportPreviewRequest,
    ModelImportRequest,
    ModelImportResult,
    ModelPage,
    ModelWrite,
    PageInfo,
    PriceSyncRequest,
    PriceSyncResult,
    Provider,
    ProviderModel,
    ProviderModelPage,
    ProviderModelWrite,
    ProviderPage,
    ProviderWrite,
    RequestLog,
    RequestLogPage,
    RequestLogSummary,
    Service,
    ServiceCreate,
    ServiceKey,
    ServiceKeyCreate,
    ServiceKeyCreated,
    ServiceKeyPage,
    ServicePage,
    ServiceUpdate,
    StatisticsDimension,
    StatisticsResult,
    Workspace,
    WorkspaceCreate,
    WorkspacePage,
)
from llmrouter_backend.object_store import ObjectStore
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
    lock_oidc_flow,
    record_activity,
    revoke_key,
    service_by_api_name,
    service_id,
    session_expiry,
    store_oidc_flow,
    update_service,
    workspace_by_api_name,
)

if TYPE_CHECKING:
    from llmrouter_backend.accounting import PriceSource

_DATABASE_CONNECT_TIMEOUT_SECONDS = 2
_DATABASE_STATEMENT_TIMEOUT_MILLISECONDS = 2_000
_DATABASE_LOCK_TIMEOUT_MILLISECONDS = 500
_MAXIMUM_MODEL_HTTP_BODY_BYTES = 70 * 1024 * 1024
_MAXIMUM_EMBEDDING_HTTP_BODY_BYTES = 2 * 1024 * 1024
_ACCOUNTING_SCHEDULE_LOCK = 4_993_044_345_824
_ADMINISTRATOR_COOKIE = "llmrouter_admin_session"
_OIDC_FLOW_COOKIE = "llmrouter_admin_oidc_flow"
_OIDC_FLOW_MINUTES = 10
ApiNamePath = Annotated[str, Path(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
AssignmentNamePath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9._-]{0,126}$")]
ObservedRequirementPath = Annotated[
    Literal[
        "text_input",
        "image_input",
        "text_output",
        "structured_json_output",
        "tool_calling",
        "streaming",
        "reasoning",
        "embedding_output",
        "image_output",
        "video_output",
        "audio_output",
    ],
    Path(),
]


def create_app(  # noqa: PLR0915 - One factory owns the native HTTP map.
    *,
    database_url: str | None = None,
    settings: Settings | None = None,
    oidc_transport: httpx.BaseTransport | None = None,
    object_store: ObjectStore | None = None,
    price_sources: dict[str, PriceSource] | None = None,
    openrouter_price_transport: httpx.BaseTransport | None = None,
    call_executor: CallExecutor | None = None,
) -> FastAPI:
    """Create one application with optional fixed runtime dependencies."""
    settings_value = settings or Settings.from_environment()
    object_store_value = (
        object_store
        if object_store is not None
        else ObjectStore.from_settings(settings_value)
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        cleanup_task = asyncio.create_task(
            _retention_cleanup_loop(database_url, object_store_value)
        )
        accounting_task = asyncio.create_task(
            _accounting_maintenance_loop(
                database_url,
                price_source_values,
            )
        )
        try:
            yield
        finally:
            cleanup_task.cancel()
            accounting_task.cancel()
            for task in (cleanup_task, accounting_task):
                with suppress(asyncio.CancelledError):
                    await task

    application = FastAPI(title="LLM Router", version="1.0.0", lifespan=lifespan)
    application.state.database_url = database_url
    application.state.settings = settings_value
    application.state.oidc_transport = oidc_transport
    application.state.object_store = object_store_value
    price_source_values = (
        price_sources
        if price_sources is not None
        else accounting.default_price_sources(openrouter_price_transport)
    )
    application.state.price_sources = price_source_values
    configured_call_database_url = database_url or os.environ.get(
        "LLMROUTER_DATABASE_URL"
    )
    credential_keys = (
        catalog.ProviderCredentialKeys.load(settings_value)
        if settings_value.provider_credential_wrapping_key_file is not None
        else None
    )
    application.state.call_executor = call_executor or (
        CallExecutor(
            database_url=configured_call_database_url,
            adapters={
                "openai": CompositeProviderAdapter(
                    {
                        "model": OpenAITextAdapter("openai"),
                        "embedding": OpenAIEmbeddingAdapter("openai"),
                    }
                ),
                "openai_compatible": CompositeProviderAdapter(
                    {
                        "model": OpenAITextAdapter("openai_compatible"),
                        "embedding": OpenAIEmbeddingAdapter("openai_compatible"),
                    }
                ),
                "openrouter": OpenAITextAdapter("openrouter"),
                "custom": CompositeProviderAdapter(
                    {
                        "model": OpenAITextAdapter("custom"),
                        "embedding": OpenAIEmbeddingAdapter("custom"),
                    }
                ),
                "ollama": CompositeProviderAdapter(
                    {
                        "model": OllamaTextAdapter(),
                        "embedding": OllamaEmbeddingAdapter(),
                    }
                ),
                "local_embeddings": LocalEmbeddingAdapter(
                    LocalEmbeddingConfiguration.from_settings(settings_value)
                ),
                "fake": FakeAdapter(),
            },
            credential_keys=credential_keys,
            object_store=object_store_value,
        )
        if configured_call_database_url is not None
        else None
    )
    _install_error_handlers(application)

    @application.middleware("http")
    async def prevent_sensitive_response_caching(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(
            (
                "/v1/admin/",
                "/v1/service-keys",
                "/v1/model-calls",
                "/v1/model-streams",
                "/v1/embeddings",
            )
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

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
            options=_database_timeout_options(),
        ) as database:
            yield database

    def control_keys(request: Request) -> ControlKeys:
        return ControlKeys.load(request.app.state.settings)

    def retained_objects(request: Request) -> ObjectStore | None:
        return cast("ObjectStore | None", request.app.state.object_store)

    def model_executor(request: Request) -> CallExecutor:
        executor = cast("CallExecutor | None", request.app.state.call_executor)
        if executor is None:
            raise ApiError(
                500,
                "internal_error",
                "The Router could not complete the operation.",
            )
        return executor

    def service_actor(
        request: Request,
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> ServiceActor:
        return _authenticate_service_request(request, database, controls)

    def call_service_actor(
        request: Request,
        controls: ControlKeys = Depends(control_keys),
    ) -> ServiceActor:
        """Authenticate without an idle database connection during provider work."""
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
            options=_database_timeout_options(),
        ) as database:
            return _authenticate_service_request(request, database, controls)

    def administrator_actor(
        request: Request,
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> AdministratorActor:
        session_token = _control_cookie(request, _ADMINISTRATOR_COOKIE)
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
                options=_database_timeout_options(),
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
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> Response:
        delete_workspace(
            database,
            service_id=actor.service_id,
            api_name=workspace_api_name,
            actor_subject=actor.activity_subject,
        )
        _commit_public_delete_and_cleanup(database, objects)
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/assignments",
        response_model=AssignmentPage,
        response_model_exclude_none=True,
    )
    def service_list_assignments(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> AssignmentPage:
        items, next_cursor = assignments.list_assignments(
            database, service_id=actor.service_id, limit=limit, cursor=cursor
        )
        return AssignmentPage(
            items=[Assignment.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.get(
        "/v1/assignments/{assignment_api_name}",
        response_model=Assignment,
        response_model_exclude_none=True,
    )
    def service_get_assignment(
        assignment_api_name: AssignmentNamePath,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        return assignments.get_assignment(
            database, service_id=actor.service_id, api_name=assignment_api_name
        )

    @application.put(
        "/v1/assignments/{assignment_api_name}",
        response_model=Assignment,
        response_model_exclude_none=True,
    )
    def service_put_assignment(
        assignment_api_name: AssignmentNamePath,
        body: AssignmentWrite,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            assignments.configuration_change(
                database,
                actor_subject=actor.activity_subject,
                service_id=actor.service_id,
                service_api_name=actor.service_api_name,
                action="assignment.update",
                assignment_api_name=assignment_api_name,
                operation=lambda: assignments.put_assignment(
                    database,
                    service_id=actor.service_id,
                    api_name=assignment_api_name,
                    value=body,
                ),
            ),
        )

    @application.delete(
        "/v1/assignments/{assignment_api_name}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def service_delete_assignment(
        assignment_api_name: AssignmentNamePath,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> Response:
        assignments.configuration_change(
            database,
            actor_subject=actor.activity_subject,
            service_id=actor.service_id,
            service_api_name=actor.service_api_name,
            action="assignment.delete",
            assignment_api_name=assignment_api_name,
            operation=lambda: assignments.delete_assignment(
                database, service_id=actor.service_id, api_name=assignment_api_name
            ),
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.delete(
        "/v1/assignments/{assignment_api_name}/observed-requirements/{observed_requirement}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def service_remove_observed_assignment_requirement(
        assignment_api_name: AssignmentNamePath,
        observed_requirement: ObservedRequirementPath,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> Response:
        assignments.configuration_change(
            database,
            actor_subject=actor.activity_subject,
            service_id=actor.service_id,
            service_api_name=actor.service_api_name,
            action="assignment.observed_requirement.remove",
            assignment_api_name=assignment_api_name,
            operation=lambda: assignments.remove_observed_requirement(
                database,
                service_id=actor.service_id,
                api_name=assignment_api_name,
                observed_requirement=observed_requirement,
            ),
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.post(
        "/v1/model-calls",
        response_model=ModelCallResult,
        response_model_exclude_none=True,
    )
    async def service_model_call(
        request: Request,
        actor: ServiceActor = Depends(call_service_actor),
        executor: CallExecutor = Depends(model_executor),
    ) -> ModelCallResult:
        """Make one synchronous native model call."""
        body = await _model_call_body(request)
        try:
            call = internal_model_call(body, streaming=False)
        except ValueError as error:
            raise invalid_request(
                "body", "The model call does not match the contract."
            ) from error
        try:
            result = await executor.execute(actor, call)
        except CallExecutionError as error:
            raise _call_api_error(error) from error
        return model_call_result(result)

    @application.post("/v1/model-streams", response_model=None)
    async def service_model_stream(
        request: Request,
        actor: ServiceActor = Depends(call_service_actor),
        executor: CallExecutor = Depends(model_executor),
    ) -> StreamingResponse:
        """Start one native SSE model stream after route admission commits."""
        body = await _model_call_body(request)
        try:
            call = internal_model_call(body, streaming=True)
        except ValueError as error:
            raise invalid_request(
                "body", "The model stream does not match the contract."
            ) from error
        queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue(maxsize=16)

        async def start(provider_model_api_name: str) -> None:
            await queue.put(
                ("start", {"provider_model_api_name": provider_model_api_name})
            )

        async def write(output: ProviderOutput) -> None:
            await queue.put(_stream_output(output))

        execution = asyncio.create_task(
            executor.execute(
                actor,
                call,
                write_visible_output=write,
                start_visible_output=start,
            )
        )
        try:
            first = await _first_stream_event(queue, execution)
        except BaseException:
            execution.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await execution
            raise
        return StreamingResponse(
            _model_stream_body(first, queue, execution),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @application.post(
        "/v1/embeddings",
        response_model=EmbeddingResult,
        response_model_exclude_none=True,
    )
    async def service_embedding_call(
        request: Request,
        actor: ServiceActor = Depends(call_service_actor),
        executor: CallExecutor = Depends(model_executor),
    ) -> EmbeddingResult:
        """Make one synchronous native embedding batch call."""
        body = await _embedding_call_body(request)
        try:
            call = internal_embedding_call(body)
        except ValueError as error:
            raise invalid_request(
                "body", "The embedding call does not match the contract."
            ) from error
        try:
            result = await executor.execute(actor, call)
        except CallExecutionError as error:
            raise _call_api_error(error) from error
        return embedding_result(result)

    @application.get(
        "/v1/statistics",
        response_model=StatisticsResult,
        response_model_by_alias=True,
    )
    def service_statistics(
        from_time: Annotated[datetime, Query(alias="from")],
        to_time: Annotated[datetime, Query(alias="to")],
        workspace: Annotated[
            str | None,
            Query(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"),
        ] = None,
        assignment: Annotated[
            str | None,
            Query(pattern=r"^(?:[a-z0-9][a-z0-9._-]{0,126}|\(exact\))$"),
        ] = None,
        provider_model: Annotated[
            str | None,
            Query(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"),
        ] = None,
        outcome: Annotated[Literal["succeeded", "failed"] | None, Query()] = None,
        tag: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        group_by: Annotated[list[StatisticsDimension] | None, Query()] = None,
        actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> StatisticsResult:
        return accounting.statistics(
            database,
            from_time=from_time,
            to_time=to_time,
            group_by=group_by or [],
            service_id=actor.service_id,
            workspace=workspace,
            assignment=assignment,
            provider_model=provider_model,
            outcome=outcome,
            tag=tag,
        )

    @application.get(
        "/v1/provider-models",
        response_model=AvailableProviderModelPage,
        response_model_exclude_none=True,
    )
    def service_list_provider_models(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: ServiceActor = Depends(service_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> AvailableProviderModelPage:
        items, next_cursor = catalog.list_available_provider_models(
            database, limit=limit, cursor=cursor
        )
        return AvailableProviderModelPage(
            items=[AvailableProviderModel.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

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
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> RedirectResponse:
        _require_single_query_value(request, "code", code)
        _require_single_query_value(request, "state", state)
        settings_value: Settings = request.app.state.settings
        secrets_value = AdministratorSecrets.load(settings_value)
        require_canonical_token(state)
        state_verifier = secrets_value.verifier(state)
        encrypted_flow = lock_oidc_flow(database, state_verifier)
        flow = secrets_value.decrypt(encrypted_flow)
        if set(flow) != {
            "binding_verifier",
            "nonce",
            "return_to",
            "verifier",
        } or not valid_return_path(flow.get("return_to", "")):
            raise authentication_required()
        browser_binding = _control_cookie(request, _OIDC_FLOW_COOKIE)
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
        consume_oidc_flow(database, state_verifier)
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
            validate_dependents=lambda: assignments.validate_all_assignments(database),
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
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        delete_service(database, api_name=service_api_name, actor=actor)
        _commit_public_delete_and_cleanup(database, objects)
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
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        delete_workspace(
            database,
            service_id=service_id(database, service_api_name),
            api_name=workspace_api_name,
            actor_subject=actor.activity_subject,
        )
        _commit_public_delete_and_cleanup(database, objects)
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
        "/v1/admin/services/{service_api_name}/assignments",
        response_model=AssignmentPage,
        response_model_exclude_none=True,
    )
    def admin_list_assignments(
        service_api_name: ApiNamePath,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> AssignmentPage:
        selected_service_id = service_id(database, service_api_name)
        items, next_cursor = assignments.list_assignments(
            database,
            service_id=selected_service_id,
            limit=limit,
            cursor=cursor,
        )
        return AssignmentPage(
            items=[Assignment.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.get(
        "/v1/admin/services/{service_api_name}/assignments/{assignment_api_name}",
        response_model=Assignment,
        response_model_exclude_none=True,
    )
    def admin_get_assignment(
        service_api_name: ApiNamePath,
        assignment_api_name: AssignmentNamePath,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        return assignments.get_assignment(
            database,
            service_id=service_id(database, service_api_name),
            api_name=assignment_api_name,
        )

    @application.put(
        "/v1/admin/services/{service_api_name}/assignments/{assignment_api_name}",
        response_model=Assignment,
        response_model_exclude_none=True,
    )
    def admin_put_assignment(
        service_api_name: ApiNamePath,
        assignment_api_name: AssignmentNamePath,
        body: AssignmentWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        selected_service_id = service_id(database, service_api_name)
        return cast(
            "dict[str, Any]",
            assignments.configuration_change(
                database,
                actor_subject=actor.activity_subject,
                service_id=selected_service_id,
                service_api_name=service_api_name,
                action="assignment.update",
                assignment_api_name=assignment_api_name,
                operation=lambda: assignments.put_assignment(
                    database,
                    service_id=selected_service_id,
                    api_name=assignment_api_name,
                    value=body,
                ),
            ),
        )

    @application.delete(
        "/v1/admin/services/{service_api_name}/assignments/{assignment_api_name}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def admin_delete_assignment(
        service_api_name: ApiNamePath,
        assignment_api_name: AssignmentNamePath,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        selected_service_id = service_id(database, service_api_name)
        assignments.configuration_change(
            database,
            actor_subject=actor.activity_subject,
            service_id=selected_service_id,
            service_api_name=service_api_name,
            action="assignment.delete",
            assignment_api_name=assignment_api_name,
            operation=lambda: assignments.delete_assignment(
                database,
                service_id=selected_service_id,
                api_name=assignment_api_name,
            ),
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.delete(
        "/v1/admin/services/{service_api_name}/assignments/{assignment_api_name}/observed-requirements/{observed_requirement}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def admin_remove_observed_assignment_requirement(
        service_api_name: ApiNamePath,
        assignment_api_name: AssignmentNamePath,
        observed_requirement: ObservedRequirementPath,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        selected_service_id = service_id(database, service_api_name)
        assignments.configuration_change(
            database,
            actor_subject=actor.activity_subject,
            service_id=selected_service_id,
            service_api_name=service_api_name,
            action="assignment.observed_requirement.remove",
            assignment_api_name=assignment_api_name,
            operation=lambda: assignments.remove_observed_requirement(
                database,
                service_id=selected_service_id,
                api_name=assignment_api_name,
                observed_requirement=observed_requirement,
            ),
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/admin/providers",
        response_model=ProviderPage,
        response_model_exclude_none=True,
    )
    def admin_list_providers(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> ProviderPage:
        items, next_cursor = catalog.list_providers(
            database, limit=limit, cursor=cursor
        )
        return ProviderPage(
            items=[Provider.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/admin/providers",
        response_model=Provider,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def admin_create_provider(
        body: ProviderWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return cast(
            "dict[str, Any]",
            catalog.configuration_change(
                database,
                actor,
                action="provider.create",
                resource_type="provider",
                resource_api_name=body.api_name,
                operation=lambda: catalog.create_provider(database, body),
            ),
        )

    @application.get(
        "/v1/admin/providers/{provider_api_name}",
        response_model=Provider,
        response_model_exclude_none=True,
    )
    def admin_get_provider(
        provider_api_name: ApiNamePath,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        row = catalog.provider_by_api_name(database, provider_api_name)
        if row is None:
            raise not_found("provider")
        return row

    @application.put(
        "/v1/admin/providers/{provider_api_name}",
        response_model=Provider,
        response_model_exclude_none=True,
    )
    def admin_put_provider(
        provider_api_name: ApiNamePath,
        body: ProviderWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return cast(
            "dict[str, Any]",
            catalog.configuration_change(
                database,
                actor,
                action="provider.update",
                resource_type="provider",
                resource_api_name=provider_api_name,
                operation=lambda: catalog.replace_provider(
                    database, provider_api_name, body
                ),
            ),
        )

    @application.delete(
        "/v1/admin/providers/{provider_api_name}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def admin_delete_provider(
        provider_api_name: ApiNamePath,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        catalog.configuration_change(
            database,
            actor,
            action="provider.delete",
            resource_type="provider",
            resource_api_name=provider_api_name,
            operation=lambda: catalog.delete_provider(database, provider_api_name),
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/admin/models",
        response_model=ModelPage,
        response_model_exclude_none=True,
    )
    def admin_list_models(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> ModelPage:
        items, next_cursor = catalog.list_models(database, limit=limit, cursor=cursor)
        return ModelPage(
            items=[Model.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/admin/models",
        response_model=Model,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def admin_create_model(
        body: ModelWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return cast(
            "dict[str, Any]",
            catalog.configuration_change(
                database,
                actor,
                action="model.create",
                resource_type="model",
                resource_api_name=body.api_name,
                operation=lambda: catalog.create_model(database, body),
            ),
        )

    @application.get(
        "/v1/admin/models/{model_api_name}",
        response_model=Model,
        response_model_exclude_none=True,
    )
    def admin_get_model(
        model_api_name: ApiNamePath,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        row = catalog.model_by_api_name(database, model_api_name)
        if row is None:
            raise not_found("model")
        return row

    @application.put(
        "/v1/admin/models/{model_api_name}",
        response_model=Model,
        response_model_exclude_none=True,
    )
    def admin_put_model(
        model_api_name: ApiNamePath,
        body: ModelWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return cast(
            "dict[str, Any]",
            catalog.configuration_change(
                database,
                actor,
                action="model.update",
                resource_type="model",
                resource_api_name=model_api_name,
                operation=lambda: catalog.replace_model(database, model_api_name, body),
            ),
        )

    @application.delete(
        "/v1/admin/models/{model_api_name}", status_code=HTTPStatus.NO_CONTENT
    )
    def admin_delete_model(
        model_api_name: ApiNamePath,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        catalog.configuration_change(
            database,
            actor,
            action="model.delete",
            resource_type="model",
            resource_api_name=model_api_name,
            operation=lambda: catalog.delete_model(database, model_api_name),
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/admin/provider-models",
        response_model=ProviderModelPage,
        response_model_exclude_none=True,
    )
    def admin_list_provider_models(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> ProviderModelPage:
        items, next_cursor = catalog.list_provider_models(
            database, limit=limit, cursor=cursor
        )
        return ProviderModelPage(
            items=[ProviderModel.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/admin/provider-models",
        response_model=ProviderModel,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def admin_create_provider_model(
        body: ProviderModelWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return cast(
            "dict[str, Any]",
            catalog.configuration_change(
                database,
                actor,
                action="provider_model.create",
                resource_type="provider_model",
                resource_api_name=body.api_name,
                operation=lambda: catalog.create_provider_model(database, body),
            ),
        )

    @application.get(
        "/v1/admin/provider-models/{provider_model_api_name}",
        response_model=ProviderModel,
        response_model_exclude_none=True,
    )
    def admin_get_provider_model(
        provider_model_api_name: ApiNamePath,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> dict[str, Any]:
        row = catalog.provider_model_by_api_name(database, provider_model_api_name)
        if row is None:
            raise not_found("provider-model")
        return row

    @application.put(
        "/v1/admin/provider-models/{provider_model_api_name}",
        response_model=ProviderModel,
        response_model_exclude_none=True,
    )
    def admin_put_provider_model(
        provider_model_api_name: ApiNamePath,
        body: ProviderModelWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return cast(
            "dict[str, Any]",
            catalog.configuration_change(
                database,
                actor,
                action="provider_model.update",
                resource_type="provider_model",
                resource_api_name=provider_model_api_name,
                operation=lambda: catalog.replace_provider_model(
                    database, provider_model_api_name, body
                ),
            ),
        )

    @application.delete(
        "/v1/admin/provider-models/{provider_model_api_name}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def admin_delete_provider_model(
        provider_model_api_name: ApiNamePath,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        catalog.configuration_change(
            database,
            actor,
            action="provider_model.delete",
            resource_type="provider_model",
            resource_api_name=provider_model_api_name,
            operation=lambda: catalog.delete_provider_model(
                database, provider_model_api_name
            ),
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.get(
        "/v1/admin/credentials",
        response_model=CredentialPage,
        response_model_exclude_none=True,
    )
    def admin_list_credentials(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> CredentialPage:
        items, next_cursor = catalog.list_credentials(
            database, limit=limit, cursor=cursor
        )
        return CredentialPage(
            items=[Credential.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.post(
        "/v1/admin/credentials",
        response_model=Credential,
        response_model_exclude_none=True,
        status_code=HTTPStatus.CREATED,
    )
    def admin_create_credential(
        body: CredentialWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return cast(
            "dict[str, Any]",
            catalog.configuration_change(
                database,
                actor,
                action="credential.create",
                resource_type="credential",
                resource_api_name=body.api_name,
                operation=lambda: catalog.create_credential(
                    database,
                    api_name=body.api_name,
                    secret=body.secret,
                    keys=catalog.ProviderCredentialKeys.load(
                        request.app.state.settings
                    ),
                ),
            ),
        )

    @application.put(
        "/v1/admin/credentials/{credential_api_name}",
        response_model=Credential,
        response_model_exclude_none=True,
    )
    def admin_put_credential(
        credential_api_name: ApiNamePath,
        body: CredentialWrite,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> dict[str, Any]:
        _require_browser_write(request, actor, controls)
        return cast(
            "dict[str, Any]",
            catalog.configuration_change(
                database,
                actor,
                action="credential.update",
                resource_type="credential",
                resource_api_name=credential_api_name,
                operation=lambda: catalog.replace_credential(
                    database,
                    api_name=credential_api_name,
                    value=body,
                    keys=catalog.ProviderCredentialKeys.load(
                        request.app.state.settings
                    ),
                ),
            ),
        )

    @application.delete(
        "/v1/admin/credentials/{credential_api_name}",
        status_code=HTTPStatus.NO_CONTENT,
    )
    def admin_delete_credential(
        credential_api_name: ApiNamePath,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> Response:
        _require_browser_write(request, actor, controls)
        catalog.configuration_change(
            database,
            actor,
            action="credential.delete",
            resource_type="credential",
            resource_api_name=credential_api_name,
            operation=lambda: catalog.delete_credential(database, credential_api_name),
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @application.post(
        "/v1/admin/prices/synchronize",
        response_model=PriceSyncResult,
        response_model_exclude_none=True,
    )
    def admin_synchronize_prices(
        body: PriceSyncRequest,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> PriceSyncResult:
        _require_browser_write(request, actor, controls)
        failure_id = uuid.uuid4()
        try:
            synchronization_id, result = accounting.synchronize_prices(
                database,
                sources=cast("dict[str, PriceSource]", request.app.state.price_sources),
                provider_model_api_names=body.provider_model_api_names,
            )
            record_activity(
                database,
                actor.activity_subject,
                "price.synchronize",
                "price_synchronization",
                resource_id=synchronization_id,
                result=(
                    "succeeded"
                    if all(
                        item.outcome in {"updated", "unchanged"}
                        for item in result.items
                    )
                    else "failed"
                ),
            )
        except Exception:
            database.rollback()
            record_activity(
                database,
                actor.activity_subject,
                "price.synchronize",
                "price_synchronization",
                resource_id=failure_id,
                result="failed",
            )
            database.commit()
            raise
        return result

    @application.post(
        "/v1/admin/model-imports/preview",
        response_model=ModelImportPreview,
        response_model_exclude_none=True,
    )
    def admin_preview_model_import(
        body: ModelImportPreviewRequest,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> ModelImportPreview:
        _require_browser_write(request, actor, controls)
        return ModelImportPreview(
            provider_api_name=body.provider_api_name,
            candidates=catalog.catalog_preview(database, body.provider_api_name),
        )

    @application.post(
        "/v1/admin/model-imports",
        response_model=ModelImportResult,
        response_model_exclude_none=True,
    )
    def admin_import_models(
        body: ModelImportRequest,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
    ) -> ModelImportResult:
        _require_browser_write(request, actor, controls)
        result = cast(
            "tuple[list[dict[str, Any]], list[dict[str, Any]]]",
            catalog.configuration_change(
                database,
                actor,
                action="model_import.apply",
                resource_type="provider",
                resource_api_name=body.provider_api_name,
                operation=lambda: catalog.import_catalog(
                    database, body.provider_api_name, body.selections
                ),
            ),
        )
        return ModelImportResult(
            models=[Model.model_validate(item) for item in result[0]],
            provider_models=[ProviderModel.model_validate(item) for item in result[1]],
        )

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
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> ActivityPage:
        apply_retention_and_cleanup(database, objects)
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

    @application.get(
        "/v1/admin/statistics",
        response_model=StatisticsResult,
        response_model_by_alias=True,
    )
    def admin_statistics(
        from_time: Annotated[datetime, Query(alias="from")],
        to_time: Annotated[datetime, Query(alias="to")],
        service: Annotated[
            str | None,
            Query(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"),
        ] = None,
        workspace: Annotated[
            str | None,
            Query(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"),
        ] = None,
        assignment: Annotated[
            str | None,
            Query(pattern=r"^(?:[a-z0-9][a-z0-9._-]{0,126}|\(exact\))$"),
        ] = None,
        provider_model: Annotated[
            str | None,
            Query(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"),
        ] = None,
        outcome: Annotated[Literal["succeeded", "failed"] | None, Query()] = None,
        tag: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        group_by: Annotated[list[StatisticsDimension] | None, Query()] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
    ) -> StatisticsResult:
        return accounting.statistics(
            database,
            from_time=from_time,
            to_time=to_time,
            group_by=group_by or [],
            service=service,
            workspace=workspace,
            assignment=assignment,
            provider_model=provider_model,
            outcome=outcome,
            tag=tag,
        )

    @application.get(
        "/v1/admin/request-logs",
        response_model=RequestLogPage,
        response_model_exclude_none=True,
    )
    def admin_list_request_logs(
        from_time: Annotated[datetime, Query(alias="from")],
        to_time: Annotated[datetime, Query(alias="to")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> RequestLogPage:
        apply_retention_and_cleanup(database, objects)
        items, next_cursor = list_request_logs(
            database,
            from_time=from_time,
            to_time=to_time,
            limit=limit,
            cursor=cursor,
        )
        return RequestLogPage(
            items=[RequestLogSummary.model_validate(item) for item in items],
            page=PageInfo(has_more=next_cursor is not None, next_cursor=next_cursor),
        )

    @application.get(
        "/v1/admin/request-logs/{request_log_id}",
        response_model=RequestLog,
        response_model_exclude_none=True,
    )
    def admin_get_request_log(
        request_log_id: str,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> dict[str, Any]:
        apply_retention_and_cleanup(database, objects)
        return get_request_log(database, _opaque_uuid(request_log_id, "request_log_id"))

    @application.get(
        "/v1/admin/request-logs/{request_log_id}/media/{media_id}/content",
        response_model=None,
    )
    def admin_get_request_log_media(
        request_log_id: str,
        media_id: str,
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> Response:
        apply_retention_and_cleanup(database, objects)
        stored = get_request_log_media(
            database,
            objects,
            request_log_id=_opaque_uuid(request_log_id, "request_log_id"),
            media_id=_opaque_uuid(media_id, "media_id"),
        )
        return Response(
            stored.body,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="retained-media"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.get(
        "/v1/admin/settings/log-retention",
        response_model=LogRetentionSettings,
    )
    def admin_get_log_retention(
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> LogRetentionSettings:
        apply_retention_and_cleanup(database, objects)
        return LogRetentionSettings(duration_days=get_log_retention(database))

    @application.put(
        "/v1/admin/settings/log-retention",
        response_model=LogRetentionSettings,
    )
    def admin_put_log_retention(
        body: LogRetentionSettings,
        request: Request,
        actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        controls: ControlKeys = Depends(control_keys),
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> LogRetentionSettings:
        _require_browser_write(request, actor, controls)
        duration = put_log_retention(
            database, duration_days=body.duration_days, actor=actor
        )
        apply_retention_and_cleanup(database, objects)
        return LogRetentionSettings(duration_days=duration)

    @application.get(
        "/v1/admin/health",
        response_model=AdministratorHealth,
        response_model_exclude_none=True,
    )
    def admin_health(
        _actor: AdministratorActor = Depends(administrator_actor),
        database: psycopg.Connection[Any] = Depends(connection),
        objects: ObjectStore | None = Depends(retained_objects),
    ) -> AdministratorHealth:
        apply_retention_and_cleanup(database, objects)
        object_status: Literal["healthy", "degraded", "unavailable"] = (
            "healthy" if objects is not None and objects.healthy() else "unavailable"
        )
        retention_status = cleanup_health(database)
        price_status_value, rollup_status_value = accounting.maintenance_health(
            database
        )
        price_status = cast(
            'Literal["healthy", "degraded", "unavailable"]', price_status_value
        )
        rollup_status = cast(
            'Literal["healthy", "degraded", "unavailable"]', rollup_status_value
        )
        components = [
            HealthComponent(name="web_application", status="healthy"),
            HealthComponent(name="postgresql", status="healthy"),
            HealthComponent(name="object_storage", status=object_status),
            HealthComponent(name="price_synchronization", status=price_status),
            HealthComponent(name="log_retention", status=retention_status),
            HealthComponent(name="accounting_rollup", status=rollup_status),
        ]
        overall: Literal["healthy", "degraded", "unavailable"] = (
            "unavailable"
            if any(component.status == "unavailable" for component in components)
            else "degraded"
            if any(component.status == "degraded" for component in components)
            else "healthy"
        )
        return AdministratorHealth(
            status=overall, checked_at=datetime.now(tz=UTC), components=components
        )

    return application


def _require_browser_write(
    request: Request, actor: AdministratorActor, control_keys: ControlKeys
) -> None:
    """Require exact origin and the session-bound CSRF token."""
    origins = request.headers.getlist("origin")
    csrf_values = request.headers.getlist("x-csrf-token")
    origin = origins[0] if len(origins) == 1 else None
    supplied_csrf = csrf_values[0] if len(csrf_values) == 1 else None
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


def _authenticate_service_request(
    request: Request,
    database: psycopg.Connection[Any],
    controls: ControlKeys,
) -> ServiceActor:
    """Authenticate one exact bearer without retaining or logging its value."""
    authorization = _single_header(request, "authorization")
    if authorization is None or not authorization.startswith("Bearer "):
        raise authentication_required()
    bearer = authorization.removeprefix("Bearer ")
    if not bearer or bearer.strip() != bearer or len(bearer) > 500:
        raise authentication_required()
    return authenticate_service_key(database, bearer, controls)


async def _model_call_body(request: Request) -> ModelCallRequest:
    """Read and validate one bounded closed JSON model-call body."""
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1:
        raise invalid_request("body", "The body must use application/json.")
    media_type = [part.strip().lower() for part in content_types[0].split(";")]
    if (
        not media_type
        or media_type[0] != "application/json"
        or len(media_type) > 2
        or (len(media_type) == 2 and media_type[1] != "charset=utf-8")
    ):
        raise invalid_request("body", "The body must use application/json.")
    encodings = request.headers.getlist("content-encoding")
    if encodings and encodings != ["identity"]:
        raise invalid_request("body", "The body encoding is not supported.")
    lengths = request.headers.getlist("content-length")
    if lengths and (
        len(lengths) != 1
        or not lengths[0].isdigit()
        or int(lengths[0]) > _MAXIMUM_MODEL_HTTP_BODY_BYTES
    ):
        raise invalid_request("body", "The model-call body is too large.")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAXIMUM_MODEL_HTTP_BODY_BYTES:
            raise invalid_request("body", "The model-call body is too large.")
        raw.extend(chunk)
    if not raw:
        raise invalid_request("body", "The model-call body is required.")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_fields,
            parse_constant=_reject_json_constant,
        )
        return ModelCallRequest.model_validate(value)
    except UnicodeDecodeError, ValueError, RecursionError, ValidationError:
        raise invalid_request(
            "body", "The model call does not match the contract."
        ) from None


async def _embedding_call_body(request: Request) -> EmbeddingRequest:
    """Read and validate one bounded closed JSON embedding-call body."""
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1:
        raise invalid_request("body", "The body must use application/json.")
    media_type = [part.strip().lower() for part in content_types[0].split(";")]
    if (
        not media_type
        or media_type[0] != "application/json"
        or len(media_type) > 2
        or (len(media_type) == 2 and media_type[1] != "charset=utf-8")
    ):
        raise invalid_request("body", "The body must use application/json.")
    encodings = request.headers.getlist("content-encoding")
    if encodings and encodings != ["identity"]:
        raise invalid_request("body", "The body encoding is not supported.")
    lengths = request.headers.getlist("content-length")
    if lengths and (
        len(lengths) != 1
        or not lengths[0].isdigit()
        or int(lengths[0]) > _MAXIMUM_EMBEDDING_HTTP_BODY_BYTES
    ):
        raise invalid_request("body", "The embedding-call body is too large.")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAXIMUM_EMBEDDING_HTTP_BODY_BYTES:
            raise invalid_request("body", "The embedding-call body is too large.")
        raw.extend(chunk)
    if not raw:
        raise invalid_request("body", "The embedding-call body is required.")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_fields,
            parse_constant=_reject_json_constant,
        )
        return EmbeddingRequest.model_validate(value)
    except UnicodeDecodeError, ValueError, RecursionError, ValidationError:
        raise invalid_request(
            "body", "The embedding call does not match the contract."
        ) from None


def _object_without_duplicate_fields(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("A JSON object field occurs more than once.")
        value[name] = item
    return value


def _reject_json_constant(_value: str) -> object:
    raise ValueError("A JSON number must be finite.")


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if not values:
        return None
    if len(values) != 1:
        raise authentication_required()
    return values[0]


def _control_cookie(request: Request, name: str) -> str | None:
    cookie_headers = request.headers.getlist("cookie")
    if not cookie_headers:
        return None
    if len(cookie_headers) != 1:
        raise authentication_required()
    matches: list[str] = []
    for item in cookie_headers[0].split(";"):
        cookie_name, separator, value = item.strip().partition("=")
        if separator and cookie_name == name:
            matches.append(value)
    if not matches:
        return None
    if len(matches) != 1:
        raise authentication_required()
    return matches[0]


def _require_single_query_value(request: Request, name: str, value: str) -> None:
    values = request.query_params.getlist(name)
    if values != [value]:
        raise invalid_request(name, f"The {name} parameter must occur exactly once.")


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


def _opaque_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise invalid_request(field, "The identifier is invalid.") from None


def _commit_public_delete_and_cleanup(
    database: psycopg.Connection[Any], object_store: ObjectStore | None
) -> None:
    """Make a scope absent before best-effort physical object cleanup."""
    database.commit()
    try:
        apply_retention_and_cleanup(database, object_store)
    except Exception:  # noqa: BLE001 - Cleanup cannot reverse the public deletion.
        database.rollback()


async def _retention_cleanup_loop(
    fixed_database_url: str | None, object_store: ObjectStore | None
) -> None:
    """Retry bounded retention and physical deletion work each minute."""
    while True:
        await asyncio.to_thread(
            _run_scheduled_cleanup, fixed_database_url, object_store
        )
        await asyncio.sleep(60)


async def _accounting_maintenance_loop(
    fixed_database_url: str | None,
    price_sources: dict[str, PriceSource],
) -> None:
    """Run due fixed price and accounting work and survive dependency failures."""
    while True:
        await asyncio.to_thread(
            _run_due_accounting_maintenance,
            fixed_database_url,
            price_sources,
        )
        await asyncio.sleep(60)


def _run_due_accounting_maintenance(
    fixed_database_url: str | None,
    price_sources: dict[str, PriceSource],
    *,
    now: datetime | None = None,
) -> None:
    """Catch up fixed daily work after a normal application restart."""
    database_url = fixed_database_url or os.environ.get("LLMROUTER_DATABASE_URL")
    if database_url is None:
        return
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    try:
        with psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
            options=_database_timeout_options(),
        ) as database:
            if current.hour >= 3:
                try:
                    accounting.rollup_pending_days(database, now=current)
                except Exception:  # noqa: BLE001 - Price work must remain independent.
                    database.rollback()
                else:
                    database.commit()
            if current.hour >= 2:
                failure_id = uuid.uuid4()
                try:
                    schedule_lock = database.execute(
                        "SELECT pg_try_advisory_xact_lock(%s) AS acquired",
                        (_ACCOUNTING_SCHEDULE_LOCK,),
                    ).fetchone()
                    if schedule_lock is not None and schedule_lock["acquired"]:
                        existing = database.execute(
                            """SELECT 1 FROM router.price_synchronizations
                               WHERE run_kind = 'scheduled'
                                 AND (attempted_at AT TIME ZONE 'UTC')::date = %s
                               LIMIT 1""",
                            (current.date(),),
                        ).fetchone()
                        if existing is None:
                            synchronization_id, _result = accounting.synchronize_prices(
                                database,
                                sources=price_sources,
                                now=current,
                                run_kind="scheduled",
                            )
                            record_activity(
                                database,
                                "system:price-synchronization",
                                "price.synchronize",
                                "price_synchronization",
                                resource_id=synchronization_id,
                                result=(
                                    "succeeded"
                                    if all(
                                        item.outcome in {"updated", "unchanged"}
                                        for item in _result.items
                                    )
                                    else "failed"
                                ),
                            )
                except Exception:  # noqa: BLE001 - The next pass retries safely.
                    database.rollback()
                    try:
                        record_activity(
                            database,
                            "system:price-synchronization",
                            "price.synchronize",
                            "price_synchronization",
                            resource_id=failure_id,
                            result="failed",
                        )
                    except Exception:  # noqa: BLE001 - Health reports the DB failure.
                        database.rollback()
                    else:
                        database.commit()
                else:
                    database.commit()
    except Exception:  # noqa: BLE001 - The next fixed maintenance pass retries safely.
        return


def _run_scheduled_cleanup(
    fixed_database_url: str | None, object_store: ObjectStore | None
) -> None:
    database_url = fixed_database_url or os.environ.get("LLMROUTER_DATABASE_URL")
    if database_url is None:
        return
    try:
        with psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
            options=_database_timeout_options(),
        ) as database:
            apply_retention_and_cleanup(database, object_store)
    except Exception:  # noqa: BLE001 - One dependency failure must not stop retries.
        return


def _database_timeout_options() -> str:
    """Bound each statement and database lock wait from connection start."""
    return (
        f"-c statement_timeout={_DATABASE_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={_DATABASE_LOCK_TIMEOUT_MILLISECONDS}"
    )


async def _first_stream_event(
    queue: asyncio.Queue[tuple[str, dict[str, object]]],
    execution: asyncio.Task[CallResult],
) -> tuple[str, dict[str, object]]:
    """Wait for committed stream output or return a normal pre-output error."""
    received = asyncio.create_task(queue.get())
    await asyncio.wait((received, execution), return_when=asyncio.FIRST_COMPLETED)
    if received.done():
        return received.result()
    if not queue.empty():
        received.cancel()
        with suppress(asyncio.CancelledError):
            await received
        return queue.get_nowait()
    received.cancel()
    with suppress(asyncio.CancelledError):
        await received
    try:
        result = execution.result()
    except CallExecutionError as error:
        raise _call_api_error(error) from error
    return (
        "start",
        {"provider_model_api_name": result.provider_model_api_name},
    )


async def _model_stream_body(
    first: tuple[str, dict[str, object]],
    queue: asyncio.Queue[tuple[str, dict[str, object]]],
    execution: asyncio.Task[CallResult],
) -> AsyncGenerator[bytes]:
    """Yield exact SSE events and cancel only connection-lifetime provider work."""
    try:
        yield _sse_event(*first)
        while not execution.done() or not queue.empty():
            if not queue.empty():
                yield _sse_event(*queue.get_nowait())
                continue
            received = asyncio.create_task(queue.get())
            await asyncio.wait(
                (received, execution), return_when=asyncio.FIRST_COMPLETED
            )
            if received.done():
                yield _sse_event(*received.result())
            else:
                received.cancel()
                with suppress(asyncio.CancelledError):
                    await received
        try:
            result = execution.result()
        except CallExecutionError as error:
            yield _sse_event("error", _call_api_error(error).envelope())
            return
        except ApiError as error:
            yield _sse_event("error", error.envelope())
            return
        except Exception:  # noqa: BLE001 - The stream must expose only a safe error.
            public = ApiError(
                500,
                "internal_error",
                "The Router could not complete the operation.",
            )
            yield _sse_event("error", public.envelope())
            return
        yield _sse_event(
            "completed",
            {
                "provider_model_api_name": result.provider_model_api_name,
                "usage": result_usage(result),
            },
        )
    finally:
        if not execution.done():
            execution.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await execution


def _stream_output(output: ProviderOutput) -> tuple[str, dict[str, object]]:
    """Compose one validated visible provider event."""
    value = json.loads(output.content_json)
    if output.kind == "text_delta" and isinstance(value, str):
        return "text_delta", {"delta": value}
    if output.kind == "tool_call" and isinstance(value, dict):
        return "tool_call", {"tool_call": value}
    raise RuntimeError("A model stream produced an invalid visible event.")


def _sse_event(event: str, value: dict[str, object]) -> bytes:
    """Encode one bounded single-line UTF-8 server-sent event."""
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode()


def _call_api_error(error: CallExecutionError) -> ApiError:
    """Map one safe engine failure to its stable HTTP status."""
    status_codes = {
        "authentication_required": HTTPStatus.UNAUTHORIZED,
        "permission_denied": HTTPStatus.FORBIDDEN,
        "invalid_request": HTTPStatus.BAD_REQUEST,
        "not_found": HTTPStatus.NOT_FOUND,
        "conflict": HTTPStatus.CONFLICT,
        "assignment_cycle": HTTPStatus.CONFLICT,
        "provider_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
        "upstream_failed": HTTPStatus.BAD_GATEWAY,
        "content_unavailable": HTTPStatus.NOT_FOUND,
        "rate_limited": HTTPStatus.TOO_MANY_REQUESTS,
        "internal_error": HTTPStatus.INTERNAL_SERVER_ERROR,
    }
    return ApiError(
        int(status_codes.get(error.code, HTTPStatus.INTERNAL_SERVER_ERROR)),
        error.code if error.code in status_codes else "internal_error",
        str(error)
        if error.code in status_codes
        else "The Router could not complete the operation.",
        field=error.field,
        reason=error.reason,
    )


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
