"""Create the backend ASGI application."""

from fastapi import FastAPI

from llmrouter_backend.administration.http import router as administration_router
from llmrouter_backend.embed_sessions.http import router as embed_session_router
from llmrouter_backend.model_requests.http import router as model_request_router

app = FastAPI(title="LLM Router", version="0.1.0")
app.include_router(model_request_router)
app.include_router(administration_router)
app.include_router(embed_session_router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Return the process health."""
    return {"status": "ok"}
