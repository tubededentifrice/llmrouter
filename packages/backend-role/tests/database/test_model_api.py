"""Native synchronous and SSE model-call contract tests."""
# ruff: noqa: D102, D107, PLR2004

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from http import HTTPStatus
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import create_app
from llmrouter_backend.app import _model_stream_body
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.model_api import (
    ModelCallRequest,
    _validate_structured_output,
    internal_model_call,
)
from llmrouter_backend.object_store import ObjectNotFoundError, StoredObject
from llmrouter_backend.security import ControlKeys
from llmrouter_backend.store import create_key
from psycopg.rows import dict_row
from pydantic import ValidationError

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from llmrouter_backend.calls import CallResult
    from llmrouter_backend.object_store import ObjectStore

_PNG = b"\x89PNG\r\n\x1a\nmodel-input"


class MemoryObjectStore:
    """Keep retained test objects in memory without external storage."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self.values[key] = (body, content_type)

    def get(self, key: str, maximum_bytes: int = 1024 * 1024 * 1024) -> StoredObject:
        try:
            body, content_type = self.values[key]
        except KeyError:
            raise ObjectNotFoundError from None
        if len(body) > maximum_bytes:
            raise ObjectNotFoundError
        return StoredObject(body, content_type)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

    def healthy(self) -> bool:
        return True


@dataclass(slots=True)
class ModelApiContext:
    """One isolated service tree, fake catalog, and native client."""

    database_url: str
    client: TestClient
    keys: dict[str, str]
    objects: MemoryObjectStore

    def headers(self, service: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[service]}"}


@pytest.fixture
def model_api_context(database_url: str, tmp_path: Path) -> ModelApiContext:
    """Create a complete network-free model-call service."""
    digest = tmp_path / "digest"
    encryption = tmp_path / "encryption"
    digest.write_text("d" * 64, encoding="utf-8")
    encryption.write_text("e" * 64, encoding="utf-8")
    settings = Settings(
        administrator_digest_key_file=digest,
        administrator_encryption_key_file=encryption,
        allowed_origins=("http://127.0.0.1:5174",),
    )
    controls = ControlKeys.load(settings)
    keys: dict[str, str] = {}
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        migrate(connection)
        service_ids: dict[str, uuid.UUID] = {}
        for service in ("alpha", "beta"):
            row = connection.execute(
                """INSERT INTO router.services (api_name, display_name)
                   VALUES (%s, %s) RETURNING id""",
                (service, service.title()),
            ).fetchone()
            assert row is not None
            service_ids[service] = row["id"]
            connection.execute(
                """INSERT INTO router.workspaces
                       (service_id, api_name, display_name)
                   VALUES (%s, 'main', 'Main')""",
                (row["id"],),
            )
            _key, secret = create_key(
                connection,
                service_id=row["id"],
                name="test",
                actor_subject="test:setup",
                control_keys=controls,
            )
            keys[service] = secret
        connection.execute(
            """INSERT INTO router.workspaces
                   (service_id, api_name, display_name)
               VALUES (%s, 'private', 'Private')""",
            (service_ids["alpha"],),
        )
        _seed_model_catalog(connection, service_ids["alpha"])
    objects = MemoryObjectStore()
    client = TestClient(
        create_app(
            database_url=database_url,
            settings=settings,
            object_store=cast("ObjectStore", objects),
        ),
        base_url="http://127.0.0.1:8010",
    )
    return ModelApiContext(database_url, client, keys, objects)


def _seed_model_catalog(
    connection: psycopg.Connection[Any], service_id: uuid.UUID
) -> None:
    price = {
        "currency": "USD",
        "unit_prices": [
            {"unit": unit, "amount": "0.01"}
            for unit in (
                "input_token",
                "output_token",
                "cached_input_token",
                "request",
                "provider_unit",
            )
        ],
        "source": "manual-test",
    }
    connection.execute(
        """INSERT INTO router.provider_connections
               (api_name, display_name, adapter, enabled)
           VALUES ('fake-provider', 'Fake provider', 'fake', true)"""
    )
    connection.execute(
        """INSERT INTO router.canonical_models
               (api_name, display_name, input_modalities, output_modalities,
                capabilities, constraints, manual_price)
           VALUES ('model', 'Model', ARRAY['text', 'image'],
                   ARRAY['text', 'structured_json'],
                   ARRAY['tool_calling', 'streaming', 'reasoning'],
                   '{"max_input_images":8,"max_input_image_bytes":20971520}'::jsonb,
                   %s::jsonb)""",
        (json.dumps(price),),
    )
    for api_name, wire_name in (
        ("failure", "fake-error-transport-v1"),
        ("primary", "fake-text-v1"),
        ("interruption", "fake-stream-interruption-v1"),
    ):
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name,
                    enabled, input_modalities, output_modalities, capabilities,
                    constraints, reasoning_mappings)
               SELECT %s, provider.id, model.id, %s, true,
                      model.input_modalities, model.output_modalities,
                      model.capabilities, model.constraints,
                      '[{"level":"none","provider_value":"none"},
                        {"level":"medium","provider_value":"medium"}]'::jsonb
               FROM router.provider_connections AS provider,
                    router.canonical_models AS model
               WHERE provider.api_name = 'fake-provider'
                 AND model.api_name = 'model'""",
            (api_name, wire_name),
        )
    for name, candidates in (
        ("default", ("primary",)),
        ("workflow", ("failure", "primary")),
        ("interrupt-chain", ("interruption", "primary")),
    ):
        assignment = connection.execute(
            """INSERT INTO router.assignment_definitions
                   (service_id, api_name, display_name)
               VALUES (%s, %s, %s) RETURNING id""",
            (service_id, name, name.title()),
        ).fetchone()
        assert assignment is not None
        for position, candidate in enumerate(candidates):
            connection.execute(
                """INSERT INTO router.assignment_candidates
                       (assignment_id, position, provider_model_id)
                   SELECT %s, %s, id FROM router.provider_models
                   WHERE api_name = %s""",
                (assignment["id"], position, candidate),
            )


def _request(
    *, assignment: str | None = "workflow", provider_model: str | None = None
) -> dict[str, object]:
    selector = (
        {"assignment_api_name": assignment}
        if assignment is not None
        else {"provider_model_api_name": provider_model}
    )
    return {
        "workspace_api_name": "main",
        "selector": selector,
        "messages": [
            {"role": "system", "content": "Use the caller data."},
            {
                "role": "user",
                "content": [{"type": "text", "text": "Hello."}],
            },
        ],
        "tags": ["zeta", "alpha", "zeta"],
    }


def _events(response_text: str) -> list[tuple[str, dict[str, object]]]:
    result: list[tuple[str, dict[str, object]]] = []
    for block in response_text.strip().split("\n\n"):
        lines = block.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        result.append((lines[0][7:], json.loads(lines[1][6:])))
    return result


def test_synchronous_model_call_supports_complete_native_content_and_accounting(
    model_api_context: ModelApiContext,
) -> None:
    """Use messages, an image, tools, tool results, controls, tags, and fallback."""
    context = model_api_context
    body = _request()
    body["messages"] = [
        {"role": "system", "content": "Use the caller data."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this."},
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data_base64": base64.b64encode(_PNG).decode(),
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_call",
                    "id": "prior-call",
                    "name": "lookup",
                    "arguments_json": "{}",
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_call_id": "prior-call",
                    "result_json": '{"value":1}',
                }
            ],
        },
    ]
    body["tools"] = [
        {
            "name": "lookup",
            "description": "Look up one value.",
            "input_schema_json": '{"type":"object","additionalProperties":false}',
        }
    ]
    body["output_limit"] = 200
    body["temperature"] = 0.25
    response = context.client.post(
        "/v1/model-calls", json=body, headers=context.headers("alpha")
    )
    assert response.status_code == HTTPStatus.OK
    assert response.headers["cache-control"] == "no-store"
    document = response.json()
    assert document["output_type"] == "standard"
    assert document["provider_model_api_name"] == "primary"
    assert document["content"] == [
        {
            "type": "tool_call",
            "id": "fake-call-001",
            "name": "lookup",
            "arguments_json": "{}",
        }
    ]
    assert document["usage"] == {
        "units": [
            {"unit": "input_token", "quantity": "4"},
            {"unit": "cached_input_token", "quantity": "1"},
            {"unit": "output_token", "quantity": "2"},
            {"unit": "request", "quantity": "1"},
            {"unit": "provider_unit", "quantity": "0.5"},
        ],
        "cost": "0.085",
        "currency": "USD",
    }
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        call = connection.execute(
            """SELECT id, tags, assignment_api_name
               FROM router.raw_accounting_calls"""
        ).fetchone()
        assert call is not None
        assert call["tags"] == ["alpha", "zeta"]
        assert call["assignment_api_name"] == "workflow"
        attempts = connection.execute(
            """SELECT provider_model_api_name, outcome
               FROM router.raw_accounting_attempts
               WHERE call_id = %s ORDER BY position""",
            (call["id"],),
        ).fetchall()
        outcomes = [
            (row["provider_model_api_name"], row["outcome"]) for row in attempts
        ]
        assert outcomes == [
            ("failure", "failed"),
            ("primary", "succeeded"),
        ]
        log = connection.execute(
            "SELECT request_json FROM router.request_logs WHERE id = %s",
            (call["id"],),
        ).fetchone()
        assert log is not None
        assert base64.b64encode(_PNG).decode() not in log["request_json"]
        assert (
            json.loads(log["request_json"])["messages"][1]["content"][1]["data_base64"]
            == ""
        )
        assert connection.execute(
            "SELECT count(*) FROM router.media_objects WHERE request_log_id = %s",
            (call["id"],),
        ).fetchone() == {"count": 1}
    assert any(
        value == (_PNG, "image/png") for value in context.objects.values.values()
    )


def test_structured_json_is_schema_validated_before_success(
    model_api_context: ModelApiContext,
) -> None:
    """Return one exact JSON string and use normal fallback for invalid output."""
    context = model_api_context
    body = _request(provider_model="primary", assignment=None)
    body["output_format"] = {
        "type": "json_schema",
        "schema_json": (
            '{"type":"object","required":["result"],'
            '"additionalProperties":false,"properties":{"result":{"const":"fake"}}}'
        ),
    }
    succeeded = context.client.post(
        "/v1/model-calls", json=body, headers=context.headers("alpha")
    )
    assert succeeded.status_code == HTTPStatus.OK
    assert succeeded.json()["structured_output_json"] == '{"result":"fake"}'

    body["selector"] = {"assignment_api_name": "workflow"}
    body["output_format"] = {
        "type": "json_schema",
        "schema_json": '{"type":"object","required":["missing"]}',
    }
    failed = context.client.post(
        "/v1/model-calls", json=body, headers=context.headers("alpha")
    )
    assert failed.status_code == HTTPStatus.BAD_GATEWAY
    assert failed.json() == {
        "error": {
            "code": "upstream_failed",
            "message": "Each eligible provider-model failed.",
        }
    }
    assert "fake" not in failed.text


def test_stream_contract_defers_start_and_enforces_the_visibility_boundary(
    model_api_context: ModelApiContext,
) -> None:
    """Fallback before start and stop fallback after the first visible delta."""
    context = model_api_context
    response = context.client.post(
        "/v1/model-streams",
        json=_request(),
        headers=context.headers("alpha"),
    )
    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert _events(response.text) == [
        ("start", {"provider_model_api_name": "primary"}),
        ("text_delta", {"delta": "Fake "}),
        ("text_delta", {"delta": "response."}),
        (
            "completed",
            {
                "provider_model_api_name": "primary",
                "usage": {
                    "units": [
                        {"unit": "input_token", "quantity": "4"},
                        {"unit": "cached_input_token", "quantity": "1"},
                        {"unit": "output_token", "quantity": "2"},
                        {"unit": "request", "quantity": "1"},
                        {"unit": "provider_unit", "quantity": "0.5"},
                    ],
                    "cost": "0.085",
                    "currency": "USD",
                },
            },
        ),
    ]

    interrupted = _request(assignment="interrupt-chain")
    failed = context.client.post(
        "/v1/model-streams",
        json=interrupted,
        headers=context.headers("alpha"),
    )
    events = _events(failed.text)
    assert [event for event, _data in events] == ["start", "text_delta", "error"]
    assert events[0][1]["provider_model_api_name"] == "interruption"
    assert events[-1][1] == {
        "error": {
            "code": "upstream_failed",
            "message": "The provider stream was interrupted.",
        }
    }
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        last_call = connection.execute(
            """SELECT id FROM router.raw_accounting_calls
               WHERE assignment_api_name = 'interrupt-chain'
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        assert last_call is not None
        attempts = connection.execute(
            """SELECT provider_model_api_name FROM router.raw_accounting_attempts
               WHERE call_id = %s ORDER BY position""",
            (last_call["id"],),
        ).fetchall()
        assert [row["provider_model_api_name"] for row in attempts] == ["interruption"]


def test_stream_tool_calls_and_pre_output_errors_use_exact_protocols(
    model_api_context: ModelApiContext,
) -> None:
    """Return tool events in SSE and keep a pre-output failure as normal JSON."""
    context = model_api_context
    body = _request(provider_model="primary", assignment=None)
    body["tools"] = [
        {
            "name": "lookup",
            "description": "Look up one value.",
            "input_schema_json": '{"type":"object"}',
        }
    ]
    streamed = context.client.post(
        "/v1/model-streams", json=body, headers=context.headers("alpha")
    )
    assert [event for event, _data in _events(streamed.text)] == [
        "start",
        "tool_call",
        "completed",
    ]

    failed = context.client.post(
        "/v1/model-streams",
        json=_request(provider_model="failure", assignment=None),
        headers=context.headers("alpha"),
    )
    assert failed.status_code == HTTPStatus.BAD_GATEWAY
    assert failed.headers["content-type"].startswith("application/json")
    assert failed.json()["error"]["code"] == "upstream_failed"


def test_service_workspace_and_actor_isolation_precede_provider_work(
    model_api_context: ModelApiContext,
) -> None:
    """Hide foreign workspaces and separate service keys from browser sessions."""
    context = model_api_context
    missing = context.client.post("/v1/model-calls", json=_request())
    assert missing.status_code == HTTPStatus.UNAUTHORIZED
    administrator = context.client.post(
        "/v1/model-calls",
        json=_request(),
        headers={"Cookie": "llmrouter_admin_session=not-a-service-key"},
    )
    assert administrator.status_code == HTTPStatus.UNAUTHORIZED
    with psycopg.connect(context.database_url) as connection:
        before = connection.execute(
            "SELECT count(*) FROM router.raw_accounting_attempts"
        ).fetchone()
    foreign = _request(provider_model="primary", assignment=None)
    foreign["workspace_api_name"] = "private"
    hidden = context.client.post(
        "/v1/model-calls", json=foreign, headers=context.headers("beta")
    )
    assert hidden.status_code == HTTPStatus.NOT_FOUND
    assert hidden.json()["error"]["code"] == "not_found"
    with psycopg.connect(context.database_url) as connection:
        after = connection.execute(
            "SELECT count(*) FROM router.raw_accounting_attempts"
        ).fetchone()
    assert after == before
    global_mapping = context.client.post(
        "/v1/model-calls",
        json=_request(provider_model="primary", assignment=None),
        headers=context.headers("beta"),
    )
    assert global_mapping.status_code == HTTPStatus.OK
    assert global_mapping.json()["provider_model_api_name"] == "primary"


@pytest.mark.parametrize(
    "change",
    [
        {"messages": []},
        {"output_limit": 0},
        {"output_limit": 1_000_001},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": True},
        {"temperature": None},
        {"output_format": {"type": "text", "schema_json": None}},
        {"tags": ["x"] * 33},
        {"tags": ["x" * 129]},
        {"tags": [f"{index:02d}" + "x" * 126 for index in range(17)]},
        {"excluded_provider_model_api_names": ["primary", "primary"]},
        {"unknown": "private-model-value"},
    ],
)
def test_closed_request_and_scalar_bounds_are_safe(
    model_api_context: ModelApiContext, change: dict[str, object]
) -> None:
    """Reject each closed-contract or scalar-bound violation before provider work."""
    context = model_api_context
    body = _request()
    body.update(change)
    response = context.client.post(
        "/v1/model-calls", json=body, headers=context.headers("alpha")
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"
    assert "private-model-value" not in response.text


def test_model_request_accepts_the_complete_positive_boundary_shape() -> None:
    """Accept exact list, scalar, image-count, and normalized-tag boundaries."""
    body = _request()
    body["excluded_provider_model_api_names"] = [
        f"excluded-{index}" for index in range(16)
    ]
    body["messages"] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data_base64": base64.b64encode(_PNG).decode(),
                }
                for _index in range(8)
            ],
        },
        *(
            {"role": "user", "content": [{"type": "text", "text": "x"}]}
            for _index in range(999)
        ),
    ]
    body["tools"] = [
        {
            "name": f"tool-{index}",
            "description": "A tool.",
            "input_schema_json": "{}",
        }
        for index in range(128)
    ]
    body["output_limit"] = 1_000_000
    body["temperature"] = 2.0
    body["tags"] = [f"{index:02d}" + "x" * 62 for index in range(32)]
    request = ModelCallRequest.model_validate(body)
    call = internal_model_call(request, streaming=False)
    assert len(call.tags) == 32
    assert sum(len(tag.encode()) for tag in call.tags) == 2048
    assert len(call.requirements.input_image_sizes) == 8

    minimum = _request()
    minimum["output_limit"] = 1
    minimum["temperature"] = 0.0
    ModelCallRequest.model_validate(minimum)


def test_selector_tools_schema_images_and_total_json_bounds_are_closed(
    model_api_context: ModelApiContext,
) -> None:
    """Reject cross-field, schema, image, and complete JSON limit violations."""
    context = model_api_context
    headers = context.headers("alpha")
    exact_exclusion = _request(provider_model="primary", assignment=None)
    exact_exclusion["excluded_provider_model_api_names"] = ["failure"]
    duplicate_tools = _request()
    duplicate_tools["tools"] = [
        {"name": "same", "description": "A.", "input_schema_json": "{}"},
        {"name": "same", "description": "B.", "input_schema_json": "{}"},
    ]
    invalid_schema = _request()
    invalid_schema["output_format"] = {
        "type": "json_schema",
        "schema_json": '{"type":"not-a-json-schema-type"}',
    }
    bad_image = _request()
    bad_image["messages"] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data_base64": base64.b64encode(b"not-png").decode(),
                }
            ],
        }
    ]
    too_many_images = _request()
    too_many_images["messages"] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data_base64": base64.b64encode(_PNG).decode(),
                }
                for _index in range(9)
            ],
        }
    ]
    oversized_json = _request()
    oversized_json["messages"] = [
        {"role": "system", "content": "x" * 800_000} for _index in range(3)
    ]
    structured_stream = _request()
    structured_stream["output_format"] = {
        "type": "json_schema",
        "schema_json": "{}",
    }
    cases = (
        ("/v1/model-calls", exact_exclusion),
        ("/v1/model-calls", duplicate_tools),
        ("/v1/model-calls", invalid_schema),
        ("/v1/model-calls", bad_image),
        ("/v1/model-calls", too_many_images),
        ("/v1/model-calls", oversized_json),
        ("/v1/model-streams", structured_stream),
    )
    for route, body in cases:
        response = context.client.post(route, json=body, headers=headers)
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.json()["error"]["code"] == "invalid_request"

    oversized_image = {
        "type": "image",
        "media_type": "image/png",
        "data_base64": base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"x" * (20 * 1024 * 1024)
        ).decode(),
    }
    with pytest.raises(ValidationError):
        ModelCallRequest.model_validate(
            {
                **_request(),
                "messages": [{"role": "user", "content": [oversized_image]}],
            }
        )

    large_image = base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"x" * (17 * 1024 * 1024)
    ).decode()
    with pytest.raises(ValidationError):
        ModelCallRequest.model_validate(
            {
                **_request(),
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "media_type": "image/png",
                                "data_base64": large_image,
                            }
                            for _index in range(3)
                        ],
                    }
                ],
            }
        )


def test_automatic_assignment_commits_before_stream_start(
    model_api_context: ModelApiContext,
) -> None:
    """Expose the first event only after the automatic assignment commit."""
    context = model_api_context
    response = context.client.post(
        "/v1/model-streams",
        json=_request(assignment="new-workflow"),
        headers=context.headers("alpha"),
    )
    assert _events(response.text)[0] == (
        "start",
        {"provider_model_api_name": "primary"},
    )
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """SELECT assignment.inherits_assignment_api_name
               FROM router.assignment_definitions AS assignment
               JOIN router.services AS service ON service.id = assignment.service_id
               WHERE service.api_name = 'alpha'
                 AND assignment.api_name = 'new-workflow'"""
        ).fetchone()
    assert row == {"inherits_assignment_api_name": "default"}


def test_removed_model_request_and_compatibility_surfaces_stay_absent(
    model_api_context: ModelApiContext,
) -> None:
    """Keep durable recovery and provider compatibility routes out of the API."""
    context = model_api_context
    headers = context.headers("alpha")
    for method, route in (
        ("get", "/v1/model-calls/request-id"),
        ("post", "/v1/model-calls/request-id/cancel"),
        ("post", "/v1/model-streams/request-id/resume"),
        ("get", "/v1/model-calls/request-id/result"),
        ("post", "/v1/chat/completions"),
    ):
        response = context.client.request(method, route, headers=headers)
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.json()["error"]["code"] == "not_found"
    idempotency = _request()
    idempotency["idempotency_key"] = "not-supported"
    denied = context.client.post("/v1/model-calls", json=idempotency, headers=headers)
    assert denied.status_code == HTTPStatus.BAD_REQUEST


def test_http_body_boundary_rejects_oversize_and_duplicate_json_fields(
    model_api_context: ModelApiContext,
) -> None:
    """Reject an oversized transport body and ambiguous JSON before provider work."""
    context = model_api_context
    headers = {
        **context.headers("alpha"),
        "Content-Type": "application/json",
        "Content-Length": str(70 * 1024 * 1024 + 1),
    }
    oversized = context.client.post("/v1/model-calls", content=b"{}", headers=headers)
    assert oversized.status_code == HTTPStatus.BAD_REQUEST
    duplicate = context.client.post(
        "/v1/model-calls",
        content=(
            b'{"workspace_api_name":"main","workspace_api_name":"private",'
            b'"selector":{"assignment_api_name":"workflow"},"messages":['
            b'{"role":"user","content":[{"type":"text","text":"x"}]}]}'
        ),
        headers={
            **context.headers("alpha"),
            "Content-Type": "application/json",
        },
    )
    assert duplicate.status_code == HTTPStatus.BAD_REQUEST
    assert duplicate.json()["error"]["code"] == "invalid_request"


def test_stream_generator_cancels_connection_lifetime_work_on_disconnect() -> None:
    """Cancel active work when the response body closes without a public state."""

    async def run() -> None:
        queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()

        async def blocked() -> CallResult:
            await asyncio.Event().wait()
            raise AssertionError

        execution = asyncio.create_task(blocked())
        stream: AsyncGenerator[bytes] = _model_stream_body(
            ("start", {"provider_model_api_name": "primary"}),
            queue,
            execution,
        )
        first = await anext(stream)
        assert first.startswith(b"event: start\n")
        await stream.aclose()
        assert execution.cancelled()

    asyncio.run(run())


def test_structured_schema_validation_has_a_hard_resource_boundary() -> None:
    """Stop a pathological regular expression without blocking the web process."""
    started = monotonic()
    assert not _validate_structured_output(
        {"type": "string", "pattern": "^(a+)+$"},
        "a" * 5000 + "b",
    )
    assert monotonic() - started < 3
