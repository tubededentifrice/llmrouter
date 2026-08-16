"""Native model-request API contract tests."""
# ruff: noqa: ANN001, ANN202, D103, EM101, FBT003, PLR0913, PLR2004, PT011, TRY003

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from llmrouter_backend.admission import (
    AdmissionError,
    AdmissionErrorCode,
    AdmissionReceipt,
    AdmissionResult,
    RequestState,
)
from llmrouter_backend.authority import Audience, Scope, ServicePrincipal
from llmrouter_backend.execution import (
    AdapterStopEvidence,
    CancellationResult,
    ExecutionAdmission,
    ExecutionError,
    ExecutionErrorCode,
    ExecutionState,
    ExecutionStatus,
    ExecutionTarget,
    StreamEvent,
    make_event,
)
from llmrouter_backend.model_requests import http as model_request_http
from llmrouter_backend.model_requests.http import (
    install_model_request_service,
    router,
)
from llmrouter_backend.model_requests.model import (
    MAXIMUM_HTTP_BODY_BYTES,
    ModelRequestDocument,
    ModelRequestError,
    ResumePoint,
)
from llmrouter_backend.model_requests.service import ModelRequestService
from llmrouter_backend.routing import AdapterResult, AttemptOutcome
from llmrouter_backend.routing.errors import RoutingError, RoutingErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
SERVICE_ID = "0198b08f-7000-7000-8000-000000000001"
OTHER_SERVICE_ID = "0198b08f-7000-7000-8000-000000000002"
WORKSPACE_ID = "0198b08f-7000-7000-8000-000000000003"
REQUEST_ID = "0198b08f-7000-7000-8000-000000000004"
TOKEN = "T" * 43


def _principal(
    *,
    service_id: str = SERVICE_ID,
    operations: frozenset[str] | None = None,
    workspaces: frozenset[str] | None = frozenset({WORKSPACE_ID}),
) -> ServicePrincipal:
    return ServicePrincipal(
        "test",
        "token-id",
        Audience.DATA_PLANE,
        service_id,
        operations or frozenset({"model.create", "model.read", "model.cancel"}),
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=4),
        1,
        workspaces,
    )


def _body(*, text: str = "private prompt", workspace_id: str = WORKSPACE_ID) -> bytes:
    return json.dumps(
        {
            "api_version": "1",
            "data_profile": "service-data",
            "workspace_id": workspace_id,
            "assignment": "chat",
            "messages": [{"role": "user", "content": text}],
            "limits": {"attempt_timeout_ms": 30_000, "max_output_units": 64},
            "output": {"format": "text"},
        },
        separators=(",", ":"),
    ).encode()


class _Authenticator:
    def __init__(self, principal: ServicePrincipal) -> None:
        self.principal = principal
        self.calls = 0

    def authenticate(self, token: str, *, request_id: str, now: datetime):
        del request_id, now
        self.calls += 1
        if token != TOKEN:
            raise RuntimeError("bad token")
        return self.principal


class _Admission:
    def __init__(self, execution: _Execution) -> None:
        self.execution = execution
        self.requests: dict[tuple[str, str | None, str], object] = {}
        self.lock = threading.Lock()

    def admit(self, context, request, *, now: datetime):
        key = (context.scope.service_id, context.scope.workspace_id, request.request_id)
        with self.lock:
            previous = self.requests.get(key)
            if previous is not None and previous != request.fingerprint:
                raise AdmissionError(
                    AdmissionErrorCode.REQUEST_IDENTITY_CONFLICT, request.request_id
                )
            created = previous is None
            self.requests[key] = request.fingerprint
            if created:
                self.execution.add(request.request_id, context.scope)
        receipt = AdmissionReceipt(
            request.request_id,
            now,
            RequestState.ADMITTED,
            1,
            f"/v1/model-requests/{request.request_id}",
            f"/v1/model-requests/{request.request_id}/cancel",
            f"/v1/model-requests/{request.request_id}/events",
        )
        return AdmissionResult(receipt, created)


class _Execution:
    def __init__(self) -> None:
        self.statuses: dict[str, ExecutionStatus] = {}
        self.events: dict[str, list[StreamEvent]] = {}
        self.disconnects = 0
        self.cancel_evidence: tuple[AdapterStopEvidence, ...] = ()
        self.unavailable_after: int | None = None

    def add(self, request_id: str, scope: Scope) -> None:
        del scope
        target = ExecutionTarget("model", request_id)
        admission = ExecutionAdmission(
            request_id,
            None,
            NOW,
            ExecutionState.ADMITTED,
            1,
            f"/v1/model-requests/{request_id}",
            f"/v1/model-requests/{request_id}/cancel",
            f"/v1/model-requests/{request_id}/events",
            "rfc8785-sha256-v1",
            True,
            "configured",
        )
        self.statuses[request_id] = ExecutionStatus(
            target,
            ExecutionState.ADMITTED,
            1,
            NOW,
            NOW,
            None,
            None,
            False,
            False,
            "revision-1",
            admission,
            admission.status_url,
            admission.cancel_url,
            admission.events_url,
        )
        self.events[request_id] = [
            make_event(
                target,
                sequence=1,
                event_name="request.admitted",
                occurred_at=NOW,
                payload={
                    "state": "admitted",
                    "state_revision": 1,
                    "admission": {
                        "request_id": request_id,
                        "admitted_at": "2026-08-16T12:00:00.000Z",
                    },
                },
            )
        ]

    def status(self, context, target):
        del context
        return self.statuses[target.public_id]

    def transition(
        self,
        context,
        target,
        *,
        expected_revision: int,
        new_state: ExecutionState,
        safe_error=None,
        owner_epoch=None,
        tool_call_id=None,
        tool_expires_at=None,
    ):
        del context, owner_epoch, tool_call_id, tool_expires_at
        current = self.statuses[target.public_id]
        assert current.state_revision == expected_revision
        revision = expected_revision + 1
        terminal_at = (
            NOW
            if new_state
            in {
                ExecutionState.SUCCEEDED,
                ExecutionState.FAILED,
                ExecutionState.INTERRUPTED,
                ExecutionState.CANCELLED,
                ExecutionState.UNCERTAIN,
            }
            else None
        )
        updated = replace(
            current,
            state=new_state,
            state_revision=revision,
            last_transition_at=NOW,
            terminal_at=terminal_at,
            safe_error=safe_error,
        )
        self.statuses[target.public_id] = updated
        event_name = (
            "request.terminal"
            if updated.terminal
            else "request.cancel_requested"
            if new_state is ExecutionState.CANCEL_REQUESTED
            else "request.running"
        )
        payload: dict[str, object] = {"state_revision": revision}
        if updated.terminal:
            payload.update(
                {
                    "state": new_state.value,
                    "partial_output": False,
                    "committed_effects": False,
                }
            )
        self.append_event(None, target, event_name=event_name, payload=payload)
        return updated

    def append_event(
        self,
        context,
        target,
        *,
        event_name: str,
        payload: dict[str, object],
        expected_sequence=None,
        owner_epoch=None,
    ):
        del context, expected_sequence, owner_epoch
        sequence = len(self.events[target.public_id]) + 1
        event = make_event(
            target,
            sequence=sequence,
            event_name=event_name,
            occurred_at=NOW,
            payload=payload,
        )
        self.events[target.public_id].append(event)
        return event

    def replay(self, context, target, *, after_sequence: int):
        del context
        if self.unavailable_after == after_sequence:
            raise ExecutionError(
                ExecutionErrorCode.STREAM_REPLAY_UNAVAILABLE, target.public_id
            )
        return tuple(
            event
            for event in self.events[target.public_id]
            if event.sequence > after_sequence
        )

    def cancel(self, context, target, *, reason: str, active_stops=()):
        del context, reason
        current = self.statuses[target.public_id]
        evidence = tuple(stop() for stop in active_stops)
        self.cancel_evidence = evidence
        if current.terminal:
            return CancellationResult(current, True, None, evidence)
        updated = self.transition(
            None,
            target,
            expected_revision=current.state_revision,
            new_state=ExecutionState.CANCEL_REQUESTED,
        )
        return CancellationResult(updated, False, NOW + timedelta(minutes=10), evidence)

    def stream_disconnected(self, context, target):
        del context
        self.disconnects += 1
        return self.statuses[target.public_id]


class _Routing:
    def __init__(self) -> None:
        self.calls = 0
        self.candidates = ("route-a", "route-b")
        self.attempted_routes: list[str] = []

    def execute(self, context, *, request_id: str, owner_id: str):
        del context, request_id, owner_id
        self.calls += 1
        self.attempted_routes.extend(self.candidates)
        return AdapterResult(AttemptOutcome.SUCCEEDED)


class _Views:
    def __init__(self, execution: _Execution) -> None:
        self.execution = execution
        self.scopes: dict[str, Scope] = {}

    def resolve_scope(self, principal, request_id: str):
        scope = self.scopes.get(request_id)
        if scope is None or scope.service_id != principal.service_id:
            return None
        if (
            scope.workspace_id is not None
            and principal.allowed_workspace_ids is not None
            and scope.workspace_id not in principal.allowed_workspace_ids
        ):
            return None
        return scope

    def status(self, context, target):
        status = self.execution.status(context, target)
        return {
            "request_id": target.public_id,
            "state": status.state.value,
            "state_revision": status.state_revision,
            "attempts": [
                {
                    "provider_model_route_id": "route-a",
                    "state": "failed",
                    "usage": [{"unit": "input_token", "quantity": "4"}],
                },
                {
                    "provider_model_route_id": "route-b",
                    "state": "succeeded",
                    "usage": [{"unit": "output_token", "quantity": "2"}],
                },
            ],
            "accounting": {
                "estimated": "0.001",
                "reserved": "0.001",
                "used": "0.0008",
                "corrected": "0.0008",
                "currency": "USD",
            },
        }

    def resume_point(self, context, target):
        del context
        status = self.execution.status(None, target)
        return ResumePoint(status.state, status.state_revision)


def _service(
    *,
    principal: ServicePrincipal | None = None,
    stops: Callable[[Any, Any], Sequence[Callable[[], AdapterStopEvidence]]]
    | None = None,
    run_inline: bool = True,
    resume_running: bool = False,
):
    execution = _Execution()
    admission = _Admission(execution)
    routing = _Routing()
    views = _Views(execution)
    authenticator = _Authenticator(principal or _principal())

    def submit(work: Callable[[], None]) -> None:
        if resume_running:
            target = ExecutionTarget("model", REQUEST_ID)
            current = execution.status(None, target)
            execution.transition(
                None,
                target,
                expected_revision=current.state_revision,
                new_state=ExecutionState.RUNNING,
            )
        if run_inline:
            work()

    service = ModelRequestService(
        authenticator=authenticator,
        admission=admission,
        execution=execution,
        routing=routing,
        views=views,
        submit=submit,
        owner_id="test-owner",
        clock=lambda: NOW,
        active_stops=stops,
    )
    return service, authenticator, admission, execution, routing, views


def _app(service: ModelRequestService) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    install_model_request_service(app, service)
    return app


def test_schema_rejects_unknown_fields_and_redacts_content_from_repr() -> None:
    document = ModelRequestDocument.model_validate_json(_body())
    assert "private prompt" not in repr(document)
    assert "private prompt" not in str(document)
    with pytest.raises(ValueError):
        ModelRequestDocument.model_validate(
            {**json.loads(_body()), "unknown": "private prompt"}
        )
    invalid_cost = json.loads(_body())
    invalid_cost["limits"]["max_cost"] = {"amount": 1, "currency": "USD"}
    with pytest.raises(ValueError):
        ModelRequestDocument.model_validate(invalid_cost)
    invalid_temperature = json.loads(_body())
    invalid_temperature["output"]["temperature"] = "0.5"
    with pytest.raises(ValueError):
        ModelRequestDocument.model_validate(invalid_temperature)


def test_create_is_idempotent_and_conflict_never_starts_provider_work() -> None:
    service, _, _, _, routing, views = _service()
    first = service.create(TOKEN, REQUEST_ID, _body(), error_request_id="transport-1")
    views.scopes[REQUEST_ID] = Scope(SERVICE_ID, WORKSPACE_ID)
    replay = service.create(TOKEN, REQUEST_ID, _body(), error_request_id="transport-2")
    assert (first.status_code, replay.status_code, routing.calls) == (201, 200, 1)
    with pytest.raises(ModelRequestError) as captured:
        service.create(
            TOKEN,
            REQUEST_ID,
            _body(text="different private prompt"),
            error_request_id="transport-3",
        )
    assert captured.value.code == "request_identity_conflict"
    assert routing.calls == 1
    assert "private prompt" not in str(captured.value)


def test_invalid_adapter_input_is_rejected_before_durable_admission() -> None:
    service, _, admission, _, routing, _ = _service()
    with pytest.raises(ModelRequestError) as captured:
        service.create(TOKEN, REQUEST_ID, _body(text=""), error_request_id="transport")
    assert captured.value.code == "invalid_request"
    assert admission.requests == {}
    assert routing.calls == 0


def test_running_request_uses_existing_routing_recovery_and_fallback_order() -> None:
    service, _, _, execution, routing, _ = _service(resume_running=True)
    service.create(TOKEN, REQUEST_ID, _body(), error_request_id="transport")
    assert routing.calls == 1
    assert routing.attempted_routes == ["route-a", "route-b"]
    assert execution.statuses[REQUEST_ID].state is ExecutionState.SUCCEEDED


def test_concurrent_create_and_equal_replays_claim_one_local_dispatch() -> None:
    execution = _Execution()
    admission = _Admission(execution)
    routing = _Routing()
    views = _Views(execution)
    submitted: list[Callable[[], None]] = []
    service = ModelRequestService(
        authenticator=_Authenticator(_principal()),
        admission=admission,
        execution=execution,
        routing=routing,
        views=views,
        submit=submitted.append,
        owner_id="test-owner",
        clock=lambda: NOW,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
        results = tuple(
            workers.map(
                lambda ordinal: service.create(
                    TOKEN,
                    REQUEST_ID,
                    _body(),
                    error_request_id=f"transport-{ordinal}",
                ),
                range(3),
            )
        )
    assert sorted(result.status_code for result in results) == [200, 200, 201]
    assert len(submitted) == 1
    submitted[0]()
    assert routing.calls == 1
    assert execution.statuses[REQUEST_ID].state is ExecutionState.SUCCEEDED


@pytest.mark.parametrize(
    "durable_state", [ExecutionState.ADMITTED, ExecutionState.RUNNING]
)
def test_equal_replay_after_restart_resumes_durable_work(
    durable_state: ExecutionState,
) -> None:
    execution = _Execution()
    admission = _Admission(execution)
    routing = _Routing()
    views = _Views(execution)
    first_process = ModelRequestService(
        authenticator=_Authenticator(_principal()),
        admission=admission,
        execution=execution,
        routing=routing,
        views=views,
        submit=lambda _work: None,
        owner_id="first-owner",
        clock=lambda: NOW,
    )
    first_process.create(TOKEN, REQUEST_ID, _body(), error_request_id="transport-1")
    if durable_state is ExecutionState.RUNNING:
        execution.transition(
            None,
            ExecutionTarget("model", REQUEST_ID),
            expected_revision=1,
            new_state=ExecutionState.RUNNING,
        )
    second_process = ModelRequestService(
        authenticator=_Authenticator(_principal()),
        admission=admission,
        execution=execution,
        routing=routing,
        views=views,
        submit=lambda work: work(),
        owner_id="second-owner",
        clock=lambda: NOW,
    )
    replay = second_process.create(
        TOKEN, REQUEST_ID, _body(), error_request_id="transport-2"
    )
    assert replay.status_code == 200
    assert routing.calls == 1
    assert execution.statuses[REQUEST_ID].state is ExecutionState.SUCCEEDED


def test_submit_failure_releases_claim_for_equal_replay_recovery() -> None:
    execution = _Execution()
    admission = _Admission(execution)
    routing = _Routing()
    views = _Views(execution)

    def fail_submit(_work: Callable[[], None]) -> None:
        raise RuntimeError("test submit failure")

    failed_process = ModelRequestService(
        authenticator=_Authenticator(_principal()),
        admission=admission,
        execution=execution,
        routing=routing,
        views=views,
        submit=fail_submit,
        owner_id="failed-owner",
        clock=lambda: NOW,
    )
    with pytest.raises(ModelRequestError) as captured:
        failed_process.create(
            TOKEN, REQUEST_ID, _body(), error_request_id="transport-1"
        )
    assert captured.value.code == "temporarily_unavailable"
    assert execution.statuses[REQUEST_ID].state is ExecutionState.ADMITTED
    recovery_process = ModelRequestService(
        authenticator=_Authenticator(_principal()),
        admission=admission,
        execution=execution,
        routing=routing,
        views=views,
        submit=lambda work: work(),
        owner_id="recovery-owner",
        clock=lambda: NOW,
    )
    replay = recovery_process.create(
        TOKEN, REQUEST_ID, _body(), error_request_id="transport-2"
    )
    assert replay.status_code == 200
    assert routing.calls == 1


def test_routing_fence_busy_preserves_running_state_for_equal_replay() -> None:
    class _BusyOnceRouting(_Routing):
        def execute(self, context, *, request_id: str, owner_id: str):
            if self.calls == 0:
                self.calls += 1
                raise RoutingError(RoutingErrorCode.BUSY, request_id)
            return super().execute(context, request_id=request_id, owner_id=owner_id)

    execution = _Execution()
    admission = _Admission(execution)
    routing = _BusyOnceRouting()
    service = ModelRequestService(
        authenticator=_Authenticator(_principal()),
        admission=admission,
        execution=execution,
        routing=routing,
        views=_Views(execution),
        submit=lambda work: work(),
        owner_id="recovery-owner",
        clock=lambda: NOW,
    )

    first = service.create(TOKEN, REQUEST_ID, _body(), error_request_id="transport-1")
    assert first.status_code == 201
    assert execution.statuses[REQUEST_ID].state is ExecutionState.RUNNING

    replay = service.create(TOKEN, REQUEST_ID, _body(), error_request_id="transport-2")
    assert replay.status_code == 200
    assert routing.calls == 2
    assert execution.statuses[REQUEST_ID].state is ExecutionState.SUCCEEDED


def test_cancel_requested_equal_replay_does_not_schedule_work() -> None:
    service, _, admission, execution, routing, views = _service(run_inline=False)
    service.create(TOKEN, REQUEST_ID, _body(), error_request_id="transport-1")
    views.scopes[REQUEST_ID] = Scope(SERVICE_ID, WORKSPACE_ID)
    scoped = service.authorize_existing(
        TOKEN, REQUEST_ID, "model.cancel", error_request_id="transport-2"
    )
    service.cancel(
        scoped,
        REQUEST_ID,
        b'{"reason":"test cancellation"}',
        error_request_id="transport-3",
    )
    submitted: list[Callable[[], None]] = []
    restarted = ModelRequestService(
        authenticator=_Authenticator(_principal()),
        admission=admission,
        execution=execution,
        routing=routing,
        views=views,
        submit=submitted.append,
        owner_id="restarted-owner",
        clock=lambda: NOW,
    )
    replay = restarted.create(
        TOKEN, REQUEST_ID, _body(), error_request_id="transport-4"
    )
    assert replay.status_code == 200
    assert submitted == []
    assert routing.calls == 0


def test_deep_json_is_a_safe_invalid_request_before_admission() -> None:
    service, _, admission, _, _, _ = _service()
    raw_body = b"[" * 2_000 + b"0" + b"]" * 2_000
    with pytest.raises(ModelRequestError) as captured:
        service.create(TOKEN, REQUEST_ID, raw_body, error_request_id="transport")
    assert captured.value.code == "invalid_request"
    assert admission.requests == {}


def test_authentication_and_scope_hide_other_service_records() -> None:
    service, authenticator, _, execution, _, views = _service(
        principal=_principal(service_id=OTHER_SERVICE_ID, workspaces=None)
    )
    execution.add(REQUEST_ID, Scope(SERVICE_ID, WORKSPACE_ID))
    views.scopes[REQUEST_ID] = Scope(SERVICE_ID, WORKSPACE_ID)
    with pytest.raises(ModelRequestError) as captured:
        service.authorize_existing(
            TOKEN, REQUEST_ID, "model.read", error_request_id="transport"
        )
    assert authenticator.calls == 1
    assert captured.value.code == "request_not_found"


def test_cancel_uses_only_existing_stop_evidence_and_disconnect_does_not_cancel() -> (
    None
):
    def confirmed_stop() -> AdapterStopEvidence:
        return AdapterStopEvidence("operation-1", True, True, True)

    service, _, _, execution, _, views = _service(
        stops=lambda _context, _target: (confirmed_stop,), run_inline=False
    )
    service.create(TOKEN, REQUEST_ID, _body(), error_request_id="transport")
    views.scopes[REQUEST_ID] = Scope(SERVICE_ID, WORKSPACE_ID)
    scoped = service.authorize_existing(
        TOKEN, REQUEST_ID, "model.cancel", error_request_id="transport"
    )
    service.disconnect(scoped, REQUEST_ID, error_request_id="transport")
    assert execution.disconnects == 1
    status = service.cancel(
        scoped,
        REQUEST_ID,
        b'{"reason":"test cancellation"}',
        error_request_id="transport",
    )
    assert status["state"] == "cancel_requested"
    assert execution.cancel_evidence == (
        AdapterStopEvidence("operation-1", True, True, True),
    )


def test_http_create_status_stream_and_safe_replay_gap() -> None:
    service, _, _, execution, _, views = _service()
    views.scopes[REQUEST_ID] = Scope(SERVICE_ID, WORKSPACE_ID)
    client = TestClient(_app(service))
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-LLMRouter-Request-ID": REQUEST_ID,
        "Content-Type": "application/json",
    }
    created = client.post("/v1/model-requests", headers=headers, content=_body())
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    status = client.get(
        f"/v1/model-requests/{REQUEST_ID}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status.status_code == 200
    assert status.json()["accounting"]["currency"] == "USD"
    assert [item["provider_model_route_id"] for item in status.json()["attempts"]] == [
        "route-a",
        "route-b",
    ]
    assert status.json()["attempts"][1]["usage"] == [
        {"unit": "output_token", "quantity": "2"}
    ]
    stream = client.get(
        f"/v1/model-requests/{REQUEST_ID}/events",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "text/event-stream; llmrouter-stream=1",
        },
    )
    assert stream.status_code == 200
    assert "event: request.terminal" in stream.text
    assert execution.disconnects == 1
    execution.unavailable_after = 1
    gap = client.get(
        f"/v1/model-requests/{REQUEST_ID}/events",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "text/event-stream; llmrouter-stream=1",
            "Last-Event-ID": "1",
        },
    )
    assert gap.status_code == 409
    assert gap.json()["error"]["code"] == "stream_replay_unavailable"


def test_http_rejects_bad_headers_and_duplicate_json_without_content_leak() -> None:
    service, _, _, _, _, _ = _service()
    client = TestClient(_app(service))
    response = client.post(
        "/v1/model-requests",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-LLMRouter-Request-ID": REQUEST_ID,
            "Content-Type": "application/json",
        },
        content=b'{"api_version":"1","api_version":"private prompt"}',
    )
    assert response.status_code == 400
    assert "private prompt" not in response.text
    unsupported = client.get(
        f"/v1/model-requests/{REQUEST_ID}/events",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"},
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] == "unsupported_contract"


def test_http_rejects_one_oversized_body_chunk_before_json_or_authentication() -> None:
    service, authenticator, _, _, _, _ = _service()
    client = TestClient(_app(service))
    response = client.post(
        "/v1/model-requests",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-LLMRouter-Request-ID": REQUEST_ID,
            "Content-Type": "application/json",
        },
        content=b"x" * (MAXIMUM_HTTP_BODY_BYTES + 1),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert authenticator.calls == 0


def test_body_reader_checks_one_chunk_before_copying_it() -> None:
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        assert not delivered
        delivered = True
        return {
            "type": "http.request",
            "body": b"x" * (MAXIMUM_HTTP_BODY_BYTES + 1),
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/model-requests",
            "headers": [],
        },
        receive,
    )
    with pytest.raises(ModelRequestError) as captured:
        asyncio.run(model_request_http._bounded_body(request, "transport"))  # noqa: SLF001
    assert captured.value.code == "invalid_request"
