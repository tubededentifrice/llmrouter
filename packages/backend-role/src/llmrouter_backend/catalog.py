"""Global provider, credential, model, and catalog current-state policy."""
# ruff: noqa: ANN401, D103, E501, EM101, PLR0913, PLR2004, TRY003

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import sql
from psycopg.types.json import Jsonb

from llmrouter_backend.control_files import ControlFileError, read_control_file
from llmrouter_backend.embedding_contract import (
    LOCAL_EMBEDDING_DIMENSION,
    LOCAL_EMBEDDING_MODEL,
)
from llmrouter_backend.errors import ApiError, conflict, invalid_request, not_found
from llmrouter_backend.models import (
    CredentialWrite,
    ModelConstraints,
    ModelImportCandidate,
    ModelImportSelection,
    ModelWrite,
    Price,
    ProviderModelWrite,
    ProviderWrite,
    ReasoningLevel,
)
from llmrouter_backend.store import AdministratorActor, record_activity

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from psycopg import Connection

    from llmrouter_backend.config import Settings

_CREDENTIAL_VERSION = b"\x01"
_CREDENTIAL_WRAP_AAD = b"llmrouter-provider-credential-wrap-v1\0"
_CREDENTIAL_DATA_AAD = b"llmrouter-provider-credential-data-v1\0"
_NONCE_BYTES = 12
_DATA_KEY_BYTES = 32
_WRAPPED_DATA_KEY_BYTES = _DATA_KEY_BYTES + 16
_ENVELOPE_CONTROL_BYTES = (
    len(_CREDENTIAL_VERSION)
    + _NONCE_BYTES
    + _WRAPPED_DATA_KEY_BYTES
    + _NONCE_BYTES
    + 16
)
_MAXIMUM_CONTROL_KEY_BYTES = 10_000
_CATALOG_WRITE_LOCK = 4993044345823
_REASONING_LEVELS = frozenset({"none", "low", "medium", "high"})
_ADAPTER_CAPABILITIES: dict[
    str, tuple[frozenset[str], frozenset[str], frozenset[str]]
] = {
    "openai": (
        frozenset({"text", "image"}),
        frozenset({"text", "structured_json", "embedding", "image", "audio"}),
        frozenset({"tool_calling", "streaming", "reasoning"}),
    ),
    "openai_compatible": (
        frozenset({"text", "image"}),
        frozenset({"text", "structured_json", "embedding"}),
        frozenset({"tool_calling", "streaming", "reasoning"}),
    ),
    "openrouter": (
        frozenset({"text", "image"}),
        frozenset({"text", "structured_json"}),
        frozenset({"tool_calling", "streaming", "reasoning"}),
    ),
    "custom": (
        frozenset({"text", "image"}),
        frozenset({"text", "structured_json", "embedding", "image", "video", "audio"}),
        frozenset({"tool_calling", "streaming", "reasoning"}),
    ),
    "wavespeed": (
        frozenset({"text"}),
        frozenset({"image"}),
        frozenset(),
    ),
    "ollama": (
        frozenset({"text", "image"}),
        frozenset({"text", "structured_json", "embedding"}),
        frozenset({"tool_calling", "streaming", "reasoning"}),
    ),
    "local_embeddings": (
        frozenset({"text"}),
        frozenset({"embedding"}),
        frozenset(),
    ),
    "fake": (
        frozenset({"text", "image"}),
        frozenset({"text", "structured_json", "embedding", "image", "video", "audio"}),
        frozenset({"tool_calling", "streaming", "reasoning"}),
    ),
}
_BUILT_IN_ENDPOINTS = frozenset(
    {"openai", "openrouter", "wavespeed", "local_embeddings", "fake"}
)
_LOCAL_ENDPOINTS: frozenset[str] = frozenset()
_REQUIRED_CREDENTIAL_ADAPTERS = frozenset({"openai", "openrouter", "wavespeed"})
_CREDENTIAL_ADAPTERS = _REQUIRED_CREDENTIAL_ADAPTERS | frozenset(
    {"openai_compatible", "custom", "ollama"}
)
_PRICE_SOURCES = frozenset({"openrouter", "wavespeed"})
_CATALOGS: dict[str, tuple[ModelImportCandidate, ...]] = {
    "openai": (
        ModelImportCandidate(
            catalog_key="openai-text",
            display_name="OpenAI text model",
            provider_model_name="openai-text",
            input_modalities=["text", "image"],
            output_modalities=["text", "structured_json"],
            capabilities=["tool_calling", "streaming", "reasoning"],
            constraints=ModelConstraints(
                max_input_images=8, max_input_image_bytes=20 * 1024 * 1024
            ),
        ),
    ),
    "openrouter": (
        ModelImportCandidate(
            catalog_key="openrouter-text",
            display_name="OpenRouter text model",
            provider_model_name="openrouter-text",
            input_modalities=["text", "image"],
            output_modalities=["text", "structured_json"],
            capabilities=["tool_calling", "streaming", "reasoning"],
            constraints=ModelConstraints(
                max_input_images=8, max_input_image_bytes=20 * 1024 * 1024
            ),
        ),
    ),
    "wavespeed": (
        ModelImportCandidate(
            catalog_key="wavespeed-image",
            display_name="WaveSpeed image model",
            provider_model_name="wavespeed-ai/flux-dev",
            input_modalities=["text"],
            output_modalities=["image"],
            capabilities=[],
            constraints=ModelConstraints(),
        ),
    ),
    "fake": (
        ModelImportCandidate(
            catalog_key="fake-text",
            display_name="Fake text model",
            provider_model_name="fake-text-v1",
            input_modalities=["text", "image"],
            output_modalities=["text", "structured_json"],
            capabilities=["tool_calling", "streaming", "reasoning"],
            constraints=ModelConstraints(
                max_input_images=8, max_input_image_bytes=20 * 1024 * 1024
            ),
        ),
        ModelImportCandidate(
            catalog_key="fake-embedding",
            display_name="Fake embedding model",
            provider_model_name="fake-embedding-v1",
            input_modalities=["text"],
            output_modalities=["embedding"],
            capabilities=[],
            constraints=ModelConstraints(embedding_dimensions=[3, 8, 1536]),
        ),
        ModelImportCandidate(
            catalog_key="fake-media",
            display_name="Fake media model",
            provider_model_name="fake-media-v1",
            input_modalities=["text", "image"],
            output_modalities=["image", "video", "audio"],
            capabilities=[],
            constraints=ModelConstraints(
                max_input_images=8,
                max_input_image_bytes=20 * 1024 * 1024,
                max_output_duration_seconds=300,
            ),
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class ProviderCredentialKeys:
    """A dedicated deployment key for provider credential envelopes."""

    key: bytes

    @classmethod
    def load(cls, settings: Settings) -> ProviderCredentialKeys:
        """Load the purpose-specific key without exposing its file content."""
        path = settings.provider_credential_wrapping_key_file
        if path is None:
            raise _credential_unavailable()
        try:
            source = read_control_file(
                Path(path), maximum=_MAXIMUM_CONTROL_KEY_BYTES
            ).strip()
        except ControlFileError as error:
            raise _credential_unavailable() from error
        if len(source) < 32:
            raise _credential_unavailable()
        source_digest = hashlib.sha256(source).digest()
        for control_path in (
            settings.administrator_digest_key_file,
            settings.administrator_encryption_key_file,
            settings.object_store_secret_key_file,
        ):
            if control_path is None:
                continue
            try:
                control_source = read_control_file(
                    Path(control_path), maximum=_MAXIMUM_CONTROL_KEY_BYTES
                ).strip()
            except ControlFileError as error:
                raise _credential_unavailable() from error
            if hmac.compare_digest(
                source_digest, hashlib.sha256(control_source).digest()
            ):
                raise _credential_unavailable()
        return cls(source_digest)

    def encrypt(self, api_name: str, secret: str) -> tuple[bytes, str]:
        """Create one authenticated envelope and a keyed short fingerprint."""
        wrapping_nonce = secrets.token_bytes(_NONCE_BYTES)
        data_nonce = secrets.token_bytes(_NONCE_BYTES)
        data_key = secrets.token_bytes(_DATA_KEY_BYTES)
        plaintext = secret.encode("utf-8")
        identity = api_name.encode("ascii")
        wrapped_data_key = AESGCM(self.key).encrypt(
            wrapping_nonce, data_key, _CREDENTIAL_WRAP_AAD + identity
        )
        encrypted_secret = AESGCM(data_key).encrypt(
            data_nonce, plaintext, _CREDENTIAL_DATA_AAD + identity
        )
        encrypted = (
            _CREDENTIAL_VERSION
            + wrapping_nonce
            + wrapped_data_key
            + data_nonce
            + encrypted_secret
        )
        fingerprint = hmac.digest(self.key, plaintext, "sha256").hex()[:12]
        return encrypted, fingerprint

    def decrypt(self, api_name: str, encrypted: bytes) -> str:
        """Resolve one current secret into process memory or fail closed."""
        if (
            len(encrypted) <= _ENVELOPE_CONTROL_BYTES
            or encrypted[:1] != _CREDENTIAL_VERSION
        ):
            raise _credential_unavailable()
        try:
            identity = api_name.encode("ascii")
            wrapping_nonce_start = 1
            wrapped_key_start = wrapping_nonce_start + _NONCE_BYTES
            data_nonce_start = wrapped_key_start + _WRAPPED_DATA_KEY_BYTES
            encrypted_secret_start = data_nonce_start + _NONCE_BYTES
            data_key = AESGCM(self.key).decrypt(
                encrypted[wrapping_nonce_start:wrapped_key_start],
                encrypted[wrapped_key_start:data_nonce_start],
                _CREDENTIAL_WRAP_AAD + identity,
            )
            plaintext = AESGCM(data_key).decrypt(
                encrypted[data_nonce_start:encrypted_secret_start],
                encrypted[encrypted_secret_start:],
                _CREDENTIAL_DATA_AAD + identity,
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeError, ValueError) as error:
            raise _credential_unavailable() from error


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    """A validated safe provider route for a later call attempt."""

    provider_model_api_name: str
    provider_connection_api_name: str
    adapter: str
    endpoint: str | None
    provider_model_name: str
    credential_api_name: str | None
    constraints: ModelConstraints
    reasoning_level: ReasoningLevel | None
    provider_reasoning_value: str | None


def resolve_credential(
    connection: Connection[Any], api_name: str, keys: ProviderCredentialKeys
) -> str:
    """Take one immutable secret snapshot for a new provider attempt."""
    row = connection.execute(
        "SELECT encrypted_secret FROM router.provider_credentials WHERE api_name = %s",
        (api_name,),
    ).fetchone()
    if row is None:
        raise _credential_unavailable()
    return keys.decrypt(api_name, cast("bytes", row["encrypted_secret"]))


def create_credential(
    connection: Connection[Any],
    *,
    api_name: str,
    secret: str,
    keys: ProviderCredentialKeys,
) -> dict[str, Any]:
    """Create one write-only provider credential."""
    _lock_catalog_write(connection)
    encrypted, fingerprint = keys.encrypt(api_name, secret)
    row = connection.execute(
        """INSERT INTO router.provider_credentials
               (api_name, encrypted_secret, fingerprint)
           VALUES (%s, %s, %s)
           RETURNING api_name, fingerprint, created_at, updated_at""",
        (api_name, encrypted, fingerprint),
    ).fetchone()
    return _required_row(row)


def replace_credential(
    connection: Connection[Any],
    *,
    api_name: str,
    value: CredentialWrite,
    keys: ProviderCredentialKeys,
) -> dict[str, Any]:
    """Replace one credential for attempts that resolve after commit."""
    if value.api_name != api_name:
        raise invalid_request("api_name", "The body identity must match the path.")
    _lock_catalog_write(connection)
    encrypted, fingerprint = keys.encrypt(api_name, value.secret)
    row = connection.execute(
        """UPDATE router.provider_credentials
           SET encrypted_secret = %s, fingerprint = %s,
               updated_at = transaction_timestamp()
           WHERE api_name = %s
           RETURNING api_name, fingerprint, created_at, updated_at""",
        (encrypted, fingerprint, api_name),
    ).fetchone()
    if row is None:
        raise not_found("credential")
    return cast("dict[str, Any]", row)


def list_credentials(
    connection: Connection[Any], *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    rows = connection.execute(
        """SELECT api_name, fingerprint, created_at, updated_at
           FROM router.provider_credentials
           WHERE (%s::text IS NULL OR api_name > %s)
           ORDER BY api_name LIMIT %s""",
        (cursor, cursor, limit + 1),
    ).fetchall()
    return _page(rows, limit)


def delete_credential(connection: Connection[Any], api_name: str) -> uuid.UUID:
    _lock_catalog_write(connection)
    row = connection.execute(
        "DELETE FROM router.provider_credentials WHERE api_name = %s RETURNING id",
        (api_name,),
    ).fetchone()
    if row is None:
        raise not_found("credential")
    return cast("uuid.UUID", row["id"])


def create_provider(
    connection: Connection[Any], value: ProviderWrite
) -> dict[str, Any]:
    """Validate and create one adapter-specific provider connection."""
    _lock_catalog_write(connection)
    _validate_provider(value)
    credential_id = _credential_id(connection, value.credential_api_name)
    row = connection.execute(
        """INSERT INTO router.provider_connections
               (api_name, display_name, adapter, endpoint, credential_id, enabled)
           VALUES (%s, %s, %s, %s, %s, %s)
           RETURNING id, api_name, display_name, adapter, endpoint, enabled, created_at""",
        (
            value.api_name,
            value.display_name,
            value.adapter,
            value.endpoint,
            credential_id,
            value.enabled,
        ),
    ).fetchone()
    result = _required_row(row)
    result.pop("id", None)
    result["credential_api_name"] = value.credential_api_name
    return result


def replace_provider(
    connection: Connection[Any], api_name: str, value: ProviderWrite
) -> dict[str, Any]:
    """Replace one provider without changing its path identity."""
    if value.api_name != api_name:
        raise invalid_request("api_name", "The body identity must match the path.")
    _lock_catalog_write(connection)
    if (
        connection.execute(
            "SELECT 1 FROM router.provider_connections WHERE api_name = %s FOR UPDATE",
            (api_name,),
        ).fetchone()
        is None
    ):
        raise not_found("provider")
    _validate_provider(value)
    credential_id = _credential_id(connection, value.credential_api_name)
    row = connection.execute(
        """UPDATE router.provider_connections
           SET display_name = %s, adapter = %s, endpoint = %s,
               credential_id = %s, enabled = %s
           WHERE api_name = %s
           RETURNING id, api_name, display_name, adapter, endpoint, enabled, created_at""",
        (
            value.display_name,
            value.adapter,
            value.endpoint,
            credential_id,
            value.enabled,
            api_name,
        ),
    ).fetchone()
    if row is None:
        raise not_found("provider")
    result = cast("dict[str, Any]", row)
    for mapping in _provider_models_for_provider(connection, api_name):
        _normalized_provider_model(
            connection, ProviderModelWrite.model_validate(mapping)
        )
    if not value.enabled and _provider_has_assigned_mapping(connection, api_name):
        raise conflict("A current assignment requires this provider.")
    result.pop("id", None)
    result["credential_api_name"] = value.credential_api_name
    return result


def provider_by_api_name(
    connection: Connection[Any], api_name: str
) -> dict[str, Any] | None:
    return connection.execute(
        """SELECT provider.api_name, provider.display_name, provider.adapter,
                  provider.endpoint, credential.api_name AS credential_api_name,
                  provider.enabled, provider.created_at
           FROM router.provider_connections AS provider
           LEFT JOIN router.provider_credentials AS credential
             ON credential.id = provider.credential_id
           WHERE provider.api_name = %s""",
        (api_name,),
    ).fetchone()


def list_providers(
    connection: Connection[Any], *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    rows = connection.execute(
        """SELECT provider.api_name, provider.display_name, provider.adapter,
                  provider.endpoint, credential.api_name AS credential_api_name,
                  provider.enabled, provider.created_at
           FROM router.provider_connections AS provider
           LEFT JOIN router.provider_credentials AS credential
             ON credential.id = provider.credential_id
           WHERE (%s::text IS NULL OR provider.api_name > %s)
           ORDER BY provider.api_name LIMIT %s""",
        (cursor, cursor, limit + 1),
    ).fetchall()
    return _page(rows, limit)


def delete_provider(connection: Connection[Any], api_name: str) -> uuid.UUID:
    _lock_catalog_write(connection)
    row = connection.execute(
        "DELETE FROM router.provider_connections WHERE api_name = %s RETURNING id",
        (api_name,),
    ).fetchone()
    if row is None:
        raise not_found("provider")
    return cast("uuid.UUID", row["id"])


def create_model(connection: Connection[Any], value: ModelWrite) -> dict[str, Any]:
    """Validate and create one canonical model."""
    _lock_catalog_write(connection)
    _validate_model(value)
    row = connection.execute(
        """INSERT INTO router.canonical_models
               (api_name, display_name, input_modalities, output_modalities,
                capabilities, constraints, price_source, price_lookup_key, manual_price)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING *""",
        _model_parameters(value),
    ).fetchone()
    return _model_row(_required_row(row))


def replace_model(
    connection: Connection[Any], api_name: str, value: ModelWrite
) -> dict[str, Any]:
    """Replace a canonical model after all current mappings stay valid."""
    if value.api_name != api_name:
        raise invalid_request("api_name", "The body identity must match the path.")
    _lock_catalog_write(connection)
    if (
        connection.execute(
            "SELECT 1 FROM router.canonical_models WHERE api_name = %s FOR UPDATE",
            (api_name,),
        ).fetchone()
        is None
    ):
        raise not_found("model")
    _validate_model(value)
    row = connection.execute(
        """UPDATE router.canonical_models
           SET synchronized_price = CASE
                   WHEN price_source IS NOT DISTINCT FROM %s
                    AND price_lookup_key IS NOT DISTINCT FROM %s
                   THEN synchronized_price ELSE NULL END,
               display_name = %s, input_modalities = %s, output_modalities = %s,
               capabilities = %s, constraints = %s, price_source = %s,
               price_lookup_key = %s, manual_price = %s
           WHERE api_name = %s RETURNING *""",
        (
            value.price_source,
            value.price_lookup_key,
            *_model_parameters(value)[1:],
            api_name,
        ),
    ).fetchone()
    if row is None:
        raise not_found("model")
    for mapping in _provider_models_for_model(connection, api_name):
        _validate_provider_model_row(connection, mapping)
    return _model_row(cast("dict[str, Any]", row))


def model_by_api_name(
    connection: Connection[Any], api_name: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM router.canonical_models WHERE api_name = %s", (api_name,)
    ).fetchone()
    return _model_row(row) if row is not None else None


def list_models(
    connection: Connection[Any], *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    rows = connection.execute(
        """SELECT * FROM router.canonical_models
           WHERE (%s::text IS NULL OR api_name > %s)
           ORDER BY api_name LIMIT %s""",
        (cursor, cursor, limit + 1),
    ).fetchall()
    return [_model_row(row) for row in rows[:limit]], (
        str(rows[limit - 1]["api_name"]) if len(rows) > limit else None
    )


def delete_model(connection: Connection[Any], api_name: str) -> uuid.UUID:
    _lock_catalog_write(connection)
    row = connection.execute(
        "DELETE FROM router.canonical_models WHERE api_name = %s RETURNING id",
        (api_name,),
    ).fetchone()
    if row is None:
        raise not_found("model")
    return cast("uuid.UUID", row["id"])


def create_provider_model(
    connection: Connection[Any], value: ProviderModelWrite
) -> dict[str, Any]:
    """Create one validated capability-narrowing provider mapping."""
    _lock_catalog_write(connection)
    normalized = _normalized_provider_model(connection, value)
    row = connection.execute(
        """INSERT INTO router.provider_models
               (api_name, provider_id, model_id, provider_model_name, enabled,
                input_modalities, output_modalities, capabilities, constraints,
                reasoning_mappings, price_source, price_lookup_key, manual_price)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        normalized,
    ).fetchone()
    if row is None:
        raise RuntimeError("The provider-model insert did not return its row.")
    return _required_provider_model(connection, value.api_name)


def replace_provider_model(
    connection: Connection[Any], api_name: str, value: ProviderModelWrite
) -> dict[str, Any]:
    """Replace one current provider-model mapping."""
    if value.api_name != api_name:
        raise invalid_request("api_name", "The body identity must match the path.")
    _lock_catalog_write(connection)
    if (
        connection.execute(
            "SELECT 1 FROM router.provider_models WHERE api_name = %s FOR UPDATE",
            (api_name,),
        ).fetchone()
        is None
    ):
        raise not_found("provider-model")
    if not value.enabled and _provider_model_is_assigned(connection, api_name):
        raise conflict("A current assignment requires this provider-model.")
    normalized = _normalized_provider_model(connection, value)
    row = connection.execute(
        """UPDATE router.provider_models SET
               synchronized_price = CASE
                   WHEN price_source IS NOT DISTINCT FROM %s
                    AND price_lookup_key IS NOT DISTINCT FROM %s
                   THEN synchronized_price ELSE NULL END,
               provider_id = %s, model_id = %s, provider_model_name = %s,
               enabled = %s, input_modalities = %s, output_modalities = %s,
               capabilities = %s, constraints = %s, reasoning_mappings = %s,
               price_source = %s, price_lookup_key = %s, manual_price = %s
           WHERE api_name = %s RETURNING id""",
        (value.price_source, value.price_lookup_key, *normalized[1:], api_name),
    ).fetchone()
    if row is None:
        raise not_found("provider-model")
    _validate_assigned_provider_model(connection, api_name)
    return _required_provider_model(connection, api_name)


def provider_model_by_api_name(
    connection: Connection[Any], api_name: str
) -> dict[str, Any] | None:
    row = connection.execute(
        _PROVIDER_MODEL_SELECT + " WHERE mapping.api_name = %s", (api_name,)
    ).fetchone()
    return _provider_model_row(row) if row is not None else None


def list_provider_models(
    connection: Connection[Any], *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    rows = connection.execute(
        _PROVIDER_MODEL_SELECT
        + " WHERE (%s::text IS NULL OR mapping.api_name > %s) ORDER BY mapping.api_name LIMIT %s",
        (cursor, cursor, limit + 1),
    ).fetchall()
    return [_provider_model_row(row) for row in rows[:limit]], (
        str(rows[limit - 1]["api_name"]) if len(rows) > limit else None
    )


def list_available_provider_models(
    connection: Connection[Any], *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """List every globally enabled mapping without service-owned filtering."""
    rows = connection.execute(
        """SELECT mapping.api_name, model.display_name,
                  mapping.input_modalities, mapping.output_modalities,
                  mapping.capabilities, mapping.constraints,
                  CASE WHEN mapping.price_source IS NOT NULL THEN mapping.synchronized_price
                       WHEN mapping.manual_price IS NOT NULL THEN mapping.manual_price
                       WHEN model.price_source IS NOT NULL THEN model.synchronized_price
                       ELSE model.manual_price
                  END AS effective_price
           FROM router.provider_models AS mapping
           JOIN router.provider_connections AS provider ON provider.id = mapping.provider_id
           JOIN router.canonical_models AS model ON model.id = mapping.model_id
           WHERE mapping.enabled AND provider.enabled
             AND (%s::text IS NULL OR mapping.api_name > %s)
           ORDER BY mapping.api_name LIMIT %s""",
        (cursor, cursor, limit + 1),
    ).fetchall()
    return rows[:limit], (
        str(rows[limit - 1]["api_name"]) if len(rows) > limit else None
    )


def delete_provider_model(connection: Connection[Any], api_name: str) -> uuid.UUID:
    _lock_catalog_write(connection)
    row = connection.execute(
        "DELETE FROM router.provider_models WHERE api_name = %s RETURNING id",
        (api_name,),
    ).fetchone()
    if row is None:
        raise not_found("provider-model")
    return cast("uuid.UUID", row["id"])


def catalog_preview(
    connection: Connection[Any], provider_api_name: str
) -> list[ModelImportCandidate]:
    """Return a fixed registered catalog without changing current state."""
    row = connection.execute(
        "SELECT adapter FROM router.provider_connections WHERE api_name = %s",
        (provider_api_name,),
    ).fetchone()
    if row is None:
        raise not_found("provider")
    return list(_CATALOGS.get(cast("str", row["adapter"]), ()))


def import_catalog(
    connection: Connection[Any],
    provider_api_name: str,
    selections: Sequence[ModelImportSelection],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate a complete selection and apply it in one serialized transaction."""
    _lock_catalog_write(connection)
    candidates = {
        item.catalog_key: item
        for item in catalog_preview(connection, provider_api_name)
    }
    catalog_keys = [item.catalog_key for item in selections]
    model_names = [item.model_api_name for item in selections]
    mapping_names = [item.provider_model_api_name for item in selections]
    if len(set(catalog_keys)) != len(catalog_keys):
        raise invalid_request(
            "selections", "A catalog entry can be selected only once."
        )
    if len(set(model_names)) != len(model_names) or len(set(mapping_names)) != len(
        mapping_names
    ):
        raise invalid_request(
            "selections", "Imported resource identities must be unique."
        )
    if any(key not in candidates for key in catalog_keys):
        raise invalid_request("selections", "A selected catalog entry does not exist.")
    created_models: list[dict[str, Any]] = []
    created_mappings: list[dict[str, Any]] = []
    for selection in selections:
        candidate = candidates[selection.catalog_key]
        model = ModelWrite(
            api_name=selection.model_api_name,
            display_name=candidate.display_name,
            input_modalities=candidate.input_modalities,
            output_modalities=candidate.output_modalities,
            capabilities=candidate.capabilities,
            constraints=candidate.constraints,
        )
        created_models.append(create_model(connection, model))
        mappings = (
            [
                {"level": level, "provider_value": level}
                for level in ("none", "low", "medium", "high")
            ]
            if "reasoning" in candidate.capabilities
            else []
        )
        provider_model = ProviderModelWrite.model_validate(
            {
                "api_name": selection.provider_model_api_name,
                "provider_api_name": provider_api_name,
                "model_api_name": selection.model_api_name,
                "provider_model_name": candidate.provider_model_name,
                "enabled": True,
                "reasoning_mappings": mappings,
            }
        )
        created_mappings.append(create_provider_model(connection, provider_model))
    return created_models, created_mappings


def resolve_provider_route(
    connection: Connection[Any],
    api_name: str,
    *,
    required_inputs: frozenset[str],
    required_output: str,
    required_capabilities: frozenset[str],
    reasoning_level: ReasoningLevel | None,
) -> ProviderRoute:
    """Validate one exact candidate before provider or credential work."""
    row = connection.execute(
        """SELECT mapping.api_name, mapping.provider_model_name,
                  mapping.input_modalities, mapping.output_modalities,
                  mapping.capabilities, mapping.constraints,
                  mapping.reasoning_mappings,
                  provider.api_name AS provider_connection_api_name,
                  provider.adapter, provider.endpoint,
                  credential.api_name AS credential_api_name
           FROM router.provider_models AS mapping
           JOIN router.provider_connections AS provider ON provider.id = mapping.provider_id
           LEFT JOIN router.provider_credentials AS credential ON credential.id = provider.credential_id
           WHERE mapping.api_name = %s AND mapping.enabled AND provider.enabled""",
        (api_name,),
    ).fetchone()
    if row is None:
        raise ApiError(
            503, "provider_unavailable", "No eligible provider-model is available."
        )
    if (
        not required_inputs <= frozenset(row["input_modalities"])
        or required_output not in row["output_modalities"]
        or not required_capabilities <= frozenset(row["capabilities"])
    ):
        raise ApiError(
            400, "invalid_request", "The provider-model does not support the request."
        )
    mapping = {
        item["level"]: item["provider_value"] for item in row["reasoning_mappings"]
    }
    selected_level = reasoning_level
    if "reasoning" in row["capabilities"] and selected_level is None:
        selected_level = "medium"
    if "reasoning" not in row["capabilities"] and selected_level == "none":
        selected_level = None
    if selected_level is not None and selected_level not in mapping:
        raise ApiError(400, "invalid_request", "The reasoning level is not available.")
    return ProviderRoute(
        provider_model_api_name=row["api_name"],
        provider_connection_api_name=row["provider_connection_api_name"],
        adapter=row["adapter"],
        endpoint=row["endpoint"],
        provider_model_name=row["provider_model_name"],
        credential_api_name=row["credential_api_name"],
        constraints=ModelConstraints.model_validate(row["constraints"]),
        reasoning_level=selected_level,
        provider_reasoning_value=mapping.get(selected_level),
    )


def validate_route_constraints(
    route: ProviderRoute,
    *,
    embedding_dimension: int | None = None,
    input_image_sizes: Sequence[int] = (),
    output_duration_seconds: int | None = None,
) -> None:
    """Reject bounded call requirements before provider or credential work."""
    constraints = route.constraints
    if embedding_dimension is not None and (
        embedding_dimension < 1
        or constraints.embedding_dimensions is None
        or embedding_dimension not in constraints.embedding_dimensions
    ):
        raise invalid_request(
            "embedding_dimension", "The embedding dimension is not available."
        )
    if input_image_sizes and (
        constraints.max_input_images is None
        or constraints.max_input_image_bytes is None
        or len(input_image_sizes) > constraints.max_input_images
        or any(
            size < 1 or size > constraints.max_input_image_bytes
            for size in input_image_sizes
        )
    ):
        raise invalid_request("images", "The input image bounds are exceeded.")
    if output_duration_seconds is not None and (
        output_duration_seconds < 1
        or constraints.max_output_duration_seconds is None
        or output_duration_seconds > constraints.max_output_duration_seconds
    ):
        raise invalid_request("duration", "The output duration is not available.")


def validate_assignment_reasoning(
    connection: Connection[Any],
    provider_model_api_names: Sequence[str],
    reasoning_level: ReasoningLevel | None,
) -> None:
    """Validate one assignment reasoning value across each exact candidate."""
    if not 1 <= len(provider_model_api_names) <= 16:
        raise invalid_request(
            "candidates", "An assignment must contain from 1 through 16 candidates."
        )
    if len(set(provider_model_api_names)) != len(provider_model_api_names):
        raise invalid_request(
            "candidates", "An assignment cannot contain a duplicate candidate."
        )
    for api_name in provider_model_api_names:
        row = connection.execute(
            """SELECT mapping.capabilities, mapping.reasoning_mappings
               FROM router.provider_models AS mapping
               JOIN router.provider_connections AS provider
                 ON provider.id = mapping.provider_id
               WHERE mapping.api_name = %s AND mapping.enabled AND provider.enabled""",
            (api_name,),
        ).fetchone()
        if row is None:
            raise ApiError(
                503,
                "provider_unavailable",
                "No eligible provider-model is available.",
            )
        mapping = {item["level"] for item in row["reasoning_mappings"]}
        selected = reasoning_level
        if selected is None and "reasoning" in row["capabilities"]:
            selected = "medium"
        if selected == "none" and "reasoning" not in row["capabilities"]:
            selected = None
        if selected is not None and selected not in mapping:
            raise invalid_request(
                "reasoning_level",
                "A candidate cannot map the assignment reasoning level.",
            )


def configuration_change(
    connection: Connection[Any],
    actor: AdministratorActor,
    *,
    action: str,
    resource_type: str,
    resource_api_name: str,
    operation: Callable[[], Any],
) -> Any:
    """Record each successful or failed configuration action without values."""
    try:
        result = operation()
        resource_id = (
            result
            if isinstance(result, uuid.UUID)
            else _configuration_resource_id(
                connection, resource_type, resource_api_name
            )
        )
        record_activity(
            connection,
            actor.activity_subject,
            action,
            resource_type,
            resource_api_name=resource_api_name,
            resource_id=resource_id,
        )
    except Exception:
        connection.rollback()
        resource_id = _configuration_resource_id(
            connection, resource_type, resource_api_name
        )
        record_activity(
            connection,
            actor.activity_subject,
            action,
            resource_type,
            resource_api_name=resource_api_name,
            resource_id=resource_id,
            result="failed",
        )
        connection.commit()
        raise
    return result


def _configuration_resource_id(
    connection: Connection[Any], resource_type: str, api_name: str
) -> uuid.UUID | None:
    table = {
        "credential": "provider_credentials",
        "provider": "provider_connections",
        "model": "canonical_models",
        "provider_model": "provider_models",
    }.get(resource_type)
    if table is None:
        return None
    row = connection.execute(
        sql.SQL("SELECT id FROM router.{} WHERE api_name = %s").format(
            sql.Identifier(table)
        ),
        (api_name,),
    ).fetchone()
    return cast("uuid.UUID", row["id"]) if row is not None else None


def _validate_provider(value: ProviderWrite) -> None:
    if value.adapter in _BUILT_IN_ENDPOINTS and value.endpoint is not None:
        raise invalid_request("endpoint", "This adapter has a fixed endpoint.")
    if value.adapter not in _BUILT_IN_ENDPOINTS and value.endpoint is None:
        raise invalid_request("endpoint", "This adapter requires an endpoint.")
    if (
        value.adapter in _REQUIRED_CREDENTIAL_ADAPTERS
        and value.credential_api_name is None
    ):
        raise invalid_request(
            "credential_api_name", "This adapter requires a credential."
        )
    if (
        value.adapter not in _CREDENTIAL_ADAPTERS
        and value.credential_api_name is not None
    ):
        raise invalid_request(
            "credential_api_name", "This adapter does not accept a credential."
        )
    if value.endpoint is not None:
        _validate_endpoint(
            value.endpoint, loopback_required=value.adapter in _LOCAL_ENDPOINTS
        )


def _validate_endpoint(value: str, *, loopback_required: bool) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise invalid_request(
            "endpoint", "The provider endpoint is not safe."
        ) from None
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    valid_host = bool(host and (_valid_public_hostname(host) or loopback))
    if (
        not valid_host
        or (loopback_required and not loopback)
        or parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or "\\" in value
        or value.strip() != value
        or not 1 <= len(value) <= 4096
        or (port is not None and not 1 <= port <= 65535)
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise invalid_request("endpoint", "The provider endpoint is not safe.")


def _valid_public_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        return bool(
            1 <= len(host) <= 253
            and len(labels) >= 2
            and all(
                1 <= len(label) <= 63
                and label.isascii()
                and label.lower() == label
                and label[0].isalnum()
                and label[-1].isalnum()
                and all(character.isalnum() or character == "-" for character in label)
                for label in labels
            )
        )
    return False


def _validate_model(value: ModelWrite) -> None:
    _unique(value.input_modalities, "input_modalities")
    _unique(value.output_modalities, "output_modalities")
    _unique(value.capabilities, "capabilities")
    _validate_constraints(value.constraints)
    _validate_constraint_applicability(
        value.input_modalities,
        value.output_modalities,
        value.constraints.model_dump(mode="json", exclude_none=True)
        if value.constraints is not None
        else {},
    )
    _validate_capability_applicability(value.output_modalities, value.capabilities)
    _validate_price_source(value.price_source, value.price_lookup_key)
    _validate_price_authority(value.price_source, value.manual_price)
    _validate_price(
        value.manual_price.model_dump(mode="json", exclude_none=True)
        if value.manual_price
        else None
    )


def _validate_constraint_applicability(
    inputs: Sequence[str], outputs: Sequence[str], constraints: dict[str, Any]
) -> None:
    if "image" in inputs and (
        constraints.get("max_input_images") is None
        or constraints.get("max_input_image_bytes") is None
    ):
        raise invalid_request(
            "constraints", "An image-input model must declare both image bounds."
        )
    if "embedding" in outputs and not constraints.get("embedding_dimensions"):
        raise invalid_request(
            "constraints", "An embedding model must declare its output dimensions."
        )
    if {"video", "audio"} & set(outputs) and (
        constraints.get("max_output_duration_seconds") is None
    ):
        raise invalid_request(
            "constraints", "A video or audio model must declare a duration bound."
        )
    if "image" not in inputs and (
        constraints.get("max_input_images") is not None
        or constraints.get("max_input_image_bytes") is not None
    ):
        raise invalid_request(
            "constraints", "Image-input bounds require the image input modality."
        )
    if "embedding" not in outputs and (
        constraints.get("embedding_dimensions") is not None
    ):
        raise invalid_request(
            "constraints", "Embedding dimensions require embedding output."
        )
    if not ({"video", "audio"} & set(outputs)) and (
        constraints.get("max_output_duration_seconds") is not None
    ):
        raise invalid_request(
            "constraints", "A duration bound requires video or audio output."
        )


def _applicable_constraints(
    constraints: dict[str, Any], inputs: Sequence[str], outputs: Sequence[str]
) -> dict[str, Any]:
    applicable: set[str] = set()
    if "image" in inputs:
        applicable.update({"max_input_images", "max_input_image_bytes"})
    if "embedding" in outputs:
        applicable.add("embedding_dimensions")
    if {"video", "audio"} & set(outputs):
        applicable.add("max_output_duration_seconds")
    return {key: value for key, value in constraints.items() if key in applicable}


def _lock_catalog_write(connection: Connection[Any]) -> None:
    """Serialize dependent current-state writes in a deadlock-safe order."""
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (_CATALOG_WRITE_LOCK,))


def _normalized_provider_model(
    connection: Connection[Any], value: ProviderModelWrite
) -> tuple[Any, ...]:
    provider = connection.execute(
        """SELECT id, adapter FROM router.provider_connections
           WHERE api_name = %s FOR KEY SHARE""",
        (value.provider_api_name,),
    ).fetchone()
    model = connection.execute(
        "SELECT * FROM router.canonical_models WHERE api_name = %s FOR KEY SHARE",
        (value.model_api_name,),
    ).fetchone()
    if provider is None:
        raise not_found("provider")
    if model is None:
        raise not_found("model")
    inputs = value.input_modalities or model["input_modalities"]
    outputs = value.output_modalities or model["output_modalities"]
    capabilities = (
        value.capabilities if value.capabilities is not None else model["capabilities"]
    )
    constraints = (
        value.constraints.model_dump(mode="json", exclude_none=True)
        if value.constraints is not None
        else _applicable_constraints(model["constraints"], inputs, outputs)
    )
    reasoning = [
        item.model_dump(mode="json") for item in (value.reasoning_mappings or [])
    ]
    _validate_mapping_values(
        adapter=provider["adapter"],
        inputs=inputs,
        outputs=outputs,
        capabilities=capabilities,
        constraints=constraints,
        canonical=model,
        reasoning=reasoning,
    )
    if provider["adapter"] == "local_embeddings" and (
        value.provider_model_name != LOCAL_EMBEDDING_MODEL
        or inputs != ["text"]
        or outputs != ["embedding"]
        or capabilities
        or constraints != {"embedding_dimensions": [LOCAL_EMBEDDING_DIMENSION]}
    ):
        raise invalid_request(
            "provider_model_name",
            "A local embedding mapping must use the approved fixed model.",
        )
    _validate_price_source(value.price_source, value.price_lookup_key)
    _validate_price_authority(value.price_source, value.manual_price)
    manual_price = (
        value.manual_price.model_dump(mode="json", exclude_none=True)
        if value.manual_price
        else None
    )
    _validate_price(manual_price)
    return (
        value.api_name,
        provider["id"],
        model["id"],
        value.provider_model_name,
        value.enabled,
        inputs,
        outputs,
        capabilities,
        Jsonb(constraints),
        Jsonb(reasoning),
        value.price_source,
        value.price_lookup_key,
        Jsonb(manual_price) if manual_price is not None else None,
    )


def _validate_mapping_values(
    *,
    adapter: str,
    inputs: Sequence[str],
    outputs: Sequence[str],
    capabilities: Sequence[str],
    constraints: dict[str, Any],
    canonical: dict[str, Any],
    reasoning: list[dict[str, str]],
) -> None:
    for values, field in (
        (inputs, "input_modalities"),
        (outputs, "output_modalities"),
        (capabilities, "capabilities"),
    ):
        _unique(values, field)
    adapter_inputs, adapter_outputs, adapter_capabilities = _ADAPTER_CAPABILITIES[
        adapter
    ]
    if not set(inputs) <= set(canonical["input_modalities"]) & adapter_inputs:
        raise invalid_request(
            "input_modalities", "The mapping claims an unavailable input modality."
        )
    if not set(outputs) <= set(canonical["output_modalities"]) & adapter_outputs:
        raise invalid_request(
            "output_modalities", "The mapping claims an unavailable output modality."
        )
    if not set(capabilities) <= set(canonical["capabilities"]) & adapter_capabilities:
        raise invalid_request(
            "capabilities", "The mapping claims an unavailable capability."
        )
    _validate_capability_applicability(outputs, capabilities)
    _validate_constraint_applicability(inputs, outputs, constraints)
    _validate_constraint_narrowing(canonical["constraints"], constraints)
    mapping_levels = [item["level"] for item in reasoning]
    _unique(mapping_levels, "reasoning_mappings")
    if any(
        item["provider_value"].strip() != item["provider_value"]
        or any(
            ord(character) <= 0x20 or ord(character) == 0x7F
            for character in item["provider_value"]
        )
        for item in reasoning
    ):
        raise invalid_request(
            "reasoning_mappings", "Provider reasoning values must be plain text."
        )
    if "reasoning" in capabilities and set(mapping_levels) != _REASONING_LEVELS:
        raise invalid_request(
            "reasoning_mappings", "A reasoning mapping must define all common levels."
        )
    if "reasoning" not in capabilities and reasoning:
        raise invalid_request(
            "reasoning_mappings", "This mapping does not support reasoning."
        )


def _validate_constraints(value: ModelConstraints | None) -> None:
    if value and value.embedding_dimensions:
        _unique(value.embedding_dimensions, "embedding_dimensions")


def _validate_capability_applicability(
    outputs: Sequence[str], capabilities: Sequence[str]
) -> None:
    if not ({"text", "structured_json"} & set(outputs)) and capabilities:
        raise invalid_request(
            "capabilities",
            "Text-route capabilities require text or structured JSON output.",
        )


def _validate_constraint_narrowing(
    canonical: dict[str, Any], mapping: dict[str, Any]
) -> None:
    for key, value in mapping.items():
        original = canonical.get(key)
        if original is None:
            raise invalid_request(
                "constraints", "The mapping cannot add a model constraint."
            )
        if key == "embedding_dimensions":
            if not set(value) <= set(original):
                raise invalid_request(
                    "constraints", "Embedding dimensions must narrow the model."
                )
        elif value > original:
            raise invalid_request(
                "constraints", "A provider constraint must narrow the model."
            )


def _validate_provider_model_row(
    connection: Connection[Any], row: dict[str, Any]
) -> None:
    value = ProviderModelWrite.model_validate(row)
    _normalized_provider_model(connection, value)


def _provider_models_for_model(
    connection: Connection[Any], model_api_name: str
) -> list[dict[str, Any]]:
    return connection.execute(
        _PROVIDER_MODEL_WRITE_SELECT
        + " WHERE model.api_name = %s FOR UPDATE OF mapping",
        (model_api_name,),
    ).fetchall()


def _provider_models_for_provider(
    connection: Connection[Any], provider_api_name: str
) -> list[dict[str, Any]]:
    return connection.execute(
        _PROVIDER_MODEL_WRITE_SELECT
        + " WHERE provider.api_name = %s FOR UPDATE OF mapping",
        (provider_api_name,),
    ).fetchall()


def _provider_model_is_assigned(connection: Connection[Any], api_name: str) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM router.assignment_candidates AS candidate
               JOIN router.provider_models AS mapping
                 ON mapping.id = candidate.provider_model_id
               WHERE mapping.api_name = %s LIMIT 1""",
            (api_name,),
        ).fetchone()
        is not None
    )


def _provider_has_assigned_mapping(connection: Connection[Any], api_name: str) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM router.assignment_candidates AS candidate
               JOIN router.provider_models AS mapping
                 ON mapping.id = candidate.provider_model_id
               JOIN router.provider_connections AS provider
                 ON provider.id = mapping.provider_id
               WHERE provider.api_name = %s LIMIT 1""",
            (api_name,),
        ).fetchone()
        is not None
    )


def _validate_assigned_provider_model(
    connection: Connection[Any], api_name: str
) -> None:
    """Keep each assignment valid after one mapping replacement."""
    rows = connection.execute(
        """SELECT assignment.id, assignment.reasoning_level
           FROM router.assignment_definitions AS assignment
           JOIN router.assignment_candidates AS candidate
             ON candidate.assignment_id = assignment.id
           JOIN router.provider_models AS mapping
             ON mapping.id = candidate.provider_model_id
           WHERE mapping.api_name = %s
           FOR UPDATE OF assignment""",
        (api_name,),
    ).fetchall()
    for row in rows:
        candidates = connection.execute(
            """SELECT mapping.api_name
               FROM router.assignment_candidates AS candidate
               JOIN router.provider_models AS mapping
                 ON mapping.id = candidate.provider_model_id
               WHERE candidate.assignment_id = %s
               ORDER BY candidate.position""",
            (row["id"],),
        ).fetchall()
        try:
            validate_assignment_reasoning(
                connection,
                [candidate["api_name"] for candidate in candidates],
                row["reasoning_level"],
            )
        except ApiError as error:
            if error.code == "provider_unavailable":
                raise conflict(
                    "A current assignment requires this provider-model."
                ) from error
            raise
    # An inherited assignment can set a reasoning level that its direct source
    # does not set. Validate the complete graph before this mapping can commit.
    from llmrouter_backend.assignments import validate_all_assignments  # noqa: PLC0415

    validate_all_assignments(connection)


def _model_parameters(value: ModelWrite) -> tuple[Any, ...]:
    constraints = (
        value.constraints.model_dump(mode="json", exclude_none=True)
        if value.constraints
        else {}
    )
    price = (
        value.manual_price.model_dump(mode="json", exclude_none=True)
        if value.manual_price
        else None
    )
    return (
        value.api_name,
        value.display_name,
        value.input_modalities,
        value.output_modalities,
        value.capabilities,
        Jsonb(constraints),
        value.price_source,
        value.price_lookup_key,
        Jsonb(price) if price is not None else None,
    )


def _model_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    manual = result.pop("manual_price", None)
    synchronized = result.pop("synchronized_price", None)
    result["current_price"] = synchronized if result.get("price_source") else manual
    result.pop("id", None)
    return result


_PROVIDER_MODEL_SELECT = """SELECT mapping.api_name,
    provider.api_name AS provider_api_name, model.api_name AS model_api_name,
    mapping.provider_model_name, mapping.enabled, mapping.input_modalities,
    mapping.output_modalities, mapping.capabilities, mapping.constraints,
    mapping.reasoning_mappings,
    CASE WHEN mapping.manual_price IS NOT NULL THEN NULL
         ELSE COALESCE(mapping.price_source, model.price_source) END AS price_source,
    CASE WHEN mapping.manual_price IS NOT NULL THEN NULL
         ELSE COALESCE(mapping.price_lookup_key, model.price_lookup_key) END AS price_lookup_key,
    CASE WHEN mapping.price_source IS NOT NULL THEN mapping.synchronized_price
         WHEN mapping.manual_price IS NOT NULL THEN mapping.manual_price
         WHEN model.price_source IS NOT NULL THEN model.synchronized_price
         ELSE model.manual_price END AS effective_price,
    mapping.created_at
FROM router.provider_models AS mapping
JOIN router.provider_connections AS provider ON provider.id = mapping.provider_id
JOIN router.canonical_models AS model ON model.id = mapping.model_id"""

_PROVIDER_MODEL_WRITE_SELECT = """SELECT mapping.api_name,
    provider.api_name AS provider_api_name, model.api_name AS model_api_name,
    mapping.provider_model_name, mapping.enabled, mapping.input_modalities,
    mapping.output_modalities, mapping.capabilities, mapping.constraints,
    mapping.reasoning_mappings, mapping.price_source, mapping.price_lookup_key,
    mapping.manual_price
FROM router.provider_models AS mapping
JOIN router.provider_connections AS provider ON provider.id = mapping.provider_id
JOIN router.canonical_models AS model ON model.id = mapping.model_id"""


def _provider_model_row(row: dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def _required_provider_model(
    connection: Connection[Any], api_name: str
) -> dict[str, Any]:
    row = provider_model_by_api_name(connection, api_name)
    if row is None:
        raise RuntimeError("The provider-model write did not return its row.")
    return row


def _credential_id(connection: Connection[Any], api_name: str | None) -> Any:
    if api_name is None:
        return None
    row = connection.execute(
        "SELECT id FROM router.provider_credentials WHERE api_name = %s", (api_name,)
    ).fetchone()
    if row is None:
        raise not_found("credential")
    return row["id"]


def _validate_price_source(source: str | None, key: str | None) -> None:
    if (source is None) != (key is None):
        raise invalid_request(
            "price_source", "A price source and lookup key must occur together."
        )
    if source is not None and source not in _PRICE_SOURCES:
        raise invalid_request("price_source", "The price source is not registered.")


def _validate_price_authority(source: str | None, manual_price: Price | None) -> None:
    if source is not None and manual_price is not None:
        raise invalid_request(
            "manual_price", "Manual pricing cannot also select a price source."
        )
    if manual_price is not None and (
        manual_price.source is not None or manual_price.synchronized_at is not None
    ):
        raise invalid_request(
            "manual_price", "Manual pricing cannot contain synchronization metadata."
        )


def _validate_price(value: dict[str, Any] | None) -> None:
    if value is None:
        return
    units = [item["unit"] for item in value["unit_prices"]]
    _unique(units, "unit_prices")


def _unique(values: Sequence[Any], field: str) -> None:
    if len(set(values)) != len(values):
        raise invalid_request(field, "Values must be unique.")


def _page(
    rows: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    return rows[:limit], (
        str(rows[limit - 1]["api_name"]) if len(rows) > limit else None
    )


def _required_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        raise RuntimeError("The configuration write did not return its row.")
    return row


def _credential_unavailable() -> ApiError:
    return ApiError(
        503, "provider_unavailable", "The provider credential is not available."
    )
