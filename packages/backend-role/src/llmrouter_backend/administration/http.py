"""FastAPI routes for the protected basic administration surface."""
# ruff: noqa: BLE001, EM101, EM102, PLR0911, PLR2004, TRY003

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from llmrouter_backend.accounting import AccountingError
from llmrouter_backend.admin_auth import AdministratorAuthError
from llmrouter_backend.budgets import BudgetError
from llmrouter_backend.configuration import CatalogKind, ConfigurationError
from llmrouter_backend.credential_store import (
    CredentialAction,
    CredentialStoreError,
)
from llmrouter_backend.execution import ExecutionError
from llmrouter_backend.lifecycle import LifecycleError, ServiceAction

from .model import (
    AssignmentInput,
    BudgetLimitInput,
    CredentialChangeInput,
    CredentialCreateInput,
    ProviderInstanceInput,
    ProviderModelRouteInput,
    ServiceActionInput,
    ServiceCreateInput,
    ServiceUpdateInput,
)
from .service import AdministrationService

if TYPE_CHECKING:
    from collections.abc import Callable

router = APIRouter(prefix="/v1", tags=["Administration"])

_ADMINISTRATION_COOKIE = "__Host-llmrouter-admin"
_LOCAL_ADMINISTRATION_COOKIE = "__Host-llmrouter-local-admin"
_MAXIMUM_BODY_BYTES = 1_048_576
_MAXIMUM_HEADER_CHARACTERS = 2_048
_MINIMUM_IDEMPOTENCY_CHARACTERS = 16
_MAXIMUM_IDEMPOTENCY_CHARACTERS = 200


def install_administration_service(
    app: FastAPI, service: AdministrationService
) -> None:
    """Install one explicit administration service."""
    state = getattr(app, "state", None)
    if state is None:
        raise TypeError("The application does not have state storage.")
    state.administration_service = service


@router.get("/admin/catalog/{catalog_kind}", response_model=None)
async def list_catalog(request: Request, catalog_kind: str) -> Response:
    """List one bounded named global catalog."""
    request_id = _request_id()
    try:
        kind = _catalog_kind(catalog_kind)
        result = await _run(
            request,
            lambda service: service.list_catalog(
                _session(request, request_id),
                kind,
                request_id=request_id,
                cursor=_optional_query(request, "cursor"),
                limit=_limit(request),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.get("/admin/services", response_model=None)
async def list_services(request: Request) -> Response:
    """List every retained service in global administration."""
    request_id = _request_id()
    try:
        result = await _run(
            request,
            lambda service: service.list_services(
                _session(request, request_id),
                request_id=request_id,
                cursor=_optional_query(request, "cursor"),
                limit=_limit(request),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.post("/admin/services", response_model=None)
async def create_service(request: Request) -> Response:
    """Create one service and show its bootstrap secret once."""
    request_id = _request_id()
    try:
        value = await _document(request, ServiceCreateInput, request_id)
        result, replayed = await _run(
            request,
            lambda service: service.create_service(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                _idempotency_key(request, request_id),
                value,
                request_id=request_id,
            ),
            request_id,
        )
        return _json(result, status_code=200 if replayed else 201)
    except Exception as error:
        return _error_response(error, request_id)


@router.get("/admin/services/{service_id}", response_model=None)
async def get_service(request: Request, service_id: str) -> Response:
    """Get one service administration record."""
    request_id = _request_id()
    try:
        result = await _run(
            request,
            lambda service: service.get_service(
                _session(request, request_id), service_id, request_id=request_id
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.put("/admin/services/{service_id}", response_model=None)
async def update_service(request: Request, service_id: str) -> Response:
    """Replace one service display name and parent link."""
    request_id = _request_id()
    try:
        value = await _document(request, ServiceUpdateInput, request_id)
        result = await _run(
            request,
            lambda service: service.update_service(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                service_id,
                value,
                request_id=request_id,
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.post("/admin/services/{service_id}/disable", response_model=None)
async def disable_service(request: Request, service_id: str) -> Response:
    """Disable one service."""
    return await _change_service_response(request, service_id, ServiceAction.DISABLE)


@router.post("/admin/services/{service_id}/restore", response_model=None)
async def restore_service(request: Request, service_id: str) -> Response:
    """Restore one disabled service."""
    return await _change_service_response(request, service_id, ServiceAction.RESTORE)


@router.post("/admin/services/{service_id}/retire", response_model=None)
async def retire_service(request: Request, service_id: str) -> Response:
    """Retire one service permanently."""
    return await _change_service_response(request, service_id, ServiceAction.RETIRE)


async def _change_service_response(
    request: Request, service_id: str, action: ServiceAction
) -> Response:
    request_id = _request_id()
    try:
        value = await _document(request, ServiceActionInput, request_id)
        result = await _run(
            request,
            lambda service: service.change_service(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                _idempotency_key(request, request_id),
                service_id,
                action,
                value,
                request_id=request_id,
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.get("/admin/services/{service_id}/state", response_model=None)
async def get_administration_state(request: Request, service_id: str) -> Response:
    """Read one exact service or workspace state."""
    request_id = _request_id()
    try:
        result = await _run(
            request,
            lambda service: service.state(
                _session(request, request_id),
                service_id,
                workspace_id=_optional_query(request, "workspace_id"),
                request_id=request_id,
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.get("/admin/credentials", response_model=None)
async def list_credentials(request: Request) -> Response:
    """List safe provider credential metadata."""
    request_id = _request_id()
    try:
        result = await _run(
            request,
            lambda service: service.list_credentials(
                _session(request, request_id),
                request_id=request_id,
                cursor=_optional_query(request, "cursor"),
                limit=_limit(request),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.post("/admin/credentials", response_model=None)
async def create_credential(request: Request) -> Response:
    """Encrypt one write-only provider credential."""
    request_id = _request_id()
    try:
        value = await _document(request, CredentialCreateInput, request_id)
        result, replayed = await _run(
            request,
            lambda service: service.create_credential(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                _idempotency_key(request, request_id),
                value,
                request_id=request_id,
            ),
            request_id,
        )
        return _json(result, status_code=200 if replayed else 201)
    except Exception as error:
        return _error_response(error, request_id)


@router.post("/admin/credentials/{credential_id}/{action}", response_model=None)
async def change_credential(
    request: Request, credential_id: str, action: str
) -> Response:
    """Replace, disable, or retire a write-only provider credential."""
    request_id = _request_id()
    try:
        credential_action = CredentialAction(action)
        value = await _document(request, CredentialChangeInput, request_id)
        _validate_credential_action(credential_action, value)
        result = await _run(
            request,
            lambda service: service.change_credential(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                credential_id,
                credential_action,
                value,
                request_id=request_id,
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.get("/admin/services/{service_id}/provider-instances", response_model=None)
async def list_provider_instances(request: Request, service_id: str) -> Response:
    """List bounded effective provider instances."""
    return await _page_response(request, service_id, "provider_instances")


@router.post("/admin/services/{service_id}/provider-instances", response_model=None)
async def create_provider_instance(request: Request, service_id: str) -> Response:
    """Create one service-owned OpenRouter provider instance."""
    return await _provider_instance_write(request, service_id, None)


@router.put(
    "/admin/services/{service_id}/provider-instances/{provider_instance_id}",
    response_model=None,
)
async def put_provider_instance(
    request: Request, service_id: str, provider_instance_id: str
) -> Response:
    """Replace, disable, or restore one OpenRouter provider instance."""
    return await _provider_instance_write(request, service_id, provider_instance_id)


@router.get("/admin/services/{service_id}/provider-model-routes", response_model=None)
async def list_provider_model_routes(request: Request, service_id: str) -> Response:
    """List bounded effective provider-model routes."""
    return await _page_response(request, service_id, "provider_model_routes")


@router.post("/admin/services/{service_id}/provider-model-routes", response_model=None)
async def create_provider_model_route(request: Request, service_id: str) -> Response:
    """Create one service-owned OpenRouter model route."""
    return await _provider_route_write(request, service_id, None)


@router.put(
    "/admin/services/{service_id}/provider-model-routes/{provider_model_route_id}",
    response_model=None,
)
async def put_provider_model_route(
    request: Request, service_id: str, provider_model_route_id: str
) -> Response:
    """Replace, disable, or restore one OpenRouter model route."""
    return await _provider_route_write(request, service_id, provider_model_route_id)


@router.get("/admin/services/{service_id}/assignments", response_model=None)
async def list_assignments(request: Request, service_id: str) -> Response:
    """List bounded effective assignments."""
    request_id = _request_id()
    try:
        result = await _run(
            request,
            lambda service: service.list_assignments(
                _session(request, request_id),
                service_id,
                request_id=request_id,
                workspace_id=_optional_query(request, "workspace_id"),
                cursor=_optional_query(request, "cursor"),
                limit=_limit(request),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.put(
    "/admin/services/{service_id}/assignments/{assignment_name}", response_model=None
)
async def put_assignment(
    request: Request, service_id: str, assignment_name: str
) -> Response:
    """Publish one complete ordered fallback chain."""
    request_id = _request_id()
    try:
        value = await _document(request, AssignmentInput, request_id)
        result = await _run(
            request,
            lambda service: service.put_assignment(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                _idempotency_key(request, request_id),
                service_id,
                assignment_name,
                value,
                request_id=request_id,
                workspace_id=_optional_query(request, "workspace_id"),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.get("/admin/services/{service_id}/model-requests", response_model=None)
async def list_model_request_status(request: Request, service_id: str) -> Response:
    """List bounded content-free request status."""
    request_id = _request_id()
    try:
        result = await _run(
            request,
            lambda service: service.request_status_page(
                _session(request, request_id),
                service_id,
                request_id=request_id,
                workspace_id=_optional_query(request, "workspace_id"),
                cursor=_optional_query(request, "cursor"),
                limit=_limit(request),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.get(
    "/admin/services/{service_id}/model-requests/{logical_request_id}",
    response_model=None,
)
async def get_model_request_status(
    request: Request, service_id: str, logical_request_id: str
) -> Response:
    """Read one content-free request status."""
    request_id = _request_id()
    try:
        result = await _run(
            request,
            lambda service: service.request_status(
                _session(request, request_id),
                service_id,
                logical_request_id,
                request_id=request_id,
                workspace_id=_optional_query(request, "workspace_id"),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.get("/admin/services/{service_id}/accounting/summary", response_model=None)
async def get_accounting_summary(request: Request, service_id: str) -> Response:
    """Read one bounded accounting aggregate."""
    request_id = _request_id()
    try:
        start = datetime.fromisoformat(_query(request, "from", request_id))
        end = datetime.fromisoformat(_query(request, "to", request_id))
        result = await _run(
            request,
            lambda service: service.accounting_summary(
                _session(request, request_id),
                service_id,
                request_id=request_id,
                workspace_id=_optional_query(request, "workspace_id"),
                start=start,
                end=end,
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.get("/admin/services/{service_id}/budgets", response_model=None)
async def get_budget_summary(request: Request, service_id: str) -> Response:
    """Read one exact selected-scope budget."""
    request_id = _request_id()
    try:
        result = await _run(
            request,
            lambda service: service.budget_summary(
                _session(request, request_id),
                service_id,
                request_id=request_id,
                workspace_id=_optional_query(request, "workspace_id"),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


@router.put("/admin/services/{service_id}/budgets", response_model=None)
async def put_budget(request: Request, service_id: str) -> Response:
    """Replace one exact selected-scope budget."""
    request_id = _request_id()
    try:
        value = await _document(request, BudgetLimitInput, request_id)
        result = await _run(
            request,
            lambda service: service.put_budget(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                _idempotency_key(request, request_id),
                service_id,
                value,
                request_id=request_id,
                workspace_id=_optional_query(request, "workspace_id"),
            ),
            request_id,
        )
        return _json(result)
    except Exception as error:
        return _error_response(error, request_id)


async def _provider_instance_write(
    request: Request, service_id: str, provider_instance_id: str | None
) -> Response:
    request_id = _request_id()
    try:
        value = await _document(request, ProviderInstanceInput, request_id)
        result, created = await _run(
            request,
            lambda service: service.put_provider_instance(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                _idempotency_key(request, request_id),
                service_id,
                value,
                request_id=request_id,
                provider_instance_id=provider_instance_id,
            ),
            request_id,
        )
        return _json(result, status_code=201 if created else 200)
    except Exception as error:
        return _error_response(error, request_id)


async def _provider_route_write(
    request: Request, service_id: str, provider_model_route_id: str | None
) -> Response:
    request_id = _request_id()
    try:
        value = await _document(request, ProviderModelRouteInput, request_id)
        result, created = await _run(
            request,
            lambda service: service.put_provider_model_route(
                _session(request, request_id),
                _header(request, "x-csrf-token", request_id),
                _header(request, "origin", request_id),
                _idempotency_key(request, request_id),
                service_id,
                value,
                request_id=request_id,
                provider_model_route_id=provider_model_route_id,
            ),
            request_id,
        )
        return _json(result, status_code=201 if created else 200)
    except Exception as error:
        return _error_response(error, request_id)


async def _page_response(request: Request, service_id: str, kind: str) -> Response:
    request_id = _request_id()
    try:
        session = _session(request, request_id)
        cursor = _optional_query(request, "cursor")
        limit = _limit(request)
        if kind == "provider_instances":

            def operation(service: AdministrationService) -> object:
                return service.list_provider_instances(
                    session,
                    service_id,
                    request_id=request_id,
                    cursor=cursor,
                    limit=limit,
                )

        else:

            def operation(service: AdministrationService) -> object:
                return service.list_provider_model_routes(
                    session,
                    service_id,
                    request_id=request_id,
                    cursor=cursor,
                    limit=limit,
                )

        return _json(await _run(request, operation, request_id))
    except Exception as error:
        return _error_response(error, request_id)


async def _run[T](
    request: Request,
    operation: Callable[[AdministrationService], T],
    request_id: str,
) -> T:
    service = getattr(request.app.state, "administration_service", None)
    if not isinstance(service, AdministrationService):
        raise _UnavailableError(request_id)
    return await asyncio.to_thread(operation, service)


async def _document[ModelT: BaseModel](
    request: Request, model: type[ModelT], request_id: str
) -> ModelT:
    _require_json(request, request_id)
    raw = await _bounded_body(request, request_id)

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("A JSON field is duplicated.")
            result[key] = value
        return result

    value = json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("A JSON number is invalid.")
        ),
    )
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return model.model_validate_json(canonical)


def _session(request: Request, request_id: str) -> str:
    dual = bool(getattr(request.app.state, "dual_administrator_authority", False))
    local = (
        request.url.scheme == "http"
        and request.url.hostname == "127.0.0.1"
        and request.url.port == 5174
    )
    cookie_name = (
        _LOCAL_ADMINISTRATION_COOKIE if dual and local else _ADMINISTRATION_COOKIE
    )
    values = request.headers.getlist("cookie")
    if len(values) != 1 or len(values[0]) > 4_096:
        raise _AuthenticationError("invalid_token", request_id)
    try:
        cookie = SimpleCookie()
        cookie.load(values[0])
    except CookieError as error:
        raise _AuthenticationError("invalid_token", request_id) from error
    if (
        sum(part.strip().startswith(f"{cookie_name}=") for part in values[0].split(";"))
        != 1
    ):
        raise _AuthenticationError("invalid_token", request_id)
    matches = [item.value for key, item in cookie.items() if key == cookie_name]
    if len(matches) != 1 or not _valid_secret(matches[0]):
        raise _AuthenticationError("invalid_token", request_id)
    if not dual:
        return matches[0]
    return f"{'local' if local else 'oidc'}:{matches[0]}"


def _valid_secret(value: str) -> bool:
    return (
        len(value) == 43
        and value.isascii()
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _catalog_kind(value: str) -> CatalogKind:
    if value == "providers":
        return CatalogKind.PROVIDER
    if value == "models":
        return CatalogKind.MODEL
    raise ValueError("The catalog kind is invalid.")


def _validate_credential_action(
    action: CredentialAction, value: CredentialChangeInput
) -> None:
    if (action is CredentialAction.ROTATE) != (value.replacement_secret is not None):
        raise ValueError("The credential action is invalid.")


def _header(request: Request, name: str, _request_id: str) -> str:
    values = request.headers.getlist(name)
    if (
        len(values) != 1
        or not values[0]
        or len(values[0]) > _MAXIMUM_HEADER_CHARACTERS
        or "\x00" in values[0]
    ):
        raise ValueError(f"The {name} header is invalid.")
    return values[0]


def _idempotency_key(request: Request, request_id: str) -> str:
    value = _header(request, "idempotency-key", request_id)
    if (
        not _MINIMUM_IDEMPOTENCY_CHARACTERS
        <= len(value)
        <= _MAXIMUM_IDEMPOTENCY_CHARACTERS
    ):
        raise ValueError("The idempotency key is invalid.")
    return value


def _query(request: Request, name: str, _request_id: str) -> str:
    values = request.query_params.getlist(name)
    if len(values) != 1 or not values[0] or len(values[0]) > 1_000:
        raise ValueError(f"The {name} query value is invalid.")
    return values[0]


def _optional_query(request: Request, name: str) -> str | None:
    values = request.query_params.getlist(name)
    if not values:
        return None
    if len(values) != 1 or not values[0] or len(values[0]) > 1_000:
        raise ValueError(f"The {name} query value is invalid.")
    return values[0]


def _limit(request: Request) -> int:
    value = _optional_query(request, "limit")
    if value is None:
        return 100
    if not value.isdecimal():
        raise ValueError("The page size is invalid.")
    return int(value)


def _require_json(request: Request, request_id: str) -> None:
    value = _header(request, "content-type", request_id)
    media_type, _, parameters = value.partition(";")
    if media_type.strip().lower() != "application/json" or (
        parameters and parameters.strip().lower() != "charset=utf-8"
    ):
        raise ValueError("The request must use JSON.")


async def _bounded_body(request: Request, _request_id: str) -> bytes:
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise ValueError("The request body is too large.")
    if lengths and (
        not lengths[0].isdecimal() or int(lengths[0]) > _MAXIMUM_BODY_BYTES
    ):
        raise ValueError("The request body is too large.")
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _MAXIMUM_BODY_BYTES - len(body):
            raise ValueError("The request body is too large.")
        body.extend(chunk)
    if not body:
        raise ValueError("The request body is required.")
    return bytes(body)


def _json(value: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        value,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


class _AuthenticationError(RuntimeError):
    def __init__(self, code: str, request_id: str) -> None:
        self.code = code
        self.request_id = request_id


class _UnavailableError(RuntimeError):
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id


def _error_response(error: Exception, request_id: str) -> JSONResponse:
    code, status, retryable, fields = _safe_error(error)
    message = {
        "invalid_request": "The request is invalid.",
        "invalid_token": "Authentication failed.",  # nosec B105 - public error text.
        "recent_auth_required": "Recent authentication is required.",
        "insufficient_scope": "The administrator grant does not permit this operation.",
        "not_found": "The requested record was not found.",
        "request_not_found": "The requested record was not found.",
        "workspace_not_found": "The requested record was not found.",
        "idempotency_conflict": "The idempotency key was used for different content.",
        "state_revision_conflict": "The expected revision does not match.",
        "configuration_revision_conflict": "The expected revision does not match.",
        "terminal_state": "The retired record cannot change.",
        "temporarily_unavailable": "The Router is temporarily unavailable.",
        "internal_error": "The Router could not complete the request.",
    }.get(code, "The request is invalid.")
    body: dict[str, object] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": request_id,
    }
    if fields:
        body["field_errors"] = fields
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if retryable:
        headers["Retry-After"] = "1"
    return JSONResponse({"error": body}, status_code=status, headers=headers)


def _safe_error(
    error: Exception,
) -> tuple[str, int, bool, list[dict[str, str]]]:
    if isinstance(error, ValidationError):
        fields = [
            {
                "path": ".".join(str(item) for item in detail["loc"]),
                "code": "invalid_request",
                "message": "The field is invalid.",
            }
            for detail in error.errors(include_input=False, include_context=False)[:100]
        ]
        return "invalid_request", 422, False, fields
    if isinstance(error, (_AuthenticationError, AdministratorAuthError)):
        code = error.code
        return code, _status(code), code == "temporarily_unavailable", []
    if isinstance(error, CredentialStoreError):
        code = error.code.value
        return code, _status(code), code == "temporarily_unavailable", []
    if isinstance(error, ConfigurationError):
        code = error.code.value
        fields = [
            {
                "path": issue.field_path,
                "code": "invalid_request",
                "message": issue.reason,
            }
            for issue in error.issues[:100]
        ]
        return code, 422 if error.issues else _status(code), False, fields
    if isinstance(error, BudgetError):
        code = (
            "invalid_request"
            if error.code.value == "currency_mismatch"
            else error.code.value
        )
        return code, _status(code), False, []
    if isinstance(error, LifecycleError):
        code = error.code.value
        return code, _status(code), False, []
    if isinstance(error, ExecutionError):
        code = error.code.value
        return code, _status(code), False, []
    if isinstance(error, _UnavailableError):
        return "temporarily_unavailable", 503, True, []
    if isinstance(error, (AccountingError, ValueError, json.JSONDecodeError)):
        return "invalid_request", 400, False, []
    return "internal_error", 500, False, []


def _status(code: str) -> int:
    if code == "invalid_token":
        return 401
    if code == "recent_auth_required":
        return 401
    if code == "insufficient_scope":
        return 403
    if code in {"not_found", "request_not_found", "workspace_not_found"}:
        return 404
    if code in {
        "idempotency_conflict",
        "state_revision_conflict",
        "configuration_revision_conflict",
        "terminal_state",
    }:
        return 409
    if code == "temporarily_unavailable":
        return 503
    return 400


def _request_id() -> str:
    return str(uuid.uuid4())
