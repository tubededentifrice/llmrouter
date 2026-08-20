"""HTTP boundary for Pocket ID administrator sessions."""
# ruff: noqa: D103, EM101, TRY003, TRY300, TRY301

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llmrouter_backend.admin_auth.errors import AdministratorAuthError
from llmrouter_backend.admin_auth.model import AuthenticationPurpose, SessionResult
from llmrouter_backend.admin_auth.oidc import administrator_session_cookie

if TYPE_CHECKING:
    from llmrouter_backend.admin_auth.repository import AdministratorAuthRepository

router = APIRouter(prefix="/v1/admin", tags=["Authentication"])
_COOKIE = "__Host-llmrouter-admin"
_LOCAL_COOKIE = "__Host-llmrouter-local-admin"
_LOCAL_PORT = 5174
_MAXIMUM_START_BYTES = 4096
_MAXIMUM_CALLBACK_VALUE = 4096
_MINIMUM_STATE_CHARACTERS = 32


class SessionStartDocument(BaseModel):
    """One bounded login or recent-authentication request."""

    model_config = ConfigDict(extra="forbid")
    purpose: Literal["login", "recent_authentication"]
    return_path: str = Field(
        max_length=2000, pattern=r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$"
    )
    trusted_grant_token: str | None = Field(
        default=None, min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$"
    )


@router.post("/session-starts", response_model=None)
async def start_session(request: Request) -> Response:
    request_id = str(uuid.uuid4())
    try:
        document = await _session_start_document(request, request_id)
        repository = _repository(request)
        result = repository.start_authorization(
            AuthenticationPurpose(document.purpose),
            document.return_path,
            request_id=request_id,
            now=datetime.now(UTC),
            session_token=request.cookies.get(_COOKIE),
            trusted_grant_token=document.trusted_grant_token,
        )
        return JSONResponse(
            {
                "authorization_url": result.authorization_url,
                "expires_at": result.expires_at.isoformat(),
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )
    except AdministratorAuthError as error:
        return _error(error)


@router.get("/oidc/callback", response_model=None)
def complete_session(request: Request) -> Response:
    request_id = str(uuid.uuid4())
    try:
        code, state = _callback_values(request, request_id)
        repository = _repository(request)
        result = repository.complete_authorization(
            code, state, request_id=request_id, now=datetime.now(UTC)
        )
        if result.session_token is None:
            raise AdministratorAuthError("invalid_token", request_id)
        response = RedirectResponse(result.return_path, status_code=303)
        response.headers["Set-Cookie"] = administrator_session_cookie(
            result.session_token.value
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    except AdministratorAuthError as error:
        return _error(error)


@router.get("/session", response_model=None)
def get_session(request: Request) -> Response:
    token = request.cookies.get(_COOKIE, "")
    local_token = request.cookies.get(_LOCAL_COOKIE, "")
    local = getattr(request.app.state, "local_admin_authority", None)
    if (
        local is not None
        and _exact_local_request(request)
        and local.valid_session(local_token)
    ):
        return JSONResponse(
            {"csrf_token": local.csrf, "authentication_mode": "local"},
            headers={"Cache-Control": "no-store"},
        )
    try:
        result = _repository(request).get_session(
            token, request_id=str(uuid.uuid4()), now=datetime.now(UTC)
        )
        return JSONResponse(
            _session_document(result), headers={"Cache-Control": "no-store"}
        )
    except AdministratorAuthError as error:
        return _error(error)


@router.delete("/session", response_model=None)
def delete_session(request: Request) -> Response:
    token = request.cookies.get(_COOKIE, "")
    try:
        _repository(request).logout(
            token,
            request.headers.get("x-csrf-token", ""),
            request.headers.get("origin", ""),
            request_id=str(uuid.uuid4()),
            now=datetime.now(UTC),
        )
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.headers["Set-Cookie"] = administrator_session_cookie("", clear=True)
        return response
    except AdministratorAuthError as error:
        return _error(error)


def _repository(request: Request) -> AdministratorAuthRepository:
    repository = getattr(request.app.state, "administrator_auth_repository", None)
    if repository is None:
        raise AdministratorAuthError("temporarily_unavailable", str(uuid.uuid4()))
    return cast("AdministratorAuthRepository", repository)


def _exact_local_request(request: Request) -> bool:
    return (
        request.url.scheme == "http"
        and request.url.hostname == "127.0.0.1"
        and request.url.port == _LOCAL_PORT
    )


async def _session_start_document(
    request: Request, request_id: str
) -> SessionStartDocument:
    lengths = request.headers.getlist("content-length")
    content_types = request.headers.getlist("content-type")
    if (
        len(lengths) != 1
        or not lengths[0].isascii()
        or not lengths[0].isdecimal()
        or not 1 <= int(lengths[0]) <= _MAXIMUM_START_BYTES
        or len(content_types) != 1
        or content_types[0].partition(";")[0].strip().lower() != "application/json"
        or request.headers.get("transfer-encoding") is not None
    ):
        raise AdministratorAuthError("invalid_request", request_id)
    body = await request.body()
    if len(body) > _MAXIMUM_START_BYTES:
        raise AdministratorAuthError("invalid_request", request_id)

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in values:
            if key in document:
                raise ValueError
            document[key] = value
        return document

    try:
        document = json.loads(
            body,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        return SessionStartDocument.model_validate(document)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise AdministratorAuthError("invalid_request", request_id) from error


def _callback_values(request: Request, request_id: str) -> tuple[str, str]:
    if set(request.query_params) != {"code", "state"}:
        raise AdministratorAuthError("invalid_request", request_id)
    codes = request.query_params.getlist("code")
    states = request.query_params.getlist("state")
    if (
        len(codes) != 1
        or len(states) != 1
        or not 1 <= len(codes[0]) <= _MAXIMUM_CALLBACK_VALUE
        or not _MINIMUM_STATE_CHARACTERS <= len(states[0]) <= _MAXIMUM_CALLBACK_VALUE
    ):
        raise AdministratorAuthError("invalid_request", request_id)
    return codes[0], states[0]


def _session_document(result: SessionResult) -> dict[str, object]:
    if result.csrf_token is None:
        raise RuntimeError("A session read must rotate its CSRF token.")
    return {
        "issuer": result.issuer,
        "subject": result.subject,
        "grants": list(result.grants),
        "authenticated_at": result.authenticated_at.isoformat(),
        "recent_authentication_at": (
            None
            if result.recent_authentication_at is None
            else result.recent_authentication_at.isoformat()
        ),
        "account_state_checked_at": result.account_state_checked_at.isoformat(),
        "idle_expires_at": result.idle_expires_at.isoformat(),
        "absolute_expires_at": result.absolute_expires_at.isoformat(),
        "csrf_token": result.csrf_token.value,
        "identity_account_url": result.identity_account_url,
        "authentication_mode": "oidc",
    }


def _error(error: AdministratorAuthError) -> JSONResponse:
    status = {
        "invalid_token": 401,
        "recent_auth_required": 401,
        "insufficient_scope": 403,
        "temporarily_unavailable": 503,
    }.get(error.code, 400)
    return JSONResponse(
        {"error": {"code": error.code, "message": "Authentication failed."}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )
