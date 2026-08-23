"""Verify the native log contract on localhost without printing controls."""
# ruff: noqa: EM101, INP001, PLR2004, S101, TRY003

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import psycopg
from llmrouter_backend.config import Settings
from llmrouter_backend.diagnostics import (
    CapturedMedia,
    DetailedLogWrite,
    write_detailed_log_best_effort,
)
from llmrouter_backend.models import RequestAttempt
from llmrouter_backend.object_store import ObjectStore
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import create_administrator_session, create_key
from psycopg.rows import dict_row

_ADMIN_ORIGIN = "http://127.0.0.1:5174"
_MODEL_CONTENT = '{"messages":[{"content":"Authorization is valid model text"}]}'
_MEDIA = b"\x89PNG\r\n\x1a\nlocalhost-contract-proof"


def main() -> None:
    """Exercise administrator separation, media reads, and scope deletion."""
    settings = Settings.from_environment()
    controls = ControlKeys.load(settings)
    storage = ObjectStore.from_settings(settings)
    if storage is None:
        raise SystemExit("Object storage is not configured.")
    database_url = _database_url()
    suffix = uuid.uuid4().hex[:10]
    service_name = f"proof-{suffix}"
    workspace_name = "primary"
    session_token = new_token()
    csrf_token = new_token()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        service = connection.execute(
            """INSERT INTO router.services (api_name, display_name)
               VALUES (%s, 'Proof service') RETURNING id""",
            (service_name,),
        ).fetchone()
        assert service is not None
        workspace = connection.execute(
            """INSERT INTO router.workspaces (service_id, api_name, display_name)
               VALUES (%s, %s, 'Proof workspace') RETURNING id""",
            (service["id"], workspace_name),
        ).fetchone()
        assert workspace is not None
        service_key = create_key(
            connection,
            service_id=service["id"],
            name="proof",
            actor_subject="verification:setup",
            control_keys=controls,
        )[1]
        create_administrator_session(
            connection,
            session_verifier=controls.verifier(session_token),
            csrf_verifier=controls.verifier(csrf_token),
            encrypted_csrf_token=controls.encrypt({"csrf_token": csrf_token}),
            issuer="https://verification.invalid",
            subject="local-proof",
            display_name="Local proof",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=10),
        )
    log_id = write_detailed_log_best_effort(
        database_url,
        storage,
        DetailedLogWrite(
            service_id=service["id"],
            workspace_id=workspace["id"],
            assignment_api_name="default",
            provider_model_api_name="fake-model",
            kind="model",
            outcome="succeeded",
            request_json=_MODEL_CONTENT,
            response_json='{"content":"proof response"}',
            attempts=(_attempt(),),
            started_at=datetime.now(tz=UTC),
            media=(CapturedMedia(_MEDIA, "image/png", "input"),),
        ),
    )
    assert log_id is not None
    read_headers = {"Cookie": f"llmrouter_admin_session={session_token}"}
    write_headers = {
        **read_headers,
        "Origin": _ADMIN_ORIGIN,
        "X-CSRF-Token": csrf_token,
    }
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as client:
        denied = client.get(
            "/v1/admin/request-logs",
            headers={"Authorization": f"Bearer {service_key}"},
            params=_time_range(),
        )
        assert denied.status_code == 401
        retention = client.put(
            "/v1/admin/settings/log-retention",
            headers=write_headers,
            json={"duration_days": 7},
        )
        assert retention.status_code == 200
        page = client.get(
            "/v1/admin/request-logs", headers=read_headers, params=_time_range()
        )
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        complete = client.get(f"/v1/admin/request-logs/{log_id}", headers=read_headers)
        assert complete.status_code == 200
        assert complete.json()["request_json"] == _MODEL_CONTENT
        assert "object_key" not in complete.text
        media_id = complete.json()["media"][0]["id"]
        media = client.get(
            f"/v1/admin/request-logs/{log_id}/media/{media_id}/content",
            headers=read_headers,
        )
        assert media.status_code == 200
        assert media.content == _MEDIA
        deleted = client.delete(
            f"/v1/workspaces/{workspace_name}",
            headers={"Authorization": f"Bearer {service_key}"},
        )
        assert deleted.status_code == 204
        assert (
            client.get(
                f"/v1/admin/request-logs/{log_id}", headers=read_headers
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/v1/admin/services/{service_name}", headers=write_headers
            ).status_code
            == 204
        )
    late = write_detailed_log_best_effort(
        database_url,
        storage,
        DetailedLogWrite(
            service_id=service["id"],
            workspace_id=workspace["id"],
            kind="media",
            outcome="succeeded",
            request_json="{}",
            response_json="{}",
            attempts=(_attempt(),),
            started_at=datetime.now(tz=UTC),
            media=(CapturedMedia(b"late", "video/mp4", "output"),),
        ),
    )
    assert late is None
    print("Local detailed-log API proof passed.")


def _attempt() -> RequestAttempt:
    return RequestAttempt.model_validate(
        {
            "provider_model_api_name": "fake-model",
            "outcome": "succeeded",
            "started_at": datetime.now(tz=UTC),
            "usage": {"units": [], "cost": "0", "currency": "USD"},
            "applied_prices": {
                "currency": "USD",
                "unit_prices": [{"unit": "request", "amount": "0"}],
            },
        }
    )


def _database_url() -> str:
    password = (
        Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    )
    return f"postgresql://llmrouter:{quote(password, safe='')}@postgres:5432/llmrouter"


def _time_range() -> dict[str, str]:
    now = datetime.now(tz=UTC)
    return {
        "from": (now - timedelta(hours=1)).isoformat(),
        "to": (now + timedelta(hours=1)).isoformat(),
    }


if __name__ == "__main__":
    main()
