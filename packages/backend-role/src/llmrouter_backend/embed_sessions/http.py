"""FastAPI routes for secure administration embed sessions."""
# ruff: noqa: BLE001, EM101, PLR2004, TRY003, TRY300

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .model import (
    MAXIMUM_BODY_BYTES,
    MAXIMUM_HEADER_CHARACTERS,
    SESSION_COOKIE,
    BootstrapRequest,
    EmbedSessionError,
    EmbedSessionRequest,
)
from .service import EmbedSessionService

router = APIRouter(prefix="/v1", tags=["Administration"])


def install_embed_session_service(app: FastAPI, service: EmbedSessionService) -> None:
    """Install one explicit embed-session service."""
    state = getattr(app, "state", None)
    if state is None:
        raise TypeError("The application does not have state storage.")
    state.embed_session_service = service


@router.post(
    "/services/{service_id}/administration/embed-sessions", response_model=None
)
async def create_embed_session(request: Request, service_id: str) -> Response:
    """Create one short-lived session from host-backend authority."""
    request_id = str(uuid.uuid4())
    try:
        document = EmbedSessionRequest.model_validate_json(
            await _body(request, request_id)
        )
        result = await asyncio.to_thread(
            _service(request, request_id).create,
            _bearer(request, request_id),
            service_id,
            document,
            request_id=request_id,
        )
        return _json(
            {
                "session_id": result.session_id,
                "bootstrap_token": result.bootstrap_token,
                "frame_url": result.frame_url,
                "expires_at": result.expires_at.isoformat().replace("+00:00", "Z"),
                "message_version": "1",
            },
            status_code=201,
        )
    except Exception as error:
        return _error(error, request_id)


@router.post(
    "/administration/embed-sessions/{session_id}/bootstrap", response_model=None
)
async def redeem_embed_session(request: Request, session_id: str) -> Response:
    """Redeem one one-use bootstrap secret from the exact frame origin."""
    request_id = str(uuid.uuid4())
    try:
        document = BootstrapRequest.model_validate_json(
            await _body(request, request_id)
        )
        result = await asyncio.to_thread(
            _service(request, request_id).redeem,
            session_id,
            document,
            request_origin=_header(request, "origin", request_id),
            request_id=request_id,
        )
        workspace_id = next(iter(result.principal.allowed_workspace_ids), None)
        body: dict[str, object] = {
            "expires_at": result.principal.expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "service_id": result.principal.service_id,
            "permissions": sorted(result.principal.operations),
            "theme": result.theme.model_dump(mode="json"),
        }
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        response = _json(body)
        maximum_age = max(
            0,
            int((result.principal.expires_at - datetime.now(UTC)).total_seconds()),
        )
        response.set_cookie(
            SESSION_COOKIE,
            result.session_token,
            max_age=maximum_age,
            path="/",
            secure=True,
            httponly=True,
            samesite="none",
        )
        return response
    except Exception as error:
        return _error(error, request_id)


@router.delete(
    "/services/{service_id}/administration/embed-sessions/{session_id}",
    response_model=None,
)
async def revoke_embed_session(
    request: Request, service_id: str, session_id: str
) -> Response:
    """Revoke one host-created session."""
    request_id = str(uuid.uuid4())
    try:
        await asyncio.to_thread(
            _service(request, request_id).revoke,
            _bearer(request, request_id),
            service_id,
            session_id,
            request_id=request_id,
        )
        return Response(
            status_code=204,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except Exception as error:
        return _error(error, request_id)


def _service(request: Request, request_id: str) -> EmbedSessionService:
    service = getattr(request.app.state, "embed_session_service", None)
    if not isinstance(service, EmbedSessionService):
        raise EmbedSessionError("temporarily_unavailable", request_id)
    return service


def _bearer(request: Request, request_id: str) -> str:
    value = _header(request, "authorization", request_id)
    if not value.startswith("Bearer ") or len(value) <= 7:
        raise EmbedSessionError("invalid_token", request_id)
    return value[7:]


def _header(request: Request, name: str, request_id: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1 or not values[0] or len(values[0]) > MAXIMUM_HEADER_CHARACTERS:
        raise EmbedSessionError("invalid_request", request_id)
    return values[0]


async def _body(request: Request, request_id: str) -> bytes:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/json":
        raise EmbedSessionError("invalid_request", request_id)
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAXIMUM_BODY_BYTES - len(body):
            raise EmbedSessionError("invalid_request", request_id)
        body.extend(chunk)
    if not body:
        raise EmbedSessionError("invalid_request", request_id)
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


def _error(error: Exception, request_id: str) -> JSONResponse:
    if isinstance(error, EmbedSessionError):
        public = error
    elif isinstance(error, (ValidationError, json.JSONDecodeError, ValueError)):
        public = EmbedSessionError("invalid_request", request_id)
    else:
        public = EmbedSessionError("temporarily_unavailable", request_id)
    return JSONResponse(
        {
            "error": {
                "code": public.code,
                "message": str(public),
                "retryable": public.code == "temporarily_unavailable",
                "request_id": request_id,
            }
        },
        status_code=public.status_code,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
