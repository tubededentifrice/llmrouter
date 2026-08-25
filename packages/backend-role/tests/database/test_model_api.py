"""Native synchronous and SSE model-call contract tests."""
# ruff: noqa: D102, D107, PLR2004, SLF001

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import create_app
from llmrouter_backend.adapters import FakeAdapter
from llmrouter_backend.app import (
    _first_administrator_stream_event,
    _model_stream_body,
)
from llmrouter_backend.calls import (
    CallExecutor,
    OutputValidationUnavailableError,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderOutput,
)
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.embedding_api import AdministratorEmbeddingResult
from llmrouter_backend.media_api import AdministratorMediaJob, run_media_worker_once
from llmrouter_backend.model_api import (
    AdministratorModelCallRequest,
    AdministratorModelCallResult,
    ModelCallRequest,
    _validate_structured_output,
    internal_model_call,
)
from llmrouter_backend.models import RequestLogSummary
from llmrouter_backend.object_store import (
    ObjectNotFoundError,
    ObjectStoreError,
    StoredObject,
)
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import (
    AdministratorActor,
    create_administrator_session,
    create_key,
)
from psycopg.rows import dict_row
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from llmrouter_backend.calls import CallResult
    from llmrouter_backend.object_store import ObjectStore

_PNG = b"\x89PNG\r\n\x1a\nmodel-input"


def _required[T](value: T | None) -> T:
    """Return one required database row in strict test code."""
    assert value is not None
    return value


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


class InvalidUnicodeAdapter(FakeAdapter):
    """Return one invalid public string before the normal fake fallback."""

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncGenerator[ProviderOutput | ProviderCompleted]:
        if request.route.provider_model_name == "fake-error-transport-v1":
            body = json.loads(request.request_json)
            if body.get("tools"):
                tool_call = (
                    r'{"type":"tool_call","id":"\ud800",'
                    r'"name":"lookup","arguments_json":"{}"}'
                )
                kind = "tool_call" if request.streaming else "standard"
                content = tool_call if request.streaming else f"[{tool_call}]"
            else:
                kind = "text_delta" if request.streaming else "standard"
                content = (
                    r'"\ud800"'
                    if request.streaming
                    else r'[{"type":"text","text":"\ud800"}]'
                )
            yield ProviderOutput(cast("Any", kind), content)
            return
        async for event in super().attempt(request):
            yield event


class BlockingFakeAdapter(FakeAdapter):
    """Pause fake provider work at a deterministic dependency boundary."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncGenerator[ProviderOutput | ProviderCompleted]:
        self.calls += 1
        self.entered.set()
        while not self.release.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        async for event in super().attempt(request):
            yield event


class ChangedUsageFakeAdapter(BlockingFakeAdapter):
    """Declare a different current operation contract after job admission."""

    def usage_units_for(self, _operation: object, /) -> frozenset[str]:
        return frozenset({"request"})


@dataclass(slots=True)
class ModelApiContext:
    """One isolated service tree, fake catalog, and native client."""

    database_url: str
    client: TestClient
    keys: dict[str, str]
    objects: MemoryObjectStore
    administrator_headers: dict[str, str]
    administrator_read_headers: dict[str, str]
    executor: CallExecutor

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
        _seed_administrator_playground_catalog(connection)
        session_token = new_token()
        csrf_token = new_token()
        create_administrator_session(
            connection,
            session_verifier=controls.verifier(session_token),
            csrf_verifier=controls.verifier(csrf_token),
            encrypted_csrf_token=controls.encrypt({"csrf_token": csrf_token}),
            issuer="https://identity.test",
            subject="administrator-subject",
            display_name="Administrator",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
    objects = MemoryObjectStore()
    app = create_app(
        database_url=database_url,
        settings=settings,
        object_store=cast("ObjectStore", objects),
    )
    client = TestClient(app, base_url="http://127.0.0.1:8010")
    cookie = f"llmrouter_admin_session={session_token}"
    return ModelApiContext(
        database_url,
        client,
        keys,
        objects,
        {
            "Cookie": cookie,
            "Origin": "http://127.0.0.1:5174",
            "X-CSRF-Token": csrf_token,
        },
        {"Cookie": cookie},
        cast("CallExecutor", app.state.call_executor),
    )


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


def _seed_administrator_playground_catalog(
    connection: psycopg.Connection[Any],
) -> None:
    """Add exact embedding and media targets to the shared fake provider."""
    connection.execute(
        """INSERT INTO router.provider_connections
               (api_name, display_name, adapter, enabled)
           VALUES ('fake-media-failures', 'Fake media failures', 'fake', true)"""
    )
    price = json.dumps(
        {
            "currency": "USD",
            "unit_prices": [
                {"unit": "input_token", "amount": "0.01"},
                {"unit": "request", "amount": "0.01"},
                {"unit": "provider_unit", "amount": "0.01"},
                {"unit": "image", "amount": "0.01"},
                {"unit": "video_second", "amount": "0.01"},
                {"unit": "audio_second", "amount": "0.01"},
            ],
            "source": "manual-test",
        }
    )
    connection.execute(
        """INSERT INTO router.canonical_models
               (api_name, display_name, input_modalities, output_modalities,
                capabilities, constraints, manual_price)
           VALUES
               ('embedding-model', 'Embedding model', ARRAY['text'],
                ARRAY['embedding'], ARRAY[]::text[],
                '{"embedding_dimensions":[3]}'::jsonb, %s::jsonb),
               ('media-model', 'Media model', ARRAY['text', 'image'],
                ARRAY['image', 'video', 'audio'], ARRAY[]::text[],
                '{"max_input_images":8,"max_input_image_bytes":20971520,
                  "max_output_duration_seconds":86400}'::jsonb, %s::jsonb)""",
        (price, price),
    )
    for api_name, model_name, canonical_name, provider_name in (
        ("embedding", "fake-embedding-v1", "embedding-model", "fake-provider"),
        ("media", "fake-media-v1", "media-model", "fake-provider"),
        (
            "media-failure",
            "fake-error-transport-v1",
            "media-model",
            "fake-media-failures",
        ),
        (
            "media-uncertain",
            "fake-media-uncertain-v1",
            "media-model",
            "fake-media-failures",
        ),
    ):
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name,
                    enabled, input_modalities, output_modalities, capabilities,
                    constraints, reasoning_mappings)
               SELECT %s, provider.id, model.id, %s, true,
                      model.input_modalities, model.output_modalities,
                      model.capabilities, model.constraints, '[]'::jsonb
               FROM router.provider_connections AS provider
               JOIN router.canonical_models AS model ON model.api_name = %s
               WHERE provider.api_name = %s""",
            (api_name, model_name, canonical_name, provider_name),
        )
    assignment = connection.execute(
        """INSERT INTO router.assignment_definitions
               (service_id, api_name, display_name)
           SELECT id, 'media-fail-chain', 'Media fail chain'
           FROM router.services WHERE api_name = 'alpha' RETURNING id"""
    ).fetchone()
    assert assignment is not None
    for position, candidate in enumerate(("media-failure", "media-uncertain")):
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


def _administrator_request(
    *, assignment: str | None = "workflow", provider_model: str | None = None
) -> dict[str, object]:
    body = _request(assignment=assignment, provider_model=provider_model)
    body.pop("workspace_api_name")
    if assignment is not None:
        selector = cast("dict[str, object]", body["selector"])
        selector["service_api_name"] = "alpha"
    return body


def test_administrator_playground_model_calls_are_global_and_isolated(
    model_api_context: ModelApiContext,
) -> None:
    """Use one unrestricted administrator session without a workspace or key."""
    context = model_api_context
    response = context.client.post(
        "/v1/admin/playground/model-calls",
        json=_administrator_request(),
        headers=context.administrator_headers,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.headers["cache-control"] == "no-store"
    document = response.json()
    assert document["selector"] == {
        "assignment_api_name": "workflow",
        "service_api_name": "alpha",
    }
    assert [item["provider_model_api_name"] for item in document["attempts"]] == [
        "failure",
        "primary",
    ]
    assert [item["outcome"] for item in document["attempts"]] == [
        "failed",
        "succeeded",
    ]
    assert "usage" not in document["attempts"][0]
    logical_call_id = document["logical_call_id"]
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        call = connection.execute(
            """SELECT call_actor, service_id, workspace_id,
                      administrator_subject, configuration_service_api_name,
                      assignment_api_name, selection_snapshot
               FROM router.raw_accounting_calls WHERE id = %s""",
            (logical_call_id,),
        ).fetchone()
        assert call is not None
        snapshot = call.pop("selection_snapshot")
        assert call == {
            "call_actor": "administrator",
            "service_id": None,
            "workspace_id": None,
            "administrator_subject": "administrator-subject",
            "configuration_service_api_name": "alpha",
            "assignment_api_name": "workflow",
        }
        assert snapshot["selector"] == {
            "kind": "assignment",
            "service_api_name": "alpha",
            "assignment_api_name": "workflow",
            "definition_kind": "direct_chain",
            "defined_by_service_api_name": "alpha",
            "inherits_assignment_api_name": None,
            "direct_chain": ["failure", "primary"],
            "effective_chain": ["failure", "primary"],
            "reasoning_level": None,
        }
        assert snapshot["controls"] == {
            "call_kind": "model",
            "required_inputs": ["text"],
            "required_output": "text",
            "required_capabilities": [],
            "embedding_dimension": None,
            "input_image_sizes": [],
            "input_image_count": 0,
            "output_duration_seconds": None,
            "streaming": False,
            "excluded_provider_model_api_names": [],
            "expected_embedding_count": None,
        }
        snapshot_text = json.dumps(snapshot)
        assert "Hello." not in snapshot_text
        assert "Use the caller data." not in snapshot_text
        assert context.keys["alpha"] not in snapshot_text
    service_stats = context.client.get(
        "/v1/statistics",
        params={
            "from": "2026-01-01T00:00:00Z",
            "to": "2027-01-01T00:00:00Z",
        },
        headers=context.headers("alpha"),
    )
    assert service_stats.status_code == HTTPStatus.OK
    assert service_stats.json()["buckets"] == []
    administrator_stats = context.client.get(
        "/v1/admin/statistics",
        params=[
            ("from", "2026-01-01T00:00:00Z"),
            ("to", "2027-01-01T00:00:00Z"),
            ("call_actor", "administrator"),
            ("group_by", "call_actor"),
            ("group_by", "configuration_service"),
            ("group_by", "assignment"),
        ],
        headers=context.administrator_read_headers,
    )
    assert administrator_stats.status_code == HTTPStatus.OK
    assert administrator_stats.json()["buckets"][0]["dimensions"] == [
        "administrator",
        "alpha",
        "workflow",
    ]
    logs = context.client.get(
        "/v1/admin/request-logs",
        params={
            "from": "2026-08-01T00:00:00Z",
            "to": "2026-09-01T00:00:00Z",
            "call_actor": "administrator",
        },
        headers=context.administrator_read_headers,
    )
    assert logs.status_code == HTTPStatus.OK
    summary = next(
        item
        for item in logs.json()["items"]
        if item["logical_call_id"] == logical_call_id
    )
    assert "service_api_name" not in summary
    assert "workspace_api_name" not in summary
    assert summary["administrator_subject"] == "administrator-subject"
    assert summary["configuration_service_api_name"] == "alpha"


def test_administrator_playground_requires_session_csrf_and_exact_origin(
    model_api_context: ModelApiContext,
) -> None:
    """Keep browser write authority separate from service keys and stale controls."""
    context = model_api_context
    body = _administrator_request(assignment=None, provider_model="primary")
    denied_key = context.client.post(
        "/v1/admin/playground/model-calls",
        json=body,
        headers=context.headers("alpha"),
    )
    assert denied_key.status_code == HTTPStatus.UNAUTHORIZED
    for headers in (
        context.administrator_read_headers,
        {
            **context.administrator_headers,
            "Origin": "http://127.0.0.1:5174/",
        },
        {**context.administrator_headers, "X-CSRF-Token": new_token()},
    ):
        denied = context.client.post(
            "/v1/admin/playground/model-calls", json=body, headers=headers
        )
        assert denied.status_code == HTTPStatus.FORBIDDEN
        assert denied.headers["cache-control"] == "no-store"


def test_administrator_database_actor_and_media_parent_constraints(
    model_api_context: ModelApiContext,
) -> None:
    """Reject cross-actor calls, cross-actor parents, and ambiguous media owners."""
    context = model_api_context
    with psycopg.connect(context.database_url) as connection:
        service, workspace = _required(
            connection.execute(
                """SELECT service.id, workspace.id
               FROM router.services AS service
               JOIN router.workspaces AS workspace
                 ON workspace.service_id = service.id
               WHERE service.api_name = 'alpha'
                 AND workspace.api_name = 'main'"""
            ).fetchone()
        )
        service_call = _required(
            connection.execute(
                """INSERT INTO router.raw_accounting_calls
                   (service_id, workspace_id, outcome, started_at, completed_at)
               VALUES (%s, %s, 'failed', statement_timestamp(),
                       statement_timestamp()) RETURNING id""",
                (service, workspace),
            ).fetchone()
        )[0]
        cross_actor_call = _required(
            connection.execute(
                """INSERT INTO router.raw_accounting_calls
                   (service_id, workspace_id, outcome, started_at, completed_at)
               VALUES (%s, %s, 'failed', statement_timestamp(),
                       statement_timestamp()) RETURNING id""",
                (service, workspace),
            ).fetchone()
        )[0]
        service_log = _required(
            connection.execute(
                """INSERT INTO router.request_logs
                   (logical_call_id, service_id, workspace_id, kind, outcome,
                    request_json, started_at)
               VALUES (%s, %s, %s, 'model', 'failed', '{}', statement_timestamp())
               RETURNING id""",
                (service_call, service, workspace),
            ).fetchone()
        )[0]
        service_job = _required(
            connection.execute(
                """INSERT INTO router.media_jobs
                   (service_id, workspace_id, provider_model_api_name, kind,
                    payload)
               VALUES (%s, %s, 'media', 'image', '{}') RETURNING id""",
                (service, workspace),
            ).fetchone()
        )[0]
        for statement, values, error_type in (
            (
                """INSERT INTO router.raw_accounting_attempts
                       (id, call_id, call_actor, position,
                        provider_connection_api_name, provider_model_api_name,
                        outcome, applied_price, failure_class, started_at,
                        completed_at)
                   VALUES (gen_random_uuid(), %s, 'administrator', 0,
                           'fake-provider', 'primary', 'failed',
                           '{"currency":"USD","unit_prices":[]}',
                           'upstream_failed', statement_timestamp(),
                           statement_timestamp())""",
                (service_call,),
                psycopg.errors.ForeignKeyViolation,
            ),
            (
                """INSERT INTO router.media_objects
                       (call_actor, request_log_id, object_key, media_type, role,
                        size_bytes, content_sha256)
                   VALUES ('administrator', %s, 'wrong-log', 'image/png',
                           'input', 1, decode(repeat('00', 32), 'hex'))""",
                (service_log,),
                psycopg.errors.ForeignKeyViolation,
            ),
            (
                """INSERT INTO router.media_objects
                       (call_actor, media_job_id, object_key, media_type, role,
                        size_bytes, content_sha256)
                   VALUES ('administrator', %s, 'wrong-job', 'image/png',
                           'input', 1, decode(repeat('00', 32), 'hex'))""",
                (service_job,),
                psycopg.errors.ForeignKeyViolation,
            ),
            (
                """INSERT INTO router.media_objects
                       (service_id, workspace_id, object_key, media_type, role,
                        size_bytes, content_sha256)
                   VALUES (%s, %s, 'orphan', 'image/png', 'input', 1,
                           decode(repeat('00', 32), 'hex'))""",
                (service, workspace),
                psycopg.errors.CheckViolation,
            ),
            (
                """INSERT INTO router.media_objects
                       (service_id, workspace_id, media_job_id, request_log_id,
                        object_key, media_type, role, size_bytes, content_sha256)
                   VALUES (%s, %s, %s, %s, 'two-parents', 'image/png', 'input',
                           1, decode(repeat('00', 32), 'hex'))""",
                (service, workspace, service_job, service_log),
                psycopg.errors.CheckViolation,
            ),
            (
                """INSERT INTO router.media_jobs
                       (logical_call_id, call_actor, administrator_subject,
                        exact_provider_model_api_name, provider_model_api_name,
                        kind, payload)
                   VALUES (%s, 'administrator', 'administrator-subject',
                           'media', 'media', 'image', '{}')""",
                (service_call,),
                psycopg.errors.ForeignKeyViolation,
            ),
        ):
            with pytest.raises(error_type), connection.transaction():
                connection.execute(statement, values)
        malformed_logs = (
            ("workflow", None, "primary"),
            (None, "alpha", "primary"),
            (None, None, None),
        )
        administrator_call = _required(
            connection.execute(
                """INSERT INTO router.raw_accounting_calls
                   (call_actor, administrator_subject,
                    exact_provider_model_api_name, kind, outcome, started_at,
                    completed_at)
               VALUES ('administrator', 'administrator-subject', 'primary',
                       'model', 'failed', statement_timestamp(),
                       statement_timestamp()) RETURNING id"""
            ).fetchone()
        )[0]
        for assignment, configuration, provider_model in malformed_logs:
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                connection.execute(
                    """INSERT INTO router.request_logs
                           (logical_call_id, call_actor, administrator_subject,
                            configuration_service_api_name, assignment_api_name,
                            provider_model_api_name, kind, outcome, request_json,
                            started_at)
                       VALUES (%s, 'administrator', 'administrator-subject', %s,
                               %s, %s, 'model', 'failed', '{}',
                               statement_timestamp())""",
                    (
                        administrator_call,
                        configuration,
                        assignment,
                        provider_model,
                    ),
                )
        for logical_call_id in (cross_actor_call, uuid.uuid4()):
            with (
                pytest.raises(psycopg.errors.ForeignKeyViolation),
                connection.transaction(),
            ):
                connection.execute(
                    """INSERT INTO router.request_logs
                           (logical_call_id, call_actor, administrator_subject,
                            provider_model_api_name, kind, outcome, request_json,
                            started_at)
                       VALUES (%s, 'administrator', 'administrator-subject',
                               'primary', 'model', 'failed', '{}',
                               statement_timestamp())""",
                    (logical_call_id,),
                )


def test_administrator_assignment_is_read_only_and_streams_one_call(
    model_api_context: ModelApiContext,
) -> None:
    """Do not create missing assignments and keep stream fallback ordered."""
    context = model_api_context
    missing = context.client.post(
        "/v1/admin/playground/model-calls",
        json=_administrator_request(assignment="missing"),
        headers=context.administrator_headers,
    )
    assert missing.status_code == HTTPStatus.NOT_FOUND
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            """SELECT count(*) AS count
                   FROM router.assignment_definitions AS assignment
                   JOIN router.services AS service ON service.id = assignment.service_id
                   WHERE service.api_name = 'alpha'
                     AND assignment.api_name = 'missing'"""
        ).fetchone() == {"count": 0}
    streamed = context.client.post(
        "/v1/admin/playground/model-streams",
        json=_administrator_request(),
        headers=context.administrator_headers,
    )
    assert streamed.status_code == HTTPStatus.OK
    assert streamed.headers["cache-control"] == "no-store"
    events = _events(streamed.text)
    assert events[0][0] == "start"
    assert events[-1][0] == "completed"
    assert events[0][1]["logical_call_id"] == events[-1][1]["logical_call_id"]
    assert (
        streamed.headers["x-llmrouter-logical-call-id"]
        == events[0][1]["logical_call_id"]
    )
    attempts = cast("list[dict[str, object]]", events[-1][1]["attempts"])
    assert [item["outcome"] for item in attempts] == [
        "failed",
        "succeeded",
    ]


def test_administrator_ineligible_assignment_is_admitted_with_zero_attempts(
    model_api_context: ModelApiContext,
) -> None:
    """Retain resolved chain facts when no candidate supports the call controls."""
    context = model_api_context
    with psycopg.connect(context.database_url) as connection:
        assignment_id = _required(
            connection.execute(
                """INSERT INTO router.assignment_definitions
                   (service_id, api_name, display_name)
               SELECT id, 'ineligible', 'Ineligible' FROM router.services
               WHERE api_name = 'alpha' RETURNING id"""
            ).fetchone()
        )[0]
        connection.execute(
            """INSERT INTO router.assignment_candidates
                   (assignment_id, position, provider_model_id)
               SELECT %s, 0, id FROM router.provider_models
               WHERE api_name = 'embedding'""",
            (assignment_id,),
        )
    response = context.client.post(
        "/v1/admin/playground/model-calls",
        json=_administrator_request(assignment="ineligible"),
        headers=context.administrator_headers,
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    document = response.json()
    assert document["error"]["code"] == "provider_unavailable"
    assert document["attempts"] == []
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        call = connection.execute(
            """SELECT outcome, selection_snapshot
               FROM router.raw_accounting_calls WHERE id = %s""",
            (document["logical_call_id"],),
        ).fetchone()
    assert call is not None
    assert call["outcome"] == "failed"
    assert call["selection_snapshot"]["selector"]["effective_chain"] == ["embedding"]
    assert call["selection_snapshot"]["controls"]["required_output"] == "text"


def test_administrator_provider_work_holds_no_database_gate(
    model_api_context: ModelApiContext,
) -> None:
    """Release the single database slot before one blocked provider attempt."""
    context = model_api_context
    settings = replace(
        cast("Any", context.client.app).state.settings,
        database_concurrency=1,
    )
    application = create_app(
        database_url=context.database_url,
        settings=settings,
        object_store=cast("ObjectStore", context.objects),
    )
    adapter = BlockingFakeAdapter()
    cast("Any", application.state.call_executor)._adapters["fake"] = adapter
    client = TestClient(application, base_url="http://127.0.0.1:8010")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post,
            "/v1/admin/playground/model-calls",
            json=_administrator_request(assignment=None, provider_model="primary"),
            headers=context.administrator_headers,
        )
        assert adapter.entered.wait(timeout=2)
        try:
            with application.state.database_connections.connect(
                context.database_url,
                connect_timeout=2,
                row_factory=dict_row,
            ) as connection:
                assert connection.execute("SELECT 1").fetchone() == {"?column?": 1}
        finally:
            adapter.release.set()
        response = future.result(timeout=5)
    assert response.status_code == HTTPStatus.OK


def test_administrator_media_content_holds_no_database_gate_during_object_io(
    model_api_context: ModelApiContext,
) -> None:
    """Close the metadata connection before a blocked retained-object fetch."""
    context = model_api_context
    created = context.client.post(
        "/v1/admin/playground/media-jobs",
        json={
            "selector": {"provider_model_api_name": "media"},
            "kind": "image",
            "prompt": "Create one image.",
        },
        headers=context.administrator_headers,
    )
    assert created.status_code == HTTPStatus.ACCEPTED
    assert asyncio.run(
        run_media_worker_once(
            context.database_url,
            context.executor,
            cast("ObjectStore", context.objects),
        )
    )
    entered = threading.Event()
    release = threading.Event()
    original_get = context.objects.get

    def blocked_get(key: str, maximum_bytes: int = 1024 * 1024 * 1024) -> StoredObject:
        _ = (key, maximum_bytes)
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError
        raise ObjectNotFoundError

    context.objects.get = blocked_get  # type: ignore[method-assign]
    settings = replace(
        cast("Any", context.client.app).state.settings,
        database_concurrency=1,
    )
    application = create_app(
        database_url=context.database_url,
        settings=settings,
        object_store=cast("ObjectStore", context.objects),
    )
    client = TestClient(application, base_url="http://127.0.0.1:8010")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.get,
            f"/v1/admin/playground/media-jobs/{created.json()['id']}/content",
            headers=context.administrator_read_headers,
        )
        assert entered.wait(timeout=2)
        try:
            with application.state.database_connections.connect(
                context.database_url,
                connect_timeout=2,
                row_factory=dict_row,
            ) as connection:
                assert connection.execute("SELECT 1").fetchone() == {"?column?": 1}
        finally:
            release.set()
        response = future.result(timeout=5)
    context.objects.get = original_get  # type: ignore[method-assign]
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "content_unavailable"


def test_administrator_exact_embedding_and_all_media_kinds_are_global(
    model_api_context: ModelApiContext,
) -> None:
    """Use exact global targets and keep results outside every service scope."""
    context = model_api_context
    embedding = context.client.post(
        "/v1/admin/playground/embeddings",
        json={
            "selector": {"provider_model_api_name": "embedding"},
            "inputs": ["private-embedding-one", "private-embedding-two"],
            "tags": ["playground"],
        },
        headers=context.administrator_headers,
    )
    assert embedding.status_code == HTTPStatus.OK
    assert embedding.headers["cache-control"] == "no-store"
    assert embedding.json()["result"]["provider_model_api_name"] == "embedding"
    assert len(embedding.json()["result"]["embeddings"]) == 2
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        snapshot = _required(
            connection.execute(
                """SELECT selection_snapshot FROM router.raw_accounting_calls
               WHERE id = %s""",
                (embedding.json()["logical_call_id"],),
            ).fetchone()
        )["selection_snapshot"]
    assert snapshot["selector"] == {
        "kind": "exact",
        "provider_model_api_name": "embedding",
    }
    assert snapshot["controls"]["call_kind"] == "embedding"
    assert snapshot["controls"]["expected_embedding_count"] == 2
    assert "private-embedding-one" not in json.dumps(snapshot)
    assert "private-embedding-two" not in json.dumps(snapshot)
    job_ids: list[str] = []
    for kind in ("image", "video", "audio"):
        media_body: dict[str, object] = {
            "selector": {"provider_model_api_name": "media"},
            "kind": kind,
            "prompt": f"Create one {kind}.",
        }
        if kind == "image":
            media_body["input_images"] = [
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data_base64": base64.b64encode(_PNG).decode(),
                }
            ]
        created = context.client.post(
            "/v1/admin/playground/media-jobs",
            json=media_body,
            headers=context.administrator_headers,
        )
        assert created.status_code == HTTPStatus.ACCEPTED
        assert created.headers["cache-control"] == "no-store"
        job_id = created.json()["id"]
        job_ids.append(job_id)
        if kind == "audio":
            with psycopg.connect(context.database_url) as connection:
                connection.execute(
                    "DELETE FROM router.provider_models WHERE api_name = 'media'"
                )
        assert asyncio.run(
            run_media_worker_once(
                context.database_url,
                context.executor,
                cast("ObjectStore", context.objects),
            )
        )
        completed = context.client.get(
            f"/v1/admin/playground/media-jobs/{job_id}",
            headers=context.administrator_read_headers,
        )
        assert completed.status_code == HTTPStatus.OK
        assert completed.headers["cache-control"] == "no-store"
        assert completed.json()["state"] == "succeeded"
        assert completed.json()["attempts"][-1]["outcome"] == "succeeded"
        content = context.client.get(
            f"/v1/admin/playground/media-jobs/{job_id}/content",
            headers=context.administrator_read_headers,
        )
        assert content.status_code == HTTPStatus.OK
        assert content.headers["cache-control"] == "no-store"
        denied = context.client.get(
            f"/v1/media-jobs/{job_id}", headers=context.headers("alpha")
        )
        assert denied.status_code == HTTPStatus.NOT_FOUND
    with psycopg.connect(context.database_url) as connection:
        connection.execute("DELETE FROM router.services WHERE api_name = 'alpha'")
    retained = context.client.get(
        f"/v1/admin/playground/media-jobs/{job_ids[-1]}",
        headers=context.administrator_read_headers,
    )
    assert retained.status_code == HTTPStatus.OK


def test_administrator_media_admits_only_the_final_transaction_selection(
    model_api_context: ModelApiContext,
) -> None:
    """Use the current route when configuration changes during input upload."""
    context = model_api_context
    with psycopg.connect(context.database_url) as connection:
        assignment_id = _required(
            connection.execute(
                """INSERT INTO router.assignment_definitions
                       (service_id, api_name, display_name)
                   SELECT id, 'media-race', 'Media race' FROM router.services
                   WHERE api_name = 'alpha' RETURNING id"""
            ).fetchone()
        )[0]
        connection.execute(
            """INSERT INTO router.assignment_candidates
                   (assignment_id, position, provider_model_id)
               SELECT %s, 0, id FROM router.provider_models
               WHERE api_name = 'media-failure'""",
            (assignment_id,),
        )
    original_put = context.objects.put
    changed = False

    def put_and_change_route(key: str, body: bytes, content_type: str) -> None:
        nonlocal changed
        original_put(key, body, content_type)
        if changed:
            return
        changed = True
        with psycopg.connect(context.database_url) as connection:
            connection.execute(
                "DELETE FROM router.assignment_candidates WHERE assignment_id = %s",
                (assignment_id,),
            )
            connection.execute(
                """INSERT INTO router.assignment_candidates
                       (assignment_id, position, provider_model_id)
                   SELECT %s, 0, id FROM router.provider_models
                   WHERE api_name = 'media'""",
                (assignment_id,),
            )

    context.objects.put = put_and_change_route  # type: ignore[method-assign]
    try:
        response = context.client.post(
            "/v1/admin/playground/media-jobs",
            json={
                "selector": {
                    "service_api_name": "alpha",
                    "assignment_api_name": "media-race",
                },
                "kind": "image",
                "prompt": "Use the uploaded input.",
                "input_images": [
                    {
                        "type": "image",
                        "media_type": "image/png",
                        "data_base64": base64.b64encode(_PNG).decode(),
                    }
                ],
            },
            headers=context.administrator_headers,
        )
    finally:
        context.objects.put = original_put  # type: ignore[method-assign]
    assert response.status_code == HTTPStatus.ACCEPTED
    assert response.json()["provider_model_api_name"] == "media"
    with psycopg.connect(context.database_url, row_factory=dict_row) as read_connection:
        snapshot = _required(
            read_connection.execute(
                """SELECT selection_snapshot FROM router.raw_accounting_calls
                   WHERE id = %s""",
                (response.json()["logical_call_id"],),
            ).fetchone()
        )["selection_snapshot"]
    assert [item["provider_model_api_name"] for item in snapshot["candidates"]] == [
        "media"
    ]
    assert context.objects.values


def test_administrator_media_cleans_uploaded_input_when_final_selection_fails(
    model_api_context: ModelApiContext,
) -> None:
    """Remove uploaded bytes when the final admission transaction cannot select."""
    context = model_api_context
    with psycopg.connect(context.database_url) as connection:
        assignment_id = _required(
            connection.execute(
                """INSERT INTO router.assignment_definitions
                       (service_id, api_name, display_name)
                   SELECT id, 'media-disappears', 'Media disappears'
                   FROM router.services WHERE api_name = 'alpha' RETURNING id"""
            ).fetchone()
        )[0]
        connection.execute(
            """INSERT INTO router.assignment_candidates
                   (assignment_id, position, provider_model_id)
               SELECT %s, 0, id FROM router.provider_models
               WHERE api_name = 'media'""",
            (assignment_id,),
        )
    original_put = context.objects.put
    deleted = False

    def put_and_delete_assignment(key: str, body: bytes, content_type: str) -> None:
        nonlocal deleted
        original_put(key, body, content_type)
        if deleted:
            return
        deleted = True
        with psycopg.connect(context.database_url) as connection:
            connection.execute(
                "DELETE FROM router.assignment_definitions WHERE id = %s",
                (assignment_id,),
            )

    context.objects.put = put_and_delete_assignment  # type: ignore[method-assign]
    try:
        response = context.client.post(
            "/v1/admin/playground/media-jobs",
            json={
                "selector": {
                    "service_api_name": "alpha",
                    "assignment_api_name": "media-disappears",
                },
                "kind": "image",
                "prompt": "Use the uploaded input.",
                "input_images": [
                    {
                        "type": "image",
                        "media_type": "image/png",
                        "data_base64": base64.b64encode(_PNG).decode(),
                    }
                ],
            },
            headers=context.administrator_headers,
        )
    finally:
        context.objects.put = original_put  # type: ignore[method-assign]
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["error"]["code"] == "not_found"
    assert context.objects.values == {}
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            """SELECT count(*) FROM router.raw_accounting_calls
               WHERE call_actor = 'administrator' AND kind = 'media'"""
        ).fetchone() == (0,)


def test_administrator_media_deadline_finalizes_zero_attempt_call(
    model_api_context: ModelApiContext,
) -> None:
    """Expire an unclaimed administrator job and its linked logical call."""
    context = model_api_context
    with psycopg.connect(context.database_url) as connection:
        logical_call_id = _required(
            connection.execute(
                """INSERT INTO router.raw_accounting_calls
                   (call_actor, administrator_subject,
                    exact_provider_model_api_name, kind, started_at,
                    selection_snapshot)
               VALUES ('administrator', 'administrator-subject', 'media', 'media',
                       statement_timestamp() - interval '2 hours', '{}')
               RETURNING id"""
            ).fetchone()
        )[0]
        job_id = _required(
            connection.execute(
                """INSERT INTO router.media_jobs
                   (logical_call_id, call_actor, administrator_subject,
                    exact_provider_model_api_name, provider_model_api_name, kind,
                    payload, created_at, deadline_at)
               VALUES (%s, 'administrator', 'administrator-subject', 'media',
                       'media', 'image', '{}',
                       statement_timestamp() - interval '2 hours',
                       statement_timestamp() - interval '1 hour')
               RETURNING id""",
                (logical_call_id,),
            ).fetchone()
        )[0]
    adapter = BlockingFakeAdapter()
    executor = cast("Any", context.executor)
    prior = executor._adapters["fake"]
    executor._adapters["fake"] = adapter
    try:
        assert not asyncio.run(
            run_media_worker_once(
                context.database_url,
                context.executor,
                cast("ObjectStore", context.objects),
            )
        )
    finally:
        executor._adapters["fake"] = prior
    assert adapter.calls == 0
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            """SELECT job.state, call.outcome,
                      (SELECT count(*) FROM router.raw_accounting_attempts
                       WHERE call_id = call.id) AS attempts
               FROM router.media_jobs AS job
               JOIN router.raw_accounting_calls AS call
                 ON call.id = job.logical_call_id
               WHERE job.id = %s""",
            (job_id,),
        ).fetchone() == {"state": "failed", "outcome": "failed", "attempts": 0}


def test_administrator_failed_media_reports_final_fallback_route(
    model_api_context: ModelApiContext,
) -> None:
    """Report the final attempted provider-model after a failed fallback chain."""
    context = model_api_context
    created = context.client.post(
        "/v1/admin/playground/media-jobs",
        json={
            "selector": {
                "service_api_name": "alpha",
                "assignment_api_name": "media-fail-chain",
            },
            "kind": "image",
            "prompt": "Create one image.",
        },
        headers=context.administrator_headers,
    )
    assert created.status_code == HTTPStatus.ACCEPTED
    assert asyncio.run(
        run_media_worker_once(
            context.database_url,
            context.executor,
            cast("ObjectStore", context.objects),
        )
    )
    job = context.client.get(
        f"/v1/admin/playground/media-jobs/{created.json()['id']}",
        headers=context.administrator_read_headers,
    )
    assert job.status_code == HTTPStatus.OK
    assert job.json()["state"] == "failed"
    assert job.json()["provider_model_api_name"] == "media-uncertain"
    assert [item["provider_model_api_name"] for item in job.json()["attempts"]] == [
        "media-failure",
        "media-uncertain",
    ]
    assert all(item["outcome"] == "failed" for item in job.json()["attempts"])


def test_administrator_media_output_retention_failure_keeps_provider_facts(
    model_api_context: ModelApiContext,
) -> None:
    """Return one safe failed job after provider success and object-store failure."""
    context = model_api_context
    created = context.client.post(
        "/v1/admin/playground/media-jobs",
        json={
            "selector": {"provider_model_api_name": "media"},
            "kind": "image",
            "prompt": "Create one image.",
        },
        headers=context.administrator_headers,
    )
    assert created.status_code == HTTPStatus.ACCEPTED
    original_put = context.objects.put

    def failed_put(_key: str, _body: bytes, _content_type: str) -> None:
        raise ObjectStoreError

    context.objects.put = failed_put  # type: ignore[assignment]
    try:
        assert asyncio.run(
            run_media_worker_once(
                context.database_url,
                context.executor,
                cast("ObjectStore", context.objects),
            )
        )
    finally:
        context.objects.put = original_put  # type: ignore[method-assign]
    response = context.client.get(
        f"/v1/admin/playground/media-jobs/{created.json()['id']}",
        headers=context.administrator_read_headers,
    )
    assert response.status_code == HTTPStatus.OK
    document = response.json()
    assert document["state"] == "failed"
    assert document["provider_model_api_name"] == "media"
    assert document["error"]["code"] == "content_unavailable"
    assert [attempt["outcome"] for attempt in document["attempts"]] == ["succeeded"]
    assert document["attempts"][0]["usage"]["currency"] == "USD"
    assert document["usage"]["currency"] == "USD"
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            """SELECT call.outcome, attempt.outcome AS attempt_outcome,
                      attempt.cost IS NOT NULL AS has_cost
               FROM router.media_jobs AS job
               JOIN router.raw_accounting_calls AS call
                 ON call.id = job.logical_call_id
               JOIN router.raw_accounting_attempts AS attempt
                 ON attempt.call_id = call.id
               WHERE job.id = %s""",
            (created.json()["id"],),
        ).fetchone() == {
            "outcome": "succeeded",
            "attempt_outcome": "succeeded",
            "has_cost": True,
        }


def test_administrator_media_input_failure_finalizes_zero_attempt_call(
    model_api_context: ModelApiContext,
) -> None:
    """Fail a missing retained input before provider work and close accounting."""
    context = model_api_context
    created = context.client.post(
        "/v1/admin/playground/media-jobs",
        json={
            "selector": {"provider_model_api_name": "media"},
            "kind": "image",
            "prompt": "Use this input.",
            "input_images": [
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data_base64": base64.b64encode(_PNG).decode(),
                }
            ],
        },
        headers=context.administrator_headers,
    )
    assert created.status_code == HTTPStatus.ACCEPTED
    with psycopg.connect(context.database_url) as connection:
        object_key = _required(
            connection.execute(
                """SELECT object_key FROM router.media_objects
               WHERE media_job_id = %s AND role = 'input'""",
                (created.json()["id"],),
            ).fetchone()
        )[0]
    context.objects.delete(object_key)
    adapter = BlockingFakeAdapter()
    executor = cast("Any", context.executor)
    prior = executor._adapters["fake"]
    executor._adapters["fake"] = adapter
    try:
        assert asyncio.run(
            asyncio.wait_for(
                run_media_worker_once(
                    context.database_url,
                    context.executor,
                    cast("ObjectStore", context.objects),
                ),
                timeout=2,
            )
        )
    finally:
        executor._adapters["fake"] = prior
    assert adapter.calls == 0
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            """SELECT job.state, call.outcome,
                      (SELECT count(*) FROM router.raw_accounting_attempts
                       WHERE call_id = call.id) AS attempts
               FROM router.media_jobs AS job
               JOIN router.raw_accounting_calls AS call
                 ON call.id = job.logical_call_id
               WHERE job.id = %s""",
            (created.json()["id"],),
        ).fetchone() == {"state": "failed", "outcome": "failed", "attempts": 0}


@pytest.mark.parametrize("restart_change", ["usage", "adapter-appeared"])
def test_administrator_media_restart_preserves_admitted_candidate_contract(
    model_api_context: ModelApiContext,
    restart_change: str,
) -> None:
    """Do not make an admitted unavailable or changed candidate callable."""
    context = model_api_context
    executor = cast("Any", context.executor)
    prior = executor._adapters["fake"]
    if restart_change == "adapter-appeared":
        del executor._adapters["fake"]
    created = context.client.post(
        "/v1/admin/playground/media-jobs",
        json={
            "selector": {"provider_model_api_name": "media"},
            "kind": "image",
            "prompt": "Create one image.",
        },
        headers=context.administrator_headers,
    )
    assert created.status_code == HTTPStatus.ACCEPTED
    adapter: BlockingFakeAdapter = (
        ChangedUsageFakeAdapter()
        if restart_change == "usage"
        else BlockingFakeAdapter()
    )
    executor._adapters["fake"] = adapter
    try:
        assert asyncio.run(
            asyncio.wait_for(
                run_media_worker_once(
                    context.database_url,
                    context.executor,
                    cast("ObjectStore", context.objects),
                ),
                timeout=2,
            )
        )
    finally:
        adapter.release.set()
        executor._adapters["fake"] = prior
    assert adapter.calls == 0
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        expected_attempts = 1 if restart_change == "adapter-appeared" else 0
        assert connection.execute(
            """SELECT job.state, call.outcome,
                      (SELECT count(*) FROM router.raw_accounting_attempts
                       WHERE call_id = call.id) AS attempts
               FROM router.media_jobs AS job
               JOIN router.raw_accounting_calls AS call
                 ON call.id = job.logical_call_id
               WHERE job.id = %s""",
            (created.json()["id"],),
        ).fetchone() == {
            "state": "failed",
            "outcome": "failed",
            "attempts": expected_attempts,
        }


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


def test_structured_validator_dependency_failure_is_safe_and_stops_fallback(
    model_api_context: ModelApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record the attempt and stop the chain after a Router validation failure."""
    context = model_api_context
    with psycopg.connect(context.database_url) as connection:
        connection.execute(
            """INSERT INTO router.provider_models
                       (api_name, provider_id, model_id, provider_model_name,
                        enabled, input_modalities, output_modalities, capabilities,
                        constraints, reasoning_mappings)
                   SELECT 'backup', provider_id, model_id, 'fake-text-backup-v1',
                          enabled, input_modalities, output_modalities, capabilities,
                          constraints, reasoning_mappings
                   FROM router.provider_models WHERE api_name = 'primary'"""
        )
        connection.execute(
            """INSERT INTO router.assignment_candidates
                       (assignment_id, position, provider_model_id)
                   SELECT assignment.id, 2, mapping.id
                   FROM router.assignment_definitions AS assignment
                   JOIN router.services AS service ON service.id = assignment.service_id
                   CROSS JOIN router.provider_models AS mapping
                   WHERE service.api_name = 'alpha'
                     AND assignment.api_name = 'workflow'
                     AND mapping.api_name = 'backup'"""
        )

    def unavailable(_schema: object, _value: object) -> bool:
        raise OutputValidationUnavailableError

    monkeypatch.setattr(
        "llmrouter_backend.model_api._validate_structured_output", unavailable
    )
    body = _request()
    body["output_format"] = {"type": "json_schema", "schema_json": "{}"}
    response = context.client.post(
        "/v1/model-calls", json=body, headers=context.headers("alpha")
    )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "The Router could not validate the provider output.",
        }
    }
    with psycopg.connect(context.database_url, row_factory=dict_row) as read_connection:
        attempts = read_connection.execute(
            """SELECT provider_model_api_name, outcome, failure_class
               FROM router.raw_accounting_attempts ORDER BY position"""
        ).fetchall()
    assert attempts == [
        {
            "provider_model_api_name": "failure",
            "outcome": "failed",
            "failure_class": "transport",
        },
        {
            "provider_model_api_name": "primary",
            "outcome": "failed",
            "failure_class": "invalid_response",
        },
    ]


def test_invalid_provider_unicode_uses_fallback_before_public_output(
    model_api_context: ModelApiContext,
) -> None:
    """Reject non-UTF-8 provider strings before synchronous or SSE success."""
    context = model_api_context
    cast("Any", context.client.app).state.call_executor = CallExecutor(
        database_url=context.database_url,
        adapters={"fake": InvalidUnicodeAdapter()},
        object_store=cast("ObjectStore", context.objects),
    )

    synchronous = context.client.post(
        "/v1/model-calls", json=_request(), headers=context.headers("alpha")
    )
    assert synchronous.status_code == HTTPStatus.OK
    assert synchronous.json()["provider_model_api_name"] == "primary"

    streamed = context.client.post(
        "/v1/model-streams", json=_request(), headers=context.headers("alpha")
    )
    events = _events(streamed.text)
    assert events[0] == ("start", {"provider_model_api_name": "primary"})
    assert [event for event, _value in events] == [
        "start",
        "text_delta",
        "text_delta",
        "completed",
    ]

    tool_body = _request()
    tool_body["tools"] = [
        {
            "name": "lookup",
            "description": "Look up one value.",
            "input_schema_json": "{}",
        }
    ]
    tool_stream = context.client.post(
        "/v1/model-streams", json=tool_body, headers=context.headers("alpha")
    )
    tool_events = _events(tool_stream.text)
    assert tool_events[0] == ("start", {"provider_model_api_name": "primary"})
    assert [event for event, _value in tool_events] == [
        "start",
        "tool_call",
        "completed",
    ]

    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        attempts = connection.execute(
            """SELECT call.started_at, attempt.provider_model_api_name,
                      attempt.outcome, attempt.failure_class
               FROM router.raw_accounting_calls AS call
               JOIN router.raw_accounting_attempts AS attempt
                 ON attempt.call_id = call.id
               ORDER BY call.started_at, attempt.position"""
        ).fetchall()
    assert [
        (row["provider_model_api_name"], row["outcome"], row["failure_class"])
        for row in attempts
    ] == [
        ("failure", "failed", "invalid_response"),
        ("primary", "succeeded", None),
        ("failure", "failed", "invalid_response"),
        ("primary", "succeeded", None),
        ("failure", "failed", "invalid_response"),
        ("primary", "succeeded", None),
    ]


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
        before = _required(
            connection.execute(
                "SELECT count(*) FROM router.raw_accounting_attempts"
            ).fetchone()
        )
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


def test_administrator_http_body_rejects_ambiguous_and_huge_lengths(
    model_api_context: ModelApiContext,
) -> None:
    """Reject malformed transport lengths safely before admission or provider work."""
    context = model_api_context
    routes = (
        "/v1/admin/playground/model-calls",
        "/v1/admin/playground/model-streams",
        "/v1/admin/playground/embeddings",
        "/v1/admin/playground/media-jobs",
    )
    header_sets: tuple[dict[str, str] | list[tuple[str, str]], ...] = (
        {
            **context.administrator_headers,
            "Content-Type": "application/json",
            "Content-Length": "12x",
        },
        {
            **context.administrator_headers,
            "Content-Type": "application/json",
            "Content-Length": "9" * 5000,
        },
        [
            *context.administrator_headers.items(),
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("Content-Length", "2"),
        ],
    )
    with psycopg.connect(context.database_url) as connection:
        before = _required(
            connection.execute(
                """SELECT count(*) FROM router.raw_accounting_calls
                   WHERE call_actor = 'administrator'"""
            ).fetchone()
        )[0]
    for route in routes:
        for headers in header_sets:
            response = context.client.post(route, content=b"{}", headers=headers)
            assert response.status_code == HTTPStatus.BAD_REQUEST
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["error"]["code"] == "invalid_request"
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            """SELECT count(*) FROM router.raw_accounting_calls
               WHERE call_actor = 'administrator'"""
        ).fetchone() == (before,)


def test_administrator_result_models_reject_invalid_attempt_sequences() -> None:
    """Keep exact and successful wrapper attempt sequences closed at runtime."""
    succeeded = {
        "provider_model_api_name": "primary",
        "outcome": "succeeded",
        "elapsed_ms": 1,
    }
    failed = {
        "provider_model_api_name": "failure",
        "outcome": "failed",
        "elapsed_ms": 1,
        "error": {
            "code": "upstream_failed",
            "message": "The provider attempt failed.",
        },
    }
    usage = {"units": [], "cost": "0", "currency": "USD"}
    model_document = {
        "logical_call_id": "call",
        "selector": {
            "service_api_name": "alpha",
            "assignment_api_name": "workflow",
        },
        "elapsed_ms": 2,
        "attempts": [failed, succeeded],
        "result": {
            "output_type": "standard",
            "provider_model_api_name": "primary",
            "content": [{"type": "text", "text": "ok"}],
            "usage": usage,
        },
    }
    AdministratorModelCallResult.model_validate(model_document)
    invalid_model = {**model_document, "attempts": [succeeded, failed]}
    with pytest.raises(ValidationError):
        AdministratorModelCallResult.model_validate(invalid_model)
    exact_model = {
        **model_document,
        "selector": {"provider_model_api_name": "primary"},
        "attempts": [failed, succeeded],
    }
    with pytest.raises(ValidationError):
        AdministratorModelCallResult.model_validate(exact_model)
    with pytest.raises(ValidationError):
        AdministratorEmbeddingResult.model_validate(
            {
                "logical_call_id": "call",
                "selector": {"provider_model_api_name": "embedding"},
                "elapsed_ms": 2,
                "attempts": [failed, succeeded],
                "result": {
                    "provider_model_api_name": "embedding",
                    "embeddings": [{"index": 0, "values": [1]}],
                    "usage": usage,
                },
            }
        )
    now = datetime.now(tz=UTC)
    with pytest.raises(ValidationError):
        AdministratorMediaJob.model_validate(
            {
                "id": "job",
                "logical_call_id": "call",
                "selector": {
                    "service_api_name": "alpha",
                    "assignment_api_name": "workflow",
                },
                "provider_model_api_name": "primary",
                "kind": "image",
                "state": "succeeded",
                "attempts": [succeeded, succeeded],
                "elapsed_ms": 2,
                "content": {"media_type": "image/png", "size_bytes": 1},
                "created_at": now,
                "completed_at": now,
            }
        )
    summary = {
        "id": "log",
        "logical_call_id": "call",
        "call_actor": "administrator",
        "administrator_subject": "administrator-subject",
        "provider_model_api_name": "primary",
        "kind": "model",
        "outcome": "failed",
        "started_at": now,
    }
    RequestLogSummary.model_validate(summary)
    for invalid in (
        {**summary, "provider_model_api_name": None},
        {
            **summary,
            "assignment_api_name": "workflow",
            "provider_model_api_name": None,
        },
        {**summary, "configuration_service_api_name": "alpha"},
    ):
        with pytest.raises(ValidationError):
            RequestLogSummary.model_validate(invalid)


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


def test_administrator_pre_first_stream_cancellation_leaves_no_orphan_call(
    model_api_context: ModelApiContext,
) -> None:
    """Cancel provider work and finalize accounting before the first SSE event."""
    context = model_api_context
    adapter = BlockingFakeAdapter()
    executor = cast("Any", context.executor)
    prior = executor._adapters["fake"]
    executor._adapters["fake"] = adapter
    logical_call_id = uuid.uuid4()

    async def run() -> None:
        body = AdministratorModelCallRequest.model_validate(
            _administrator_request(assignment=None, provider_model="primary")
        )
        call = internal_model_call(body, streaming=True)
        actor = AdministratorActor(
            b"",
            "https://identity.test",
            "administrator-subject",
            "Administrator",
            datetime.now(tz=UTC) + timedelta(hours=1),
            "",
            b"",
        )
        queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()

        async def write(_output: ProviderOutput) -> None:
            raise AssertionError

        async def start(_provider_model_api_name: str) -> None:
            raise AssertionError

        execution = asyncio.create_task(
            context.executor.execute(
                actor,
                call,
                write_visible_output=write,
                start_visible_output=start,
                call_id=logical_call_id,
            )
        )
        waiting = asyncio.create_task(
            _first_administrator_stream_event(
                queue,
                execution,
                logical_call_id,
                body.selector.model_dump(mode="json"),
            )
        )
        assert await asyncio.to_thread(adapter.entered.wait, 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

    try:
        asyncio.run(run())
    finally:
        adapter.release.set()
        executor._adapters["fake"] = prior
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            """SELECT outcome FROM router.raw_accounting_calls
               WHERE id = %s""",
            (logical_call_id,),
        ).fetchone() == ("failed",)
        assert connection.execute(
            """SELECT count(*) FROM router.raw_accounting_attempts
               WHERE call_id = %s""",
            (logical_call_id,),
        ).fetchone() == (1,)


def test_structured_schema_validation_has_a_hard_resource_boundary() -> None:
    """Stop a pathological regular expression without blocking the web process."""
    started = monotonic()
    with pytest.raises(OutputValidationUnavailableError):
        _validate_structured_output(
            {"type": "string", "pattern": "^(a+)+$"},
            "a" * 5000 + "b",
        )
    assert monotonic() - started < 3
