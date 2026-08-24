"""Prove the simplified Router against the localhost deployment."""
# ruff: noqa: EM101, INP001, PLR0915, PLR2004, S101, TRY003

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


def main() -> None:
    """Seed one clean fake deployment and prove its public boundaries."""
    database_url = _database_url()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        controls = ControlKeys.load(_settings())
        alpha_key, child_key, beta_key, admin_session, csrf = _seed(
            connection, controls
        )

    _prove_native_http(alpha_key, child_key, beta_key, admin_session, csrf)
    _prove_sdk_and_harness(alpha_key)
    _prove_persisted_facts(database_url, alpha_key, child_key, beta_key)
    _prove_hydrated_administration(admin_session)
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
) -> tuple[str, str, str, str, str]:
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

    admin_session = new_token()
    csrf = new_token()
    create_administrator_session(
        connection,
        session_verifier=controls.verifier(admin_session),
        csrf_verifier=controls.verifier(csrf),
        encrypted_csrf_token=controls.encrypt({"csrf_token": csrf}),
        issuer="https://proof.invalid",
        subject="localhost-proof",
        display_name="Localhost proof",
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
    )
    connection.commit()
    return alpha_key, child_key, beta_key, admin_session, csrf


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
                name.replace("-", " ").title(),
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


def _prove_native_http(
    alpha_key: str,
    child_key: str,
    beta_key: str,
    admin_session: str,
    csrf: str,
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
            f"{interrupted.text}"
        )
        assert alpha_key not in serialized
        assert child_key not in serialized
        assert beta_key not in serialized
        assert admin_session not in serialized
        assert csrf not in serialized
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
    """Hydrate real administrator data at desktop and phone widths."""
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
                "--remote-allow-origins=*",
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
                _prove_viewport(browser, width=360, mobile=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        _remove_chrome_profile(profile)


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


def _prove_viewport(browser: _Cdp, *, width: int, mobile: bool) -> None:
    """Assert one hydrated responsive viewport without horizontal overflow."""
    browser.command(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": width,
            "height": 900,
            "deviceScaleFactor": 1,
            "mobile": mobile,
        },
    )
    browser.command("Page.navigate", {"url": f"{ADMIN_ORIGIN}/overview"})
    last_text = ""
    last_value: object = None
    for _attempt in range(200):
        value = browser.evaluate(
            """(() => {
              const mobile = document.querySelector(
                '.od-application-mobile-navigation'
              );
              const sidebar = document.querySelector('.od-application-sidebar');
              return {
                url: location.href,
                title: document.title,
                text: document.body?.innerText ?? "",
                ready: document.readyState,
                overflow:
                  document.documentElement.scrollWidth > window.innerWidth,
                mobile: mobile === null ? null : getComputedStyle(mobile).display,
                sidebar:
                  sidebar === null ? null : getComputedStyle(sidebar).display
              };
            })()"""
        )
        last_value = value
        if isinstance(value, dict):
            last_text = str(value.get("text", ""))[:300]
        if isinstance(value, dict) and "Router overview" in str(value.get("text")):
            assert value["ready"] == "complete"
            assert value["overflow"] is False
            if mobile:
                assert value["mobile"] != "none"
                assert value["sidebar"] == "none"
            else:
                assert value["mobile"] == "none"
                assert value["sidebar"] != "none"
            text = str(value["text"])
            assert "Services\n2" in text
            assert "Provider connections\n1" in text
            assert "Provider-models\n5" in text
            return
        time.sleep(0.05)
    message = (
        "The hydrated administration view did not become ready: "
        f"{last_text}; browser value: {last_value!r}"
    )
    raise AssertionError(message)


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
