"""Authenticate and coordinate native model-request operations."""
# ruff: noqa: C901, EM101, PLR0911, PLR0913, PLR2004, TC003, TRY003

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, cast

from pydantic import ValidationError

from llmrouter_backend.adapters import (
    MessageRole,
    ModelAdapterRequest,
    ModelMessage,
    ModelOperation,
    ModelOutputEvent,
    ModelOutputEventKind,
)
from llmrouter_backend.admission import (
    AdmissionError,
    AdmissionErrorCode,
    AdmissionRequest,
    AdmissionResult,
    FingerprintInput,
    RequestKind,
    validate_uuidv7,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityPath,
    OperationPolicy,
    PrincipalKind,
    RequestContext,
    SafeAuthorityError,
    Scope,
    ScopeKind,
    ScopeMismatchMode,
    ServicePrincipal,
    authorize,
)
from llmrouter_backend.execution import (
    AdapterStop,
    CancellationResult,
    ErrorScope,
    ExecutionError,
    ExecutionErrorCode,
    ExecutionKind,
    ExecutionState,
    ExecutionStatus,
    ExecutionTarget,
    StreamEvent,
    TerminalError,
    TerminalErrorClass,
)
from llmrouter_backend.machine_identity import MachineIdentityError
from llmrouter_backend.routing import AdapterResult, AttemptOutcome, AttemptPlan
from llmrouter_backend.routing.errors import RoutingError, RoutingErrorCode

from .model import (
    CancelDocument,
    CompatibleChatRequest,
    CreateModelRequestResult,
    FieldError,
    ModelRequestDocument,
    ModelRequestError,
    PreparedModelRequest,
    ResumePoint,
    ScopedRequest,
    TextPart,
)

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)


class MachineAuthenticator(Protocol):
    """Resolve one opaque service access token."""

    def authenticate(
        self, token: str, *, request_id: str, now: datetime
    ) -> ServicePrincipal:
        """Return one validated service principal."""
        ...


class AdmissionStore(Protocol):
    """Create or replay one durable admission."""

    def admit(
        self, context: RequestContext, request: AdmissionRequest, *, now: datetime
    ) -> AdmissionResult:
        """Bind one scoped identity before provider work."""
        ...


class ExecutionStore(Protocol):
    """Persist lifecycle, output events, status, and cancellation."""

    def status(
        self, context: RequestContext, target: ExecutionTarget
    ) -> ExecutionStatus:
        """Read one exact scoped execution."""
        ...

    def transition(
        self,
        context: RequestContext,
        target: ExecutionTarget,
        *,
        expected_revision: int,
        new_state: ExecutionState,
        safe_error: TerminalError | None = None,
        owner_epoch: int | None = None,
        tool_call_id: str | None = None,
        tool_expires_at: datetime | None = None,
    ) -> ExecutionStatus:
        """Commit one legal state transition."""
        ...

    def append_event(
        self,
        context: RequestContext,
        target: ExecutionTarget,
        *,
        event_name: str,
        payload: dict[str, object],
        expected_sequence: int | None = None,
        owner_epoch: int | None = None,
    ) -> StreamEvent:
        """Append one provider-neutral output event."""
        ...

    def replay(
        self,
        context: RequestContext,
        target: ExecutionTarget,
        *,
        after_sequence: int,
    ) -> tuple[StreamEvent, ...]:
        """Read retained events after one exact cursor."""
        ...

    def cancel(
        self,
        context: RequestContext,
        target: ExecutionTarget,
        *,
        reason: str,
        active_stops: Sequence[AdapterStop] = (),
    ) -> CancellationResult:
        """Record cancellation before adapter stop calls."""
        ...

    def stream_disconnected(
        self, context: RequestContext, target: ExecutionTarget
    ) -> ExecutionStatus:
        """Observe a disconnect without cancelling work."""
        ...


class RoutingExecutor(Protocol):
    """Run the durable ordered provider fallback chain."""

    def execute(
        self, context: RequestContext, *, request_id: str, owner_id: str
    ) -> AdapterResult:
        """Return one terminal durable routing result."""
        ...


class RequestViews(Protocol):
    """Resolve hidden scopes and build the bounded public status."""

    def resolve_scope(
        self, principal: ServicePrincipal, request_id: str
    ) -> Scope | None:
        """Find at most one request inside the principal's allowed scope."""
        ...

    def status(
        self, context: RequestContext, target: ExecutionTarget
    ) -> dict[str, object]:
        """Return one bounded, content-safe status document."""
        ...

    def resume_point(
        self, context: RequestContext, target: ExecutionTarget
    ) -> ResumePoint:
        """Return lifecycle state for equal-replay recovery in the exact scope."""
        ...


class ActiveStopSource(Protocol):
    """Return stop operations for only the current scoped request."""

    def __call__(
        self, context: RequestContext, target: ExecutionTarget
    ) -> Sequence[AdapterStop]:
        """Return zero or more active adapter stop operations."""
        ...


class WorkSubmitter(Protocol):
    """Start one admitted request without a response-delivery dependency."""

    def __call__(self, work: Callable[[], None]) -> None:
        """Accept one no-argument unit of work or raise before acceptance."""
        ...


class ThreadWorkSubmitter:
    """Run bounded request work on a private local thread pool."""

    def __init__(self, *, maximum_workers: int = 4) -> None:
        """Create a bounded pool for one backend process."""
        if not 1 <= maximum_workers <= 64:
            raise ValueError("The worker count must be from 1 through 64.")
        self._executor = ThreadPoolExecutor(
            max_workers=maximum_workers,
            thread_name_prefix="llmrouter-model",
        )

    def __call__(self, work: Callable[[], None]) -> None:
        """Submit work without retaining request content in the future repr."""
        self._executor.submit(work)

    def close(self) -> None:
        """Stop accepting work and wait for current requests."""
        self._executor.shutdown(wait=True, cancel_futures=False)


class TransientModelInputRegistry:
    """Keep admitted input only while one local execution can use it."""

    def __init__(self, execution: ExecutionStore) -> None:
        """Use the durable execution store for every visible output."""
        self._execution = execution
        self._lock = threading.RLock()
        self._requests: dict[tuple[str, str | None, str], PreparedModelRequest] = {}

    def claim(self, request_id: str, prepared: PreparedModelRequest) -> bool:
        """Claim one local dispatch without replacing active request content."""
        key = _registry_key(prepared.scope, request_id)
        with self._lock:
            if key in self._requests:
                return False
            self._requests[key] = prepared
            return True

    def release(self, request_id: str, scope: Scope) -> None:
        """Remove request content after provider work stops."""
        with self._lock:
            self._requests.pop(_registry_key(scope, request_id), None)

    def request_for_plan(self, plan: AttemptPlan) -> ModelAdapterRequest:
        """Return the exact scoped admitted adapter request."""
        key = (plan.service_id, plan.workspace_id, plan.request_id)
        with self._lock:
            prepared = self._requests.get(key)
        if prepared is None:
            raise RuntimeError("The local admitted request input is unavailable.")
        return prepared.adapter_request

    def output_for_plan(self, plan: AttemptPlan, event: ModelOutputEvent) -> None:
        """Commit bounded provider output through the native event journal."""
        key = (plan.service_id, plan.workspace_id, plan.request_id)
        with self._lock:
            prepared = self._requests.get(key)
        if prepared is None:
            raise RuntimeError(
                "The local admitted request output scope is unavailable."
            )
        target = ExecutionTarget(ExecutionKind.MODEL, plan.request_id)
        if event.kind is ModelOutputEventKind.DELTA:
            self._execution.append_event(
                prepared.context,
                target,
                event_name="output.delta",
                payload={
                    "output_index": 0,
                    "content_type": "text/plain",
                    "delta": cast("str", event.text),
                },
            )
            return
        self._execution.append_event(
            prepared.context,
            target,
            event_name="output.completed",
            payload={"output_index": 0, "content_type": "text/plain"},
        )


class ModelRequestService:
    """Coordinate one native model request through existing security boundaries."""

    def __init__(
        self,
        *,
        authenticator: MachineAuthenticator,
        admission: AdmissionStore,
        execution: ExecutionStore,
        routing: RoutingExecutor,
        views: RequestViews,
        submit: WorkSubmitter,
        owner_id: str,
        clock: Callable[[], datetime] | None = None,
        active_stops: ActiveStopSource | None = None,
        inputs: TransientModelInputRegistry | None = None,
    ) -> None:
        """Use explicit dependencies without another authentication path."""
        if not owner_id or len(owner_id) > 500:
            raise ValueError("The routing owner identity is invalid.")
        self._authenticator = authenticator
        self._admission = admission
        self._execution = execution
        self._routing = routing
        self._views = views
        self._submit = submit
        self._owner_id = owner_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active_stops = active_stops or (lambda _context, _target: ())
        self.inputs = inputs or TransientModelInputRegistry(execution)

    def create(
        self,
        token: str,
        request_id: str,
        raw_body: bytes,
        *,
        error_request_id: str,
    ) -> CreateModelRequestResult:
        """Authenticate, validate, bind, and schedule one new request exactly once."""
        now = self._clock()
        principal = self._authenticate(token, error_request_id, now=now)
        try:
            validate_uuidv7(request_id)
        except ValueError as error:
            raise _invalid_request(
                error_request_id, ("X-LLMRouter-Request-ID",)
            ) from error
        document = _parse_model_document(raw_body, error_request_id)
        return self._create_document(
            principal,
            request_id,
            document,
            now=now,
            error_request_id=error_request_id,
            adapter_operation=ModelOperation.STREAM,
            fingerprint_operation="model.create",
        )

    def create_compatible_chat(
        self,
        token: str,
        request_id: str,
        raw_body: bytes,
        *,
        error_request_id: str,
    ) -> tuple[CreateModelRequestResult, bool]:
        """Map one accepted compatible chat request before normal admission."""
        now = self._clock()
        principal = self._authenticate(token, error_request_id, now=now)
        try:
            validate_uuidv7(request_id)
        except ValueError as error:
            raise _invalid_request(
                error_request_id, ("X-LLMRouter-Request-ID",)
            ) from error
        compatible = _parse_compatible_chat(raw_body, error_request_id)
        document = _compatible_native_document(compatible, error_request_id)
        compatible_fingerprint = _compatible_fingerprint(compatible)
        result = self._create_document(
            principal,
            request_id,
            document,
            now=now,
            error_request_id=error_request_id,
            adapter_operation=(
                ModelOperation.STREAM
                if compatible.stream
                else ModelOperation.COMPLETE
            ),
            fingerprint_operation="openai.chat.completions.create",
            fingerprint_execution=compatible_fingerprint,
        )
        return result, compatible.stream

    def _create_document(
        self,
        principal: ServicePrincipal,
        request_id: str,
        document: ModelRequestDocument,
        *,
        now: datetime,
        error_request_id: str,
        adapter_operation: ModelOperation,
        fingerprint_operation: str,
        fingerprint_execution: dict[str, JsonValue] | None = None,
    ) -> CreateModelRequestResult:
        """Admit one validated document through the shared native boundaries."""
        scope = Scope(principal.service_id, document.workspace_id)
        context = _authorize(
            principal, "model.create", scope, error_request_id, now, mutation=True
        )
        _require_mvp_capabilities(document, error_request_id)
        adapter_request = _validated_adapter_request(
            document, error_request_id, operation=adapter_operation
        )
        execution_fingerprint = (
            _fingerprint_execution(document)
            if fingerprint_execution is None
            else fingerprint_execution
        )
        fingerprint = FingerprintInput(
            operation=fingerprint_operation,
            contract_major=1,
            service_id=principal.service_id,
            workspace_id=document.workspace_id,
            data_profile=document.data_profile,
            execution=execution_fingerprint,
            resolved_exact_route_scope=(
                None
                if document.exact_route is None
                else {
                    "service_id": principal.service_id,
                    "workspace_id": document.workspace_id,
                    "exact_route_id": document.exact_route,
                }
            ),
        )
        try:
            admission_request = AdmissionRequest(
                request_id,
                RequestKind.MODEL,
                fingerprint,
                assignment=document.assignment,
                exact_route_id=document.exact_route,
                diagnostic_grant=(
                    None
                    if document.exact_route_grant is None
                    else document.exact_route_grant.get_secret_value()
                ),
            )
        except (TypeError, ValueError) as error:
            raise _invalid_request(error_request_id, ("exact_route_grant",)) from error
        try:
            result = self._admission.admit(context, admission_request, now=now)
        except Exception as error:
            raise _map_error(error, error_request_id) from error
        candidate = PreparedModelRequest(
            context,
            scope,
            adapter_request,
        )
        self._schedule_if_resumable(request_id, candidate, error_request_id)
        return CreateModelRequestResult(
            201 if result.created else 200,
            _receipt_document(result),
        )

    def authorize_existing(
        self,
        token: str,
        request_id: str,
        operation: str,
        *,
        error_request_id: str,
    ) -> ScopedRequest:
        """Check the operation before a scope-filtered record lookup."""
        now = self._clock()
        principal = self._authenticate(token, error_request_id, now=now)
        _authorize(
            principal,
            operation,
            Scope(principal.service_id),
            error_request_id,
            now,
            mutation=operation == "model.cancel",
            scope_kind=ScopeKind.SERVICE,
        )
        try:
            validate_uuidv7(request_id)
        except ValueError as error:
            raise _not_found(error_request_id) from error
        try:
            scope = self._views.resolve_scope(principal, request_id)
        except Exception as error:
            raise _map_error(error, error_request_id) from error
        if scope is None:
            raise _not_found(error_request_id)
        context = _authorize(
            principal,
            operation,
            scope,
            error_request_id,
            now,
            mutation=operation == "model.cancel",
        )
        return ScopedRequest(context, scope)

    def status(
        self, scoped: ScopedRequest, request_id: str, *, error_request_id: str
    ) -> dict[str, object]:
        """Return one bounded public status in the resolved scope."""
        target = ExecutionTarget(ExecutionKind.MODEL, request_id)
        try:
            return self._views.status(scoped.context, target)
        except Exception as error:
            raise _map_error(error, error_request_id) from error

    def events(
        self,
        scoped: ScopedRequest,
        request_id: str,
        *,
        after_sequence: int,
        error_request_id: str,
    ) -> tuple[StreamEvent, ...]:
        """Read one exact retained replay page."""
        target = ExecutionTarget(ExecutionKind.MODEL, request_id)
        try:
            return self._execution.replay(
                scoped.context, target, after_sequence=after_sequence
            )
        except Exception as error:
            raise _map_error(error, error_request_id) from error

    def execution_status(
        self, scoped: ScopedRequest, request_id: str, *, error_request_id: str
    ) -> ExecutionStatus:
        """Read lifecycle state for an open stream."""
        target = ExecutionTarget(ExecutionKind.MODEL, request_id)
        try:
            return self._execution.status(scoped.context, target)
        except Exception as error:
            raise _map_error(error, error_request_id) from error

    def disconnect(
        self, scoped: ScopedRequest, request_id: str, *, error_request_id: str
    ) -> None:
        """Record no cancellation when an SSE client disconnects."""
        target = ExecutionTarget(ExecutionKind.MODEL, request_id)
        try:
            self._execution.stream_disconnected(scoped.context, target)
        except Exception as error:
            raise _map_error(error, error_request_id) from error

    def cancel(
        self,
        scoped: ScopedRequest,
        request_id: str,
        raw_body: bytes,
        *,
        error_request_id: str,
    ) -> dict[str, object]:
        """Validate a reason and use only existing adapter stop evidence."""
        document = _parse_cancel_document(raw_body, error_request_id)
        target = ExecutionTarget(ExecutionKind.MODEL, request_id)
        try:
            current = self._views.status(scoped.context, target)
            stops: Sequence[AdapterStop] = ()
            if current.get("state") not in {
                "succeeded",
                "failed",
                "interrupted",
                "cancelled",
                "uncertain",
            }:
                try:
                    stops = tuple(self._active_stops(scoped.context, target))
                except Exception:  # noqa: BLE001 -- Durable cancellation stays primary.
                    stops = ()
            self._execution.cancel(
                scoped.context,
                target,
                reason=document.reason,
                active_stops=stops,
            )
            return self._views.status(scoped.context, target)
        except Exception as error:
            raise _map_error(error, error_request_id) from error

    def _authenticate(
        self, token: str, request_id: str, *, now: datetime
    ) -> ServicePrincipal:
        try:
            return self._authenticator.authenticate(
                token, request_id=request_id, now=now
            )
        except Exception as error:
            mapped = _map_error(error, request_id)
            if mapped.code != "invalid_token":
                mapped = ModelRequestError(
                    "invalid_token", 401, "Authentication failed.", request_id
                )
            raise mapped from error

    def _schedule_if_resumable(
        self,
        request_id: str,
        prepared: PreparedModelRequest,
        error_request_id: str,
    ) -> bool:
        target = ExecutionTarget(ExecutionKind.MODEL, request_id)
        try:
            point = self._views.resume_point(prepared.context, target)
        except Exception as error:
            raise _map_error(error, error_request_id) from error
        if point.state not in {ExecutionState.ADMITTED, ExecutionState.RUNNING}:
            return False
        if not self.inputs.claim(request_id, prepared):
            return False
        try:
            point = self._views.resume_point(prepared.context, target)
        except Exception as error:
            self.inputs.release(request_id, prepared.scope)
            raise _map_error(error, error_request_id) from error
        if point.state not in {ExecutionState.ADMITTED, ExecutionState.RUNNING}:
            self.inputs.release(request_id, prepared.scope)
            return False

        def work() -> None:
            self._execute(request_id, prepared)

        try:
            self._submit(work)
        except Exception as error:
            self.inputs.release(request_id, prepared.scope)
            raise ModelRequestError(
                "temporarily_unavailable",
                503,
                "The request was admitted. Read its status before a retry.",
                error_request_id,
                retryable=True,
            ) from error
        return True

    def _execute(self, request_id: str, prepared: PreparedModelRequest) -> None:
        target = ExecutionTarget(ExecutionKind.MODEL, request_id)
        try:
            point = self._views.resume_point(prepared.context, target)
            if point.state is ExecutionState.ADMITTED:
                try:
                    self._execution.transition(
                        prepared.context,
                        target,
                        expected_revision=point.state_revision,
                        new_state=ExecutionState.RUNNING,
                    )
                except ExecutionError as error:
                    if error.code not in {
                        ExecutionErrorCode.REVISION_CONFLICT,
                        ExecutionErrorCode.INVALID_TRANSITION,
                    }:
                        raise
                point = self._views.resume_point(prepared.context, target)
            if point.state is not ExecutionState.RUNNING:
                return
            result = self._routing.execute(
                prepared.context,
                request_id=request_id,
                owner_id=self._owner_id,
            )
            point = self._views.resume_point(prepared.context, target)
            if point.state is ExecutionState.RUNNING:
                self._execution.transition(
                    prepared.context,
                    target,
                    expected_revision=point.state_revision,
                    new_state=_terminal_state(result),
                    safe_error=None if result.failure is None else result.failure.error,
                )
        except RoutingError as error:
            if error.code in {
                RoutingErrorCode.BUSY,
                RoutingErrorCode.CLAIM_CONFLICT,
            }:
                return
            self._fail_request(request_id, prepared, "request_execution_failed")
        except Exception:  # noqa: BLE001 -- The stored error stays content-free.
            self._fail_request(request_id, prepared, "request_execution_failed")
        finally:
            self.inputs.release(request_id, prepared.scope)

    def _fail_request(
        self, request_id: str, prepared: PreparedModelRequest, detail: str
    ) -> None:
        del detail
        target = ExecutionTarget(ExecutionKind.MODEL, request_id)
        try:
            point = self._views.resume_point(prepared.context, target)
            if point.state not in {ExecutionState.ADMITTED, ExecutionState.RUNNING}:
                return
            self._execution.transition(
                prepared.context,
                target,
                expected_revision=point.state_revision,
                new_state=ExecutionState.FAILED,
                safe_error=TerminalError(
                    TerminalErrorClass.ROUTER_INTERNAL,
                    ErrorScope.LOGICAL_REQUEST,
                    "The Router could not complete the request.",
                ),
            )
        except Exception:  # noqa: BLE001 -- A later recovery pass owns the state.
            return


def _authorize(
    principal: ServicePrincipal,
    operation: str,
    scope: Scope,
    request_id: str,
    now: datetime,
    *,
    mutation: bool,
    scope_kind: ScopeKind = ScopeKind.SERVICE_OR_WORKSPACE,
) -> RequestContext:
    policy = OperationPolicy(
        operation=operation,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=Audience.DATA_PLANE,
        principal_kinds=frozenset({PrincipalKind.SERVICE}),
        scope_kind=scope_kind,
        scope_mismatch_mode=ScopeMismatchMode.HIDDEN_RECORD,
        mutation=mutation,
    )
    try:
        return authorize(principal, policy, scope, request_id=request_id, now=now)
    except SafeAuthorityError as error:
        raise _map_error(error, request_id) from error


def _parse_model_document(raw_body: bytes, request_id: str) -> ModelRequestDocument:
    value = _strict_json(raw_body, request_id)
    try:
        return ModelRequestDocument.model_validate(value)
    except ValidationError as error:
        raise _validation_error(error, request_id) from error


def _parse_compatible_chat(
    raw_body: bytes, request_id: str
) -> CompatibleChatRequest:
    value = _strict_json(raw_body, request_id)
    try:
        return CompatibleChatRequest.model_validate(value)
    except ValidationError as error:
        raise _validation_error(error, request_id) from error


def _compatible_native_document(
    value: CompatibleChatRequest, request_id: str
) -> ModelRequestDocument:
    if (
        value.tools is not None
        or value.tool_choice is not None
        or (
            value.response_format is not None
            and (
                value.response_format.type != "text"
                or value.response_format.schema_name is not None
                or value.response_format.schema_major_version is not None
            )
        )
    ):
        raise ModelRequestError(
            "unsupported_capability",
            400,
            "This deployment does not support the requested compatibility feature.",
            request_id,
        )
    output: dict[str, object] = {"format": "text"}
    if value.temperature is not None:
        output["temperature"] = value.temperature
    limits: dict[str, object] = {
        "attempt_timeout_ms": 30_000,
        "max_output_units": value.max_completion_tokens or 128,
    }
    if value.x_llmrouter_max_cost is not None:
        limits["max_cost"] = value.x_llmrouter_max_cost.model_dump(mode="json")
    try:
        target: dict[str, object]
        if value.x_llmrouter_exact_route is None:
            target = {"assignment": value.model}
        else:
            target = {
                "exact_route": value.x_llmrouter_exact_route,
                "exact_route_grant": value.x_llmrouter_exact_route_grant,
            }
        return ModelRequestDocument.model_validate(
            {
                "api_version": "1",
                "data_profile": value.x_llmrouter_data_profile or "service-data",
                "workspace_id": value.x_llmrouter_workspace_id,
                "messages": [item.model_dump(mode="json") for item in value.messages],
                "limits": limits,
                "output": output,
                **target,
            }
        )
    except ValidationError as error:
        raise _validation_error(error, request_id) from error


def _compatible_fingerprint(value: CompatibleChatRequest) -> dict[str, JsonValue]:
    return cast(
        "dict[str, JsonValue]",
        value.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"stream", "x_llmrouter_exact_route_grant"},
        ),
    )


def _parse_cancel_document(raw_body: bytes, request_id: str) -> CancelDocument:
    value = _strict_json(raw_body, request_id)
    try:
        return CancelDocument.model_validate(value)
    except ValidationError as error:
        raise _validation_error(error, request_id) from error


def _strict_json(raw_body: bytes, request_id: str) -> object:
    def constant(_value: str) -> None:
        raise ValueError("A JSON number must be finite.")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("A JSON object contains a duplicate field.")
            result[key] = value
        return result

    try:
        return json.loads(
            raw_body,
            parse_float=Decimal,
            parse_constant=constant,
            object_pairs_hook=pairs,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise _invalid_request(request_id, ("body",)) from error


def _validation_error(error: ValidationError, request_id: str) -> ModelRequestError:
    fields = tuple(
        FieldError(
            path=".".join(str(item) for item in issue["loc"])[:500] or "body",
            code=str(issue["type"])[:100],
            message="The field value is invalid.",
        )
        for issue in error.errors(include_input=False, include_url=False)[:100]
    )
    return ModelRequestError(
        "invalid_request",
        400,
        "The request is invalid.",
        request_id,
        field_errors=fields,
    )


def _require_mvp_capabilities(document: ModelRequestDocument, request_id: str) -> None:
    has_attachments = any(
        not isinstance(message.content, str)
        and any(part.type != "text" for part in message.content)
        for message in document.messages
    )
    if has_attachments:
        raise ModelRequestError(
            "unsupported_capability",
            400,
            "This deployment does not support model attachments.",
            request_id,
        )
    if any(message.role == "tool" for message in document.messages):
        raise ModelRequestError(
            "unsupported_capability",
            400,
            "This deployment does not support model tool messages.",
            request_id,
        )
    if document.tools or document.tool_allow_list:
        raise ModelRequestError(
            "unsupported_capability",
            400,
            "This deployment does not support model tools.",
            request_id,
        )
    if document.output.format == "json":
        raise ModelRequestError(
            "unsupported_capability",
            400,
            "This deployment supports text model output only.",
            request_id,
        )


def _fingerprint_execution(document: ModelRequestDocument) -> Mapping[str, JsonValue]:
    result: dict[str, JsonValue] = {
        "api_version": document.api_version,
        "messages": cast(
            "list[JsonValue]",
            [message.model_dump(mode="json") for message in document.messages],
        ),
        "limits": {
            "attempt_timeout_ms": document.limits.attempt_timeout_ms,
            "max_output_units": document.limits.max_output_units,
        },
        "output": {},
    }
    if document.assignment is not None:
        result["assignment"] = document.assignment
    if document.exact_route is not None:
        result["exact_route"] = document.exact_route
    if document.tools is not None:
        result["tools"] = cast(
            "list[JsonValue]",
            [tool.model_dump(mode="json") for tool in document.tools],
        )
    if document.tool_allow_list is not None:
        result["tool_allow_list"] = list(document.tool_allow_list)
    limits = cast("dict[str, JsonValue]", result["limits"])
    if document.limits.max_cost is not None:
        limits["max_cost"] = {
            "amount": str(document.limits.max_cost.amount),
            "currency": document.limits.max_cost.currency,
        }
    if document.limits.logical_timeout_ms is not None:
        limits["logical_timeout_ms"] = document.limits.logical_timeout_ms
    output = cast("dict[str, JsonValue]", result["output"])
    if document.output.format is not None:
        output["format"] = document.output.format
    if document.output.json_schema_name is not None:
        output["json_schema_name"] = document.output.json_schema_name
    if document.output.json_schema_major_version is not None:
        output["json_schema_major_version"] = document.output.json_schema_major_version
    if document.output.temperature is not None:
        output["temperature"] = float(document.output.temperature)
    return result


def _adapter_request(
    document: ModelRequestDocument, *, operation: ModelOperation
) -> ModelAdapterRequest:
    messages: list[ModelMessage] = []
    for message in document.messages:
        content = (
            message.content
            if isinstance(message.content, str)
            else "".join(
                part.text for part in message.content if isinstance(part, TextPart)
            )
        )
        messages.append(ModelMessage(MessageRole(message.role), content))
    return ModelAdapterRequest(
        operation,
        tuple(messages),
        document.limits.max_output_units,
        None
        if document.output.temperature is None
        else Decimal(str(float(document.output.temperature))),
    )


def _validated_adapter_request(
    document: ModelRequestDocument,
    request_id: str,
    *,
    operation: ModelOperation = ModelOperation.STREAM,
) -> ModelAdapterRequest:
    try:
        return _adapter_request(document, operation=operation)
    except (TypeError, ValueError) as error:
        raise _invalid_request(request_id, ("messages",)) from error


def _receipt_document(result: AdmissionResult) -> dict[str, object]:
    receipt = result.receipt
    document: dict[str, object] = {
        "request_id": receipt.request_id,
        "admitted_at": _timestamp(receipt.admitted_at),
        "state": receipt.state.value,
        "state_revision": receipt.state_revision,
        "status_url": receipt.status_url,
        "cancel_url": receipt.cancel_url,
        "fingerprint_version": receipt.fingerprint_version,
        "capture_enabled": receipt.capture_enabled,
        "capture_reason": receipt.capture_reason,
    }
    if receipt.events_url is not None:
        document["events_url"] = receipt.events_url
    return document


def _terminal_state(result: AdapterResult) -> ExecutionState:
    return {
        AttemptOutcome.SUCCEEDED: ExecutionState.SUCCEEDED,
        AttemptOutcome.FAILED: ExecutionState.FAILED,
        AttemptOutcome.INTERRUPTED: ExecutionState.INTERRUPTED,
        AttemptOutcome.CANCELLED: ExecutionState.CANCELLED,
        AttemptOutcome.UNCERTAIN: ExecutionState.UNCERTAIN,
    }[result.outcome]


def _map_error(error: Exception, request_id: str) -> ModelRequestError:
    if isinstance(error, ModelRequestError):
        return error
    if isinstance(error, SafeAuthorityError):
        return ModelRequestError(
            error.code.value, error.status_code, str(error), request_id
        )
    if isinstance(error, MachineIdentityError):
        status = 401 if error.code == "invalid_token" else 403
        return ModelRequestError(error.code, status, str(error), request_id)
    if isinstance(error, AdmissionError):
        metadata = {
            AdmissionErrorCode.INVALID_REQUEST: (400, "invalid_request"),
            AdmissionErrorCode.INSUFFICIENT_SCOPE: (403, "insufficient_scope"),
            AdmissionErrorCode.REQUEST_IDENTITY_CONFLICT: (
                409,
                "request_identity_conflict",
            ),
            AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED: (
                410,
                "request_identity_expired",
            ),
            AdmissionErrorCode.REQUEST_NOT_FOUND: (404, "request_not_found"),
            AdmissionErrorCode.ATTACHMENT_INVALID: (422, "attachment_invalid"),
            AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE: (
                422,
                "assignment_unavailable",
            ),
            AdmissionErrorCode.WORKSPACE_UNAVAILABLE: (
                422,
                "workspace_unavailable",
            ),
            AdmissionErrorCode.CONFIGURATION_UNAVAILABLE: (
                503,
                "stale_configuration",
            ),
            AdmissionErrorCode.DIAGNOSTIC_PERMISSION_REQUIRED: (
                403,
                "diagnostic_permission_required",
            ),
        }[error.code]
        retryable = metadata[0] == 503
        return ModelRequestError(
            metadata[1], metadata[0], str(error), request_id, retryable=retryable
        )
    if isinstance(error, ExecutionError):
        metadata = {
            ExecutionErrorCode.INSUFFICIENT_SCOPE: (403, "insufficient_scope"),
            ExecutionErrorCode.NOT_FOUND: (404, "request_not_found"),
            ExecutionErrorCode.REVISION_CONFLICT: (409, "state_revision_conflict"),
            ExecutionErrorCode.INVALID_TRANSITION: (409, "terminal_state"),
            ExecutionErrorCode.STREAM_CONFLICT: (409, "stream_replay_unavailable"),
            ExecutionErrorCode.STREAM_REPLAY_UNAVAILABLE: (
                409,
                "stream_replay_unavailable",
            ),
            ExecutionErrorCode.STREAM_INCOMPATIBLE: (
                400,
                "unsupported_contract",
            ),
            ExecutionErrorCode.OWNER_FENCED: (503, "temporarily_unavailable"),
        }[error.code]
        return ModelRequestError(
            metadata[1],
            metadata[0],
            _safe_message(metadata[1]),
            request_id,
            retryable=metadata[0] == 503,
        )
    if isinstance(error, RoutingError):
        metadata = {
            RoutingErrorCode.INSUFFICIENT_SCOPE: (403, "insufficient_scope"),
            RoutingErrorCode.NOT_FOUND: (404, "request_not_found"),
            RoutingErrorCode.BUSY: (503, "temporarily_unavailable"),
            RoutingErrorCode.NO_CANDIDATE: (422, "assignment_unavailable"),
            RoutingErrorCode.LOGICAL_DEADLINE: (422, "assignment_unavailable"),
            RoutingErrorCode.CLAIM_CONFLICT: (500, "internal_error"),
            RoutingErrorCode.DIAGNOSTIC_PERMISSION_REQUIRED: (
                403,
                "diagnostic_permission_required",
            ),
            RoutingErrorCode.POLICY_DENIED: (403, "policy_denied"),
            RoutingErrorCode.BUDGET_EXHAUSTED: (422, "budget_exhausted"),
            RoutingErrorCode.RATE_LIMITED: (429, "rate_limited"),
            RoutingErrorCode.INVALID_ADAPTER_RESULT: (500, "internal_error"),
        }[error.code]
        return ModelRequestError(
            metadata[1],
            metadata[0],
            _safe_message(metadata[1]),
            request_id,
            retryable=metadata[0] in {429, 500, 503},
        )
    return ModelRequestError(
        "internal_error",
        500,
        "The Router could not complete the operation.",
        request_id,
        retryable=True,
    )


def _safe_message(code: str) -> str:
    return {
        "request_not_found": "The request was not found.",
        "stream_replay_unavailable": "The requested stream replay is unavailable.",
        "unsupported_contract": "The requested contract is not supported.",
        "terminal_state": "The request is already terminal.",
        "state_revision_conflict": "The request state changed.",
        "temporarily_unavailable": "The Router is temporarily unavailable.",
        "assignment_unavailable": "The selected assignment is not available.",
        "diagnostic_permission_required": "A valid diagnostic grant is required.",
        "policy_denied": "Policy denied the request.",
        "budget_exhausted": "The applicable budget is exhausted.",
        "rate_limited": "The operation is rate limited.",
        "insufficient_scope": "The token does not permit this operation.",
        "internal_error": "The Router could not complete the operation.",
    }.get(code, "The Router could not complete the operation.")


def _invalid_request(request_id: str, path: tuple[str, ...]) -> ModelRequestError:
    return ModelRequestError(
        "invalid_request",
        400,
        "The request is invalid.",
        request_id,
        field_errors=(
            FieldError(".".join(path), "invalid", "The field value is invalid."),
        ),
    )


def _not_found(request_id: str) -> ModelRequestError:
    return ModelRequestError(
        "request_not_found", 404, "The request was not found.", request_id
    )


def _registry_key(scope: Scope, request_id: str) -> tuple[str, str | None, str]:
    if scope.service_id is None:
        raise ValueError("A model request needs a service scope.")
    return scope.service_id, scope.workspace_id, request_id


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
