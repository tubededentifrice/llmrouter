"""Create the backend ASGI application."""

from __future__ import annotations

import os
from base64 import urlsafe_b64decode
from pathlib import Path

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from llmrouter_backend.administration.http import router as administration_router
from llmrouter_backend.embed_sessions import (
    EmbedSessionRepository,
    EmbedSessionService,
    install_embed_session_service,
)
from llmrouter_backend.embed_sessions.http import router as embed_session_router
from llmrouter_backend.machine_identity import MachineCredentialRepository
from llmrouter_backend.model_requests.http import router as model_request_router

_DEPLOYMENT_KEY_BYTES = 32

app = FastAPI(title="LLM Router", version="0.1.0")
app.include_router(model_request_router)
app.include_router(administration_router)
app.include_router(embed_session_router)


def _install_local_embed_service() -> None:
    """Install local embed authority only for an explicit deployment."""
    if os.environ.get("LLMROUTER_LOCAL_RUNTIME") != "1":
        return
    database_url = os.environ.get("LLMROUTER_DATABASE_URL")
    digest_path = os.environ.get("LLMROUTER_MACHINE_DIGEST_KEY_FILE")
    frame_origin = os.environ.get("LLMROUTER_FRAME_ORIGIN")
    if database_url is None or digest_path is None or frame_origin is None:
        message = "The local embed service configuration is incomplete."
        raise RuntimeError(message)
    digest_key = _secret_bytes(Path(digest_path))
    service_id = "0198a080-0000-7000-8000-000000000101"
    machine = MachineCredentialRepository(
        database_url,
        issuer="llmrouter-local-development",
        digest_keys={"local-v1": digest_key},
        current_digest_key_id="local-v1",
    )
    repository = EmbedSessionRepository(
        database_url,
        frame_origin=frame_origin,
        allowed_host_origins={service_id: frozenset({"http://127.0.0.1:5176"})},
    )
    install_embed_session_service(app, EmbedSessionService(machine, repository))


def _secret_bytes(path: Path) -> bytes:
    """Read one generated base64url deployment key without displaying it."""
    if path.is_symlink() or not path.is_file():
        message = "A local secret file is unavailable."
        raise RuntimeError(message)
    value = path.read_text(encoding="ascii").strip()
    try:
        decoded = urlsafe_b64decode(value + "=")
    except ValueError as error:
        message = "A local secret file is invalid."
        raise RuntimeError(message) from error
    if len(decoded) != _DEPLOYMENT_KEY_BYTES:
        message = "A local secret file is invalid."
        raise RuntimeError(message)
    return decoded


_install_local_embed_service()


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Return the process health."""
    return {"status": "ok"}


@app.get("/ready", include_in_schema=False, response_model=None)
def ready() -> dict[str, str] | JSONResponse:
    """Return readiness only when the migrated database is available."""
    database_url = os.environ.get("LLMROUTER_DATABASE_URL")
    if database_url is None:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    try:
        with psycopg.connect(database_url, connect_timeout=2) as connection:
            row = connection.execute("SELECT to_regclass('router.services')").fetchone()
    except psycopg.Error:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    if row != ("router.services",):
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return {"status": "ready"}
