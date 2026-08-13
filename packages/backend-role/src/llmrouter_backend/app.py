"""Create the placeholder ASGI application."""

from fastapi import FastAPI

app = FastAPI(title="LLM Router", version="0.0.0")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Return the placeholder process health."""
    return {"status": "ok"}
