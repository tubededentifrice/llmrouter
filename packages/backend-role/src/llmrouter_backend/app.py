"""Create the LLM Router web application."""

from __future__ import annotations

import os
from http import HTTPStatus

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

_DATABASE_CONNECT_TIMEOUT_SECONDS = 2


def create_app(*, database_url: str | None = None) -> FastAPI:
    """Create one application with an optional fixed database URL."""
    application = FastAPI(title="LLM Router", version="1.0.0")
    application.state.database_url = database_url

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Report that the web process can serve requests."""
        return {"status": "ok"}

    @application.get("/ready", include_in_schema=False, response_model=None)
    def ready() -> dict[str, str] | JSONResponse:
        """Report readiness only when the clean database base is available."""
        configured_url = application.state.database_url or os.environ.get(
            "LLMROUTER_DATABASE_URL"
        )
        if configured_url is None:
            return _not_ready()
        try:
            with psycopg.connect(
                configured_url,
                connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            ) as connection:
                row = connection.execute(
                    """SELECT to_regnamespace('router') IS NOT NULL
                       AND EXISTS (
                           SELECT 1
                           FROM public.router_schema_migrations
                           WHERE version = 1 AND name = 'foundation'
                       )"""
                ).fetchone()
        except psycopg.Error:
            return _not_ready()
        if row != (True,):
            return _not_ready()
        return {"status": "ready"}

    return application


def _not_ready() -> JSONResponse:
    """Create the closed readiness failure response."""
    return JSONResponse(
        {"status": "not_ready"},
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
    )


app = create_app()
