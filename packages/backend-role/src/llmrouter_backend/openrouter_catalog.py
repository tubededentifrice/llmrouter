"""Strict OpenRouter preview and atomic reviewed-value import policy."""
# ruff: noqa: C901, E501, EM101, PLR0912, PLR2004, TRY003

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

import httpx
from opendle import (
    OpenRouterCapability,
    OpenRouterCatalogError,
    OpenRouterDuplicateModelError,
    OpenRouterInputModality,
    OpenRouterModelFacts,
    OpenRouterModelNotFoundError,
    OpenRouterOutputModality,
    OpenRouterPriceUnit,
    OpenRouterReferenceError,
    normalize_openrouter_model_reference,
    parse_openrouter_model_snapshot,
)
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from llmrouter_backend import catalog
from llmrouter_backend.errors import ApiError, conflict, invalid_request, not_found
from llmrouter_backend.models import (
    ModelConstraints,
    ModelWrite,
    OpenRouterImportConflict,
    OpenRouterImportIssue,
    OpenRouterModelImportPreview,
    OpenRouterModelImportRequest,
    OpenRouterProviderModelOption,
    OpenRouterReasoningPreview,
    Price,
    ProviderModelWrite,
    ReasoningMapping,
    UnitPriceWrite,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from psycopg import Connection

_MODELS_URL = "https://openrouter.ai/api/v1/models?output_modalities=all"
_MAXIMUM_BODY_BYTES = 8 * 1024 * 1024
_MAXIMUM_HEADER_BYTES = 32 * 1024
_MAXIMUM_HEADERS = 64
_CONNECT_TIMEOUT_SECONDS = 3.0
_READ_TIMEOUT_SECONDS = 5.0
_TOTAL_TIMEOUT_SECONDS = 12.0
_ROUTER_IMAGE_COUNT_LIMIT = 8
_ROUTER_IMAGE_BYTE_LIMIT = 20 * 1024 * 1024
_API_NAME_PART = re.compile(r"[^a-z0-9]+")
_COMMON_REASONING_LEVELS = ("none", "low", "medium", "high")
_SUPPORTED_PRICE_UNITS = {
    OpenRouterPriceUnit.INPUT_TOKEN: "input_token",
    OpenRouterPriceUnit.OUTPUT_TOKEN: "output_token",
    OpenRouterPriceUnit.CACHED_INPUT_TOKEN: "cached_input_token",
    OpenRouterPriceUnit.INPUT_IMAGE: "image",
    OpenRouterPriceUnit.REQUEST: "request",
}


@dataclass(frozen=True, slots=True)
class _OpenRouterModelProposal:
    """Keep one bounded parsed proposal between fetch and current-state projection."""

    facts: OpenRouterModelFacts
    model: ModelWrite
    reviewed_price: Price | None
    issues: list[OpenRouterImportIssue]


def prepare_openrouter_model_preview(
    model_id_or_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    monotonic_clock: Callable[[], float] = monotonic,
) -> _OpenRouterModelProposal:
    """Fetch one fixed public snapshot and prepare a database-free proposal."""
    try:
        model_id = normalize_openrouter_model_reference(model_id_or_url)
    except OpenRouterReferenceError as error:
        raise invalid_request(
            "model_id_or_url", "Enter one OpenRouter model identifier or supported URL."
        ) from error
    snapshot = _fetch_snapshot(transport=transport, monotonic_clock=monotonic_clock)
    try:
        facts = parse_openrouter_model_snapshot(snapshot, model_id)
    except OpenRouterModelNotFoundError as error:
        raise not_found("OpenRouter model") from error
    except (OpenRouterCatalogError, OpenRouterDuplicateModelError) as error:
        raise _catalog_unavailable() from error

    model, reviewed_price, issues = _native_model(facts)
    return _OpenRouterModelProposal(facts, model, reviewed_price, issues)


def project_openrouter_model_preview(
    connection: Connection[Any], proposal: _OpenRouterModelProposal
) -> OpenRouterModelImportPreview:
    """Project one prepared proposal against a short current-state snapshot."""
    conflicts = _model_conflicts(connection, proposal.model, proposal.facts.model_id)
    options, mapping_conflicts = _provider_options(
        connection,
        facts=proposal.facts,
        model=proposal.model,
        model_blocked=bool(conflicts),
    )
    conflicts.extend(mapping_conflicts)
    return OpenRouterModelImportPreview(
        source_model_id=proposal.facts.model_id,
        model=proposal.model,
        reviewed_price=proposal.reviewed_price,
        reasoning=_reasoning_preview(proposal.facts),
        supported_constraints=[
            item.value for item in proposal.facts.supported_constraints
        ],
        provider_options=options,
        conflicts=conflicts,
        issues=proposal.issues,
        can_confirm=not conflicts and any(item.selectable for item in options),
    )


def import_reviewed_openrouter_model(
    connection: Connection[Any], value: OpenRouterModelImportRequest
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Create reviewed native values in one serialized create-only transaction."""
    try:
        source_model_id = normalize_openrouter_model_reference(value.source_model_id)
    except OpenRouterReferenceError as error:
        raise invalid_request(
            "source_model_id", "The source model identity is invalid."
        ) from error
    if source_model_id != value.source_model_id:
        raise invalid_request(
            "source_model_id", "Use the exact reviewed model identity."
        )

    catalog.lock_catalog_write(connection)
    _validate_reviewed_model(value.model, source_model_id, value.reviewed_price)
    _validate_selected_mappings(value.model, source_model_id, value.provider_models)
    _reject_model_conflicts(connection, value.model, source_model_id)
    _lock_and_validate_connections(connection, value.provider_models)

    created_model = catalog.create_model(connection, value.model)
    if value.reviewed_price is not None:
        connection.execute(
            """UPDATE router.canonical_models
               SET synchronized_price = %s
               WHERE api_name = %s""",
            (
                Jsonb(value.reviewed_price.model_dump(mode="json", exclude_none=True)),
                value.model.api_name,
            ),
        )
        refreshed = catalog.model_by_api_name(connection, value.model.api_name)
        if refreshed is None:
            raise RuntimeError("The imported model is unavailable after creation.")
        created_model = refreshed
    created_mappings = [
        catalog.create_provider_model(connection, mapping)
        for mapping in value.provider_models
    ]
    return created_model, created_mappings


def _fetch_snapshot(
    *,
    transport: httpx.BaseTransport | None,
    monotonic_clock: Callable[[], float],
) -> bytes:
    deadline = monotonic_clock() + _TOTAL_TIMEOUT_SECONDS

    def remaining() -> float:
        seconds = deadline - monotonic_clock()
        if seconds <= 0:
            raise _catalog_unavailable()
        return seconds

    try:
        with httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT_SECONDS,
                read=_READ_TIMEOUT_SECONDS,
                write=_READ_TIMEOUT_SECONDS,
                pool=_CONNECT_TIMEOUT_SECONDS,
            ),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            remaining()
            with client.stream(
                "GET",
                _MODELS_URL,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "LLM-Router/1.0",
                },
            ) as response:
                remaining()
                _validate_response_control(response)
                content = bytearray()
                chunks = iter(response.iter_bytes())
                while True:
                    timeout_extension = response.request.extensions.get("timeout")
                    if isinstance(timeout_extension, dict):
                        timeout_extension["read"] = min(
                            _READ_TIMEOUT_SECONDS, remaining()
                        )
                    try:
                        chunk = next(chunks)
                    except StopIteration:
                        break
                    remaining()
                    if len(chunk) > _MAXIMUM_BODY_BYTES - len(content):
                        raise _catalog_unavailable()
                    content.extend(chunk)
                remaining()
    except ApiError:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError) as error:
        raise _catalog_unavailable() from error
    if not content:
        raise _catalog_unavailable()
    return bytes(content)


def _validate_response_control(response: httpx.Response) -> None:
    if response.status_code != 200:
        raise _catalog_unavailable()
    if len(response.headers) > _MAXIMUM_HEADERS:
        raise _catalog_unavailable()
    header_bytes = sum(
        len(name.encode("ascii", "ignore")) + len(value.encode("latin-1", "ignore")) + 4
        for name, value in response.headers.multi_items()
    )
    if header_bytes > _MAXIMUM_HEADER_BYTES:
        raise _catalog_unavailable()
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        raise _catalog_unavailable()
    content_encoding = response.headers.get("content-encoding")
    if content_encoding not in {None, "identity"}:
        raise _catalog_unavailable()
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError as error:
            raise _catalog_unavailable() from error
        if length < 1 or length > _MAXIMUM_BODY_BYTES:
            raise _catalog_unavailable()


def _native_model(
    facts: OpenRouterModelFacts,
) -> tuple[ModelWrite, Price | None, list[OpenRouterImportIssue]]:
    issues: list[OpenRouterImportIssue] = []
    inputs: list[str] = []
    for input_modality in facts.input_modalities:
        if input_modality in {
            OpenRouterInputModality.TEXT,
            OpenRouterInputModality.IMAGE,
        }:
            inputs.append(input_modality.value)
        else:
            issues.append(
                _issue(
                    "input_modality_unsupported",
                    "input_modalities",
                    input_modality.value,
                    "The Router OpenRouter adapter does not support this input modality.",
                )
            )
    if not inputs:
        raise invalid_request(
            "model_id_or_url",
            "The OpenRouter model has no input modality that this adapter supports.",
        )

    outputs: list[str] = []
    for output_modality in facts.output_modalities:
        if output_modality is OpenRouterOutputModality.EMBEDDING:
            issues.append(
                _issue(
                    "embedding_dimensions_unknown",
                    "output_modalities",
                    output_modality.value,
                    "OpenRouter does not supply the embedding dimensions required by the Router contract.",
                )
            )
        elif output_modality in {
            OpenRouterOutputModality.VIDEO,
            OpenRouterOutputModality.AUDIO,
        }:
            issues.append(
                _issue(
                    "media_duration_unknown",
                    "output_modalities",
                    output_modality.value,
                    "OpenRouter does not supply the duration bound required by the Router contract.",
                )
            )
        else:
            outputs.append(output_modality.value)
    source_capabilities = set(facts.capabilities)
    if (
        OpenRouterCapability.STRUCTURED_JSON in source_capabilities
        and "text" in outputs
    ):
        outputs.append("structured_json")
    if not outputs:
        raise invalid_request(
            "model_id_or_url",
            "The OpenRouter model has no output modality that maps to the native contract.",
        )

    text_route = bool({"text", "structured_json"} & set(outputs))
    reasoning_mappings = _reasoning_mappings(facts)
    capabilities = [
        capability.value
        for capability in (
            OpenRouterCapability.TOOL_CALLING,
            OpenRouterCapability.STREAMING,
        )
        if capability in source_capabilities and text_route
    ]
    if (
        OpenRouterCapability.REASONING in source_capabilities
        and text_route
        and reasoning_mappings is not None
    ):
        capabilities.append(OpenRouterCapability.REASONING.value)
    constraints = ModelConstraints(
        max_context_tokens=facts.context_window_tokens,
        max_output_tokens=facts.maximum_output_tokens,
        max_input_images=_ROUTER_IMAGE_COUNT_LIMIT if "image" in inputs else None,
        max_input_image_bytes=_ROUTER_IMAGE_BYTE_LIMIT if "image" in inputs else None,
    )
    if "image" in inputs:
        issues.append(
            _issue(
                "router_input_limits_applied",
                "constraints",
                "image",
                "OpenRouter does not supply per-model image upload bounds. The proposal uses the fixed Router input safety limits.",
            )
        )
    if "image" in outputs:
        issues.append(
            _issue(
                "output_modality_unsupported",
                "output_modalities",
                "image",
                "The canonical model keeps the source image output fact, but the current Router OpenRouter adapter cannot execute that output.",
            )
        )
    api_name = _api_name(facts.canonical_slug or facts.model_id)
    display_name = facts.display_source_name or facts.model_id
    if len(display_name) > 200:
        display_name = display_name[:197].rstrip() + "..."
        issues.append(
            _issue(
                "display_name_shortened",
                "display_name",
                None,
                "The source display name was shortened to the native field limit.",
            )
        )
    model = ModelWrite(
        api_name=api_name,
        display_name=display_name,
        input_modalities=cast("Any", inputs),
        output_modalities=cast("Any", outputs),
        capabilities=cast("Any", capabilities),
        constraints=constraints if constraints.model_dump(exclude_none=True) else None,
        price_source="openrouter",
        price_lookup_key=facts.model_id,
    )
    reviewed_price = _reviewed_price(facts, issues)
    if facts.price_overrides:
        issues.append(
            _issue(
                "conditional_price_unsupported",
                "reviewed_price",
                None,
                "Conditional OpenRouter price overrides are not in the native price contract and were not applied.",
            )
        )
    if facts.reasoning.supported and reasoning_mappings is None:
        issues.append(
            _issue(
                "reasoning_mapping_incomplete",
                "reasoning_mappings",
                None,
                "The source does not prove a complete mapping for all common Router reasoning levels. The provider route does not claim reasoning.",
            )
        )
    return model, reviewed_price, issues


def _reviewed_price(
    facts: OpenRouterModelFacts, issues: list[OpenRouterImportIssue]
) -> Price | None:
    prices: list[UnitPriceWrite] = []
    seen: set[str] = set()
    for source_price in facts.price_source_values:
        if source_price.amount == 0:
            issues.append(
                _issue(
                    "source_price_zero_omitted",
                    "reviewed_price",
                    source_price.unit.value,
                    "A zero OpenRouter source price is not accepted price authority and was not imported.",
                )
            )
            continue
        native_unit = _SUPPORTED_PRICE_UNITS.get(source_price.unit)
        if native_unit is None:
            issues.append(
                _issue(
                    "price_unit_unsupported",
                    "reviewed_price",
                    source_price.unit.value,
                    "This OpenRouter price unit has no exact Router accounting unit and was not imported.",
                )
            )
            continue
        if native_unit in seen:
            continue
        try:
            price = UnitPriceWrite(
                unit=cast("Any", native_unit), amount=_decimal_text(source_price.amount)
            )
        except ValidationError:
            issues.append(
                _issue(
                    "price_unit_unsupported",
                    "reviewed_price",
                    source_price.unit.value,
                    "This OpenRouter price value is outside the exact native decimal bounds and was not imported.",
                )
            )
            continue
        prices.append(price)
        seen.add(native_unit)
    return (
        Price(currency="USD", unit_prices=prices, source="openrouter")
        if prices
        else None
    )


def _provider_options(
    connection: Connection[Any],
    *,
    facts: OpenRouterModelFacts,
    model: ModelWrite,
    model_blocked: bool,
) -> tuple[list[OpenRouterProviderModelOption], list[OpenRouterImportConflict]]:
    providers = connection.execute(
        """SELECT api_name, display_name, enabled
           FROM router.provider_connections
           WHERE adapter = 'openrouter'
           ORDER BY api_name
           LIMIT 201"""
    ).fetchall()
    if len(providers) > 200:
        raise ApiError(409, "conflict", "More than 200 OpenRouter connections exist.")
    reasoning_mappings = _reasoning_mappings(facts)
    adapter_outputs = [
        output
        for output in model.output_modalities
        if output in {"text", "structured_json"}
    ]
    if not adapter_outputs:
        raise invalid_request(
            "model_id_or_url",
            "The OpenRouter adapter cannot execute a mapped output for this model.",
        )
    adapter_capabilities = [
        capability
        for capability in model.capabilities
        if capability != "reasoning" or reasoning_mappings is not None
    ]
    conflicts: list[OpenRouterImportConflict] = []
    options: list[OpenRouterProviderModelOption] = []
    for provider in providers:
        provider_name = cast("str", provider["api_name"])
        mapping_name = _api_name(f"{model.api_name}-{provider_name}")
        current = connection.execute(
            """SELECT mapping.api_name, provider.api_name AS provider_api_name
               FROM router.provider_models AS mapping
               JOIN router.provider_connections AS provider
                 ON provider.id = mapping.provider_id
               WHERE mapping.api_name = %s
                  OR (provider.api_name = %s AND mapping.provider_model_name = %s)
               ORDER BY mapping.api_name LIMIT 1""",
            (mapping_name, provider_name, facts.model_id),
        ).fetchone()
        mapping_conflict = current is not None
        if current is not None:
            conflicts.append(
                OpenRouterImportConflict(
                    kind="provider_model",
                    api_name=current["api_name"],
                    provider_api_name=current["provider_api_name"],
                    message="An existing provider-model already uses the proposed mapping identity or this connection's wire model.",
                )
            )
        enabled = cast("bool", provider["enabled"])
        selectable = enabled and not model_blocked and not mapping_conflict
        reason = None
        if not enabled:
            reason = "The provider connection is disabled."
        elif model_blocked:
            reason = "An existing canonical model blocks create-only import."
        elif mapping_conflict:
            reason = "An existing provider-model blocks create-only import."
        options.append(
            OpenRouterProviderModelOption(
                provider_api_name=provider_name,
                provider_display_name=provider["display_name"],
                provider_enabled=enabled,
                selectable=selectable,
                unavailable_reason=reason,
                provider_model=ProviderModelWrite(
                    api_name=mapping_name,
                    provider_api_name=provider_name,
                    model_api_name=model.api_name,
                    provider_model_name=facts.model_id,
                    enabled=True,
                    input_modalities=model.input_modalities,
                    output_modalities=cast("Any", adapter_outputs),
                    capabilities=cast("Any", adapter_capabilities),
                    constraints=model.constraints,
                    reasoning_mappings=reasoning_mappings or [],
                ),
            )
        )
    return options, conflicts


def _model_conflicts(
    connection: Connection[Any], model: ModelWrite, source_model_id: str
) -> list[OpenRouterImportConflict]:
    rows = connection.execute(
        """SELECT api_name
           FROM router.canonical_models
           WHERE api_name = %s
              OR (price_source = 'openrouter' AND price_lookup_key = %s)
           ORDER BY api_name LIMIT 2""",
        (model.api_name, source_model_id),
    ).fetchall()
    return [
        OpenRouterImportConflict(
            kind="model",
            api_name=row["api_name"],
            message="This canonical model identity or OpenRouter source model already exists.",
        )
        for row in rows
    ]


def _reject_model_conflicts(
    connection: Connection[Any], model: ModelWrite, source_model_id: str
) -> None:
    if _model_conflicts(connection, model, source_model_id):
        raise conflict(
            "The reviewed canonical model or OpenRouter source already exists."
        )


def _validate_reviewed_model(
    model: ModelWrite, source_model_id: str, reviewed_price: Price | None
) -> None:
    if (
        model.price_source != "openrouter"
        or model.price_lookup_key != source_model_id
        or model.manual_price is not None
    ):
        raise invalid_request(
            "model", "The reviewed model must keep its exact OpenRouter price source."
        )
    if reviewed_price is not None and (
        reviewed_price.currency != "USD"
        or reviewed_price.source != "openrouter"
        or reviewed_price.synchronized_at is not None
    ):
        raise invalid_request(
            "reviewed_price",
            "The reviewed OpenRouter price must use USD source values without synchronization metadata.",
        )
    if reviewed_price is not None and any(
        Decimal(item.amount) == 0 for item in reviewed_price.unit_prices
    ):
        raise invalid_request(
            "reviewed_price",
            "A reviewed OpenRouter source price must be greater than zero.",
        )


def _validate_selected_mappings(
    model: ModelWrite,
    source_model_id: str,
    mappings: Sequence[ProviderModelWrite],
) -> None:
    provider_names = [item.provider_api_name for item in mappings]
    mapping_names = [item.api_name for item in mappings]
    if len(provider_names) != len(set(provider_names)):
        raise invalid_request(
            "provider_models", "Select an OpenRouter connection only once."
        )
    if len(mapping_names) != len(set(mapping_names)):
        raise invalid_request(
            "provider_models", "Each provider-model identity must be unique."
        )
    for mapping in mappings:
        if (
            mapping.model_api_name != model.api_name
            or mapping.provider_model_name != source_model_id
            or not mapping.enabled
            or mapping.price_source is not None
            or mapping.price_lookup_key is not None
            or mapping.manual_price is not None
        ):
            raise invalid_request(
                "provider_models",
                "Each selected mapping must keep the reviewed model, wire identity, enabled state, and inherited price.",
            )


def _lock_and_validate_connections(
    connection: Connection[Any], mappings: Sequence[ProviderModelWrite]
) -> None:
    for mapping in mappings:
        provider = connection.execute(
            """SELECT adapter, enabled
               FROM router.provider_connections
               WHERE api_name = %s FOR UPDATE""",
            (mapping.provider_api_name,),
        ).fetchone()
        if provider is None:
            raise not_found("provider")
        if provider["adapter"] != "openrouter":
            raise conflict("A selected provider is no longer an OpenRouter connection.")
        if not provider["enabled"]:
            raise conflict("A selected OpenRouter connection is disabled.")
        current = connection.execute(
            """SELECT 1 FROM router.provider_models AS mapping
               JOIN router.provider_connections AS provider
                 ON provider.id = mapping.provider_id
               WHERE mapping.api_name = %s
                  OR (provider.api_name = %s AND mapping.provider_model_name = %s)
               LIMIT 1""",
            (mapping.api_name, mapping.provider_api_name, mapping.provider_model_name),
        ).fetchone()
        if current is not None:
            raise conflict("A reviewed provider-model already exists.")


def _reasoning_mappings(
    facts: OpenRouterModelFacts,
) -> list[ReasoningMapping] | None:
    reasoning = facts.reasoning
    if not reasoning.supported or not reasoning.source_configuration_available:
        return None
    efforts = reasoning.supported_efforts
    enabled_supported = efforts is None or all(
        level in efforts for level in _COMMON_REASONING_LEVELS[1:]
    )
    if reasoning.mandatory is not False or not enabled_supported:
        return None
    return [
        ReasoningMapping(level=cast("Any", level), provider_value=level)
        for level in _COMMON_REASONING_LEVELS
    ]


def _reasoning_preview(facts: OpenRouterModelFacts) -> OpenRouterReasoningPreview:
    reasoning = facts.reasoning
    return OpenRouterReasoningPreview(
        supported=reasoning.supported,
        mandatory=reasoning.mandatory,
        source_configuration_available=reasoning.source_configuration_available,
        default_enabled=reasoning.default_enabled,
        default_effort=reasoning.default_effort,
        supported_efforts=(
            list(reasoning.supported_efforts)
            if reasoning.supported_efforts is not None
            else None
        ),
        supports_max_tokens=reasoning.supports_max_tokens,
    )


def _api_name(value: str) -> str:
    normalized = _API_NAME_PART.sub("-", value.lower()).strip("-")
    if not normalized or not normalized[0].isalpha():
        normalized = f"model-{normalized}".rstrip("-")
    if len(normalized) <= 63:
        return normalized
    digest = hashlib.sha256(value.encode("ascii")).hexdigest()[:10]
    return f"{normalized[:52].rstrip('-')}-{digest}"


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _issue(
    code: str, field: str, source_value: str | None, message: str
) -> OpenRouterImportIssue:
    return OpenRouterImportIssue.model_validate(
        {
            "code": code,
            "field": field,
            "source_value": source_value,
            "message": message,
        }
    )


def _catalog_unavailable() -> ApiError:
    return ApiError(
        503,
        "upstream_failed",
        "The OpenRouter catalog is unavailable.",
    )
