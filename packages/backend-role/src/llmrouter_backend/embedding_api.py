"""Closed provider-neutral synchronous embedding API composition."""
# ruff: noqa: D102, EM101, TRY003, TRY004

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from opendle import AssignmentSelector, ExactModelSelector
from pydantic import ConfigDict, Field, field_validator, model_validator

from llmrouter_backend.calls import (
    AdministratorAssignmentCallSelector,
    CallRequest,
    CallRequirements,
    CallResult,
)
from llmrouter_backend.embedding_contract import (
    MAXIMUM_EMBEDDING_INPUTS,
    validate_embedding_inputs,
)
from llmrouter_backend.model_api import (
    AdministratorAssignmentModelSelector,
    AdministratorModelSelector,
    AssignmentModelSelector,
    ModelSelector,
    administrator_attempt_results,
)
from llmrouter_backend.models import (
    AdministratorAttemptResult,
    ClosedModel,
    Usage,
    validate_administrator_attempt_sequence,
)

if TYPE_CHECKING:
    from decimal import Decimal

_API_NAME_PATTERN = r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"


class NativeEmbeddingModel(ClosedModel):
    """Reject coercion and hide private embedding text in diagnostics."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class EmbeddingRequest(NativeEmbeddingModel):
    """One complete closed native embedding-call body."""

    workspace_api_name: str = Field(pattern=_API_NAME_PATTERN)
    selector: ModelSelector
    inputs: list[str] = Field(min_length=1, max_length=MAXIMUM_EMBEDDING_INPUTS)
    tags: list[str] | None = Field(default=None, max_length=32)

    @field_validator("inputs")
    @classmethod
    def validate_inputs(cls, values: list[str]) -> list[str]:
        validate_embedding_inputs(values)
        return values

    @model_validator(mode="after")
    def validate_optional_values(self) -> EmbeddingRequest:
        if "tags" in self.model_fields_set and self.tags is None:
            raise ValueError("The optional tags field cannot be null.")
        return self


class AdministratorEmbeddingRequest(NativeEmbeddingModel):
    """One administrator embedding call without service ownership fields."""

    selector: AdministratorModelSelector
    inputs: list[str] = Field(min_length=1, max_length=MAXIMUM_EMBEDDING_INPUTS)
    tags: list[str] | None = Field(default=None, max_length=32)

    @field_validator("inputs")
    @classmethod
    def validate_inputs(cls, values: list[str]) -> list[str]:
        validate_embedding_inputs(values)
        return values

    @model_validator(mode="after")
    def validate_optional_values(self) -> AdministratorEmbeddingRequest:
        if "tags" in self.model_fields_set and self.tags is None:
            raise ValueError("The optional tags field cannot be null.")
        return self


class EmbeddingVector(NativeEmbeddingModel):
    """One returned vector with its original input index."""

    index: int = Field(strict=True, ge=0)
    values: list[float | int] = Field(min_length=1, max_length=65_536)


class EmbeddingResult(NativeEmbeddingModel):
    """One complete ordered embedding batch result."""

    provider_model_api_name: str = Field(pattern=_API_NAME_PATTERN)
    embeddings: list[EmbeddingVector]
    usage: Usage


class AdministratorEmbeddingResult(NativeEmbeddingModel):
    """One complete administrator embedding result with ordered attempts."""

    logical_call_id: str
    selector: AdministratorModelSelector
    elapsed_ms: int = Field(strict=True, ge=0, le=900_000)
    attempts: list[AdministratorAttemptResult] = Field(min_length=1, max_length=16)
    result: EmbeddingResult

    @model_validator(mode="after")
    def validate_attempts(self) -> AdministratorEmbeddingResult:
        validate_administrator_attempt_sequence(
            self.attempts,
            exact=not isinstance(self.selector, AdministratorAssignmentModelSelector),
            succeeded=True,
        )
        return self


def internal_embedding_call(
    body: EmbeddingRequest | AdministratorEmbeddingRequest,
) -> CallRequest:
    """Translate one validated HTTP body without provider-specific fields."""
    selector: (
        AssignmentSelector | ExactModelSelector | AdministratorAssignmentCallSelector
    )
    if isinstance(body.selector, AdministratorAssignmentModelSelector):
        selector = AdministratorAssignmentCallSelector(
            body.selector.assignment_api_name, body.selector.service_api_name
        )
    elif isinstance(body.selector, AssignmentModelSelector):
        selector = AssignmentSelector(body.selector.assignment_api_name)
    else:
        selector = ExactModelSelector(body.selector.provider_model_api_name)
    return CallRequest(
        workspace_api_name=(
            body.workspace_api_name if isinstance(body, EmbeddingRequest) else None
        ),
        selector=selector,
        kind="embedding",
        requirements=CallRequirements(
            required_inputs=frozenset({"text"}),
            required_output="embedding",
        ),
        request_json=json.dumps(
            body.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        tags=tuple(body.tags or ()),
        expected_embedding_count=len(body.inputs),
    )


def embedding_result(result: CallResult) -> EmbeddingResult:
    """Compose one exact closed synchronous embedding response."""
    if len(result.outputs) != 1 or result.outputs[0].kind != "embedding":
        raise RuntimeError("A synchronous embedding call has no single result.")
    value = json.loads(result.outputs[0].content_json)
    if not isinstance(value, list):
        raise RuntimeError("A synchronous embedding result is invalid.")
    return EmbeddingResult.model_validate(
        {
            "provider_model_api_name": result.provider_model_api_name,
            "embeddings": [
                {"index": index, "values": vector} for index, vector in enumerate(value)
            ],
            "usage": {
                "units": [
                    {"unit": item.unit, "quantity": _decimal_text(item.quantity)}
                    for item in result.usage
                ],
                "cost": _decimal_text(result.cost),
                "currency": result.applied_price.currency,
            },
        }
    )


def administrator_embedding_result(
    body: AdministratorEmbeddingRequest, result: CallResult
) -> AdministratorEmbeddingResult:
    """Compose the closed administrator embedding wrapper."""
    return AdministratorEmbeddingResult(
        logical_call_id=str(result.call_id),
        selector=body.selector,
        elapsed_ms=result.elapsed_ms,
        attempts=administrator_attempt_results(result.attempts),
        result=embedding_result(result),
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
