"""Create the backend ASGI application."""

from fastapi import FastAPI

from llmrouter_backend.model_requests.http import router as model_request_router

app = FastAPI(title="LLM Router", version="0.1.0")
app.include_router(model_request_router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Return the process health."""
    return {"status": "ok"}
