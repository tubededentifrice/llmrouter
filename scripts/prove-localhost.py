"""Prove the simplified Router against the localhost deployment."""
# ruff: noqa: E501, EM101, INP001, PLR0915, PLR2004, S101, TRY003

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast
from urllib.parse import quote

import httpx
import psycopg
from llmrouter_backend.config import Settings
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import create_administrator_session, create_key
from local_development_admin_session import (
    read_development_administrator_session,
)
from opendle import (
    AssignmentSelector,
    ContextLimits,
    ContextMethod,
    ContextPolicy,
    ConversationHarness,
    ConversationState,
    ExactModelSelector,
    HarnessConfig,
    ImageInputPart,
    MediaJobState,
    MediaKind,
    ModelCall,
    RouterClient,
    RouteState,
    SystemMessage,
    TextInputPart,
    ToolDefinition,
    UserMessage,
)
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from uuid import UUID

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STATE_DIRECTORY = REPOSITORY_ROOT / ".local-development"
ROUTER_URL = "http://127.0.0.1:8010"
ADMIN_ORIGIN = "http://127.0.0.1:5174"
_CONTROL_MARKER = "proof-control-must-not-appear"
_BROWSER_PROOF_BOOTSTRAP = r"""
(() => {
  globalThis.__llmrouterProofErrors = [];
  const originalConsoleError = globalThis.console.error.bind(globalThis.console);
  globalThis.console.error = (...values) => {
    globalThis.__llmrouterProofErrors.push(
      values.map((value) => String(value?.stack ?? value)).join(" ")
    );
    originalConsoleError(...values);
  };
  globalThis.addEventListener("error", (event) => {
    globalThis.__llmrouterProofErrors.push(String(event.error?.stack ?? event.message));
  });
  globalThis.addEventListener("unhandledrejection", (event) => {
    globalThis.__llmrouterProofErrors.push(String(event.reason?.stack ?? event.reason));
  });
  const originalFetch = globalThis.fetch.bind(globalThis);
  const listPaths = new Set([
    "/v1/admin/services",
    "/v1/admin/providers",
    "/v1/admin/models",
    "/v1/admin/provider-models",
    "/v1/admin/credentials"
  ]);
  const modeFromLocation = () =>
    globalThis.__llmrouterProofMode ??
    new URL(globalThis.location.href).searchParams.get("proof_mode") ??
    "normal";
  globalThis.__llmrouterProofMode = undefined;
  globalThis.fetch = async (input, init) => {
    const requestUrl =
      typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(requestUrl, globalThis.location.href);
    const mode = modeFromLocation();
    if (mode === "loading" && listPaths.has(url.pathname))
      return new Promise(() => undefined);
    if (mode === "error" && url.pathname === "/v1/admin/providers")
      return new Response(
        JSON.stringify({
          error: { code: "internal_error", message: "Injected proof failure." }
        }),
        {
          status: 503,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
        }
      );
    const response = await originalFetch(input, init);
    if (!response.ok) return response;
    const empty = mode === "empty" && listPaths.has(url.pathname);
    const removeText =
      mode === "remove-text" && url.pathname === "/v1/admin/provider-models";
    if (!empty && !removeText) return response;
    const document = await response.clone().json();
    if (empty) {
      document.items = [];
      if (document.page) document.page.has_more = false;
      if (document.retrieval) document.retrieval.complete = true;
    }
    if (removeText)
      document.items = document.items.filter((item) => item.api_name !== "text");
    const headers = new Headers(response.headers);
    headers.delete("Content-Length");
    headers.delete("Content-Encoding");
    return new Response(JSON.stringify(document), {
      status: response.status,
      statusText: response.statusText,
      headers
    });
  };
})();
"""


def main() -> None:
    """Seed one clean fake deployment and prove its public boundaries."""
    database_url = _database_url()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        controls = ControlKeys.load(_settings())
        alpha_key, child_key, beta_key, expired_admin_session = _seed(
            connection, controls
        )

    administrator = read_development_administrator_session()

    _prove_native_http(
        alpha_key,
        child_key,
        beta_key,
        administrator.cookie_value,
        administrator.csrf_token,
        expired_admin_session,
    )
    _prove_sdk_and_harness(alpha_key)
    _prove_persisted_facts(database_url, alpha_key, child_key, beta_key)
    _prove_hydrated_administration(administrator.cookie_value)
    _prove_administrator_logout(administrator.cookie_value, administrator.csrf_token)
    print("Localhost fake Router proof passed.")


def _settings() -> Settings:
    """Load only the local control-file settings."""
    return Settings(
        administrator_digest_key_file=STATE_DIRECTORY / "administrator-digest-key",
        administrator_encryption_key_file=(
            STATE_DIRECTORY / "administrator-encryption-key"
        ),
        allowed_origins=(ADMIN_ORIGIN,),
    )


def _database_url() -> str:
    """Build the loopback database URL without displaying its control value."""
    password = (
        (STATE_DIRECTORY / "postgres-password").read_text(encoding="utf-8").strip()
    )
    return f"postgresql://llmrouter:{quote(password, safe='')}@127.0.0.1:5434/llmrouter"


def _seed(
    connection: psycopg.Connection[dict[str, object]], controls: ControlKeys
) -> tuple[str, str, str, str]:
    """Create one deterministic service tree and fake-only route catalog."""
    existing = connection.execute(
        "SELECT count(*) AS count FROM router.services"
    ).fetchone()
    if existing is None or existing["count"] != 0:
        raise SystemExit(
            "The localhost proof requires the approved clean database reset."
        )

    services: dict[str, UUID] = {}
    for api_name, parent in (
        ("alpha", None),
        ("alpha-child", "alpha"),
        ("beta", None),
    ):
        parent_id = None if parent is None else services[parent]
        row = connection.execute(
            """INSERT INTO router.services
                   (api_name, display_name, parent_service_id)
               VALUES (%s, %s, %s) RETURNING id""",
            (api_name, api_name.replace("-", " ").title(), parent_id),
        ).fetchone()
        assert row is not None
        services[api_name] = cast("UUID", row["id"])
        connection.execute(
            """INSERT INTO router.workspaces
                   (service_id, api_name, display_name)
               VALUES (%s, 'main', 'Main workspace')""",
            (row["id"],),
        )
        connection.execute(
            """INSERT INTO router.workspaces
                   (service_id, api_name, display_name)
               VALUES (%s, %s, %s)""",
            (
                row["id"],
                f"{api_name}-private",
                f"{api_name.replace('-', ' ').title()} private",
            ),
        )

    alpha_key = create_key(
        connection,
        service_id=services["alpha"],
        name="localhost proof",
        actor_subject="proof:setup",
        control_keys=controls,
    )[1]
    child_key = create_key(
        connection,
        service_id=services["alpha-child"],
        name="localhost proof",
        actor_subject="proof:setup",
        control_keys=controls,
    )[1]
    beta_key = create_key(
        connection,
        service_id=services["beta"],
        name="localhost proof",
        actor_subject="proof:setup",
        control_keys=controls,
    )[1]
    _seed_fake_catalog(connection, services["alpha"])
    _seed_child_assignment(connection, services["alpha-child"])
    connection.execute(
        """INSERT INTO router.activity_events
               (actor_subject, action, resource_type, resource_api_name, result)
           SELECT 'proof:setup', 'prove incremental activity', 'proof-fixture',
                  'proof-event-' || series::text, 'succeeded'
           FROM generate_series(1, 205) AS series"""
    )

    expired_admin_session = new_token()
    expired_csrf = new_token()
    create_administrator_session(
        connection,
        session_verifier=controls.verifier(expired_admin_session),
        csrf_verifier=controls.verifier(expired_csrf),
        encrypted_csrf_token=controls.encrypt({"csrf_token": expired_csrf}),
        issuer="https://local-development.invalid",
        subject="expired-localhost-proof-administrator",
        display_name="Expired localhost proof administrator",
        expires_at=datetime.now(tz=UTC) - timedelta(seconds=1),
    )

    connection.commit()
    return alpha_key, child_key, beta_key, expired_admin_session


def _price(*units: str) -> str:
    value = {
        "currency": "USD",
        "unit_prices": [{"unit": unit, "amount": "0.01"} for unit in units],
        "source": "localhost-fake",
    }
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _seed_fake_catalog(
    connection: psycopg.Connection[dict[str, object]], alpha_id: UUID
) -> None:
    """Insert all fake operation shapes with complete price snapshots."""
    connection.execute(
        """INSERT INTO router.provider_connections
               (api_name, display_name, adapter, enabled)
           VALUES ('fake-provider', 'Fake provider', 'fake', true)"""
    )
    model_rows: tuple[
        tuple[
            str,
            list[str],
            list[str],
            list[str],
            dict[str, int | list[int]],
            str,
        ],
        ...,
    ] = (
        (
            "text-model",
            ["text", "image"],
            ["text", "structured_json"],
            ["tool_calling", "streaming", "reasoning"],
            {"max_input_images": 8, "max_input_image_bytes": 20 * 1024 * 1024},
            _price(
                "input_token",
                "output_token",
                "cached_input_token",
                "request",
                "provider_unit",
            ),
        ),
        (
            "embedding-model",
            ["text"],
            ["embedding"],
            [],
            {"embedding_dimensions": [3]},
            _price("input_token", "request", "provider_unit"),
        ),
        (
            "media-model",
            ["text", "image"],
            ["image", "video", "audio"],
            [],
            {
                "max_input_images": 8,
                "max_input_image_bytes": 20 * 1024 * 1024,
                "max_output_duration_seconds": 60,
            },
            _price(
                "image",
                "video_second",
                "audio_second",
                "request",
                "provider_unit",
            ),
        ),
    )
    for name, inputs, outputs, capabilities, constraints, price in model_rows:
        connection.execute(
            """INSERT INTO router.canonical_models
                   (api_name, display_name, input_modalities, output_modalities,
                    capabilities, constraints, manual_price)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)""",
            (
                name,
                (
                    "Text model with a deliberately long name for responsive proof"
                    if name == "text-model"
                    else name.replace("-", " ").title()
                ),
                inputs,
                outputs,
                capabilities,
                json.dumps(constraints),
                price,
            ),
        )

    routes = (
        ("failed-text", "text-model", "fake-error-transport-v1"),
        ("text", "text-model", "fake-text-v1"),
        ("interrupted-text", "text-model", "fake-stream-interruption-v1"),
        ("embedding", "embedding-model", "fake-embedding-v1"),
        ("media", "media-model", "fake-media-v1"),
    )
    for api_name, model_name, wire_name in routes:
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name,
                    enabled, input_modalities, output_modalities, capabilities,
                    constraints, reasoning_mappings)
               SELECT %s, provider.id, model.id, %s, true,
                      model.input_modalities, model.output_modalities,
                      model.capabilities, model.constraints,
                      CASE WHEN %s = 'text-model'
                        THEN '[{"level":"none","provider_value":"none"},
                               {"level":"medium","provider_value":"medium"}]'::jsonb
                        ELSE '[]'::jsonb END
               FROM router.provider_connections AS provider,
                    router.canonical_models AS model
               WHERE provider.api_name = 'fake-provider'
                 AND model.api_name = %s""",
            (api_name, wire_name, model_name, model_name),
        )

    assignments = (
        ("default", ("text",)),
        ("workflow", ("failed-text", "text")),
        ("interrupt", ("interrupted-text", "text")),
        ("embedding", ("embedding",)),
        ("image", ("media",)),
        ("video", ("media",)),
        ("audio", ("media",)),
    )
    for name, candidates in assignments:
        row = connection.execute(
            """INSERT INTO router.assignment_definitions
                   (service_id, api_name, display_name, reasoning_level)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (
                alpha_id,
                name,
                name.title(),
                "none" if name in {"default", "workflow", "interrupt"} else None,
            ),
        ).fetchone()
        assert row is not None
        for position, candidate in enumerate(candidates):
            connection.execute(
                """INSERT INTO router.assignment_candidates
                       (assignment_id, position, provider_model_id)
                   SELECT %s, %s, id FROM router.provider_models
                   WHERE api_name = %s""",
                (row["id"], position, candidate),
            )


def _seed_child_assignment(
    connection: psycopg.Connection[dict[str, object]], child_id: UUID
) -> None:
    """Replace one inherited fallback chain at the nearest service."""
    row = connection.execute(
        """INSERT INTO router.assignment_definitions
               (service_id, api_name, display_name, reasoning_level)
           VALUES (%s, 'workflow', 'Child workflow', 'none') RETURNING id""",
        (child_id,),
    ).fetchone()
    assert row is not None
    connection.execute(
        """INSERT INTO router.assignment_candidates
               (assignment_id, position, provider_model_id)
           SELECT %s, 0, id FROM router.provider_models WHERE api_name = 'text'""",
        (row["id"],),
    )


def _prove_native_http(  # noqa: PLR0913, PLR0917
    alpha_key: str,
    child_key: str,
    beta_key: str,
    admin_session: str,
    csrf: str,
    expired_admin_session: str,
) -> None:
    """Prove service scope, fake calls, safe errors, and OIDC denial."""
    alpha_headers = {"Authorization": f"Bearer {alpha_key}"}
    child_headers = {"Authorization": f"Bearer {child_key}"}
    beta_headers = {"Authorization": f"Bearer {beta_key}"}
    with httpx.Client(base_url=ROUTER_URL, timeout=20, trust_env=False) as client:
        assert client.get("/ready").status_code == 200
        alpha_workspaces = client.get("/v1/workspaces", headers=alpha_headers)
        beta_workspaces = client.get("/v1/workspaces", headers=beta_headers)
        assert alpha_workspaces.status_code == 200, alpha_workspaces.status_code
        assert beta_workspaces.status_code == 200, beta_workspaces.status_code
        assert {item["api_name"] for item in alpha_workspaces.json()["items"]} == {
            "alpha-private",
            "main",
        }
        assert {item["api_name"] for item in beta_workspaces.json()["items"]} == {
            "beta-private",
            "main",
        }
        foreign = client.get("/v1/workspaces/alpha-private", headers=beta_headers)
        assert foreign.status_code == 404
        denied_admin = client.get("/v1/admin/services", headers=alpha_headers)
        assert denied_admin.status_code == 401
        denied_service = client.get(
            "/v1/workspaces",
            headers={"Cookie": f"llmrouter_admin_session={admin_session}"},
        )
        assert denied_service.status_code == 401
        active_admin = client.get(
            "/v1/admin/session",
            headers={"Cookie": f"llmrouter_admin_session={admin_session}"},
        )
        assert active_admin.status_code == 200, active_admin.status_code
        active_expiry = active_admin.json()["expires_at"]
        repeated_admin = client.get(
            "/v1/admin/session",
            headers={"Cookie": f"llmrouter_admin_session={admin_session}"},
        )
        assert repeated_admin.status_code == 200
        assert repeated_admin.json()["expires_at"] == active_expiry
        expired_admin = client.get(
            "/v1/admin/session",
            headers={"Cookie": f"llmrouter_admin_session={expired_admin_session}"},
        )
        assert expired_admin.status_code == 401
        assert expired_admin.json()["error"]["code"] == "authentication_required"
        unsafe_return = client.post(
            "/v1/admin/session/start", json={"return_to": "https://outside.invalid"}
        )
        assert unsafe_return.status_code == 400
        callback = client.get(
            "/v1/admin/oidc/callback", params={"code": "invalid", "state": "invalid"}
        )
        assert callback.status_code == 401

        inherited = client.post(
            "/v1/model-calls",
            headers=child_headers,
            json=_model_body("default"),
        )
        assert inherited.status_code == 200, inherited.status_code
        assert inherited.json()["provider_model_api_name"] == "text"
        replaced = client.post(
            "/v1/model-calls",
            headers=child_headers,
            json=_model_body("workflow"),
        )
        assert replaced.status_code == 200, replaced.status_code
        assert replaced.json()["provider_model_api_name"] == "text"

        model = _model_body("workflow", tags=["proof", "proof", "scope:alpha"])
        response = client.post("/v1/model-calls", headers=alpha_headers, json=model)
        assert response.status_code == 200, response.status_code
        result = response.json()
        assert result["provider_model_api_name"] == "text"
        assert result["content"] == [{"type": "text", "text": "Fake response."}]

        admin_headers = {
            "Cookie": f"llmrouter_admin_session={admin_session}",
            "Origin": ADMIN_ORIGIN,
            "X-CSRF-Token": csrf,
        }
        admin_read_headers = {"Cookie": f"llmrouter_admin_session={admin_session}"}
        administrator_model = _administrator_model_body(
            assignment="workflow", service="alpha", tags=["administrator", "proof"]
        )
        administrator_result = client.post(
            "/v1/admin/playground/model-calls",
            headers=admin_headers,
            json=administrator_model,
        )

        exact_failure_body = _model_body("workflow")
        exact_failure_body["selector"] = {"provider_model_api_name": "failed-text"}
        exact_failure = client.post(
            "/v1/model-calls", headers=alpha_headers, json=exact_failure_body
        )
        assert exact_failure.status_code == 502
        assert exact_failure.json()["error"]["code"] == "upstream_failed"

        exact_body = _model_body("workflow", tags=["exact", "proof"])
        exact_body["selector"] = {"provider_model_api_name": "text"}
        exact = client.post("/v1/model-calls", headers=alpha_headers, json=exact_body)
        assert exact.status_code == 200
        assert exact.json()["provider_model_api_name"] == "text"

        foreign_model_body = _model_body("workflow")
        foreign_model_body["workspace_api_name"] = "alpha-private"
        foreign_model_body["selector"] = {"provider_model_api_name": "text"}
        foreign_model = client.post(
            "/v1/model-calls", headers=beta_headers, json=foreign_model_body
        )
        assert foreign_model.status_code == 404

        foreign_embedding = client.post(
            "/v1/embeddings",
            headers=beta_headers,
            json={
                "workspace_api_name": "alpha-private",
                "selector": {"provider_model_api_name": "embedding"},
                "inputs": ["foreign"],
            },
        )
        assert foreign_embedding.status_code == 404

        foreign_media = client.post(
            "/v1/media-jobs",
            headers=beta_headers,
            json={
                "workspace_api_name": "alpha-private",
                "selector": {"provider_model_api_name": "media"},
                "kind": "image",
                "prompt": "This foreign request must fail.",
            },
        )
        assert foreign_media.status_code == 404

        tool_body = _model_body("workflow")
        tool_body["tools"] = [
            {
                "name": "lookup",
                "description": "Return one fake result.",
                "input_schema_json": '{"type":"object"}',
            }
        ]
        tool = client.post("/v1/model-calls", headers=alpha_headers, json=tool_body)
        assert tool.status_code == 200
        assert tool.json()["content"][0]["type"] == "tool_call"

        structured = _model_body("workflow")
        structured["output_format"] = {
            "type": "json_schema",
            "schema_json": (
                '{"type":"object","properties":{"result":{"const":"fake"}},'
                '"required":["result"],"additionalProperties":false}'
            ),
        }
        structured_result = client.post(
            "/v1/model-calls", headers=alpha_headers, json=structured
        )
        assert structured_result.status_code == 200
        assert structured_result.json()["structured_output_json"] == '{"result":"fake"}'

        image_body = _model_body("workflow")
        image_body["messages"] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect it."},
                    {
                        "type": "image",
                        "media_type": "image/png",
                        "data_base64": "iVBORw0KGgo=",
                    },
                ],
            }
        ]
        image_input = client.post(
            "/v1/model-calls", headers=alpha_headers, json=image_body
        )
        assert image_input.status_code == 200, image_input.status_code

        stream = client.post(
            "/v1/model-streams",
            headers={**alpha_headers, "Accept": "text/event-stream"},
            json=_model_body("workflow"),
        )
        assert stream.status_code == 200
        assert "event: completed" in stream.text
        interrupted = client.post(
            "/v1/model-streams",
            headers={**alpha_headers, "Accept": "text/event-stream"},
            json=_model_body("interrupt"),
        )
        assert interrupted.status_code == 200
        assert "Fake visible output." in interrupted.text
        assert "event: error" in interrupted.text

        for denied_headers in (
            admin_read_headers,
            {**admin_headers, "Origin": f"{ADMIN_ORIGIN}/"},
            {**admin_headers, "X-CSRF-Token": new_token()},
        ):
            denied_playground = client.post(
                "/v1/admin/playground/model-calls",
                headers=denied_headers,
                json=administrator_model,
            )
            assert denied_playground.status_code == 403
            assert denied_playground.headers["cache-control"] == "no-store"
        denied_playground_key = client.post(
            "/v1/admin/playground/model-calls",
            headers=alpha_headers,
            json=_administrator_model_body(provider_model="text"),
        )
        assert denied_playground_key.status_code == 401

        assert administrator_result.status_code == 200
        assert administrator_result.headers["cache-control"] == "no-store"
        administrator_document = administrator_result.json()
        assert administrator_document["selector"] == {
            "assignment_api_name": "workflow",
            "service_api_name": "alpha",
        }
        assert [
            item["provider_model_api_name"]
            for item in administrator_document["attempts"]
        ] == ["failed-text", "text"]
        assert [item["outcome"] for item in administrator_document["attempts"]] == [
            "failed",
            "succeeded",
        ]
        assert administrator_document["result"]["provider_model_api_name"] == "text"
        assert administrator_document["result"]["usage"]["cost"] != "0"

        administrator_exact = client.post(
            "/v1/admin/playground/model-calls",
            headers=admin_headers,
            json=_administrator_model_body(
                provider_model="text",
                text=(
                    "<script>globalThis.__llmrouterProofExecuted = true</script>"
                    + "L" * 4_096
                ),
                tags=["proof", "ui-long-content"],
            ),
        )
        assert administrator_exact.status_code == 200
        administrator_exact_document = administrator_exact.json()
        assert administrator_exact_document["selector"] == {
            "provider_model_api_name": "text"
        }
        assert len(administrator_exact_document["attempts"]) == 1
        assert (
            administrator_exact_document["result"]["provider_model_api_name"] == "text"
        )

        administrator_structured_body = _administrator_model_body(
            provider_model="text", tags=["administrator", "proof"]
        )
        administrator_structured_body["output_format"] = {
            "type": "json_schema",
            "schema_json": (
                '{"type":"object","properties":{"result":{"const":"fake"}},'
                '"required":["result"],"additionalProperties":false}'
            ),
        }
        administrator_structured = client.post(
            "/v1/admin/playground/model-calls",
            headers=admin_headers,
            json=administrator_structured_body,
        )
        assert administrator_structured.status_code == 200
        assert (
            administrator_structured.json()["result"]["structured_output_json"]
            == '{"result":"fake"}'
        )

        administrator_tool_body = _administrator_model_body(provider_model="text")
        administrator_tool_body["tools"] = [
            {
                "name": "lookup",
                "description": "Return one fake result.",
                "input_schema_json": '{"type":"object"}',
            }
        ]
        administrator_tool = client.post(
            "/v1/admin/playground/model-calls",
            headers=admin_headers,
            json=administrator_tool_body,
        )
        assert administrator_tool.status_code == 200
        assert administrator_tool.json()["result"]["content"][0]["type"] == "tool_call"

        administrator_image_body = _administrator_model_body(provider_model="text")
        administrator_image_body["messages"] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this proof image."},
                    {
                        "type": "image",
                        "media_type": "image/png",
                        "data_base64": "iVBORw0KGgo=",
                    },
                ],
            }
        ]
        administrator_image = client.post(
            "/v1/admin/playground/model-calls",
            headers=admin_headers,
            json=administrator_image_body,
        )
        assert administrator_image.status_code == 200

        administrator_stream = client.post(
            "/v1/admin/playground/model-streams",
            headers={**admin_headers, "Accept": "text/event-stream"},
            json=administrator_model,
        )
        assert administrator_stream.status_code == 200
        assert administrator_stream.headers["cache-control"] == "no-store"
        administrator_events = _stream_events(administrator_stream.text)
        assert administrator_events[0][0] == "start"
        assert administrator_events[-1][0] == "completed"
        assert (
            administrator_events[0][1]["logical_call_id"]
            == administrator_events[-1][1]["logical_call_id"]
            == administrator_stream.headers["x-llmrouter-logical-call-id"]
        )
        administrator_attempts = administrator_events[-1][1]["attempts"]
        assert isinstance(administrator_attempts, list)
        assert all(isinstance(item, dict) for item in administrator_attempts)
        assert [
            item["outcome"]
            for item in cast("list[dict[str, object]]", administrator_attempts)
        ] == ["succeeded"]

        administrator_embedding = client.post(
            "/v1/admin/playground/embeddings",
            headers=admin_headers,
            json={
                "selector": {"provider_model_api_name": "embedding"},
                "inputs": ["one", "two"],
                "tags": ["proof", "administrator"],
            },
        )
        assert administrator_embedding.status_code == 200
        assert administrator_embedding.headers["cache-control"] == "no-store"
        assert [
            item["index"]
            for item in administrator_embedding.json()["result"]["embeddings"]
        ] == [0, 1]
        assert (
            administrator_embedding.json()["result"]["provider_model_api_name"]
            == "embedding"
        )

        administrator_media_ids: list[str] = []
        for kind in ("image", "video", "audio"):
            administrator_media = client.post(
                "/v1/admin/playground/media-jobs",
                headers=admin_headers,
                json={
                    "selector": {"provider_model_api_name": "media"},
                    "kind": kind,
                    "prompt": f"Create one administrator {kind} proof.",
                    "tags": ["proof", "administrator"],
                },
            )
            assert administrator_media.status_code == 202
            assert administrator_media.headers["cache-control"] == "no-store"
            administrator_job = _wait_administrator_media(
                client, admin_read_headers, administrator_media.json()["id"]
            )
            administrator_media_ids.append(str(administrator_job["id"]))
            assert administrator_job["state"] == "succeeded"
            assert administrator_job["provider_model_api_name"] == "media"
            administrator_attempts = administrator_job["attempts"]
            assert isinstance(administrator_attempts, list)
            assert administrator_attempts
            final_administrator_attempt = administrator_attempts[-1]
            assert isinstance(final_administrator_attempt, dict)
            assert final_administrator_attempt["outcome"] == "succeeded"
            administrator_content = client.get(
                f"/v1/admin/playground/media-jobs/{administrator_job['id']}/content",
                headers=admin_read_headers,
            )
            assert administrator_content.status_code == 200
            assert administrator_content.headers["cache-control"] == "no-store"
            assert administrator_content.content == b"fake-media-bytes"
            hidden_administrator_job = client.get(
                f"/v1/media-jobs/{administrator_job['id']}", headers=alpha_headers
            )
            assert hidden_administrator_job.status_code == 404

        admin_statistics = client.get(
            "/v1/admin/statistics",
            headers=admin_read_headers,
            params=[
                (
                    "from",
                    (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
                ),
                ("to", (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()),
                ("call_actor", "administrator"),
                ("tag", "proof"),
                ("group_by", "call_actor"),
                ("group_by", "configuration_service"),
                ("group_by", "assignment"),
            ],
        )
        assert admin_statistics.status_code == 200
        assert any(
            bucket["dimensions"] == ["administrator", "alpha", "workflow"]
            for bucket in admin_statistics.json()["buckets"]
        )

        administrator_logs = client.get(
            "/v1/admin/request-logs",
            headers=admin_read_headers,
            params={
                "from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
                "to": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
                "call_actor": "administrator",
            },
        )
        assert administrator_logs.status_code == 200
        administrator_log_items = administrator_logs.json()["items"]
        assert any(
            item.get("configuration_service_api_name") == "alpha"
            and item.get("assignment_api_name") == "workflow"
            and "service_api_name" not in item
            and "workspace_api_name" not in item
            for item in administrator_log_items
        )
        assert any(
            item.get("provider_model_api_name") == "media"
            for item in administrator_log_items
        )
        assert all(media_id for media_id in administrator_media_ids)

        embedding = client.post(
            "/v1/embeddings",
            headers=alpha_headers,
            json={
                "workspace_api_name": "main",
                "selector": {"assignment_api_name": "embedding"},
                "inputs": ["one", "two"],
                "tags": ["proof"],
            },
        )
        assert embedding.status_code == 200
        assert [item["index"] for item in embedding.json()["embeddings"]] == [0, 1]
        too_many = client.post(
            "/v1/embeddings",
            headers=alpha_headers,
            json={
                "workspace_api_name": "main",
                "selector": {"assignment_api_name": "embedding"},
                "inputs": ["x"] * 33,
            },
        )
        assert too_many.status_code in {400, 422}

        for kind in ("image", "video", "audio"):
            created = client.post(
                "/v1/media-jobs",
                headers=alpha_headers,
                json={
                    "workspace_api_name": "main",
                    "selector": {"assignment_api_name": kind},
                    "kind": kind,
                    "prompt": f"Create one fake {kind}.",
                    "tags": ["proof"],
                },
            )
            assert created.status_code == 202, created.status_code
            job = _wait_media(client, alpha_headers, created.json()["id"])
            assert job["state"] == "succeeded"
            content = client.get(
                f"/v1/media-jobs/{job['id']}/content", headers=alpha_headers
            )
            assert content.status_code == 200
            assert content.content == b"fake-media-bytes"
            hidden_job = client.get(f"/v1/media-jobs/{job['id']}", headers=beta_headers)
            assert hidden_job.status_code == 404
            hidden_content = client.get(
                f"/v1/media-jobs/{job['id']}/content", headers=beta_headers
            )
            assert hidden_content.status_code == 404

        statistics = client.get(
            "/v1/statistics",
            headers=alpha_headers,
            params={
                "from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
                "to": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
                "tag": "proof",
                "group_by": ["assignment", "provider_model", "outcome", "tag"],
            },
        )
        assert statistics.status_code == 200, statistics.status_code
        buckets = statistics.json()["buckets"]
        assert buckets
        assert any(bucket["dimensions"][0] == "(exact)" for bucket in buckets)
        foreign_statistics = client.get(
            "/v1/statistics",
            headers=beta_headers,
            params={
                "from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
                "to": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
                "tag": "proof",
                "group_by": ["assignment"],
            },
        )
        assert foreign_statistics.status_code == 200
        assert foreign_statistics.json()["buckets"] == []

        serialized = (
            f"{response.text}\n{tool.text}\n{structured_result.text}\n"
            f"{interrupted.text}\n{administrator_result.text}\n"
            f"{administrator_stream.text}\n{administrator_embedding.text}\n"
            f"{administrator_logs.text}"
        )
        assert alpha_key not in serialized
        assert child_key not in serialized
        assert beta_key not in serialized
        assert admin_session not in serialized
        assert csrf not in serialized
        assert expired_admin_session not in serialized
        assert _CONTROL_MARKER not in serialized

        deleted = client.delete(
            "/v1/admin/services/beta",
            headers={
                "Cookie": f"llmrouter_admin_session={admin_session}",
                "Origin": ADMIN_ORIGIN,
                "X-CSRF-Token": csrf,
            },
        )
        assert deleted.status_code == 204, deleted.status_code
        deleted_scope = client.get("/v1/workspaces", headers=beta_headers)
        assert deleted_scope.status_code == 401, deleted_scope.status_code


def _model_body(assignment: str, *, tags: list[str] | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "workspace_api_name": "main",
        "selector": {"assignment_api_name": assignment},
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Run the localhost proof."}],
            }
        ],
        "output_limit": 128,
        "temperature": 0,
    }
    if tags is not None:
        body["tags"] = tags
    return body


def _administrator_model_body(
    *,
    assignment: str | None = None,
    service: str | None = None,
    provider_model: str | None = None,
    text: str = "Run the administrator localhost proof.",
    tags: list[str] | None = None,
) -> dict[str, object]:
    """Build one closed administrator playground model request."""
    if assignment is not None:
        assert service is not None
        assert provider_model is None
        selector: dict[str, str] = {
            "assignment_api_name": assignment,
            "service_api_name": service,
        }
    else:
        assert service is None
        assert provider_model is not None
        selector = {"provider_model_api_name": provider_model}
    body: dict[str, object] = {
        "selector": selector,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            }
        ],
        "output_limit": 128,
        "temperature": 0,
    }
    if tags is not None:
        body["tags"] = tags
    return body


def _stream_events(value: str) -> list[tuple[str, dict[str, object]]]:
    """Parse one strict native event stream for proof assertions."""
    result: list[tuple[str, dict[str, object]]] = []
    for block in value.strip().split("\n\n"):
        lines = block.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        document = json.loads(lines[1][6:])
        assert isinstance(document, dict)
        result.append((lines[0][7:], document))
    return result


def _wait_media(
    client: httpx.Client, headers: dict[str, str], job_id: str
) -> dict[str, object]:
    for _attempt in range(100):
        response = client.get(f"/v1/media-jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        value = response.json()
        if value["state"] in {"succeeded", "failed"}:
            return cast("dict[str, object]", value)
        time.sleep(0.05)
    raise AssertionError("The fake media job did not finish.")


def _wait_administrator_media(
    client: httpx.Client, headers: dict[str, str], job_id: str
) -> dict[str, object]:
    """Wait for one global administrator fake media result."""
    for _attempt in range(100):
        response = client.get(
            f"/v1/admin/playground/media-jobs/{job_id}", headers=headers
        )
        assert response.status_code == 200
        value = response.json()
        if value["state"] in {"succeeded", "failed"}:
            return cast("dict[str, object]", value)
        time.sleep(0.05)
    raise AssertionError("The administrator fake media job did not finish.")


def _prove_sdk_and_harness(alpha_key: str) -> None:
    """Use the installed OpenDLE SDK and stateless harness against the Router."""
    client = RouterClient(base_url=ROUTER_URL, service_key=alpha_key, timeout=20)
    assert {item.api_name for item in client.list_workspaces(limit=10).items} == {
        "alpha-private",
        "main",
    }
    call = ModelCall(
        "main",
        AssignmentSelector("workflow"),
        (
            SystemMessage("Use only the fake Router."),
            UserMessage(
                (
                    TextInputPart("Inspect the image."),
                    ImageInputPart("image/png", b"\x89PNG\r\n\x1a\n"),
                )
            ),
        ),
        tags=("proof", "sdk"),
    )
    result = client.model_call(call)
    assert result.route.provider_model_api_name == "text"
    assert result.usage.cost != "0"

    exact = client.model_call(
        ModelCall(
            "main",
            ExactModelSelector("text"),
            (UserMessage((TextInputPart("Run one exact SDK call."),)),),
            tags=("exact", "proof", "sdk"),
        )
    )
    assert exact.route == ExactModelSelector("text")

    stream = tuple(client.stream_model(call))
    assert len(stream) >= 3
    embedding = client.create_embedding(
        "main", AssignmentSelector("embedding"), ("one", "two"), tags=("proof",)
    )
    assert [item.index for item in embedding.embeddings] == [0, 1]
    job = client.create_media_job(
        "main",
        AssignmentSelector("image"),
        MediaKind.IMAGE,
        "Create one fake image.",
        tags=("proof",),
    )
    terminal = client.wait_media_job(job.id, timeout=10, poll_interval=0.05)
    assert terminal.state is MediaJobState.SUCCEEDED
    assert client.get_media_job_content(job.id).data == b"fake-media-bytes"

    harness = ConversationHarness(
        model_caller=client,
        tools=(),
        config=HarnessConfig(
            workspace_api_name="main",
            assignment_api_name="workflow",
            context=ContextPolicy(ContextMethod.PRUNE, ContextLimits(20, 50_000)),
            tags=("harness", "proof"),
        ),
    )
    state = ConversationState(
        messages=(UserMessage((TextInputPart("Run one harness turn."),)),),
        route=RouteState(),
    )
    updated = asyncio.run(harness.run(state))
    assert updated is not state
    assert len(updated.messages) == 2
    assert updated.route.sticky is not None

    # Construct one SDK tool definition to keep its strict native shape in proof.
    ToolDefinition("lookup", "One caller-owned tool.", '{"type":"object"}')


def _prove_persisted_facts(
    database_url: str, alpha_key: str, child_key: str, beta_key: str
) -> None:
    """Check fallback boundaries, normalized tags, price snapshots, and logs."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        attempts = connection.execute(
            """SELECT services.api_name AS service_api_name,
                      logs.assignment_api_name, logs.attempts
               FROM router.request_logs AS logs
               JOIN router.services AS services ON services.id = logs.service_id
               WHERE kind = 'model'
               ORDER BY logs.started_at"""
        ).fetchall()
        workflow = next(
            row
            for row in attempts
            if row["service_api_name"] == "alpha"
            and row["assignment_api_name"] == "workflow"
        )
        assert [item["outcome"] for item in workflow["attempts"]] == [
            "failed",
            "succeeded",
        ]
        inherited = next(
            row
            for row in attempts
            if row["service_api_name"] == "alpha-child"
            and row["assignment_api_name"] == "default"
        )
        assert [item["provider_model_api_name"] for item in inherited["attempts"]] == [
            "text"
        ]
        replaced = next(
            row
            for row in attempts
            if row["service_api_name"] == "alpha-child"
            and row["assignment_api_name"] == "workflow"
        )
        assert [item["provider_model_api_name"] for item in replaced["attempts"]] == [
            "text"
        ]
        interrupted = next(
            row for row in attempts if row["assignment_api_name"] == "interrupt"
        )
        assert len(interrupted["attempts"]) == 1

        accounting = connection.execute(
            """SELECT tags, count(*) AS calls
               FROM router.raw_accounting_calls
               WHERE assignment_api_name = 'workflow'
               GROUP BY tags"""
        ).fetchall()
        assert any(row["tags"] == ["proof", "scope:alpha"] for row in accounting)
        priced = connection.execute(
            """SELECT count(*) AS count
               FROM router.raw_accounting_attempts
               WHERE applied_price ? 'unit_prices' AND cost > 0"""
        ).fetchone()
        assert priced is not None
        assert priced["count"] > 0

        log_text = "\n".join(
            row["request_json"] + (row["response_json"] or "")
            for row in connection.execute(
                "SELECT request_json, response_json FROM router.request_logs"
            ).fetchall()
        )
        assert "Run the localhost proof." in log_text
        for control in (alpha_key, child_key, beta_key, _CONTROL_MARKER):
            assert control not in log_text


def _prove_hydrated_administration(admin_session: str) -> None:
    """Prove the complete graph-first administrator UI at two widths."""
    chrome = Path("/usr/bin/google-chrome")
    if not chrome.is_file():
        raise SystemExit("Google Chrome is required for the hydrated UI proof.")
    port = _unused_port()
    profile = Path(tempfile.mkdtemp(prefix="llmrouter-proof-chrome-"))
    try:
        process = subprocess.Popen(  # noqa: S603 - Fixed Chrome and loopback inputs.
            [
                chrome,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--remote-allow-origins=http://127.0.0.1:{port}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            endpoint = _debugging_endpoint(port)
            with _Cdp(endpoint) as browser:
                browser.command("Network.enable")
                browser.command("Page.enable")
                browser.command("Accessibility.enable")
                browser.command(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": _BROWSER_PROOF_BOOTSTRAP},
                )
                cookie = browser.command(
                    "Network.setCookie",
                    {
                        "name": "llmrouter_admin_session",
                        "value": admin_session,
                        "url": ADMIN_ORIGIN,
                        "path": "/",
                        # The real callback Secure flag is covered by the identity
                        # suite. This temporary loopback fixture does not weaken it.
                        "secure": False,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    },
                )
                assert cookie.get("success") is True
                _prove_viewport(browser, width=1440, mobile=False)
                _prove_viewport(browser, width=390, mobile=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        _remove_chrome_profile(profile)


def _prove_administrator_logout(admin_session: str, csrf: str) -> None:
    """Revoke the exact localhost proof session and reject its later use."""
    headers = {
        "Cookie": f"llmrouter_admin_session={admin_session}",
        "Origin": ADMIN_ORIGIN,
        "X-CSRF-Token": csrf,
    }
    with httpx.Client(base_url=ROUTER_URL, timeout=10, trust_env=False) as client:
        logged_out = client.delete("/v1/admin/session", headers=headers)
        assert logged_out.status_code == 204
        assert "Max-Age=0" in logged_out.headers["set-cookie"]
        rejected = client.get(
            "/v1/admin/session",
            headers={"Cookie": f"llmrouter_admin_session={admin_session}"},
        )
        assert rejected.status_code == 401, rejected.status_code
        assert rejected.json()["error"]["code"] == "authentication_required"


def _remove_chrome_profile(profile: Path) -> None:
    """Remove a Chrome profile after its child processes stop writing."""
    for _attempt in range(100):
        try:
            shutil.rmtree(profile)
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05)
        else:
            return
    raise AssertionError("The temporary Chrome profile did not become removable.")


def _wait_browser(
    browser: _Cdp,
    expression: str,
    message: str,
    *,
    attempts: int = 200,
) -> object:
    """Wait for one fixed browser condition without hiding its final value."""
    last_value: object = None
    for _attempt in range(attempts):
        last_value = browser.evaluate(expression)
        if last_value:
            return last_value
        time.sleep(0.05)
    failure = f"{message}: {last_value!r}"
    raise AssertionError(failure)


def _navigate(browser: _Cdp, path: str, expected_text: str) -> None:
    """Navigate to one localhost application path and wait for hydration."""
    browser.command("Page.navigate", {"url": f"{ADMIN_ORIGIN}{path}"})
    _wait_browser(
        browser,
        f"""(() => document.readyState === "complete" &&
          (document.body?.innerText ?? "").includes({json.dumps(expected_text)}))()""",
        f"The page did not show {expected_text}",
    )


def _click_selector(browser: _Cdp, selector: str) -> None:
    """Activate one visible semantic browser control."""
    clicked = browser.evaluate(
        f"""(() => {{
          const target = document.querySelector({json.dumps(selector)});
          if (!(target instanceof HTMLElement)) return false;
          target.click();
          return true;
        }})()"""
    )
    assert clicked is True, selector


def _click_text(browser: _Cdp, text_value: str, *, scope: str = "body") -> None:
    """Activate one scoped button by its exact visible text."""
    clicked = browser.evaluate(
        f"""(() => {{
          const scope = document.querySelector({json.dumps(scope)});
          if (!(scope instanceof HTMLElement)) return false;
          const target = [...scope.querySelectorAll("button")].find(
            (item) => (item.textContent ?? "").trim() === {json.dumps(text_value)}
          );
          if (!(target instanceof HTMLButtonElement)) return false;
          target.click();
          return true;
        }})()"""
    )
    assert clicked is True, text_value


def _set_control(browser: _Cdp, selector: str, value: str) -> None:
    """Set one React-controlled input through its native browser setter."""
    changed = browser.evaluate(
        f"""(() => {{
          const control = document.querySelector({json.dumps(selector)});
          if (!(
            control instanceof HTMLInputElement ||
            control instanceof HTMLTextAreaElement ||
            control instanceof HTMLSelectElement
          )) return false;
          const prototype = control instanceof HTMLInputElement
            ? HTMLInputElement.prototype
            : control instanceof HTMLTextAreaElement
              ? HTMLTextAreaElement.prototype
              : HTMLSelectElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
          if (setter === undefined) return false;
          setter.call(control, {json.dumps(value)});
          control.dispatchEvent(new Event("input", {{ bubbles: true }}));
          control.dispatchEvent(new Event("change", {{ bubbles: true }}));
          return true;
        }})()"""
    )
    assert changed is True, selector


def _press_key(browser: _Cdp, key: str) -> None:
    """Send one trusted keyboard action to the focused browser control."""
    key_codes = (
        {"windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27}
        if key == "Escape"
        else {}
    )
    browser.command(
        "Input.dispatchKeyEvent",
        {
            "type": "rawKeyDown" if key == "Escape" else "keyDown",
            "key": key,
            "code": key,
            **key_codes,
        },
    )
    browser.command(
        "Input.dispatchKeyEvent",
        {
            "type": "keyUp",
            "key": key,
            "code": key,
            **key_codes,
        },
    )


def _assert_layout(browser: _Cdp, *, mobile: bool) -> None:
    """Keep the complete document inside one full-width responsive shell."""
    value = browser.evaluate(
        """(() => {
          const root = document.documentElement;
          const main = document.querySelector("main");
          const content = document.querySelector(".administration-content");
          const mobileNav = document.querySelector(".od-application-mobile-navigation");
          const sidebar = document.querySelector(".od-application-sidebar");
          return {
            overflow: root.scrollWidth > root.clientWidth + 1,
            mainRight: main?.getBoundingClientRect().right ?? 0,
            contentRight: content?.getBoundingClientRect().right ?? 0,
            viewport: root.clientWidth,
            mobile: mobileNav === null ? null : getComputedStyle(mobileNav).display,
            sidebar: sidebar === null ? null : getComputedStyle(sidebar).display
          };
        })()"""
    )
    assert isinstance(value, dict)
    assert value["overflow"] is False
    assert abs(float(value["mainRight"]) - float(value["viewport"])) <= 2
    assert abs(float(value["contentRight"]) - float(value["viewport"])) <= 2
    if mobile:
        assert value["mobile"] != "none"
        assert value["sidebar"] == "none"
    else:
        assert value["mobile"] == "none"
        assert value["sidebar"] != "none"


def _assert_dialog_layout(browser: _Cdp, selector: str, *, mobile: bool) -> None:
    """Keep one inspector or modal inside its local responsive viewport."""
    value = browser.evaluate(
        f"""(() => {{
          const dialog = document.querySelector({json.dumps(selector)});
          if (!(dialog instanceof HTMLElement)) return null;
          const bounds = dialog.getBoundingClientRect();
          const workspace = dialog.closest(
            ".od-graph-workspace, .od-relationship-graph"
          );
          const workspaceBounds = workspace?.getBoundingClientRect();
          const body = dialog.querySelector(
            ".od-dialog-body, .od-graph-inspector-content, .configuration-playground-dialog-body"
          );
          return {{
            bottom: bounds.bottom,
            height: bounds.height,
            left: bounds.left,
            right: bounds.right,
            top: bounds.top,
            width: bounds.width,
            viewportHeight: innerHeight,
            viewportWidth: innerWidth,
            fixed: getComputedStyle(dialog).position === "fixed",
            workspaceBottom: workspaceBounds?.bottom ?? null,
            workspaceLeft: workspaceBounds?.left ?? null,
            workspaceRight: workspaceBounds?.right ?? null,
            workspaceTop: workspaceBounds?.top ?? null,
            bodyBounded: body === null || body.scrollHeight <= body.clientHeight + 1 ||
              ["auto", "scroll"].includes(getComputedStyle(body).overflowY)
          }};
        }})()"""
    )
    assert isinstance(value, dict)
    assert value["bodyBounded"] is True
    if mobile or value["fixed"] is True:
        assert float(value["top"]) >= -1
        assert float(value["left"]) >= -1
        assert float(value["bottom"]) <= float(value["viewportHeight"]) + 1
        assert float(value["right"]) <= float(value["viewportWidth"]) + 1
    else:
        assert value["workspaceTop"] is not None
        assert float(value["top"]) >= float(value["workspaceTop"]) - 1
        assert float(value["left"]) >= float(value["workspaceLeft"]) - 1
        assert float(value["bottom"]) <= float(value["workspaceBottom"]) + 1
        assert float(value["right"]) <= float(value["workspaceRight"]) + 1
    if mobile:
        assert float(value["width"]) >= float(value["viewportWidth"]) * 0.9
        assert float(value["bottom"]) >= float(value["viewportHeight"]) - 16
    elif value["fixed"] is not True:
        assert float(value["left"]) >= float(value["viewportWidth"]) * 0.45


def _assert_axe(browser: _Cdp) -> None:
    """Run the installed shared UI Axe engine against the hydrated page."""
    _wait_browser(
        browser,
        "document.getAnimations().every((animation) => animation.playState !== 'running')",
        "The page animations did not settle before its accessibility scan",
    )
    browser_errors = browser.evaluate("globalThis.__llmrouterProofErrors ?? []")
    assert browser_errors == [], browser_errors
    source = (
        REPOSITORY_ROOT.parent
        / "opendle-ui"
        / "node_modules"
        / "axe-core"
        / "axe.min.js"
    ).read_text(encoding="utf-8")
    assert browser.evaluate("typeof globalThis.axe === 'object'") is False
    browser.evaluate(source)
    result = browser.evaluate(
        """globalThis.axe.run(document, {
          resultTypes: ["violations"],
          rules: { "region": { enabled: true } }
        }).then((value) => value.violations.map((item) => ({
          id: item.id,
          nodes: item.nodes.map((node) => ({
            target: node.target,
            failure: node.failureSummary
          }))
        })))"""
    )
    assert result == [], result


def _assert_accessibility_tree(browser: _Cdp, required_names: set[str]) -> None:
    """Find important graph controls in Chrome's accessibility tree."""
    result = browser.command("Accessibility.getFullAXTree")
    nodes = result.get("nodes")
    assert isinstance(nodes, list)
    names = {
        str(node.get("name", {}).get("value", ""))
        for node in nodes
        if isinstance(node, dict)
        and node.get("role", {}).get("value") in {"button", "region"}
    }
    for required in required_names:
        assert any(required in name for name in names), required


def _prove_service_tree(browser: _Cdp, *, mobile: bool) -> None:
    """Prove the graph-only service and access interaction."""
    _navigate(browser, "/services", "Services and parent relationships")
    _assert_layout(browser, mobile=mobile)
    tree = browser.evaluate(
        """(() => {
          const viewport = document.querySelector("[aria-label='Service tree canvas']");
          const canvas = viewport?.querySelector(".od-graph-canvas");
          if (!(viewport instanceof HTMLElement) || !(canvas instanceof HTMLElement))
            return null;
          const outer = viewport.getBoundingClientRect();
          const inner = canvas.getBoundingClientRect();
          return {
            alignment: canvas.dataset.alignment,
            leftGap: inner.left - outer.left,
            rightGap: outer.right - inner.right,
            nodes: viewport.querySelectorAll("[data-service-api-name]").length,
            tabStops: viewport.querySelectorAll("[data-service-api-name][tabindex='0']").length,
            toolbarText: document.querySelector(".od-graph-toolbar")?.textContent ?? "",
            duplicateList: (document.body.innerText ?? "").includes("Accessible service list")
          };
        })()"""
    )
    assert isinstance(tree, dict)
    assert tree["alignment"] == "center"
    assert tree["nodes"] == 2
    assert tree["tabStops"] == 1
    assert tree["toolbarText"].strip() == "Create service"
    assert tree["duplicateList"] is False
    if float(tree["leftGap"]) >= 0 and float(tree["rightGap"]) >= 0:
        assert abs(float(tree["leftGap"]) - float(tree["rightGap"])) <= 3
    _assert_accessibility_tree(browser, {"Alpha", "Alpha Child"})

    browser.evaluate(
        "document.querySelector(\"[data-service-api-name='alpha']\")?.focus()"
    )
    _press_key(browser, "ArrowRight")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-service-api-name') === 'alpha-child'",
        "The service tree did not move to the first child",
    )
    _press_key(browser, "ArrowLeft")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-service-api-name') === 'alpha'",
        "The service tree did not move to its parent",
    )
    _press_key(browser, "End")
    _press_key(browser, "Home")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-service-api-name') === 'alpha'",
        "The service tree Home key did not reach the first node",
    )

    _click_selector(browser, "[data-service-api-name='alpha']")
    _wait_browser(
        browser,
        "(() => { const text = document.querySelector('.od-graph-inspector[open]')?.innerText ?? ''; "
        "return text.includes('Alpha private') && text.includes('localhost proof'); })()",
        "The selected service inspector did not load workspaces and keys",
    )
    inspector_text = browser.evaluate(
        "document.querySelector('.od-graph-inspector[open]')?.innerText ?? ''"
    )
    assert isinstance(inspector_text, str)
    assert "Service API keys" in inspector_text
    assert "Alpha private" in inspector_text
    assert "localhost proof" in inspector_text
    _assert_dialog_layout(browser, ".od-graph-inspector[open]", mobile=mobile)
    _assert_layout(browser, mobile=mobile)
    _press_key(browser, "Escape")
    _wait_browser(
        browser,
        "document.querySelector('.od-graph-inspector[open]') === null && document.activeElement?.getAttribute('data-service-api-name') === 'alpha'",
        "The service inspector did not restore node focus",
    )

    _click_text(browser, "Create service", scope=".od-graph-toolbar")
    _wait_browser(
        browser,
        'document.querySelector(".od-graph-inspector[open] '
        "input[name='api_name']\") === document.activeElement",
        "The create-service inspector did not receive focus",
    )
    _assert_dialog_layout(browser, ".od-graph-inspector[open]", mobile=mobile)
    _press_key(browser, "Escape")
    _wait_browser(
        browser,
        "document.querySelector('.od-graph-inspector[open]') === null && (document.activeElement?.textContent ?? '').includes('Create service')",
        "The create-service inspector did not restore action focus",
    )
    _assert_axe(browser)


def _prove_configuration_graph(browser: _Cdp, *, mobile: bool) -> None:
    """Prove global catalog, selected assignments, and contextual playground."""
    _navigate(browser, "/configuration", "LLM configuration")
    _assert_layout(browser, mobile=mobile)
    _wait_browser(
        browser,
        """(() => {
          const columns = [...document.querySelectorAll(".od-relationship-graph-column h2")]
            .map((item) => item.textContent?.trim());
          return JSON.stringify(columns) === JSON.stringify([
            "Providers",
            "Canonical models",
            "Assignments"
          ]) && document.querySelector(
            "[aria-label='LLM configuration relationships']"
          ) !== null;
        })()""",
        "The global configuration graph columns did not become ready",
    )
    initial = browser.evaluate(
        """(() => ({
          columns: [...document.querySelectorAll(".od-relationship-graph-column h2")]
            .map((item) => item.textContent?.trim()),
          assignmentDisabled:
            [...document.querySelectorAll("button")]
              .find((item) => item.textContent?.trim() === "Add assignment")?.disabled,
          tabStops: document.querySelectorAll("[data-node-id][tabindex='0']").length,
          pageText: document.body.innerText
        }))()"""
    )
    assert isinstance(initial, dict)
    assert initial["columns"] == [
        "Providers",
        "Canonical models",
        "Assignments",
    ]
    assert initial["assignmentDisabled"] is True
    assert initial["tabStops"] == 1
    assert "Select a service to view assignments." in initial["pageText"]
    for removed in (
        "Workspaces & keys",
        "Accessible service list",
        "Permission scope",
    ):
        assert removed not in initial["pageText"]

    _set_control(
        browser,
        "input[aria-label='Search configuration']",
        "no-such-route",
    )
    _wait_browser(
        browser,
        "(document.body.innerText ?? '').includes('No configuration matches this search.')",
        "The graph did not show its no-result state",
    )
    _click_text(browser, "Clear search")
    _wait_browser(
        browser,
        "document.querySelectorAll('[data-node-id]').length >= 9",
        "The graph did not restore its complete result",
    )

    _set_control(browser, "select[aria-label='Selected service']", "alpha")
    _wait_browser(
        browser,
        "document.querySelector(\"[data-node-id='assignment:workflow']\") !== null",
        "The selected service assignments did not load",
    )
    columns = browser.evaluate(
        "[...document.querySelectorAll('.od-relationship-graph-column h2')].map((item) => item.textContent?.trim())"
    )
    assert columns == [
        "Providers",
        "Canonical models",
        "Assignments",
    ]
    compound = browser.evaluate(
        """(() => {
          const model = document.querySelector("[data-group-id='model:text-model']");
          const routes = model?.querySelectorAll("[data-node-id^='mapping:']") ?? [];
          const workflow = document.querySelector("[data-group-id='assignment:workflow']");
          const columns = [...document.querySelectorAll(".od-relationship-graph-column")];
          const columnTops = columns.map((item) => item.getBoundingClientRect().top);
          const longName = [...document.querySelectorAll("[data-node-id='model:text-model'] strong")]
            .find((item) => item.textContent?.includes("deliberately long"));
          return {
            modelRoutes: routes.length,
            primary: workflow?.querySelector("[data-node-id='rung:workflow:1']") !== null,
            fallback: workflow?.querySelector("[data-node-id='rung:workflow:2']") !== null,
            stacked: columnTops[0] < columnTops[1] && columnTops[1] < columnTops[2],
            sideBySide: Math.max(...columnTops) - Math.min(...columnTops) < 4,
            longNameWraps: longName instanceof HTMLElement &&
              longName.scrollWidth <= longName.clientWidth + 1
          };
        })()"""
    )
    assert isinstance(compound, dict)
    assert int(compound["modelRoutes"]) == 3
    assert compound["primary"] is True
    assert compound["fallback"] is True
    assert compound["stacked" if mobile else "sideBySide"] is True
    assert compound["longNameWraps"] is True
    _wait_browser(
        browser,
        "document.querySelector(\"[data-relationship-id='mapping-assignment:text:workflow:1'][data-source-node-id='mapping:text'][data-target-node-id='rung:workflow:2']\") !== null",
        "The exact route-to-rung connector did not render",
    )
    route_relationship = browser.evaluate(
        "document.querySelector(\"[data-node-id='rung:workflow:2']\")?.getAttribute('aria-label') ?? ''"
    )
    assert "Fallback 2: Route ID: text for Workflow (Assignment ID: workflow)" in str(
        route_relationship
    )
    _assert_accessibility_tree(browser, {"Fake provider", "Workflow"})

    for node_id, key, expected_facts in (
        (
            "provider:fake-provider",
            "Enter",
            ("Provider ID", "fake-provider", "Adapter", "Fake", "State", "Ready"),
        ),
        (
            "model:text-model",
            "Space",
            ("Model ID", "text-model", "Text input", "Text output"),
        ),
    ):
        browser.evaluate(
            f"document.querySelector({json.dumps(f'[data-node-id={node_id!r}]')})?.focus()"
        )
        _press_key(browser, key)
        _wait_browser(
            browser,
            "document.querySelector('.od-graph-inspector[open]') !== null",
            f"The {node_id} inspector did not open with {key}",
        )
        inspector_text = browser.evaluate(
            "document.querySelector('.od-graph-inspector[open]')?.innerText ?? ''"
        )
        assert isinstance(inspector_text, str)
        for fact in expected_facts:
            assert fact in inspector_text
        _press_key(browser, "Escape")
        _wait_browser(
            browser,
            f"document.activeElement?.getAttribute('data-node-id') === {json.dumps(node_id)}",
            f"The {node_id} inspector did not restore focus",
        )

    _set_control(
        browser,
        "input[aria-label='Search configuration']",
        "fake-text-v1",
    )
    _wait_browser(
        browser,
        "document.querySelector(\"[data-node-id='mapping:text'][data-search-match='true']\") !== null && document.querySelectorAll('[data-search-context=true]').length >= 3",
        "The route search did not keep its connected context",
    )
    _click_text(browser, "Clear search")
    _wait_browser(
        browser,
        "document.querySelector(\"[data-node-id='rung:workflow:2']\") !== null",
        "The compound board did not restore after route search",
    )

    browser.evaluate(
        "document.querySelector(\"[data-node-id='mapping:text']\")?.focus()"
    )
    _press_key(browser, "Home")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-node-id')?.startsWith('model:') ?? false",
        "Home did not move to the first canonical-model control",
    )
    _press_key(browser, "End")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-node-id') === 'mapping:text'",
        "End did not move to the last provider-route control",
    )
    _press_key(browser, "ArrowUp")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-node-id') !== 'mapping:text'",
        "Arrow Up did not move through the compound model card",
    )
    _press_key(browser, "ArrowDown")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-node-id') === 'mapping:text'",
        "Arrow Down did not move through the compound model card",
    )
    _press_key(browser, "/")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('aria-label') === 'Search configuration'",
        "The slash key did not focus configuration search",
    )

    browser.evaluate(
        "document.querySelector(\"[data-node-id='provider:fake-provider']\")?.focus()"
    )
    _press_key(browser, "ArrowRight")
    moved = _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-node-id')?.startsWith('mapping:') ?? false",
        "The configuration graph did not move to a connected mapping",
    )
    assert moved is True
    _press_key(browser, "ArrowLeft")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-node-id') === 'provider:fake-provider'",
        "The configuration board did not return to the connected provider",
    )
    _press_key(browser, "ArrowRight")
    _press_key(browser, "ArrowRight")
    _wait_browser(
        browser,
        "document.activeElement?.getAttribute('data-node-id')?.startsWith('rung:') ?? false",
        "The configuration board did not move to an exact assignment rung",
    )
    active_rung = browser.evaluate(
        "document.activeElement?.getAttribute('data-node-id') ?? ''"
    )
    _press_key(browser, "Enter")
    _wait_browser(
        browser,
        "(document.querySelector('.od-graph-inspector[open]')?.innerText ?? '').includes('Selected rung')",
        "The assignment rung did not identify itself in the inspector",
    )
    assignment_inspector_text = browser.evaluate(
        "document.querySelector('.od-graph-inspector[open]')?.innerText ?? ''"
    )
    assert isinstance(assignment_inspector_text, str)
    for fact in (
        "Assignment ID",
        "workflow",
        "Selected route",
        "text",
        "Provider",
        "Fake provider",
        "Canonical model",
        "Text model",
        "Route state",
        "Enabled",
        "Definition source",
        "Local definition",
        "Text input",
        "Text output",
    ):
        assert fact in assignment_inspector_text
    _press_key(browser, "Escape")
    _wait_browser(
        browser,
        f"document.activeElement?.getAttribute('data-node-id') === {json.dumps(active_rung)}",
        "The assignment inspector did not restore rung focus",
    )

    _click_text(browser, "Add canonical model", scope="[data-column-id='catalog']")
    _wait_browser(
        browser,
        "(() => { const text = document.querySelector('.od-graph-inspector[open]')?.innerText ?? ''; "
        "return text.includes('Add canonical model') && text.includes('Create from OpenRouter'); })()",
        "The model-create inspector did not open in the graph",
    )
    _assert_dialog_layout(browser, ".od-graph-inspector[open]", mobile=mobile)
    _press_key(browser, "Escape")
    _wait_browser(
        browser,
        "document.querySelector('.od-graph-inspector[open]') === null",
        "The model-create inspector did not close",
    )

    _click_selector(browser, "[data-node-id='mapping:text']")
    _wait_browser(
        browser,
        "[...document.querySelectorAll('.od-graph-inspector[open] button')].some((item) => item.textContent?.trim() === 'Play exact route')",
        "The exact mapping inspector did not offer its playground",
    )
    route_inspector_text = browser.evaluate(
        "document.querySelector('.od-graph-inspector[open]')?.innerText ?? ''"
    )
    assert isinstance(route_inspector_text, str)
    for fact in (
        "Route ID",
        "text",
        "Provider",
        "Fake provider",
        "Canonical model",
        "Text model",
        "Provider wire model",
        "fake-text-v1",
        "State",
        "Enabled",
    ):
        assert fact in route_inspector_text
    _click_text(browser, "Play exact route", scope=".od-graph-inspector[open]")
    _wait_browser(
        browser,
        "document.querySelector('dialog.od-dialog[open]') !== null",
        "The contextual playground did not open",
    )
    _assert_dialog_layout(browser, "dialog.od-dialog[open]", mobile=mobile)
    modal_text = browser.evaluate(
        "document.querySelector('dialog.od-dialog[open]')?.innerText ?? ''"
    )
    assert isinstance(modal_text, str)
    assert "exact provider-model" in modal_text.lower()
    assert "text" in modal_text
    assert "Service API key" not in modal_text
    assert "Workspace" not in modal_text
    assert "Permission scope" not in modal_text
    _set_control(
        browser,
        "#configuration-playground-input",
        "Run one contextual browser model proof.",
    )
    _click_text(browser, "Run operation", scope="dialog.od-dialog[open]")
    _wait_browser(
        browser,
        "document.querySelector('dialog.od-dialog[open]')?.innerText.includes('Result ready')",
        "The contextual model call did not finish",
        attempts=400,
    )
    modal_text = browser.evaluate(
        "document.querySelector('dialog.od-dialog[open]')?.innerText ?? ''"
    )
    assert isinstance(modal_text, str)
    for fact in (
        "Fake response.",
        "Selected route",
        "Latency",
        "Usage",
        "Cost",
        "Logical call",
        "Route details",
    ):
        assert fact.lower() in modal_text.lower()

    browser.evaluate("globalThis.__llmrouterProofMode = 'remove-text'")
    _click_text(browser, "Refresh target", scope="dialog.od-dialog[open]")
    _wait_browser(
        browser,
        "document.querySelector('dialog.od-dialog[open]')?.innerText.includes('Target unavailable')",
        "The open playground did not keep its unavailable target state",
    )
    preserved_input = browser.evaluate(
        "document.querySelector('#configuration-playground-input')?.value"
    )
    assert preserved_input == "Run one contextual browser model proof."
    run_disabled = browser.evaluate(
        "[...document.querySelectorAll('dialog.od-dialog[open] button')].find((item) => item.textContent?.trim() === 'Run operation')?.disabled"
    )
    assert run_disabled is True
    browser.evaluate("globalThis.__llmrouterProofMode = 'normal'")
    _press_key(browser, "Escape")
    _wait_browser(
        browser,
        "document.querySelector('dialog.od-dialog[open]') === null",
        "The playground did not close with Escape",
    )
    _click_text(browser, "Refresh")
    _wait_browser(
        browser,
        "document.querySelector(\"[data-node-id='mapping:text']\") !== null",
        "The graph did not restore the mapping after refresh",
    )

    if not mobile:
        _prove_other_playground_operations(browser)
    _assert_layout(browser, mobile=mobile)
    _assert_axe(browser)


def _close_graph_inspector(browser: _Cdp) -> None:
    """Close a graph inspector when one remains open."""
    if browser.evaluate("document.querySelector('.od-graph-inspector[open]') !== null"):
        _press_key(browser, "Escape")
        _wait_browser(
            browser,
            "document.querySelector('.od-graph-inspector[open]') === null",
            "The graph inspector did not close",
        )


def _open_exact_playground(browser: _Cdp, mapping: str) -> None:
    """Open one exact mapping playground from its graph node."""
    _close_graph_inspector(browser)
    _click_selector(browser, f"[data-node-id='mapping:{mapping}']")
    _wait_browser(
        browser,
        "[...document.querySelectorAll('.od-graph-inspector[open] button')].some((item) => item.textContent?.trim() === 'Play exact route')",
        f"The {mapping} mapping did not offer its playground",
    )
    _click_text(browser, "Play exact route", scope=".od-graph-inspector[open]")
    _wait_browser(
        browser,
        "document.querySelector('dialog.od-dialog[open]') !== null",
        f"The {mapping} playground did not open",
    )


def _prove_other_playground_operations(browser: _Cdp) -> None:
    """Run embedding and each media kind through the contextual UI."""
    _open_exact_playground(browser, "embedding")
    _set_control(browser, "#configuration-playground-input", "one\ntwo")
    _click_text(browser, "Run operation", scope="dialog.od-dialog[open]")
    _wait_browser(
        browser,
        "[...document.querySelectorAll('dialog.od-dialog[open] dl div')].some((item) => "
        "item.querySelector('dt')?.textContent?.trim() === 'Vectors' && "
        "item.querySelector('dd')?.textContent?.trim() === '2')",
        "The contextual embedding call did not show its vector result",
        attempts=400,
    )
    embedding_text = browser.evaluate(
        "document.querySelector('dialog.od-dialog[open]')?.innerText ?? ''"
    )
    assert "dimensions\n3" in str(embedding_text).lower()
    assert "logical call" in str(embedding_text).lower()
    _press_key(browser, "Escape")
    _wait_browser(
        browser,
        "document.querySelector('dialog.od-dialog[open]') === null",
        "The embedding playground did not close",
    )

    _open_exact_playground(browser, "media")
    for kind in ("image", "video", "audio"):
        _set_control(browser, "#configuration-playground-operation", kind)
        _set_control(
            browser,
            "#configuration-playground-input",
            f"Create one contextual {kind} proof.",
        )
        _click_text(browser, "Run operation", scope="dialog.od-dialog[open]")
        _wait_browser(
            browser,
            "document.querySelector('dialog.od-dialog[open]')?.innerText.includes('Result ready')",
            f"The contextual {kind} call did not finish",
            attempts=500,
        )
        media = browser.evaluate(
            f"""(() => {{
              const dialog = document.querySelector("dialog.od-dialog[open]");
              const output = dialog?.querySelector({json.dumps("img" if kind == "image" else kind)});
              return {{
                output: output !== null,
                text: dialog?.innerText ?? ""
              }};
            }})()"""
        )
        assert isinstance(media, dict)
        assert media["output"] is True
        assert "media job" in str(media["text"]).lower()
        assert "logical call" in str(media["text"]).lower()
    _press_key(browser, "Escape")
    _wait_browser(
        browser,
        "document.querySelector('dialog.od-dialog[open]') === null",
        "The media playground did not close",
    )


def _prove_observation_pages(browser: _Cdp, *, mobile: bool) -> None:
    """Prove retained logs, statistics, health, and activity pages."""
    _navigate(browser, "/logs", "Detailed request logs")
    _assert_layout(browser, mobile=mobile)
    if not mobile:
        _click_text(browser, "Load logs")
        _wait_browser(
            browser,
            "document.querySelectorAll("
            "\"[aria-label='Detailed request logs'] tbody tr\").length > 0",
            "The detailed-log table did not load",
            attempts=400,
        )
        clicked_long = browser.evaluate(
            """(() => {
              const row = [...document.querySelectorAll("tbody tr")].find(
                (item) => item.innerText.includes("ui-long-content")
              );
              const action = row?.querySelector("button");
              if (!(action instanceof HTMLButtonElement)) return false;
              action.click();
              return true;
            })()"""
        )
        assert clicked_long is True
        _wait_browser(
            browser,
            "document.querySelector('.log-detail')?.innerText.includes('Request content')",
            "The selected detailed log did not load",
        )
        detail = browser.evaluate(
            """(() => ({
              text: document.querySelector(".log-detail")?.innerText ?? "",
              activeMarkup: document.querySelector(".log-detail script, .log-detail iframe") !== null,
              executed: globalThis.__llmrouterProofExecuted === true,
              overflow: document.documentElement.scrollWidth > innerWidth + 1
            }))()"""
        )
        assert isinstance(detail, dict)
        assert "LLLLLLLLLLLL" in detail["text"]
        assert detail["activeMarkup"] is False
        assert detail["executed"] is False
        assert detail["overflow"] is False
        _click_text(browser, "Close")
        _wait_browser(
            browser,
            "document.querySelector('.log-detail') === null",
            "The detailed-log panel did not close",
        )
        clicked_media = browser.evaluate(
            """(() => {
              const row = [...document.querySelectorAll("tbody tr")].find(
                (item) => ["image", "video", "audio"].some(
                  (kind) => item.innerText.includes(kind)
                )
              );
              const action = row?.querySelector("button");
              if (!(action instanceof HTMLButtonElement)) return false;
              action.click();
              return true;
            })()"""
        )
        assert clicked_media is True
        _wait_browser(
            browser,
            "[...document.querySelectorAll('.log-detail button')].some((item) => item.textContent?.trim() === 'Prepare retained media download')",
            "The media log did not expose its authenticated download action",
        )
        _click_text(browser, "Prepare retained media download", scope=".log-detail")
        _wait_browser(
            browser,
            "document.querySelector('.log-detail a[download]')?.href.startsWith('blob:') ?? false",
            "The retained media download was not prepared",
        )
    _assert_axe(browser)

    _navigate(browser, "/statistics", "Usage and cost statistics")
    _assert_layout(browser, mobile=mobile)
    if not mobile:
        _click_text(browser, "Run statistics")
        _wait_browser(
            browser,
            "document.querySelectorAll("
            "\"[aria-label='Usage and cost statistics'] tbody tr\").length > 0",
            "The accounting table did not load",
            attempts=400,
        )
        statistics_text = browser.evaluate(
            "document.querySelector("
            "\"[aria-label='Usage and cost statistics']\")?.innerText ?? ''"
        )
        statistics_copy = str(statistics_text).lower()
        assert "calls" in statistics_copy
        assert "attempts" in statistics_copy
        assert "typed usage" in statistics_copy
        assert "cost" in statistics_copy
    _assert_axe(browser)

    _navigate(browser, "/operations", "Activity and health")
    _assert_layout(browser, mobile=mobile)
    _wait_browser(
        browser,
        "(document.body.innerText ?? '').includes('activity records loaded')",
        "The retained activity table did not load",
        attempts=400,
    )
    operations_text = browser.evaluate("document.body.innerText")
    for text_value in (
        "Health components",
        "Global retention",
        "Current provider-model cooldowns",
        "Configuration activity, last 7 days",
    ):
        assert text_value in str(operations_text)
    if not mobile:
        _wait_browser(
            browser,
            "[...document.querySelectorAll('button')].some("
            "(item) => item.textContent?.trim() === 'Load more rows')",
            "The activity table did not expose bounded incremental loading",
        )
        initial_activity_rows = browser.evaluate(
            "document.querySelectorAll("
            "\"[aria-label='Configuration activity'] tbody tr\").length"
        )
        assert initial_activity_rows == 200
        _click_text(browser, "Load more rows")
        _wait_browser(
            browser,
            "document.querySelectorAll("
            "\"[aria-label='Configuration activity'] tbody tr\").length > 200",
            "The activity table did not append its next bounded page",
        )
    _assert_axe(browser)


def _prove_route_and_state_matrix(browser: _Cdp, *, mobile: bool) -> None:
    """Prove retained routes, removed routes, and bounded UI states."""
    retained = (
        ("/overview", "Router overview"),
        ("/services", "Services and parent relationships"),
        ("/configuration", "LLM configuration"),
        ("/logs", "Detailed request logs"),
        ("/statistics", "Usage and cost statistics"),
        ("/operations", "Activity and health"),
    )
    for path, title in retained:
        _navigate(browser, path, title)
        _assert_layout(browser, mobile=mobile)
    for path in ("/providers", "/models", "/assignments", "/playground"):
        _navigate(browser, path, "LLM configuration")
        current_path = browser.evaluate("location.pathname")
        assert current_path == "/configuration"
    _navigate(browser, "/access", "Services and parent relationships")
    assert "Workspaces & keys" not in str(browser.evaluate("document.body.innerText"))

    _navigate(browser, "/overview?proof_mode=loading", "Loading administration data")
    _assert_layout(browser, mobile=mobile)
    _navigate(browser, "/overview?proof_mode=error", "Router overview")
    _wait_browser(
        browser,
        "document.querySelector(\"[role='alert']\")?.textContent?.includes('Injected proof failure.') === true && "
        "(document.body?.innerText ?? '').includes('Services\\n2') && "
        "(document.body?.innerText ?? '').includes('Provider-models\\n5')",
        "One failed global source discarded unrelated overview results",
    )
    _assert_layout(browser, mobile=mobile)
    _navigate(
        browser,
        "/configuration?proof_mode=error",
        "Unable to load Providers.",
    )
    initial_failure_state = browser.evaluate(
        """({
          canonicalFailure: (document.body?.innerText ?? "").includes(
            "Unable to load Canonical models."
          ),
          modelVisible: document.querySelector(
            "[data-node-id='model:text-model']"
          ) !== null,
          retryVisible: [...document.querySelectorAll(
            "[data-column-id='providers'] button"
          )].some((item) => item.textContent?.trim() === "Retry"),
          wholePageFailure: (document.body?.innerText ?? "").includes(
            "The administration data is not available"
          )
        })"""
    )
    assert initial_failure_state == {
        "canonicalFailure": False,
        "modelVisible": True,
        "retryVisible": True,
        "wholePageFailure": False,
    }
    _assert_layout(browser, mobile=mobile)
    _navigate(browser, "/services?proof_mode=empty", "No services")
    _assert_layout(browser, mobile=mobile)
    _navigate(
        browser,
        "/configuration?proof_mode=empty",
        "No providers are configured.",
    )
    _assert_layout(browser, mobile=mobile)
    _navigate(browser, "/configuration", "LLM configuration")
    _wait_browser(
        browser,
        "document.querySelector(\"[aria-label='LLM configuration relationships']\") !== null && "
        "[...document.querySelectorAll('.administration-topbar-actions button')].some("
        "(item) => item.textContent?.trim() === 'Refresh' && !item.disabled)",
        "The current configuration graph was not ready for its failed refresh proof",
    )
    browser.evaluate("globalThis.__llmrouterProofMode = 'error'")
    _click_text(browser, "Refresh", scope=".administration-topbar-actions")
    _wait_browser(
        browser,
        "document.querySelector(\"[role='alert']\")?.textContent?.includes('Injected proof failure.') === true && "
        "document.querySelector(\"[data-node-id='provider:fake-provider']\") !== null && "
        "(document.querySelector(\"[data-column-id='providers']\")?.innerText ?? '').includes('Unable to load Providers.') && "
        "[...document.querySelectorAll(\"[data-column-id='providers'] button\")].some("
        "(item) => item.textContent?.trim() === 'Retry')",
        "A failed refresh did not retain and label the current configuration graph",
    )
    browser.evaluate("globalThis.__llmrouterProofMode = 'normal'")
    _assert_layout(browser, mobile=mobile)


def _prove_emulated_media(browser: _Cdp, *, mobile: bool) -> None:
    """Keep the UI usable in forced colors and reduced motion."""
    browser.command(
        "Emulation.setEmulatedMedia",
        {
            "features": [
                {"name": "forced-colors", "value": "active"},
                {"name": "prefers-reduced-motion", "value": "reduce"},
            ]
        },
    )
    _navigate(browser, "/configuration?service=alpha", "LLM configuration")
    media = browser.evaluate(
        """({
          forced: matchMedia("(forced-colors: active)").matches,
          reduced: matchMedia("(prefers-reduced-motion: reduce)").matches
        })"""
    )
    assert media == {"forced": True, "reduced": True}
    _assert_layout(browser, mobile=mobile)
    browser_errors = browser.evaluate("globalThis.__llmrouterProofErrors ?? []")
    assert browser_errors == [], browser_errors
    browser.command("Emulation.setEmulatedMedia", {"features": []})


def _prove_viewport(browser: _Cdp, *, width: int, mobile: bool) -> None:
    """Prove one complete responsive administrator viewport."""
    browser.command(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": width,
            "height": 844 if mobile else 900,
            "deviceScaleFactor": 1,
            "mobile": mobile,
        },
    )
    browser.command("Emulation.setEmulatedMedia", {"features": []})
    _prove_route_and_state_matrix(browser, mobile=mobile)
    _navigate(browser, "/overview", "Router overview")
    overview = browser.evaluate("document.body.innerText")
    assert "Services\n2" in str(overview)
    assert "Provider connections\n1" in str(overview)
    assert "Provider-models\n5" in str(overview)
    _assert_axe(browser)
    _prove_service_tree(browser, mobile=mobile)
    _prove_configuration_graph(browser, mobile=mobile)
    _prove_observation_pages(browser, mobile=mobile)
    _prove_emulated_media(browser, mobile=mobile)


def _unused_port() -> int:
    """Reserve and release one loopback port for Chrome."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _debugging_endpoint(port: int) -> str:
    """Wait for Chrome and return its loopback websocket endpoint."""
    for _attempt in range(100):
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/json/list", timeout=1, trust_env=False
            )
            values = response.json()
            for value in values:
                if not isinstance(value, dict) or value.get("type") != "page":
                    continue
                endpoint = value.get("webSocketDebuggerUrl")
                if isinstance(endpoint, str):
                    return endpoint
        except httpx.HTTPError, IndexError, KeyError, TypeError, ValueError:
            time.sleep(0.05)
    raise AssertionError("The loopback Chrome debugging endpoint did not start.")


class _Cdp:
    """Use the small Chrome DevTools websocket subset needed by the proof."""

    def __init__(self, endpoint: str) -> None:
        """Open one validated loopback websocket."""
        url = httpx.URL(endpoint)
        if url.host != "127.0.0.1" or url.port is None or url.scheme != "ws":
            raise AssertionError("The Chrome endpoint is not on loopback.")
        self._socket = socket.create_connection((url.host, url.port), timeout=10)
        self._socket.settimeout(10)
        self._buffer = bytearray()
        self._identifier = 0
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {url.raw_path.decode()} HTTP/1.1\r\n"
            f"Host: {url.host}:{url.port}\r\n"
            f"Origin: http://{url.host}:{url.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(self._socket.recv(4096))
        header, remaining = bytes(response).split(b"\r\n\r\n", 1)
        if not header.startswith(b"HTTP/1.1 101 "):
            raise AssertionError("The Chrome debugging socket did not upgrade.")
        self._buffer.extend(remaining)

    def __enter__(self) -> Self:
        """Return the open client."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the socket."""
        self._socket.close()

    def command(
        self, method: str, parameters: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Run one command and ignore unrelated browser events."""
        self._identifier += 1
        identifier = self._identifier
        value: dict[str, object] = {"id": identifier, "method": method}
        if parameters is not None:
            value["params"] = parameters
        self._send(json.dumps(value, separators=(",", ":")))
        while True:
            response = json.loads(self._receive())
            if response.get("id") != identifier:
                continue
            if "error" in response:
                message = f"Chrome command failed: {method}"
                raise AssertionError(message)
            result = response.get("result", {})
            return result if isinstance(result, dict) else {}

    def evaluate(self, expression: str) -> object:
        """Evaluate one fixed proof expression and return its value."""
        result = self.command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise AssertionError("The Chrome proof expression failed.")
        remote = result.get("result")
        if not isinstance(remote, dict):
            return None
        return remote.get("value")

    def _send(self, value: str) -> None:
        self._send_frame(1, value.encode())

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) <= 65_535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", len(payload)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", len(payload)))
        header.extend(mask)
        header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(header)

    def _receive(self) -> str:
        fragments = bytearray()
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 8:
                raise AssertionError("The Chrome debugging socket closed.")
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode in {0, 1}:
                fragments.extend(payload)
                if first & 0x80:
                    return fragments.decode()

    def _read(self, length: int) -> bytes:
        while len(self._buffer) < length:
            part = self._socket.recv(max(4096, length - len(self._buffer)))
            if not part:
                raise AssertionError("The Chrome debugging socket closed.")
            self._buffer.extend(part)
        result = bytes(self._buffer[:length])
        del self._buffer[:length]
        return result


if __name__ == "__main__":
    main()
