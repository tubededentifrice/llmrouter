"""Closed synchronous native embedding API contract tests."""
# ruff: noqa: D102

from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import create_app
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.security import ControlKeys
from llmrouter_backend.store import create_key
from psycopg.rows import dict_row

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable
    from pathlib import Path


@dataclass(slots=True)
class EmbeddingApiContext:
    """Keep two isolated services and one deterministic embedding chain."""

    database_url: str
    client: TestClient
    keys: dict[str, str]

    def headers(self, service: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[service]}"}


@pytest.fixture
def embedding_api_context(database_url: str, tmp_path: Path) -> EmbeddingApiContext:
    """Create one complete network-free native embedding service."""
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
            if service == "beta":
                connection.execute(
                    """INSERT INTO router.workspaces
                           (service_id, api_name, display_name)
                       VALUES (%s, 'beta-only', 'Beta only')""",
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
        _seed_embedding_catalog(connection, service_ids["alpha"])
    return EmbeddingApiContext(
        database_url,
        TestClient(
            create_app(database_url=database_url, settings=settings),
            base_url="http://127.0.0.1:8010",
        ),
        keys,
    )


def _seed_embedding_catalog(
    connection: psycopg.Connection[Any], service_id: uuid.UUID
) -> None:
    price = {
        "currency": "USD",
        "unit_prices": [
            {"unit": "input_token", "amount": "0.01"},
            {"unit": "request", "amount": "0.10"},
            {"unit": "provider_unit", "amount": "0.20"},
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
           VALUES ('embedding-model', 'Embedding model', ARRAY['text'],
                   ARRAY['embedding'], ARRAY[]::text[],
                   '{"embedding_dimensions":[3]}'::jsonb, %s::jsonb)""",
        (json.dumps(price),),
    )
    for api_name, wire_name in (
        ("embedding-failure", "fake-error-transport-v1"),
        ("embedding-primary", "fake-embedding-v1"),
    ):
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name,
                    enabled, input_modalities, output_modalities, capabilities,
                    constraints, reasoning_mappings)
               SELECT %s, provider.id, model.id, %s, true,
                      model.input_modalities, model.output_modalities,
                      model.capabilities, model.constraints, '[]'::jsonb
               FROM router.provider_connections AS provider,
                    router.canonical_models AS model
               WHERE provider.api_name = 'fake-provider'
                 AND model.api_name = 'embedding-model'""",
            (api_name, wire_name),
        )
    assignment = connection.execute(
        """INSERT INTO router.assignment_definitions
               (service_id, api_name, display_name)
           VALUES (%s, 'embeddings', 'Embeddings') RETURNING id""",
        (service_id,),
    ).fetchone()
    assert assignment is not None
    for position, candidate in enumerate(("embedding-failure", "embedding-primary")):
        connection.execute(
            """INSERT INTO router.assignment_candidates
                   (assignment_id, position, provider_model_id)
               SELECT %s, %s, id FROM router.provider_models WHERE api_name = %s""",
            (assignment["id"], position, candidate),
        )


def _request(
    *, assignment: str | None = "embeddings", provider_model: str | None = None
) -> dict[str, object]:
    selector = (
        {"assignment_api_name": assignment}
        if assignment is not None
        else {"provider_model_api_name": provider_model}
    )
    return {
        "workspace_api_name": "main",
        "selector": selector,
        "inputs": ["first", "second"],
        "tags": ["zeta", "alpha", "zeta"],
    }


def test_embedding_batch_uses_full_fallback_and_records_exact_facts(
    embedding_api_context: EmbeddingApiContext,
) -> None:
    """Return ordered vectors and keep both attempts, tags, usage, cost, and logs."""
    context = embedding_api_context
    response = context.client.post(
        "/v1/embeddings", json=_request(), headers=context.headers("alpha")
    )

    assert response.status_code == HTTPStatus.OK
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "provider_model_api_name": "embedding-primary",
        "embeddings": [
            {"index": 0, "values": [0.0, 0.0, 0.0]},
            {"index": 1, "values": [1.0, 0.0, 0.0]},
        ],
        "usage": {
            "units": [
                {"unit": "input_token", "quantity": "3"},
                {"unit": "request", "quantity": "1"},
                {"unit": "provider_unit", "quantity": "0.5"},
            ],
            "cost": "0.23",
            "currency": "USD",
        },
    }
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        call = connection.execute(
            """SELECT id, tags, assignment_api_name, outcome
               FROM router.raw_accounting_calls"""
        ).fetchone()
        assert call is not None
        assert call["tags"] == ["alpha", "zeta"]
        assert call["assignment_api_name"] == "embeddings"
        assert call["outcome"] == "succeeded"
        attempts = connection.execute(
            """SELECT provider_model_api_name, outcome, failure_class
               FROM router.raw_accounting_attempts
               WHERE call_id = %s ORDER BY position""",
            (call["id"],),
        ).fetchall()
        assert attempts == [
            {
                "provider_model_api_name": "embedding-failure",
                "outcome": "failed",
                "failure_class": "transport",
            },
            {
                "provider_model_api_name": "embedding-primary",
                "outcome": "succeeded",
                "failure_class": None,
            },
        ]
        log = connection.execute(
            """SELECT kind, request_json, response_json
               FROM router.request_logs WHERE id = %s""",
            (call["id"],),
        ).fetchone()
        assert log is not None
        assert log["kind"] == "embedding"
        assert json.loads(log["request_json"])["inputs"] == ["first", "second"]
        assert json.loads(log["response_json"]) == [
            {"kind": "embedding", "value": [[0, 0, 0], [1, 0, 0]]}
        ]
        media_count = connection.execute(
            "SELECT count(*) FROM router.media_objects WHERE request_log_id = %s",
            (call["id"],),
        ).fetchone()
        assert media_count == {"count": 0}


def test_embedding_exact_selection_has_no_fallback(
    embedding_api_context: EmbeddingApiContext,
) -> None:
    """Use one exact route and do not continue to the assignment primary route."""
    context = embedding_api_context
    response = context.client.post(
        "/v1/embeddings",
        json=_request(assignment=None, provider_model="embedding-failure"),
        headers=context.headers("alpha"),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json() == {
        "error": {
            "code": "upstream_failed",
            "message": "Each eligible provider-model failed.",
        }
    }
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        attempts = connection.execute(
            "SELECT provider_model_api_name FROM router.raw_accounting_attempts"
        ).fetchall()
    assert attempts == [{"provider_model_api_name": "embedding-failure"}]


def test_embedding_service_workspace_and_identity_isolation_precede_provider_work(
    embedding_api_context: EmbeddingApiContext,
) -> None:
    """Reject missing auth and a foreign workspace without an accounting attempt."""
    context = embedding_api_context
    missing = context.client.post("/v1/embeddings", json=_request())
    foreign = context.client.post(
        "/v1/embeddings", json=_request(), headers=context.headers("beta")
    )
    malformed_key = context.client.post(
        "/v1/embeddings",
        json=_request(),
        headers={"Authorization": "Bearer invalid-control"},
    )
    administrator_cookie = context.client.post(
        "/v1/embeddings",
        json=_request(),
        headers={"Cookie": "llmrouter_admin_session=administrator-control"},
    )

    assert missing.status_code == HTTPStatus.UNAUTHORIZED
    assert foreign.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert foreign.json()["error"]["code"] == "provider_unavailable"
    assert malformed_key.status_code == HTTPStatus.UNAUTHORIZED
    assert administrator_cookie.status_code == HTTPStatus.UNAUTHORIZED
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.raw_accounting_attempts"
        ).fetchone() == {"count": 0}

    foreign_workspace = _request()
    foreign_workspace["workspace_api_name"] = "beta-only"
    denied = context.client.post(
        "/v1/embeddings",
        json=foreign_workspace,
        headers=context.headers("alpha"),
    )
    assert denied.status_code == HTTPStatus.NOT_FOUND
    assert denied.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(inputs=[]),
        lambda body: body.update(inputs=["x"] * 33),
        lambda body: body.update(inputs=[""]),
        lambda body: body.update(inputs=["x" * 32_769]),
        lambda body: body.update(inputs=["é" * 16_385]),
        lambda body: body.update(inputs=["x" * 32_768] * 9),
        lambda body: body.update(tags=None),
        lambda body: body.update(excluded_provider_model_api_names=[]),
        lambda body: body.update(idempotency_key="removed"),
        lambda body: body.update(inputs=[True]),
    ],
)
def test_embedding_request_bounds_and_removed_fields_are_closed(
    embedding_api_context: EmbeddingApiContext,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    """Reject each count, byte, scalar, null, and removed-surface violation."""
    context = embedding_api_context
    body = _request()
    mutate(body)
    response = context.client.post(
        "/v1/embeddings", json=body, headers=context.headers("alpha")
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"] == {
        "code": "invalid_request",
        "message": "The request is invalid.",
        "details": {
            "field": "body",
            "reason": "The embedding call does not match the contract.",
        },
    }


@pytest.mark.parametrize(
    "inputs",
    [
        ["x" * 32_768],
        ["x" * 8_192] * 32,
        ["\x00" * 32_768] * 8,
    ],
)
def test_embedding_accepts_each_exact_native_input_boundary(
    embedding_api_context: EmbeddingApiContext, inputs: list[str]
) -> None:
    """Accept the exact per-item, total-byte, and item-count boundaries."""
    body = _request()
    body["inputs"] = inputs
    response = embedding_api_context.client.post(
        "/v1/embeddings",
        json=body,
        headers=embedding_api_context.headers("alpha"),
    )
    assert response.status_code == HTTPStatus.OK
    assert len(response.json()["embeddings"]) == len(inputs)


def test_embedding_duplicate_json_and_durable_surfaces_stay_absent(
    embedding_api_context: EmbeddingApiContext,
) -> None:
    """Reject ambiguous JSON and keep job, status, replay, and result routes absent."""
    context = embedding_api_context
    duplicate = context.client.post(
        "/v1/embeddings",
        content=(
            b'{"workspace_api_name":"main","workspace_api_name":"other",'
            b'"selector":{"assignment_api_name":"embeddings"},"inputs":["x"]}'
        ),
        headers={
            **context.headers("alpha"),
            "Content-Type": "application/json",
        },
    )
    assert duplicate.status_code == HTTPStatus.BAD_REQUEST
    for method, route in (
        ("get", "/v1/embeddings/request-id"),
        ("get", "/v1/embeddings/request-id/status"),
        ("post", "/v1/embeddings/request-id/cancel"),
        ("post", "/v1/embeddings/request-id/replay"),
        ("get", "/v1/embeddings/request-id/result"),
    ):
        response = context.client.request(
            method, route, headers=context.headers("alpha")
        )
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.json()["error"]["code"] == "not_found"
