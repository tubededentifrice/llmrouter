"""PostgreSQL contract tests for global provider and model configuration."""
# ruff: noqa: D102, D107, FBT001, PLR0913, PLR0917, S105, SIM117

from __future__ import annotations

import concurrent.futures
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import catalog, create_app
from llmrouter_backend.catalog import (
    ProviderCredentialKeys,
    resolve_credential,
    resolve_provider_route,
    validate_assignment_reasoning,
    validate_route_constraints,
)
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.errors import ApiError
from llmrouter_backend.models import (
    ModelConstraints,
    ModelWrite,
    ProviderModelWrite,
    ProviderWrite,
)
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import create_administrator_session
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from pathlib import Path

ADMIN_ORIGIN = "http://127.0.0.1:5174"


class CatalogContext:
    """Authenticated local administrator and database test controls."""

    def __init__(self, database_url: str, settings: Settings) -> None:
        self.database_url = database_url
        self.settings = settings
        self.controls = ControlKeys.load(settings)
        self.credential_keys = ProviderCredentialKeys.load(settings)
        self.client = TestClient(
            create_app(database_url=database_url, settings=settings),
            base_url="https://llmrouter.test",
        )
        self.session = new_token()
        self.csrf = new_token()
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            create_administrator_session(
                connection,
                session_verifier=self.controls.verifier(self.session),
                csrf_verifier=self.controls.verifier(self.csrf),
                encrypted_csrf_token=self.controls.encrypt({"csrf_token": self.csrf}),
                issuer="https://identity.example.test",
                subject="administrator",
                display_name="Administrator",
                expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            )

    @property
    def write_headers(self) -> dict[str, str]:
        return {
            "Cookie": f"llmrouter_admin_session={self.session}",
            "Origin": ADMIN_ORIGIN,
            "X-CSRF-Token": self.csrf,
        }

    @property
    def read_headers(self) -> dict[str, str]:
        return {"Cookie": f"llmrouter_admin_session={self.session}"}


def assert_provider_model_cooldown(context: CatalogContext) -> None:
    """Check current cooldown data in list and item administrator reads."""
    executor = cast("Any", context.client.app).state.call_executor
    for _ in range(3):
        executor.cooldowns.record_failure("fake-text", "timeout")
    cooldown_list = context.client.get(
        "/v1/admin/provider-models", headers=context.read_headers
    )
    assert cooldown_list.status_code == HTTPStatus.OK
    cooldown = cooldown_list.json()["items"][0]["cooldown"]
    assert cooldown["reason"] == "timeout"
    assert datetime.fromisoformat(cooldown["until"]) > datetime.now(tz=UTC)
    cooldown_item = context.client.get(
        "/v1/admin/provider-models/fake-text", headers=context.read_headers
    )
    assert cooldown_item.json()["cooldown"]["reason"] == "timeout"


@pytest.fixture
def catalog_settings(tmp_path: Path) -> Settings:
    """Create separate administrator and provider wrapping keys."""
    digest = tmp_path / "admin-digest"
    encryption = tmp_path / "admin-encryption"
    wrapping = tmp_path / "provider-wrapping"
    digest.write_text("d" * 64, encoding="utf-8")
    encryption.write_text("e" * 64, encoding="utf-8")
    wrapping.write_text("w" * 64, encoding="utf-8")
    return Settings(
        administrator_digest_key_file=digest,
        administrator_encryption_key_file=encryption,
        provider_credential_wrapping_key_file=wrapping,
        allowed_origins=(ADMIN_ORIGIN,),
    )


@pytest.fixture
def catalog_database(database_url: str) -> str:
    """Apply the complete clean schema."""
    with psycopg.connect(database_url) as connection:
        migrate(connection)
    return database_url


@pytest.mark.parametrize(
    ("api_name", "inputs", "outputs", "constraints"),
    [
        (
            "boolean-embedding",
            ["text"],
            ["embedding"],
            {"embedding_dimensions": [True]},
        ),
        (
            "boolean-image-count",
            ["text", "image"],
            ["text"],
            {"max_input_images": True, "max_input_image_bytes": 1000},
        ),
        (
            "boolean-image-size",
            ["text", "image"],
            ["text"],
            {"max_input_images": 1, "max_input_image_bytes": True},
        ),
        (
            "boolean-duration",
            ["text"],
            ["video"],
            {"max_output_duration_seconds": True},
        ),
    ],
)
def test_model_constraint_integer_fields_reject_booleans(
    catalog_database: str,
    catalog_settings: Settings,
    api_name: str,
    inputs: list[str],
    outputs: list[str],
    constraints: dict[str, Any],
) -> None:
    """Reject JSON Boolean values where the native contract requires integers."""
    context = CatalogContext(catalog_database, catalog_settings)
    response = context.client.post(
        "/v1/admin/models",
        json={
            "api_name": api_name,
            "display_name": "Boolean constraint",
            "input_modalities": inputs,
            "output_modalities": outputs,
            "capabilities": [],
            "constraints": constraints,
        },
        headers=context.write_headers,
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"


def test_credential_envelopes_rotate_snapshot_and_never_leave_control_fields(  # noqa: PLR0915
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Keep credential values write-only and effective at transaction commit."""
    context = CatalogContext(catalog_database, catalog_settings)
    secret_one = "secret-value-one-that-looks-like-Bearer-control"
    created = context.client.post(
        "/v1/admin/credentials",
        json={"api_name": "primary", "secret": secret_one},
        headers=context.write_headers,
    )
    assert created.status_code == HTTPStatus.CREATED
    assert created.headers["cache-control"] == "no-store"
    assert set(created.json()) == {
        "api_name",
        "fingerprint",
        "created_at",
        "updated_at",
    }
    assert secret_one not in created.text

    mismatched = context.client.put(
        "/v1/admin/credentials/primary",
        json={"api_name": "other", "secret": "mismatched-value"},
        headers=context.write_headers,
    )
    assert mismatched.status_code == HTTPStatus.BAD_REQUEST
    assert "mismatched-value" not in mismatched.text

    with psycopg.connect(catalog_database, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM router.provider_credentials WHERE api_name = 'primary'"
        ).fetchone()
        assert row is not None
        assert secret_one.encode() not in bytes(row["encrypted_secret"])
        assert secret_one not in json.dumps(row, default=str)
        snapshot = resolve_credential(connection, "primary", context.credential_keys)
        assert snapshot == secret_one

    secret_two = "replacement-value-two"
    replaced = context.client.put(
        "/v1/admin/credentials/primary",
        json={"api_name": "primary", "secret": secret_two},
        headers=context.write_headers,
    )
    assert replaced.status_code == HTTPStatus.OK
    assert secret_two not in replaced.text
    assert snapshot == secret_one
    with psycopg.connect(catalog_database, row_factory=dict_row) as connection:
        assert (
            resolve_credential(connection, "primary", context.credential_keys)
            == secret_two
        )

    wrapping_path = catalog_settings.provider_credential_wrapping_key_file
    assert wrapping_path is not None
    wrong_key_path = wrapping_path.parent / "wrong"
    wrong_key_path.write_text("x" * 64, encoding="utf-8")
    wrong_settings = Settings(
        provider_credential_wrapping_key_file=wrong_key_path,
        allowed_origins=(ADMIN_ORIGIN,),
    )
    with psycopg.connect(catalog_database, row_factory=dict_row) as connection:
        with pytest.raises(Exception, match="provider credential is not available"):
            resolve_credential(
                connection, "primary", ProviderCredentialKeys.load(wrong_settings)
            )

    provider = context.client.post(
        "/v1/admin/providers",
        json={
            "api_name": "openai",
            "display_name": "OpenAI",
            "adapter": "openai",
            "credential_api_name": "primary",
            "enabled": True,
        },
        headers=context.write_headers,
    )
    assert provider.status_code == HTTPStatus.CREATED
    assert (
        context.client.delete(
            "/v1/admin/credentials/primary", headers=context.write_headers
        ).status_code
        == HTTPStatus.CONFLICT
    )
    assert (
        context.client.delete(
            "/v1/admin/providers/openai", headers=context.write_headers
        ).status_code
        == HTTPStatus.NO_CONTENT
    )
    assert (
        context.client.delete(
            "/v1/admin/credentials/primary", headers=context.write_headers
        ).status_code
        == HTTPStatus.NO_CONTENT
    )

    unavailable_path = wrapping_path.with_suffix(".unavailable")
    wrapping_path.rename(unavailable_path)
    try:
        unavailable = context.client.post(
            "/v1/admin/credentials",
            json={"api_name": "unavailable", "secret": "not-stored"},
            headers=context.write_headers,
        )
    finally:
        unavailable_path.rename(wrapping_path)
    assert unavailable.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert unavailable.json()["error"]["code"] == "provider_unavailable"

    activity = context.client.get(
        "/v1/admin/activity",
        params={
            "from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
            "to": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
        },
        headers=context.read_headers,
    )
    assert activity.status_code == HTTPStatus.OK
    assert secret_one not in activity.text
    assert secret_two not in activity.text
    assert any(
        item["action"] == "credential.create" and item["result"] == "failed"
        for item in activity.json()["items"]
    )
    assert any(
        item["action"] == "credential.update" and item["result"] == "failed"
        for item in activity.json()["items"]
    )


def test_provider_wrapping_key_cannot_reuse_administrator_control_key(
    catalog_settings: Settings, tmp_path: Path
) -> None:
    """Keep credential envelopes separate from session and verifier controls."""
    with pytest.raises(ValueError, match="must have one purpose"):
        replace(
            catalog_settings,
            provider_credential_wrapping_key_file=(
                catalog_settings.administrator_encryption_key_file
            ),
        )
    copied_control = tmp_path / "copied-administrator-key"
    administrator_path = catalog_settings.administrator_encryption_key_file
    assert administrator_path is not None
    copied_control.write_bytes(administrator_path.read_bytes())
    copied_settings = replace(
        catalog_settings, provider_credential_wrapping_key_file=copied_control
    )
    with pytest.raises(Exception, match="provider credential is not available"):
        ProviderCredentialKeys.load(copied_settings)

    object_access = tmp_path / "object-access"
    object_secret = tmp_path / "object-secret"
    object_access.write_text("access", encoding="utf-8")
    object_secret.write_bytes(copied_control.read_bytes())
    with pytest.raises(ValueError, match="must have one purpose"):
        replace(
            catalog_settings,
            object_store_endpoint="http://127.0.0.1:9000",
            object_store_bucket="controls",
            object_store_access_key_file=object_access,
            object_store_secret_key_file=object_secret,
            provider_credential_wrapping_key_file=object_secret,
        )
    object_settings = replace(
        catalog_settings,
        object_store_endpoint="http://127.0.0.1:9000",
        object_store_bucket="controls",
        object_store_access_key_file=object_access,
        object_store_secret_key_file=object_secret,
        provider_credential_wrapping_key_file=copied_control,
    )
    with pytest.raises(Exception, match="provider credential is not available"):
        ProviderCredentialKeys.load(object_settings)


@pytest.mark.parametrize(
    "invalid_kind", ["symlink", "hardlink", "nonregular", "empty", "short", "oversized"]
)
def test_provider_wrapping_key_requires_one_bounded_regular_file(
    catalog_settings: Settings, tmp_path: Path, invalid_kind: str
) -> None:
    """Reject linked, non-regular, empty, short, and oversized key files."""
    original = catalog_settings.provider_credential_wrapping_key_file
    assert original is not None
    invalid = tmp_path / f"invalid-{invalid_kind}"
    if invalid_kind == "symlink":
        invalid.symlink_to(original)
    elif invalid_kind == "hardlink":
        os.link(original, invalid)
    elif invalid_kind == "nonregular":
        invalid.mkdir()
    elif invalid_kind == "empty":
        invalid.write_bytes(b"")
    elif invalid_kind == "short":
        invalid.write_bytes(b"short")
    else:
        invalid.write_bytes(b"x" * 10_001)
    settings = replace(catalog_settings, provider_credential_wrapping_key_file=invalid)
    with pytest.raises(Exception, match="provider credential is not available"):
        ProviderCredentialKeys.load(settings)


@pytest.mark.parametrize(
    ("adapter", "endpoint", "credential", "accepted"),
    [
        ("openai", None, "primary", True),
        ("openrouter", None, "primary", True),
        ("wavespeed", None, "primary", True),
        ("openai_compatible", "https://api.example.test/v1", "primary", True),
        ("openai_compatible", "https://api.example.test/v1", None, True),
        ("custom", "http://127.0.0.1:9000/v1", "primary", True),
        ("custom", "http://127.0.0.1:9000/v1", None, True),
        ("ollama", "http://localhost:11434", None, True),
        ("ollama", "http://localhost:11434", "primary", True),
        ("local_embeddings", None, None, True),
        ("local_embeddings", "http://[::1]:8080", None, False),
        ("fake", None, None, True),
        ("openai", "https://api.example.test", "primary", False),
        ("openai_compatible", "http://api.example.test", "primary", False),
        ("custom", "https://127.0.0.2/v1", "primary", False),
        ("custom", "https://user:pass@example.test/v1", "primary", False),
        ("custom", "https://api.example.test/v1?token=x", "primary", False),
        ("custom", "https://[broken", "primary", False),
        ("custom", "https://example.test\uff1a443", "primary", False),
        ("ollama", "https://api.example.test", None, True),
        ("fake", None, "primary", False),
    ],
)
def test_adapter_schemas_and_endpoint_trust_are_closed(
    catalog_database: str,
    catalog_settings: Settings,
    adapter: str,
    endpoint: str | None,
    credential: str | None,
    accepted: bool,
) -> None:
    """Accept only the eight registered adapters and their safe settings."""
    context = CatalogContext(catalog_database, catalog_settings)
    assert (
        context.client.post(
            "/v1/admin/credentials",
            json={"api_name": "primary", "secret": "control"},
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    document: dict[str, Any] = {
        "api_name": "provider",
        "display_name": "Provider",
        "adapter": adapter,
        "enabled": True,
    }
    if endpoint is not None:
        document["endpoint"] = endpoint
    if credential is not None:
        document["credential_api_name"] = credential
    response = context.client.post(
        "/v1/admin/providers", json=document, headers=context.write_headers
    )
    assert response.status_code == (
        HTTPStatus.CREATED if accepted else HTTPStatus.BAD_REQUEST
    )
    assert "control" not in response.text

    unknown_setting = context.client.post(
        "/v1/admin/providers",
        json={**document, "api_name": "other", "headers": {"Authorization": "x"}},
        headers=context.write_headers,
    )
    assert unknown_setting.status_code == HTTPStatus.BAD_REQUEST
    for removed_adapter in (
        "anthropic",
        "zai",
        "chatgpt_subscription",
        "codex_subscription",
        "azure_openai",
    ):
        removed = context.client.post(
            "/v1/admin/providers",
            json={**document, "api_name": "removed", "adapter": removed_adapter},
            headers=context.write_headers,
        )
        assert removed.status_code == HTTPStatus.BAD_REQUEST


def test_global_models_mappings_reasoning_and_service_visibility(
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Narrow model capabilities and show enabled mappings to all services."""
    context = CatalogContext(catalog_database, catalog_settings)
    client = context.client
    assert (
        client.post(
            "/v1/admin/providers",
            json={
                "api_name": "fake",
                "display_name": "Fake",
                "adapter": "fake",
                "enabled": True,
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    for invalid_model in (
        {
            "api_name": "unbounded-image",
            "display_name": "Unbounded image",
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "capabilities": [],
        },
        {
            "api_name": "unbounded-embedding",
            "display_name": "Unbounded embedding",
            "input_modalities": ["text"],
            "output_modalities": ["embedding"],
            "capabilities": [],
        },
        {
            "api_name": "unbounded-media",
            "display_name": "Unbounded media",
            "input_modalities": ["text"],
            "output_modalities": ["video"],
            "capabilities": [],
        },
        {
            "api_name": "invalid-embedding-dimension",
            "display_name": "Invalid embedding dimension",
            "input_modalities": ["text"],
            "output_modalities": ["embedding"],
            "capabilities": [],
            "constraints": {"embedding_dimensions": [65537]},
        },
        {
            "api_name": "unknown-price-source",
            "display_name": "Unknown price source",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "capabilities": [],
            "price_source": "unknown",
            "price_lookup_key": "wire-model",
        },
        {
            "api_name": "mixed-price-authority",
            "display_name": "Mixed price authority",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "capabilities": [],
            "price_source": "openrouter",
            "price_lookup_key": "wire-model",
            "manual_price": {
                "currency": "USD",
                "unit_prices": [{"unit": "request", "amount": "1"}],
            },
        },
        {
            "api_name": "manual-synchronization-metadata",
            "display_name": "Manual synchronization metadata",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "capabilities": [],
            "manual_price": {
                "currency": "USD",
                "unit_prices": [{"unit": "request", "amount": "1"}],
                "source": "openrouter",
            },
        },
        {
            "api_name": "invalid-media-capability",
            "display_name": "Invalid media capability",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "capabilities": ["streaming"],
        },
    ):
        assert (
            client.post(
                "/v1/admin/models",
                json=invalid_model,
                headers=context.write_headers,
            ).status_code
            == HTTPStatus.BAD_REQUEST
        )
    model = {
        "api_name": "text-model",
        "display_name": "Text model",
        "input_modalities": ["text", "image"],
        "output_modalities": ["text", "structured_json"],
        "capabilities": ["tool_calling", "streaming", "reasoning"],
        "constraints": {"max_input_images": 4, "max_input_image_bytes": 1000},
    }
    assert (
        client.post(
            "/v1/admin/models", json=model, headers=context.write_headers
        ).status_code
        == HTTPStatus.CREATED
    )
    mapping = {
        "api_name": "fake-text",
        "provider_api_name": "fake",
        "model_api_name": "text-model",
        "provider_model_name": "wire-text",
        "enabled": True,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "capabilities": ["streaming", "reasoning"],
        "reasoning_mappings": [
            {"level": "none", "provider_value": "off"},
            {"level": "low", "provider_value": "low"},
            {"level": "medium", "provider_value": "normal"},
            {"level": "high", "provider_value": "high"},
        ],
    }
    assert (
        client.post(
            "/v1/admin/provider-models", json=mapping, headers=context.write_headers
        ).status_code
        == HTTPStatus.CREATED
    )
    assert_provider_model_cooldown(context)
    with psycopg.connect(catalog_database, row_factory=dict_row) as connection:
        route = resolve_provider_route(
            connection,
            "fake-text",
            required_inputs=frozenset({"text"}),
            required_output="text",
            required_capabilities=frozenset({"streaming"}),
            reasoning_level=None,
        )
        assert route.reasoning_level == "medium"
        assert route.provider_reasoning_value == "normal"
        validate_route_constraints(route)
        with pytest.raises(ApiError) as image_error:
            validate_route_constraints(route, input_image_sizes=(1,))
        assert image_error.value.field == "images"
        validate_assignment_reasoning(connection, ["fake-text"], "high")
        with pytest.raises(ApiError) as duplicate_candidate:
            validate_assignment_reasoning(
                connection, ["fake-text", "fake-text"], "high"
            )
        assert duplicate_candidate.value.field == "candidates"
        for invalid_chain in ([], ["fake-text"] * 17):
            with pytest.raises(ApiError) as chain_size:
                validate_assignment_reasoning(connection, invalid_chain, "high")
            assert chain_size.value.field == "candidates"
        with pytest.raises(Exception, match="does not support"):
            resolve_provider_route(
                connection,
                "fake-text",
                required_inputs=frozenset({"image"}),
                required_output="image",
                required_capabilities=frozenset(),
                reasoning_level=None,
            )

    incomplete_reasoning = client.post(
        "/v1/admin/provider-models",
        json={
            **mapping,
            "api_name": "bad-reasoning",
            "provider_model_name": "bad-reasoning",
            "reasoning_mappings": [{"level": "none", "provider_value": "off"}],
        },
        headers=context.write_headers,
    )
    assert incomplete_reasoning.status_code == HTTPStatus.BAD_REQUEST
    control_reasoning = client.post(
        "/v1/admin/provider-models",
        json={
            **mapping,
            "api_name": "bad-control",
            "provider_model_name": "bad-control",
            "reasoning_mappings": [
                {"level": "none", "provider_value": "off"},
                {"level": "low", "provider_value": "low\r\nheader"},
                {"level": "medium", "provider_value": "medium"},
                {"level": "high", "provider_value": "high"},
            ],
        },
        headers=context.write_headers,
    )
    assert control_reasoning.status_code == HTTPStatus.BAD_REQUEST
    broader = client.post(
        "/v1/admin/provider-models",
        json={
            **mapping,
            "api_name": "bad-capability",
            "provider_model_name": "bad-capability",
            "output_modalities": ["image"],
        },
        headers=context.write_headers,
    )
    assert broader.status_code == HTTPStatus.BAD_REQUEST
    valid_model_update = client.put(
        "/v1/admin/models/text-model",
        json={**model, "display_name": "Updated text model"},
        headers=context.write_headers,
    )
    assert valid_model_update.status_code == HTTPStatus.OK
    invalid_model_update = client.put(
        "/v1/admin/models/text-model",
        json={**model, "input_modalities": ["image"]},
        headers=context.write_headers,
    )
    assert invalid_model_update.status_code == HTTPStatus.BAD_REQUEST
    assert (
        client.get("/v1/admin/models/text-model", headers=context.read_headers).json()[
            "display_name"
        ]
        == "Updated text model"
    )

    service_keys: list[str] = []
    for service in ("alpha", "beta"):
        assert (
            client.post(
                "/v1/admin/services",
                json={"api_name": service, "display_name": service.title()},
                headers=context.write_headers,
            ).status_code
            == HTTPStatus.CREATED
        )
        key = client.post(
            f"/v1/admin/services/{service}/keys",
            json={"name": "caller"},
            headers=context.write_headers,
        ).json()["secret"]
        service_keys.append(key)
    for key in service_keys:
        response = client.get(
            "/v1/provider-models", headers={"Authorization": f"Bearer {key}"}
        )
        assert response.status_code == HTTPStatus.OK
        assert [item["api_name"] for item in response.json()["items"]] == ["fake-text"]
        assert "provider_api_name" not in response.text
        assert "wire-text" not in response.text

    denied_admin = client.get(
        "/v1/admin/models", headers={"Authorization": f"Bearer {service_keys[0]}"}
    )
    assert denied_admin.status_code == HTTPStatus.UNAUTHORIZED
    assert (
        client.get("/v1/provider-models", headers=context.read_headers).status_code
        == HTTPStatus.UNAUTHORIZED
    )


def test_provider_model_outputs_control_capabilities_and_bounds(
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Apply text-route capabilities and bounds to the effective mapping state."""
    context = CatalogContext(catalog_database, catalog_settings)
    client = context.client
    provider = {
        "api_name": "fake",
        "display_name": "Fake",
        "adapter": "fake",
        "enabled": True,
    }
    assert (
        client.post(
            "/v1/admin/providers", json=provider, headers=context.write_headers
        ).status_code
        == HTTPStatus.CREATED
    )
    model = {
        "api_name": "mixed",
        "display_name": "Mixed",
        "input_modalities": ["text", "image"],
        "output_modalities": ["text", "embedding", "video"],
        "capabilities": ["streaming"],
        "constraints": {
            "max_input_images": 4,
            "max_input_image_bytes": 1000,
            "embedding_dimensions": [3, 6],
            "max_output_duration_seconds": 60,
        },
    }
    assert (
        client.post(
            "/v1/admin/models", json=model, headers=context.write_headers
        ).status_code
        == HTTPStatus.CREATED
    )
    base = {
        "provider_api_name": "fake",
        "model_api_name": "mixed",
        "provider_model_name": "wire",
        "enabled": True,
        "input_modalities": ["text"],
    }
    invalid_values = (
        {
            "api_name": "embedding-capability",
            "output_modalities": ["embedding"],
            "capabilities": ["streaming"],
            "constraints": {"embedding_dimensions": [3]},
        },
        {
            "api_name": "embedding-unbounded",
            "output_modalities": ["embedding"],
            "capabilities": [],
            "constraints": {},
        },
        {
            "api_name": "image-bound-without-image",
            "output_modalities": ["text"],
            "capabilities": [],
            "constraints": {"max_input_images": 1},
        },
        {
            "api_name": "video-unbounded",
            "output_modalities": ["video"],
            "capabilities": [],
            "constraints": {},
        },
        {
            "api_name": "duration-without-media",
            "output_modalities": ["text"],
            "capabilities": [],
            "constraints": {"max_output_duration_seconds": 10},
        },
    )
    for invalid in invalid_values:
        response = client.post(
            "/v1/admin/provider-models",
            json={**base, **invalid},
            headers=context.write_headers,
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST

    valid = client.post(
        "/v1/admin/provider-models",
        json={
            **base,
            "api_name": "mixed-text-embedding",
            "output_modalities": ["text", "embedding"],
            "capabilities": ["streaming"],
            "constraints": {"embedding_dimensions": [3]},
        },
        headers=context.write_headers,
    )
    assert valid.status_code == HTTPStatus.CREATED
    assert valid.json()["constraints"] == {"embedding_dimensions": [3]}


def test_provider_model_price_override_replaces_complete_canonical_authority(
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Do not combine one inherited source with a mapping manual price."""
    context = CatalogContext(catalog_database, catalog_settings)
    assert (
        context.client.post(
            "/v1/admin/providers",
            json={
                "api_name": "fake",
                "display_name": "Fake",
                "adapter": "fake",
                "enabled": True,
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    assert (
        context.client.post(
            "/v1/admin/models",
            json={
                "api_name": "priced",
                "display_name": "Priced",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "capabilities": [],
                "price_source": "openrouter",
                "price_lookup_key": "source-model",
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    manual_mapping = {
        "api_name": "priced",
        "provider_api_name": "fake",
        "model_api_name": "priced",
        "provider_model_name": "wire-priced",
        "enabled": True,
        "manual_price": {
            "currency": "USD",
            "unit_prices": [{"unit": "input_token", "amount": "0.000001"}],
        },
    }
    created = context.client.post(
        "/v1/admin/provider-models",
        json=manual_mapping,
        headers=context.write_headers,
    )
    assert created.status_code == HTTPStatus.CREATED
    assert "price_source" not in created.json()
    assert created.json()["effective_price"]["currency"] == "USD"

    selected_source = {
        **manual_mapping,
        "manual_price": None,
        "price_source": "wavespeed",
        "price_lookup_key": "source-media-model",
    }
    replaced = context.client.put(
        "/v1/admin/provider-models/priced",
        json=selected_source,
        headers=context.write_headers,
    )
    assert replaced.status_code == HTTPStatus.OK
    assert replaced.json()["price_source"] == "wavespeed"
    assert "effective_price" not in replaced.json()

    mixed_authority = context.client.put(
        "/v1/admin/provider-models/priced",
        json={**selected_source, "manual_price": manual_mapping["manual_price"]},
        headers=context.write_headers,
    )
    assert mixed_authority.status_code == HTTPStatus.BAD_REQUEST
    current = context.client.get(
        "/v1/admin/provider-models/priced", headers=context.read_headers
    ).json()
    assert current["price_source"] == "wavespeed"
    assert "effective_price" not in current
    with psycopg.connect(catalog_database, row_factory=dict_row) as connection:
        available, next_cursor = catalog.list_available_provider_models(
            connection, limit=50, cursor=None
        )
    assert next_cursor is None
    assert available[0]["api_name"] == "priced"
    assert available[0]["effective_price"] is None


def test_provider_adapter_change_waits_for_concurrent_mapping(
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Keep the committed provider and mapping state valid after concurrent writes."""
    context = CatalogContext(catalog_database, catalog_settings)
    assert (
        context.client.post(
            "/v1/admin/providers",
            json={
                "api_name": "provider",
                "display_name": "Provider",
                "adapter": "fake",
                "enabled": True,
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    assert (
        context.client.post(
            "/v1/admin/models",
            json={
                "api_name": "text",
                "display_name": "Text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "capabilities": [],
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    mapping = ProviderModelWrite(
        api_name="concurrent",
        provider_api_name="provider",
        model_api_name="text",
        provider_model_name="wire",
        enabled=True,
    )
    incompatible_provider = ProviderWrite(
        api_name="provider",
        display_name="Provider",
        adapter="local_embeddings",
        enabled=True,
    )
    with (
        psycopg.connect(catalog_database, row_factory=dict_row) as mapping_connection,
        psycopg.connect(catalog_database, row_factory=dict_row) as provider_connection,
    ):
        catalog.create_provider_model(mapping_connection, mapping)
        provider_connection.execute("SET LOCAL lock_timeout = '100ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            catalog.replace_provider(
                provider_connection, "provider", incompatible_provider
            )
        provider_connection.rollback()
        mapping_connection.commit()

        with pytest.raises(ApiError) as invalid_change:
            catalog.replace_provider(
                provider_connection, "provider", incompatible_provider
            )
        assert invalid_change.value.field == "output_modalities"
        provider_connection.rollback()
        current = provider_connection.execute(
            """SELECT adapter FROM router.provider_connections
               WHERE api_name = 'provider'"""
        ).fetchone()
        assert current == {"adapter": "fake"}


def test_local_embedding_mapping_is_one_fixed_model_space(
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Accept only the approved local wire model and exact vector dimension."""
    context = CatalogContext(catalog_database, catalog_settings)
    assert (
        context.client.post(
            "/v1/admin/providers",
            json={
                "api_name": "local",
                "display_name": "Local embedding",
                "adapter": "local_embeddings",
                "enabled": True,
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    assert (
        context.client.post(
            "/v1/admin/models",
            json={
                "api_name": "embedding",
                "display_name": "Embedding",
                "input_modalities": ["text"],
                "output_modalities": ["embedding"],
                "capabilities": [],
                "constraints": {"embedding_dimensions": [384, 768]},
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    mapping = {
        "api_name": "local-embedding",
        "provider_api_name": "local",
        "model_api_name": "embedding",
        "provider_model_name": "BAAI/bge-small-en-v1.5",
        "enabled": True,
        "constraints": {"embedding_dimensions": [384]},
    }
    accepted = context.client.post(
        "/v1/admin/provider-models", json=mapping, headers=context.write_headers
    )
    assert accepted.status_code == HTTPStatus.CREATED
    for api_name, wire_name, dimensions in (
        ("wrong-model", "other/model", [384]),
        ("wrong-dimension", "BAAI/bge-small-en-v1.5", [768]),
    ):
        rejected = context.client.post(
            "/v1/admin/provider-models",
            json={
                **mapping,
                "api_name": api_name,
                "provider_model_name": wire_name,
                "constraints": {"embedding_dimensions": dimensions},
            },
            headers=context.write_headers,
        )
        assert rejected.status_code == HTTPStatus.BAD_REQUEST


def test_canonical_model_change_waits_for_concurrent_mapping(
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Keep the committed canonical model and mapping valid after concurrent writes."""
    context = CatalogContext(catalog_database, catalog_settings)
    assert (
        context.client.post(
            "/v1/admin/providers",
            json={
                "api_name": "provider",
                "display_name": "Provider",
                "adapter": "fake",
                "enabled": True,
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    original_model = {
        "api_name": "mixed",
        "display_name": "Mixed",
        "input_modalities": ["text"],
        "output_modalities": ["text", "embedding"],
        "capabilities": ["streaming"],
        "constraints": {"embedding_dimensions": [3]},
    }
    assert (
        context.client.post(
            "/v1/admin/models", json=original_model, headers=context.write_headers
        ).status_code
        == HTTPStatus.CREATED
    )
    mapping = ProviderModelWrite(
        api_name="concurrent",
        provider_api_name="provider",
        model_api_name="mixed",
        provider_model_name="wire",
        enabled=True,
        output_modalities=["text"],
        capabilities=["streaming"],
        constraints=ModelConstraints(),
    )
    incompatible_model = ModelWrite(
        api_name="mixed",
        display_name="Mixed",
        input_modalities=["text"],
        output_modalities=["embedding"],
        capabilities=[],
        constraints=ModelConstraints(embedding_dimensions=[3]),
    )
    with (
        psycopg.connect(catalog_database, row_factory=dict_row) as mapping_connection,
        psycopg.connect(catalog_database, row_factory=dict_row) as model_connection,
    ):
        catalog.create_provider_model(mapping_connection, mapping)
        model_connection.execute("SET LOCAL lock_timeout = '100ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            catalog.replace_model(model_connection, "mixed", incompatible_model)
        model_connection.rollback()
        mapping_connection.commit()

        with pytest.raises(ApiError) as invalid_change:
            catalog.replace_model(model_connection, "mixed", incompatible_model)
        assert invalid_change.value.field == "output_modalities"
        model_connection.rollback()
        current = model_connection.execute(
            """SELECT output_modalities FROM router.canonical_models
               WHERE api_name = 'mixed'"""
        ).fetchone()
        assert current == {"output_modalities": ["text", "embedding"]}


def test_catalog_preview_import_atomicity_dependencies_and_failed_activity(  # noqa: PLR0915
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Preview without writes and import a complete selection atomically."""
    context = CatalogContext(catalog_database, catalog_settings)
    client = context.client
    assert (
        client.post(
            "/v1/admin/providers",
            json={
                "api_name": "fake",
                "display_name": "Fake",
                "adapter": "fake",
                "enabled": True,
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    preview = client.post(
        "/v1/admin/model-imports/preview",
        json={"provider_api_name": "fake"},
        headers=context.write_headers,
    )
    assert preview.status_code == HTTPStatus.OK
    assert {item["catalog_key"] for item in preview.json()["candidates"]} == {
        "fake-text",
        "fake-embedding",
        "fake-media",
    }
    assert (
        client.get("/v1/admin/models", headers=context.read_headers).json()["items"]
        == []
    )

    duplicate = client.post(
        "/v1/admin/model-imports",
        json={
            "provider_api_name": "fake",
            "selections": [
                {
                    "catalog_key": "fake-text",
                    "model_api_name": "first",
                    "provider_model_api_name": "first",
                },
                {
                    "catalog_key": "fake-text",
                    "model_api_name": "second",
                    "provider_model_api_name": "second",
                },
            ],
        },
        headers=context.write_headers,
    )
    assert duplicate.status_code == HTTPStatus.BAD_REQUEST
    assert (
        client.get("/v1/admin/models", headers=context.read_headers).json()["items"]
        == []
    )

    imported = client.post(
        "/v1/admin/model-imports",
        json={
            "provider_api_name": "fake",
            "selections": [
                {
                    "catalog_key": "fake-text",
                    "model_api_name": "text",
                    "provider_model_api_name": "text",
                },
                {
                    "catalog_key": "fake-embedding",
                    "model_api_name": "embedding",
                    "provider_model_api_name": "embedding",
                },
                {
                    "catalog_key": "fake-media",
                    "model_api_name": "media",
                    "provider_model_api_name": "media",
                },
            ],
        },
        headers=context.write_headers,
    )
    assert imported.status_code == HTTPStatus.OK
    assert [item["api_name"] for item in imported.json()["models"]] == [
        "text",
        "embedding",
        "media",
    ]
    with psycopg.connect(catalog_database, row_factory=dict_row) as connection:
        embedding_route = resolve_provider_route(
            connection,
            "embedding",
            required_inputs=frozenset({"text"}),
            required_output="embedding",
            required_capabilities=frozenset(),
            reasoning_level=None,
        )
        validate_route_constraints(embedding_route, embedding_dimension=3)
        with pytest.raises(ApiError) as dimension_error:
            validate_route_constraints(embedding_route, embedding_dimension=4)
        assert dimension_error.value.field == "embedding_dimension"
        media_route = resolve_provider_route(
            connection,
            "media",
            required_inputs=frozenset({"text"}),
            required_output="video",
            required_capabilities=frozenset(),
            reasoning_level=None,
        )
        validate_route_constraints(media_route, output_duration_seconds=300)
        with pytest.raises(ApiError) as duration_error:
            validate_route_constraints(media_route, output_duration_seconds=301)
        assert duration_error.value.field == "duration"

    with psycopg.connect(catalog_database) as connection:
        connection.execute(
            """INSERT INTO router.services (api_name, display_name)
               VALUES ('assigned-service', 'Assigned service')"""
        )
        connection.execute(
            """INSERT INTO router.assignment_definitions (service_id, api_name)
               SELECT id, 'default' FROM router.services
               WHERE api_name = 'assigned-service'"""
        )
        connection.execute(
            """INSERT INTO router.assignment_candidates
                   (assignment_id, position, provider_model_id)
               SELECT assignment.id, 0, mapping.id
               FROM router.assignment_definitions AS assignment
               JOIN router.services AS service ON service.id = assignment.service_id
               JOIN router.provider_models AS mapping ON mapping.api_name = 'text'
               WHERE service.api_name = 'assigned-service'"""
        )
    assert (
        client.delete(
            "/v1/admin/provider-models/text", headers=context.write_headers
        ).status_code
        == HTTPStatus.CONFLICT
    )
    with psycopg.connect(catalog_database) as connection:
        connection.execute(
            """UPDATE router.assignment_definitions
               SET reasoning_level = 'high'
               WHERE api_name = 'default'"""
        )
    incompatible_reasoning = client.put(
        "/v1/admin/provider-models/text",
        json={
            "api_name": "text",
            "provider_api_name": "fake",
            "model_api_name": "text",
            "provider_model_name": "fake-text-v1",
            "enabled": True,
            "capabilities": [],
            "reasoning_mappings": [],
        },
        headers=context.write_headers,
    )
    assert incompatible_reasoning.status_code == HTTPStatus.BAD_REQUEST
    assert (
        "reasoning"
        in client.get(
            "/v1/admin/provider-models/text", headers=context.read_headers
        ).json()["capabilities"]
    )
    disabled = client.put(
        "/v1/admin/provider-models/text",
        json={
            "api_name": "text",
            "provider_api_name": "fake",
            "model_api_name": "text",
            "provider_model_name": "fake-text-v1",
            "enabled": False,
            "reasoning_mappings": [
                {"level": level, "provider_value": level}
                for level in ("none", "low", "medium", "high")
            ],
        },
        headers=context.write_headers,
    )
    assert disabled.status_code == HTTPStatus.CONFLICT
    with psycopg.connect(catalog_database) as connection:
        assignment_id = connection.execute(
            """SELECT assignment.id
               FROM router.assignment_definitions AS assignment
               JOIN router.services AS service ON service.id = assignment.service_id
               WHERE service.api_name = 'assigned-service'
                 AND assignment.api_name = 'default'"""
        ).fetchone()
        provider_model_id = connection.execute(
            "SELECT id FROM router.provider_models WHERE api_name = 'embedding'"
        ).fetchone()
        assert assignment_id is not None
        assert provider_model_id is not None
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.assignment_candidates
                       (assignment_id, position, provider_model_id)
                   VALUES (%s, 16, %s)""",
                (assignment_id[0], provider_model_id[0]),
            )
        connection.rollback()
        connection.execute(
            """DELETE FROM router.assignment_definitions
               WHERE api_name = 'default'"""
        )

    assert (
        client.delete(
            "/v1/admin/models/text", headers=context.write_headers
        ).status_code
        == HTTPStatus.CONFLICT
    )
    assert (
        client.delete(
            "/v1/admin/providers/fake", headers=context.write_headers
        ).status_code
        == HTTPStatus.CONFLICT
    )
    assert (
        client.delete(
            "/v1/admin/provider-models/text", headers=context.write_headers
        ).status_code
        == HTTPStatus.NO_CONTENT
    )
    assert (
        client.delete(
            "/v1/admin/models/text", headers=context.write_headers
        ).status_code
        == HTTPStatus.NO_CONTENT
    )

    activity = client.get(
        "/v1/admin/activity",
        params={
            "from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
            "to": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
        },
        headers=context.read_headers,
    ).json()["items"]
    assert any(
        item["action"] == "model.delete" and item["result"] == "failed"
        for item in activity
    )
    assert any(
        item["action"] == "model_import.apply" and item["result"] == "succeeded"
        for item in activity
    )
    assert all(
        "resource_id" in item
        for item in activity
        if item["result"] == "succeeded"
        and item["resource_type"]
        in {"credential", "provider", "model", "provider_model"}
    )


def test_concurrent_catalog_import_leaves_one_complete_current_state(
    catalog_database: str, catalog_settings: Settings
) -> None:
    """Serialize competing selected imports and never commit a partial import."""
    context = CatalogContext(catalog_database, catalog_settings)
    assert (
        context.client.post(
            "/v1/admin/providers",
            json={
                "api_name": "fake",
                "display_name": "Fake",
                "adapter": "fake",
                "enabled": True,
            },
            headers=context.write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    body = {
        "provider_api_name": "fake",
        "selections": [
            {
                "catalog_key": "fake-media",
                "model_api_name": "media",
                "provider_model_api_name": "media",
            }
        ],
    }

    def import_once() -> int:
        client = TestClient(
            create_app(database_url=catalog_database, settings=catalog_settings),
            base_url="https://llmrouter.test",
        )
        return cast(
            "int",
            client.post(
                "/v1/admin/model-imports", json=body, headers=context.write_headers
            ).status_code,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _index: import_once(), range(2)))
    assert statuses == [HTTPStatus.OK, HTTPStatus.CONFLICT]
    with psycopg.connect(catalog_database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.canonical_models"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM router.provider_models"
        ).fetchone() == (1,)
