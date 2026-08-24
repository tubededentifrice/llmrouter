"""Provider-neutral connection-lifetime call routing and fallback."""
# ruff: noqa: BLE001, C901, D105, D107, EM101, FBT001, FBT003, PLR0912, PLR0913, PLR0915, PLR2004, TRY003, TRY301

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import re
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import psycopg
from opendle import (
    AssignmentSelector,
    CallFailurePhase,
    ExactModelSelector,
    RouterContractError,
    normalize_tags,
)
from psycopg.rows import dict_row

from llmrouter_backend import accounting, catalog, diagnostics
from llmrouter_backend.accounting import (
    AttemptAccountingWrite,
    AttemptPriceSnapshot,
    CallAccountingWrite,
    UsageAmount,
)
from llmrouter_backend.assignments import resolve_assignment_for_call
from llmrouter_backend.errors import ApiError, not_found
from llmrouter_backend.models import RequestAttempt
from llmrouter_backend.store import ServiceActor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from llmrouter_backend.catalog import ProviderCredentialKeys, ProviderRoute
    from llmrouter_backend.object_store import ObjectStore

type CallKind = Literal["model", "embedding", "media"]
type ProviderFailureClass = Literal[
    "authentication",
    "rate_limited",
    "timeout",
    "transport",
    "unavailable",
    "refusal",
    "incompatible",
    "invalid_response",
    "interrupted",
    "upstream_failed",
]
type OutputKind = Literal[
    "text_delta", "tool_call", "standard", "structured_json", "embedding", "media"
]
type OutputValidator = Callable[[object], bool | Awaitable[bool]]
type VisibleOutputWriter = Callable[[ProviderOutput], Awaitable[None]]
type VisibleOutputStart = Callable[[str], Awaitable[None]]

_API_NAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_INPUTS = frozenset({"text", "image"})
_OUTPUTS = frozenset(
    {"text", "structured_json", "embedding", "image", "video", "audio"}
)
_MEDIA_OUTPUT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/webm",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
    }
)
_CAPABILITIES = frozenset({"tool_calling", "streaming", "reasoning"})
_COOLDOWN_FAILURES = frozenset(
    {
        "authentication",
        "rate_limited",
        "timeout",
        "transport",
        "unavailable",
        "invalid_response",
    }
)
_FAILURE_CLASSES = _COOLDOWN_FAILURES | frozenset(
    {"refusal", "incompatible", "interrupted", "upstream_failed"}
)
_MAXIMUM_REQUEST_JSON_BYTES = 2 * 1024 * 1024
_MAXIMUM_OUTPUT_JSON_BYTES = 5_000_000
_MAXIMUM_EMBEDDING_INPUTS = 32
_COST_DECIMAL_PRECISION = 112
_USAGE_UNITS = frozenset(
    {
        "input_token",
        "output_token",
        "cached_input_token",
        "image",
        "video_second",
        "audio_second",
        "request",
        "provider_unit",
    }
)
_CATALOG_WRITE_LOCK = 4_993_044_345_823
_DATABASE_CONNECT_TIMEOUT_SECONDS = 2
_DATABASE_STATEMENT_TIMEOUT_MILLISECONDS = 2_000
_DATABASE_LOCK_TIMEOUT_MILLISECONDS = 500
_COOLDOWN_FAILURE_COUNT = 3
_COOLDOWN_WINDOW_SECONDS = 60.0
_COOLDOWN_DURATION_SECONDS = 60.0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CallRequirements:
    """One validated set of actual call capabilities and modalities."""

    required_inputs: frozenset[str]
    required_output: str
    required_capabilities: frozenset[str] = frozenset()
    embedding_dimension: int | None = None
    input_image_sizes: tuple[int, ...] = ()
    output_duration_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.required_inputs or not self.required_inputs <= _INPUTS:
            raise ValueError("The required input modalities are invalid.")
        if self.required_output not in _OUTPUTS:
            raise ValueError("The required output modality is invalid.")
        if not self.required_capabilities <= _CAPABILITIES:
            raise ValueError("The required call capabilities are invalid.")
        if self.embedding_dimension is not None and (
            type(self.embedding_dimension) is not int
            or not 1 <= self.embedding_dimension <= 65_536
        ):
            raise ValueError("The embedding dimension is invalid.")
        if (
            len(self.input_image_sizes) > 8
            or any(
                type(size) is not int or not 1 <= size <= 20 * 1024 * 1024
                for size in self.input_image_sizes
            )
            or sum(self.input_image_sizes) > 50 * 1024 * 1024
        ):
            raise ValueError("The input image bounds are invalid.")
        if self.output_duration_seconds is not None and (
            type(self.output_duration_seconds) is not int
            or not 1 <= self.output_duration_seconds <= 86_400
        ):
            raise ValueError("The output duration is invalid.")
        if (self.embedding_dimension is not None) != (
            self.required_output == "embedding"
        ):
            raise ValueError("The embedding dimension must match embedding output.")
        if self.input_image_sizes and "image" not in self.required_inputs:
            raise ValueError("Image bytes require the image input modality.")
        if self.output_duration_seconds is not None and self.required_output not in {
            "video",
            "audio",
        }:
            raise ValueError("Output duration applies only to video or audio.")


@dataclass(frozen=True, slots=True)
class CallRequest:
    """One internal native call after HTTP shape validation."""

    workspace_api_name: str
    selector: AssignmentSelector | ExactModelSelector
    kind: CallKind
    requirements: CallRequirements
    request_json: str
    tags: tuple[str, ...] = ()
    excluded_provider_model_api_names: tuple[str, ...] = ()
    streaming: bool = False
    expected_embedding_count: int | None = None
    output_validator: OutputValidator | None = None
    media: tuple[diagnostics.CapturedMedia, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"model", "embedding", "media"}:
            raise ValueError("The call kind is invalid.")
        if _API_NAME.fullmatch(self.workspace_api_name) is None:
            raise ValueError("The workspace API name is invalid.")
        if not isinstance(self.selector, AssignmentSelector | ExactModelSelector):
            raise TypeError("The call selector is invalid.")
        try:
            parsed = _load_json(self.request_json)
        except (ValueError, RecursionError) as error:
            raise ValueError("The request log value must be valid JSON.") from error
        if (
            not isinstance(parsed, dict)
            or not 1
            <= len(self.request_json.encode("utf-8"))
            <= _MAXIMUM_REQUEST_JSON_BYTES
        ):
            raise ValueError("The request JSON is outside its safe bounds.")
        try:
            object.__setattr__(self, "tags", normalize_tags(self.tags))
        except RouterContractError as error:
            raise ValueError("The request tags are invalid.") from error
        excluded = self.excluded_provider_model_api_names
        if (
            len(excluded) > 16
            or len(excluded) != len(set(excluded))
            or any(_API_NAME.fullmatch(name) is None for name in excluded)
        ):
            raise ValueError("The excluded provider-model list is invalid.")
        if excluded and not isinstance(self.selector, AssignmentSelector):
            raise ValueError("Only an assignment call can exclude candidates.")
        if self.streaming and (
            self.kind != "model"
            or self.requirements.required_output != "text"
            or "streaming" not in self.requirements.required_capabilities
        ):
            raise ValueError("Only a streaming text model call can stream.")
        expected_outputs = {
            "model": {"text", "structured_json"},
            "embedding": {"embedding"},
            "media": {"image", "video", "audio"},
        }
        if self.requirements.required_output not in expected_outputs[self.kind]:
            raise ValueError("The call kind and output modality do not agree.")
        if self.kind == "embedding":
            if (
                type(self.expected_embedding_count) is not int
                or not 1 <= self.expected_embedding_count <= 32
            ):
                raise ValueError("The embedding input count is invalid.")
        elif self.expected_embedding_count is not None:
            raise ValueError("Only an embedding call has an embedding input count.")
        if (self.output_validator is not None) != (
            self.requirements.required_output == "structured_json"
        ):
            raise ValueError("Structured output requires one result validator.")
        if self.kind == "embedding" and self.media:
            raise ValueError("An embedding call cannot contain retained media.")
        if self.requirements.required_output == "audio" and self.media:
            raise ValueError("Audio generation cannot contain input media.")
        if any(item.role != "input" for item in self.media):
            raise ValueError("A call request can contain only input media.")
        if any(
            type(item.body) is not bytes
            or item.media_type not in {"image/jpeg", "image/png", "image/webp"}
            or not 1 <= len(item.body) <= 20 * 1024 * 1024
            for item in self.media
        ):
            raise ValueError("A call request input image is invalid.")
        if tuple(len(item.body) for item in self.media) != (
            self.requirements.input_image_sizes
        ):
            raise ValueError("The retained input image bytes do not match the call.")


@dataclass(frozen=True, slots=True)
class ProviderOperation:
    """Immutable provider-neutral facts for price and usage admission."""

    kind: CallKind
    requirements: CallRequirements
    streaming: bool
    expected_embedding_count: int | None


@dataclass(frozen=True, slots=True)
class ProviderAttemptRequest:
    """One adapter request with control credentials outside logged content."""

    route: ProviderRoute
    request_json: str
    credential: str | None
    kind: CallKind
    requirements: CallRequirements
    streaming: bool
    expected_embedding_count: int | None
    input_media: tuple[diagnostics.CapturedMedia, ...]

    @property
    def operation(self) -> ProviderOperation:
        """Return operation facts without request content or control values."""
        return ProviderOperation(
            self.kind,
            self.requirements,
            self.streaming,
            self.expected_embedding_count,
        )


@dataclass(frozen=True, slots=True)
class ProviderOutput:
    """One provider-neutral output value from an active attempt."""

    kind: OutputKind
    content_json: str
    media_body: bytes | None = None

    def __post_init__(self) -> None:
        if self.kind not in {
            "text_delta",
            "tool_call",
            "standard",
            "structured_json",
            "embedding",
            "media",
        }:
            raise ValueError("The provider output kind is invalid.")
        if (
            not 1
            <= len(self.content_json.encode("utf-8"))
            <= _MAXIMUM_OUTPUT_JSON_BYTES
        ):
            raise ValueError("The provider output is outside its safe bounds.")
        try:
            value = _load_json(self.content_json)
        except (ValueError, RecursionError) as error:
            raise ValueError("The provider output is not valid JSON.") from error
        if self.kind == "media":
            if (
                not isinstance(value, dict)
                or type(value.get("size_bytes")) is not int
                or type(self.media_body) is not bytes
                or len(self.media_body) != value["size_bytes"]
                or not 1 <= len(self.media_body) <= 1024 * 1024 * 1024
            ):
                raise ValueError("The provider media body is invalid.")
        elif self.media_body is not None:
            raise ValueError("Only a media result can contain a media body.")


@dataclass(frozen=True, slots=True)
class ProviderCompleted:
    """Finish one provider attempt with all reported usage."""

    usage: tuple[UsageAmount, ...] = ()

    def __post_init__(self) -> None:
        units = [item.unit for item in self.usage]
        if len(units) != len(set(units)):
            raise ValueError("Provider usage units must be unique.")


type ProviderEvent = ProviderOutput | ProviderCompleted


class ProviderFailureError(Exception):
    """Report one safe failure and mark a possible provider-side write uncertain."""

    def __init__(
        self,
        failure_class: ProviderFailureClass,
        *,
        usage: tuple[UsageAmount, ...] = (),
        phase: CallFailurePhase = CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
    ) -> None:
        if failure_class not in _FAILURE_CLASSES:
            raise ValueError("The provider failure class is invalid.")
        if len({item.unit for item in usage}) != len(usage):
            raise ValueError("Provider failure usage units must be unique.")
        if phase not in {
            CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
            CallFailurePhase.UNCERTAIN,
        }:
            raise ValueError("The provider failure phase is invalid.")
        self.failure_class = failure_class
        self.usage = usage
        self.phase = phase
        super().__init__("The provider attempt failed.")


class ProviderAdapter(Protocol):
    """Create one attempt and declare every unit that it can report."""

    usage_units: frozenset[str]

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Declare the possible usage units for this exact operation."""
        ...

    def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one attempt. The adapter must not retry the provider-model."""
        ...


class CallExecutionError(RuntimeError):
    """Return one safe call failure and its output-visibility phase."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: CallFailurePhase,
        field: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.code = code
        self.phase = phase
        self.field = field
        self.reason = reason
        super().__init__(message)


class OutputValidationUnavailableError(RuntimeError):
    """Stop fallback when the Router cannot safely validate provider output."""


@dataclass(frozen=True, slots=True)
class CallResult:
    """One complete provider-neutral call result before HTTP composition."""

    call_id: uuid.UUID
    provider_model_api_name: str
    outputs: tuple[ProviderOutput, ...]
    usage: tuple[UsageAmount, ...]
    applied_price: AttemptPriceSnapshot
    cost: Decimal


@dataclass(frozen=True, slots=True)
class CooldownSnapshot:
    """One current best-effort provider-model cooldown."""

    provider_model_api_name: str
    remaining_seconds: float
    last_failure_class: ProviderFailureClass


@dataclass(slots=True)
class _CooldownState:
    failures: list[tuple[float, ProviderFailureClass]]
    cooldown_until: float = 0.0


class ProviderCooldowns:
    """Keep the accepted three-failure cooldown in one process cache."""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._states: dict[str, _CooldownState] = {}
        self._lock = threading.Lock()

    def is_active(self, provider_model_api_name: str) -> bool:
        """Return true only before the exact known cooldown expiry."""
        now = self._clock()
        with self._lock:
            state = self._states.get(provider_model_api_name)
            return state is not None and now < state.cooldown_until

    def record_failure(
        self,
        provider_model_api_name: str,
        failure_class: ProviderFailureClass,
    ) -> bool:
        """Count one applicable failure and start exactly 60 seconds at three."""
        if failure_class not in _COOLDOWN_FAILURES:
            return False
        now = self._clock()
        with self._lock:
            state = self._states.setdefault(provider_model_api_name, _CooldownState([]))
            cutoff = now - _COOLDOWN_WINDOW_SECONDS
            state.failures = [item for item in state.failures if item[0] >= cutoff]
            state.failures.append((now, failure_class))
            if (
                len(state.failures) >= _COOLDOWN_FAILURE_COUNT
                and now >= state.cooldown_until
            ):
                state.cooldown_until = now + _COOLDOWN_DURATION_SECONDS
            return now < state.cooldown_until

    def snapshots(self) -> tuple[CooldownSnapshot, ...]:
        """Return each known current cooldown and its last failure class."""
        now = self._clock()
        with self._lock:
            return tuple(
                CooldownSnapshot(
                    name, state.cooldown_until - now, state.failures[-1][1]
                )
                for name, state in sorted(self._states.items())
                if state.failures and now < state.cooldown_until
            )


@dataclass(frozen=True, slots=True)
class CallLimits:
    """Bound attempts, complete connections, and local concurrency."""

    attempt_timeout_seconds: float = 60.0
    connection_timeout_seconds: float = 900.0
    concurrency: int = 100
    maximum_output_json_bytes: int = _MAXIMUM_OUTPUT_JSON_BYTES
    maximum_output_events: int = 100_000

    def __post_init__(self) -> None:
        if isinstance(self.attempt_timeout_seconds, bool) or not isinstance(
            self.attempt_timeout_seconds, int | float
        ):
            raise TypeError("The provider-attempt timeout is invalid.")
        if not 1 <= self.attempt_timeout_seconds <= 600:
            raise ValueError("The provider-attempt timeout is invalid.")
        if isinstance(self.connection_timeout_seconds, bool) or not isinstance(
            self.connection_timeout_seconds, int | float
        ):
            raise TypeError("The complete connection timeout is invalid.")
        if not 1 <= self.connection_timeout_seconds <= 900:
            raise ValueError("The complete connection timeout is invalid.")
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 100_000:
            raise ValueError("The call concurrency bound is invalid.")
        if (
            type(self.maximum_output_json_bytes) is not int
            or not 1 <= self.maximum_output_json_bytes <= _MAXIMUM_OUTPUT_JSON_BYTES
        ):
            raise ValueError("The output JSON byte bound is invalid.")
        if (
            type(self.maximum_output_events) is not int
            or not 1 <= self.maximum_output_events <= 100_000
        ):
            raise ValueError("The output event bound is invalid.")


@dataclass(frozen=True, slots=True)
class _AttemptResult:
    route: ProviderRoute
    outputs: tuple[ProviderOutput, ...]
    usage: tuple[UsageAmount, ...]
    visible: bool
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class _AttemptError(Exception):
    route: ProviderRoute
    failure_class: ProviderFailureClass
    usage: tuple[UsageAmount, ...]
    outputs: tuple[ProviderOutput, ...]
    visible: bool
    started_at: datetime
    completed_at: datetime
    phase: CallFailurePhase = CallFailurePhase.BEFORE_VISIBLE_OUTPUT


class _AttemptCancelled(BaseException):
    def __init__(self, failure: _AttemptError) -> None:
        self.failure = failure


class _AttemptValidationUnavailableError(Exception):
    def __init__(self, failure: _AttemptError) -> None:
        self.failure = failure


@dataclass(frozen=True, slots=True)
class _FrozenCandidate:
    route: ProviderRoute
    price: AttemptPriceSnapshot
    adapter: ProviderAdapter | None
    credential: str | None
    usage_units: frozenset[str]
    admission_failure: ProviderFailureClass | None = None


@dataclass(frozen=True, slots=True)
class _AdmittedCall:
    connection: psycopg.Connection[Any]
    workspace_id: uuid.UUID
    assignment_name: str | None
    candidates: tuple[_FrozenCandidate, ...]
    admission_error: CallExecutionError | None


class CallExecutor:
    """Execute one native call with ordered fallback and durable facts."""

    def __init__(
        self,
        *,
        database_url: str,
        adapters: Mapping[str, ProviderAdapter],
        cooldowns: ProviderCooldowns | None = None,
        credential_keys: ProviderCredentialKeys | None = None,
        object_store: ObjectStore | None = None,
        limits: CallLimits | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._database_url = database_url
        self._adapters = dict(adapters)
        if any(
            not adapter.usage_units or not adapter.usage_units <= _USAGE_UNITS
            for adapter in self._adapters.values()
        ):
            raise ValueError("Each provider adapter must declare valid usage units.")
        self._cooldowns = cooldowns or ProviderCooldowns()
        self._credential_keys = credential_keys
        self._object_store = object_store
        self._limits = limits or CallLimits()
        self._wall_clock = wall_clock or (lambda: datetime.now(tz=UTC))
        self._uuid_factory = uuid_factory
        self._active = 0
        self._active_lock = asyncio.Lock()

    @property
    def cooldowns(self) -> ProviderCooldowns:
        """Expose only the safe best-effort cooldown cache."""
        return self._cooldowns

    async def execute(
        self,
        actor: ServiceActor,
        request: CallRequest,
        *,
        write_visible_output: VisibleOutputWriter | None = None,
        start_visible_output: VisibleOutputStart | None = None,
    ) -> CallResult:
        """Run one connection-lifetime call for one authenticated service."""
        if not isinstance(actor, ServiceActor):
            raise CallExecutionError(
                "permission_denied",
                "A service API key must authorize the call.",
                phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
            )
        if request.streaming and write_visible_output is None:
            raise CallExecutionError(
                "invalid_request",
                "A streaming call requires a visible-output writer.",
                phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
                field="streaming",
                reason="A stream output writer is required.",
            )
        async with self._active_lock:
            if self._active >= self._limits.concurrency:
                raise CallExecutionError(
                    "rate_limited",
                    "The Router call concurrency limit is full.",
                    phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
                )
            self._active += 1
        try:
            return await self._execute(
                actor, request, write_visible_output, start_visible_output
            )
        finally:
            async with self._active_lock:
                self._active -= 1

    async def _execute(
        self,
        actor: ServiceActor,
        request: CallRequest,
        write_visible_output: VisibleOutputWriter | None,
        start_visible_output: VisibleOutputStart | None,
    ) -> CallResult:
        call_id = self._uuid_factory()
        call_started = self._now()
        deadline = (
            asyncio.get_running_loop().time() + self._limits.connection_timeout_seconds
        )
        attempt_writes: list[AttemptAccountingWrite] = []
        detailed_attempts: list[RequestAttempt] = []
        final_outputs: tuple[ProviderOutput, ...] = ()
        final_usage: tuple[UsageAmount, ...] = ()
        selected_route: ProviderRoute | None = None
        selected_price: AttemptPriceSnapshot | None = None
        succeeded = False
        failure: CallExecutionError | None = None
        cancelled: asyncio.CancelledError | None = None

        admission_task = asyncio.create_task(
            asyncio.to_thread(self._open_admitted, actor, request)
        )
        try:
            admitted = await asyncio.shield(admission_task)
        except asyncio.CancelledError:
            with suppress(Exception):
                admitted_after_cancel = await admission_task
                await asyncio.to_thread(
                    self._rollback_and_close, admitted_after_cancel.connection
                )
            raise
        except ApiError:
            raise
        except CallExecutionError:
            raise
        except Exception as error:
            raise CallExecutionError(
                "internal_error",
                "The Router database is not available.",
                phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
            ) from error
        connection = admitted.connection
        workspace_id = admitted.workspace_id
        assignment_name = admitted.assignment_name
        admission_error = admitted.admission_error
        try:
            seen: set[str] = set()
            for candidate in admitted.candidates:
                route = candidate.route
                name = route.provider_model_api_name
                if name in seen:
                    continue
                seen.add(name)
                if self._cooldowns.is_active(name):
                    continue
                if asyncio.get_running_loop().time() >= deadline:
                    failure = CallExecutionError(
                        "upstream_failed",
                        "The complete provider connection timed out.",
                        phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
                    )
                    break
                price = candidate.price
                selected_route = route
                try:
                    result = await self._attempt(
                        candidate,
                        request,
                        deadline,
                        write_visible_output,
                        start_visible_output,
                    )
                except _AttemptValidationUnavailableError as error:
                    attempt_failure = error.failure
                    accounting_write, detailed = _failed_attempt_values(
                        attempt_failure, price, self._uuid_factory()
                    )
                    attempt_writes.append(accounting_write)
                    detailed_attempts.append(detailed)
                    final_outputs = attempt_failure.outputs
                    failure = CallExecutionError(
                        "internal_error",
                        "The Router could not validate the provider output.",
                        phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
                    )
                    break
                except _AttemptCancelled as error:
                    attempt_failure = error.failure
                    accounting_write, detailed = _failed_attempt_values(
                        attempt_failure, price, self._uuid_factory()
                    )
                    attempt_writes.append(accounting_write)
                    detailed_attempts.append(detailed)
                    final_outputs = attempt_failure.outputs
                    cancelled = asyncio.CancelledError()
                    break
                except _AttemptError as attempt_failure:
                    accounting_write, detailed = _failed_attempt_values(
                        attempt_failure, price, self._uuid_factory()
                    )
                    attempt_writes.append(accounting_write)
                    detailed_attempts.append(detailed)
                    final_outputs = attempt_failure.outputs
                    self._cooldowns.record_failure(name, attempt_failure.failure_class)
                    if attempt_failure.visible:
                        failure = CallExecutionError(
                            "upstream_failed",
                            "The provider stream was interrupted.",
                            phase=CallFailurePhase.AFTER_VISIBLE_OUTPUT,
                        )
                        break
                    if attempt_failure.phase is CallFailurePhase.UNCERTAIN:
                        failure = CallExecutionError(
                            "upstream_failed",
                            "The provider result state is uncertain.",
                            phase=CallFailurePhase.UNCERTAIN,
                        )
                        break
                    continue
                accounting_write, detailed = _successful_attempt_values(
                    result, price, self._uuid_factory()
                )
                attempt_writes.append(accounting_write)
                detailed_attempts.append(detailed)
                final_outputs = result.outputs
                final_usage = result.usage
                selected_price = price
                succeeded = True
                break

            if cancelled is None and failure is None and not succeeded:
                if admission_error is not None:
                    failure = admission_error
                elif attempt_writes:
                    failure = CallExecutionError(
                        "upstream_failed",
                        "Each eligible provider-model failed.",
                        phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
                    )
                else:
                    failure = CallExecutionError(
                        "provider_unavailable",
                        "No eligible provider-model is available.",
                        phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
                    )

            call_completed = self._now()
            call_outcome = "succeeded" if succeeded else "failed"
            accounting_value = CallAccountingWrite(
                id=call_id,
                service_id=actor.service_id,
                workspace_id=workspace_id,
                assignment_api_name=assignment_name,
                tags=request.tags,
                outcome=call_outcome,
                started_at=call_started,
                completed_at=call_completed,
                attempts=tuple(attempt_writes),
            )
            accounting_task = asyncio.create_task(
                asyncio.to_thread(
                    self._record_accounting_and_close, connection, accounting_value
                )
            )
            try:
                await asyncio.shield(accounting_task)
            except asyncio.CancelledError:
                try:
                    await accounting_task
                except Exception as error:
                    raise CallExecutionError(
                        "internal_error",
                        "The Router could not record the call.",
                        phase=CallFailurePhase.UNCERTAIN,
                    ) from error
                cancelled = asyncio.CancelledError()
            except Exception as error:
                raise CallExecutionError(
                    "internal_error",
                    "The Router could not record the call.",
                    phase=CallFailurePhase.UNCERTAIN,
                ) from error
        except BaseException:
            await asyncio.to_thread(self._rollback_and_close, connection)
            raise

        response_json = (
            _response_json(final_outputs) if call_outcome == "succeeded" else None
        )
        await asyncio.to_thread(
            diagnostics.write_detailed_log_best_effort,
            self._database_url,
            self._object_store,
            diagnostics.DetailedLogWrite(
                service_id=actor.service_id,
                workspace_id=workspace_id,
                assignment_api_name=assignment_name,
                provider_model_api_name=(
                    selected_route.provider_model_api_name
                    if selected_route is not None
                    else None
                ),
                kind=request.kind,
                outcome=cast("Any", call_outcome),
                request_json=request.request_json,
                response_json=response_json,
                attempts=tuple(detailed_attempts),
                tags=request.tags,
                started_at=call_started,
                media=request.media + _captured_output_media(final_outputs),
                accounting_call_id=call_id,
            ),
        )
        if cancelled is not None:
            raise cancelled
        if failure is not None:
            raise failure
        if selected_route is None or selected_price is None:
            raise RuntimeError("A successful call has no selected provider-model.")
        return CallResult(
            call_id=call_id,
            provider_model_api_name=selected_route.provider_model_api_name,
            outputs=final_outputs,
            usage=final_usage,
            applied_price=selected_price,
            cost=_usage_cost(final_usage, selected_price),
        )

    def _open_admitted(
        self,
        actor: ServiceActor,
        request: CallRequest,
    ) -> _AdmittedCall:
        connection = psycopg.connect(
            self._database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            options=(
                f"-c statement_timeout={_DATABASE_STATEMENT_TIMEOUT_MILLISECONDS} "
                f"-c lock_timeout={_DATABASE_LOCK_TIMEOUT_MILLISECONDS}"
            ),
            row_factory=dict_row,
        )
        try:
            workspace = connection.execute(
                """SELECT workspace.id
                   FROM router.workspaces AS workspace
                   JOIN router.services AS service ON service.id = workspace.service_id
                   WHERE workspace.service_id = %s AND workspace.api_name = %s
                   FOR KEY SHARE OF workspace, service""",
                (actor.service_id, request.workspace_api_name),
            ).fetchone()
            if workspace is None:
                raise not_found("workspace")
            assignment_name: str | None = None
            routes: tuple[ProviderRoute, ...] = ()
            admission_error: CallExecutionError | None = None
            requirements = request.requirements
            if isinstance(request.selector, AssignmentSelector):
                assignment_name = request.selector.assignment_api_name
                try:
                    _resolved, routes = resolve_assignment_for_call(
                        connection,
                        service_id=actor.service_id,
                        workspace_api_name=request.workspace_api_name,
                        assignment_api_name=assignment_name,
                        required_inputs=requirements.required_inputs,
                        required_output=requirements.required_output,
                        required_capabilities=requirements.required_capabilities,
                        actor_subject=actor.activity_subject,
                        embedding_dimension=requirements.embedding_dimension,
                        input_image_sizes=requirements.input_image_sizes,
                        output_duration_seconds=requirements.output_duration_seconds,
                        excluded_provider_model_api_names=(
                            request.excluded_provider_model_api_names
                        ),
                        commit_evidence=False,
                    )
                except ApiError as error:
                    if error.code != "provider_unavailable":
                        raise
                    admission_error = _api_call_error(error)
            else:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (_CATALOG_WRITE_LOCK,)
                )
                try:
                    route = catalog.resolve_provider_route(
                        connection,
                        request.selector.provider_model_api_name,
                        required_inputs=requirements.required_inputs,
                        required_output=requirements.required_output,
                        required_capabilities=requirements.required_capabilities,
                        reasoning_level=None,
                    )
                    catalog.validate_route_constraints(
                        route,
                        embedding_dimension=requirements.embedding_dimension,
                        input_image_sizes=requirements.input_image_sizes,
                        output_duration_seconds=requirements.output_duration_seconds,
                    )
                    routes = (route,)
                except ApiError as error:
                    admission_error = _api_call_error(error)
            candidates = tuple(
                candidate
                for route in routes
                if (candidate := self._freeze_candidate(connection, route, request))
                is not None
            )
            # Commit assignment evidence and release the shared catalog lock only
            # after route, price, and credential values are immutable in memory.
            connection.commit()
            workspace = connection.execute(
                """SELECT id FROM router.workspaces
                   WHERE service_id = %s AND api_name = %s FOR KEY SHARE""",
                (actor.service_id, request.workspace_api_name),
            ).fetchone()
            if workspace is None:
                raise not_found("workspace")
            return _AdmittedCall(
                connection,
                cast("uuid.UUID", workspace["id"]),
                assignment_name,
                candidates,
                admission_error,
            )
        except BaseException:
            connection.rollback()
            connection.close()
            raise

    def _freeze_candidate(
        self,
        connection: psycopg.Connection[Any],
        route: ProviderRoute,
        request: CallRequest,
    ) -> _FrozenCandidate | None:
        price = accounting.effective_price_snapshot(
            connection, route.provider_model_api_name
        )
        if price is None:
            return None
        adapter = self._adapters.get(route.adapter)
        if adapter is None:
            return _FrozenCandidate(
                route, price, None, None, frozenset(), "unavailable"
            )
        usage_units = frozenset(adapter.usage_units_for(_provider_operation(request)))
        if (
            not usage_units
            or not usage_units <= adapter.usage_units
            or not usage_units <= _USAGE_UNITS
        ):
            raise ValueError("The provider adapter usage declaration is invalid.")
        if not usage_units <= {item.unit for item in price.unit_prices}:
            return None
        try:
            credential = self._credential(connection, route)
        except ApiError, RuntimeError, ValueError:
            return _FrozenCandidate(
                route, price, adapter, None, usage_units, "authentication"
            )
        return _FrozenCandidate(route, price, adapter, credential, usage_units)

    @staticmethod
    def _record_accounting_and_close(
        connection: psycopg.Connection[Any], value: CallAccountingWrite
    ) -> None:
        try:
            accounting.record_call_accounting(connection, value)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _rollback_and_close(connection: psycopg.Connection[Any]) -> None:
        if not connection.closed:
            connection.rollback()
            connection.close()

    async def _attempt(
        self,
        candidate: _FrozenCandidate,
        request: CallRequest,
        deadline: float,
        write_visible_output: VisibleOutputWriter | None,
        start_visible_output: VisibleOutputStart | None,
    ) -> _AttemptResult:
        route = candidate.route
        price = candidate.price
        started_at = self._now()
        outputs: list[ProviderOutput] = []
        tool_call_ids: set[str] = set()
        output_bytes = 0
        visible = False
        usage: tuple[UsageAmount, ...] | None = None
        if candidate.admission_failure is not None:
            raise _AttemptError(
                route,
                candidate.admission_failure,
                (),
                (),
                False,
                started_at,
                self._now(),
            )
        adapter = candidate.adapter
        if adapter is None:
            raise RuntimeError("An admitted candidate has no provider adapter.")
        attempt_request = _provider_attempt_request(
            route, request, candidate.credential
        )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise _AttemptError(
                route, "timeout", (), (), False, started_at, self._now()
            )
        timeout = min(self._limits.attempt_timeout_seconds, remaining)
        try:
            async with asyncio.timeout(timeout):
                async for event in adapter.attempt(attempt_request):
                    if usage is not None:
                        raise ValueError("The adapter emitted data after completion.")
                    if isinstance(event, ProviderCompleted):
                        usage = event.usage
                        continue
                    output_bytes += len(event.content_json.encode("utf-8"))
                    if output_bytes > self._limits.maximum_output_json_bytes or (
                        len(outputs) >= self._limits.maximum_output_events
                    ):
                        raise ValueError("The provider output exceeds its bound.")
                    # Retain every bounded provider event even when semantic
                    # validation rejects it and normal fallback continues.
                    try:
                        await _validate_output(request, event, outputs, tool_call_ids)
                    finally:
                        outputs.append(event)
                    if request.streaming and event.kind in {"text_delta", "tool_call"}:
                        first_visible = not visible
                        visible = True
                        if write_visible_output is not None:
                            try:
                                if first_visible and start_visible_output is not None:
                                    await start_visible_output(
                                        route.provider_model_api_name
                                    )
                                await write_visible_output(event)
                            except asyncio.CancelledError:
                                raise
                            except Exception as error:
                                raise ProviderFailureError("interrupted") from error
        except asyncio.CancelledError:
            raise _AttemptCancelled(
                _AttemptError(
                    route,
                    "interrupted",
                    usage or (),
                    tuple(outputs),
                    visible,
                    started_at,
                    self._now(),
                )
            ) from None
        except OutputValidationUnavailableError:
            raise _AttemptValidationUnavailableError(
                _AttemptError(
                    route,
                    "invalid_response",
                    usage or (),
                    tuple(outputs),
                    visible,
                    started_at,
                    self._now(),
                )
            ) from None
        except TimeoutError:
            failure = ProviderFailureError(
                "timeout",
                usage=usage or (),
                phase=_implicit_failure_phase(request),
            )
        except ProviderFailureError as error:
            failure = (
                ProviderFailureError(
                    "invalid_response",
                    usage=usage,
                    phase=(
                        CallFailurePhase.UNCERTAIN
                        if error.phase is CallFailurePhase.UNCERTAIN
                        else _implicit_failure_phase(request)
                    ),
                )
                if usage is not None
                else error
            )
        except ValueError:
            failure = ProviderFailureError(
                "invalid_response",
                usage=usage or (),
                phase=_implicit_failure_phase(request),
            )
        except Exception:
            failure = ProviderFailureError(
                "transport",
                usage=usage or (),
                phase=_implicit_failure_phase(request),
            )
        else:
            if usage is None:
                failure = ProviderFailureError(
                    "invalid_response", phase=_implicit_failure_phase(request)
                )
            else:
                try:
                    _validate_completion(
                        request,
                        outputs,
                        usage,
                        price,
                        candidate.usage_units,
                    )
                except ValueError:
                    failure = ProviderFailureError(
                        "invalid_response",
                        usage=usage,
                        phase=_implicit_failure_phase(request),
                    )
                else:
                    return _AttemptResult(
                        route,
                        tuple(outputs),
                        usage,
                        visible,
                        started_at,
                        self._now(),
                    )
        if not _usage_is_declared(failure.usage, candidate.usage_units):
            self._report_usage_declaration_breach(
                route, failure.usage, candidate.usage_units
            )
            raise CallExecutionError(
                "internal_error",
                "The Router could not determine the complete provider cost.",
                phase=CallFailurePhase.UNCERTAIN,
            )
        if not _usage_is_priced(failure.usage, price):
            self._report_usage_declaration_breach(
                route, failure.usage, candidate.usage_units
            )
            raise CallExecutionError(
                "internal_error",
                "The Router could not determine the complete provider cost.",
                phase=CallFailurePhase.UNCERTAIN,
            )
        return self._raise_attempt_failure(route, failure, outputs, visible, started_at)

    @staticmethod
    def _report_usage_declaration_breach(
        route: ProviderRoute,
        usage: Sequence[UsageAmount],
        declared_usage_units: frozenset[str],
    ) -> None:
        # Accounting rejects an unpriced unit. Store no false complete cost,
        # and make this internal adapter invariant breach visible to operators.
        _LOGGER.error(
            "Provider adapter usage declaration breach for provider-model %s; "
            "reported units=%s; declared units=%s",
            route.provider_model_api_name,
            sorted({item.unit for item in usage}),
            sorted(declared_usage_units),
        )

    def _raise_attempt_failure(
        self,
        route: ProviderRoute,
        failure: ProviderFailureError,
        outputs: Sequence[ProviderOutput],
        visible: bool,
        started_at: datetime,
    ) -> _AttemptResult:
        raise _AttemptError(
            route,
            failure.failure_class,
            failure.usage,
            tuple(outputs),
            visible,
            started_at,
            self._now(),
            failure.phase,
        )

    def _credential(
        self, connection: psycopg.Connection[Any], route: ProviderRoute
    ) -> str | None:
        if route.credential_api_name is None:
            return None
        if self._credential_keys is None:
            raise RuntimeError("Provider credential controls are unavailable.")
        return catalog.resolve_credential(
            connection, route.credential_api_name, self._credential_keys
        )

    def _now(self) -> datetime:
        value = self._wall_clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("The call clock must return an aware time.")
        return value


async def _validate_output(
    request: CallRequest,
    event: ProviderOutput,
    current: Sequence[ProviderOutput],
    tool_call_ids: set[str],
) -> None:
    value = _load_json(event.content_json)
    if event.kind in {"text_delta", "tool_call"}:
        if not request.streaming:
            raise ValueError("Visible stream output requires a streaming call.")
        if event.kind == "text_delta" and (
            not isinstance(value, str) or not value or not _valid_utf8_text(value)
        ):
            raise ValueError("A text delta must contain non-empty text.")
        if event.kind == "tool_call":
            if "tool_calling" not in request.requirements.required_capabilities:
                raise ValueError("The call did not require tool output.")
            if not _valid_tool_call(value):
                raise ValueError("A tool call is invalid.")
            tool_call_id = cast("str", cast("dict[str, object]", value)["id"])
            if tool_call_id in tool_call_ids:
                raise ValueError("A tool call identity cannot occur more than once.")
            tool_call_ids.add(tool_call_id)
        return
    if request.streaming:
        raise ValueError("A streaming attempt emitted a buffered result.")
    expected_kind = {
        "text": "standard",
        "structured_json": "structured_json",
        "embedding": "embedding",
        "image": "media",
        "video": "media",
        "audio": "media",
    }[request.requirements.required_output]
    if event.kind != expected_kind or current:
        raise ValueError("The provider result shape is invalid.")
    if event.kind == "standard" and not _valid_standard_content(request, value):
        raise ValueError("A standard model result must be one content list.")
    if event.kind == "structured_json":
        validator = request.output_validator
        valid = False
        if validator is not None:
            validation = validator(value)
            valid = await validation if inspect.isawaitable(validation) else validation
        if (
            len(event.content_json.encode("utf-8")) > 1_000_000
            or validator is None
            or not valid
        ):
            raise ValueError("The structured provider result is invalid.")
    if event.kind == "embedding":
        _validate_embeddings(request, value)
    if event.kind == "media" and not _valid_media_result(request, value):
        raise ValueError("A media provider result must be one object.")


def _validate_completion(
    request: CallRequest,
    outputs: Sequence[ProviderOutput],
    usage: Sequence[UsageAmount],
    price: AttemptPriceSnapshot,
    declared_usage_units: frozenset[str],
) -> None:
    if not request.streaming and len(outputs) != 1:
        raise ValueError("A non-streaming provider result is incomplete.")
    if not _usage_is_declared(usage, declared_usage_units):
        raise ValueError("Provider usage contains an undeclared unit.")
    if not _usage_is_priced(usage, price):
        raise ValueError("Provider usage has no applied price.")


def _validate_embeddings(request: CallRequest, value: object) -> None:
    if not isinstance(value, list) or len(value) != request.expected_embedding_count:
        raise ValueError("The embedding count is invalid.")
    dimension = request.requirements.embedding_dimension
    for vector in value:
        if (
            not isinstance(vector, list)
            or len(vector) != dimension
            or any(not _finite_number(item) for item in vector)
        ):
            raise ValueError("An embedding vector is invalid.")


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_tool_call(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "type",
        "id",
        "name",
        "arguments_json",
    }:
        return False
    if value["type"] != "tool_call":
        return False
    if not all(
        isinstance(value[name], str)
        and 1 <= len(value[name]) <= maximum
        and _valid_utf8_text(cast("str", value[name]))
        for name, maximum in (("id", 200), ("name", 200), ("arguments_json", 1_000_000))
    ):
        return False
    try:
        arguments = _load_json(value["arguments_json"])
    except ValueError, RecursionError:
        return False
    return isinstance(arguments, dict)


def _valid_standard_content(request: CallRequest, value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    tool_call_ids: list[str] = []
    for part in value:
        if not isinstance(part, dict):
            return False
        if set(part) == {"type", "text"}:
            if (
                part["type"] != "text"
                or not isinstance(part["text"], str)
                or not _valid_utf8_text(part["text"])
            ):
                return False
            continue
        if not _valid_tool_call(part):
            return False
        if "tool_calling" not in request.requirements.required_capabilities:
            return False
        tool_call_ids.append(cast("str", part["id"]))
    return len(tool_call_ids) == len(set(tool_call_ids))


def _valid_utf8_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_media_result(request: CallRequest, value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"media_type", "size_bytes"}:
        return False
    output = request.requirements.required_output
    return (
        isinstance(value["media_type"], str)
        and value["media_type"] in _MEDIA_OUTPUT_TYPES
        and value["media_type"].startswith(f"{output}/")
        and type(value["size_bytes"]) is int
        and 1 <= value["size_bytes"] <= 1024 * 1024 * 1024
    )


def _provider_attempt_request(
    route: ProviderRoute,
    request: CallRequest,
    credential: str | None,
) -> ProviderAttemptRequest:
    return ProviderAttemptRequest(
        route=route,
        request_json=request.request_json,
        credential=credential,
        kind=request.kind,
        requirements=request.requirements,
        streaming=request.streaming,
        expected_embedding_count=request.expected_embedding_count,
        input_media=request.media,
    )


def _provider_operation(request: CallRequest) -> ProviderOperation:
    return ProviderOperation(
        request.kind,
        request.requirements,
        request.streaming,
        request.expected_embedding_count,
    )


def _usage_is_priced(usage: Sequence[UsageAmount], price: AttemptPriceSnapshot) -> bool:
    rates = {item.unit for item in price.unit_prices}
    return {item.unit for item in usage} <= rates


def _implicit_failure_phase(request: CallRequest) -> CallFailurePhase:
    """Prevent replacement work when a media side effect can be uncertain."""
    if request.kind == "media":
        return CallFailurePhase.UNCERTAIN
    return CallFailurePhase.BEFORE_VISIBLE_OUTPUT


def _usage_is_declared(
    usage: Sequence[UsageAmount], declared_usage_units: frozenset[str]
) -> bool:
    return {item.unit for item in usage} <= declared_usage_units


def _usage_cost(usage: Sequence[UsageAmount], price: AttemptPriceSnapshot) -> Decimal:
    rates = {item.unit: item.amount for item in price.unit_prices}
    with localcontext() as context:
        context.prec = _COST_DECIMAL_PRECISION
        return sum((item.quantity * rates[item.unit] for item in usage), Decimal(0))


def _api_call_error(error: ApiError) -> CallExecutionError:
    return CallExecutionError(
        error.code,
        error.message,
        phase=CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
        field=error.field,
        reason=error.reason,
    )


def _successful_attempt_values(
    result: _AttemptResult,
    price: AttemptPriceSnapshot,
    attempt_id: uuid.UUID,
) -> tuple[AttemptAccountingWrite, RequestAttempt]:
    accounting_value = AttemptAccountingWrite(
        id=attempt_id,
        provider_connection_api_name=result.route.provider_connection_api_name,
        provider_model_api_name=result.route.provider_model_api_name,
        outcome="succeeded",
        usage=result.usage,
        applied_price=price,
        started_at=result.started_at,
        completed_at=result.completed_at,
    )
    return accounting_value, _request_attempt(accounting_value, result.outputs)


def _failed_attempt_values(
    failure: _AttemptError,
    price: AttemptPriceSnapshot,
    attempt_id: uuid.UUID,
) -> tuple[AttemptAccountingWrite, RequestAttempt]:
    accounting_value = AttemptAccountingWrite(
        id=attempt_id,
        provider_connection_api_name=failure.route.provider_connection_api_name,
        provider_model_api_name=failure.route.provider_model_api_name,
        outcome="failed",
        usage=failure.usage,
        applied_price=price,
        failure_class=failure.failure_class,
        started_at=failure.started_at,
        completed_at=failure.completed_at,
    )
    return accounting_value, _request_attempt(accounting_value, failure.outputs)


def _request_attempt(
    value: AttemptAccountingWrite, outputs: Sequence[ProviderOutput]
) -> RequestAttempt:
    cost = _usage_cost(value.usage, value.applied_price)
    document: dict[str, Any] = {
        "provider_model_api_name": value.provider_model_api_name,
        "outcome": value.outcome,
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "usage": {
            "units": [
                {"unit": item.unit, "quantity": _decimal_text(item.quantity)}
                for item in value.usage
            ],
            "cost": _decimal_text(cost),
            "currency": value.applied_price.currency,
        },
        "applied_prices": {
            "currency": value.applied_price.currency,
            "unit_prices": [
                {"unit": item.unit, "amount": _decimal_text(item.amount)}
                for item in value.applied_price.unit_prices
            ],
        },
    }
    if value.applied_price.source is not None:
        document["applied_prices"]["source"] = value.applied_price.source
    if value.applied_price.synchronized_at is not None:
        document["applied_prices"]["synchronized_at"] = (
            value.applied_price.synchronized_at
        )
    if value.failure_class is not None:
        document["error"] = {
            "code": "rate_limited"
            if value.failure_class == "rate_limited"
            else "upstream_failed",
            "message": "The provider attempt failed.",
        }
    if outputs:
        document["response_json"] = _response_json(outputs)
    return RequestAttempt.model_validate(document)


def _response_json(outputs: Sequence[ProviderOutput]) -> str:
    # Keep each validated provider JSON value byte-for-byte inside the safe
    # provider-neutral envelope. Detailed logs must not rewrite model content.
    return (
        "["
        + ",".join(
            '{"kind":'
            + json.dumps(output.kind)
            + ',"value":'
            + output.content_json
            + "}"
            for output in outputs
        )
        + "]"
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _load_json(value: str) -> object:
    def reject_constant(_value: str) -> object:
        raise ValueError("A JSON number must be finite.")

    result = json.loads(value, parse_constant=reject_constant)
    if not _json_numbers_are_finite(result):
        raise ValueError("A JSON number must be finite.")
    return result


def _captured_output_media(
    outputs: Sequence[ProviderOutput],
) -> tuple[diagnostics.CapturedMedia, ...]:
    captured: list[diagnostics.CapturedMedia] = []
    for output in outputs:
        if output.kind != "media" or output.media_body is None:
            continue
        value = _load_json(output.content_json)
        if (
            isinstance(value, dict)
            and isinstance(value.get("media_type"), str)
            and value["media_type"] in _MEDIA_OUTPUT_TYPES
        ):
            captured.append(
                diagnostics.CapturedMedia(
                    output.media_body,
                    cast("str", value["media_type"]),
                    "output",
                )
            )
    return tuple(captured)


def _json_numbers_are_finite(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_numbers_are_finite(item) for item in value)
    if isinstance(value, dict):
        return all(_json_numbers_are_finite(item) for item in value.values())
    return True
