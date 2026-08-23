"""Compose the complete localhost-only MVP runtime."""
# ruff: noqa: D102, D107, EM101, PLR0913, PLR0915, TRY003

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import uuid
from base64 import b64decode, b64encode
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Never, cast

import httpx
import psycopg
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from llmrouter_backend.accounting import (
    AccountingEvent,
    AccountingSubjectKind,
    PostgresAccountingRepository,
    UsageUnit,
)
from llmrouter_backend.accounting import (
    AttemptOutcome as AccountingOutcome,
)
from llmrouter_backend.adapters.openrouter import (
    OpenRouterAdapter,
    openrouter_registered_schemas,
)
from llmrouter_backend.administration.audit import PostgresAuditRepository
from llmrouter_backend.administration.diagnostics import (
    AdministratorDiagnosticRunner,
    TransientDiagnosticAuthenticator,
)
from llmrouter_backend.administration.http import install_administration_service
from llmrouter_backend.administration.service import AdministrationService
from llmrouter_backend.admission import PostgresAdmissionRepository
from llmrouter_backend.authority import (
    AuthorityClass,
    AuthorityPath,
    BrowserWriteProof,
    OperationPolicy,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets import (
    PostgresBudgetRepository,
    ReservationState,
)
from llmrouter_backend.configuration import (
    ConfigurationRevisionDistribution,
    DistributionScope,
    NormalConfigurationRevision,
    PostgresConfigurationRepository,
    RevisionAuthenticator,
    SettingsSchemaRegistry,
)
from llmrouter_backend.credential_store import (
    DataPlaneCredentialDistributor,
    EncryptedCredentialRepository,
    SecretLease,
)
from llmrouter_backend.execution import (
    AdapterStopEvidence,
    ErrorScope,
    ExecutionKind,
    ExecutionState,
    ExecutionTarget,
    PostgresExecutionRepository,
    TerminalError,
    TerminalErrorClass,
)
from llmrouter_backend.lifecycle import PostgresLifecycleRepository
from llmrouter_backend.machine_identity import MachineCredentialRepository
from llmrouter_backend.model_requests import (
    ModelRequestService,
    PostgresModelRequestViews,
    ThreadWorkSubmitter,
    TransientModelInputRegistry,
    install_model_request_service,
)
from llmrouter_backend.routing import (
    AdapterResult,
    AttemptFailure,
    AttemptOutcome,
    BudgetDecision,
    PostgresRoutingRepository,
    RoutingCoordinator,
    SafeFailureEvidence,
)
from llmrouter_backend.spool import CanonicalLedger, EncryptedFrameJournal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from llmrouter_backend.admin_auth import AdministratorAuthRepository
    from llmrouter_backend.admission import AdmissionRequest, AdmissionResult
    from llmrouter_backend.execution import AdapterStop
    from llmrouter_backend.routing import AdapterProgress, AttemptPlan
    from llmrouter_backend.spool import CanonicalEvent

LOCAL_ADMIN_ORIGIN = "http://127.0.0.1:5174"
LOCAL_SOURCE_NODE_ID = "0198a080-0000-7000-8000-000000000150"
_COOKIE = "__Host-llmrouter-local-admin"
_LOCAL_ADMIN_PORT = 5174
_MINIMUM_LOCAL_SECRET_CHARACTERS = 20
_MAXIMUM_LOCAL_SECRET_CHARACTERS = 500
_MAXIMUM_LOCAL_ACTIVATION_BYTES = 1024
_router = APIRouter()
_LOGGER = logging.getLogger(__name__)


class LocalAdministratorAuthority:
    """Authorize one generated localhost administrator session."""

    def __init__(self, session: str, csrf: str) -> None:
        self._session = session
        self._csrf = csrf

    @property
    def csrf(self) -> str:
        return self._csrf

    def valid_session(self, value: str) -> bool:
        """Compare one local administrator cookie without exposing it."""
        return hmac.compare_digest(value, self._session)

    def authorize_session(
        self,
        session_token: str,
        *,
        request_id: str,
        now: datetime,
        policy: OperationPolicy,
        scope: Scope,
        csrf_token: str | None = None,
        origin: str | None = None,
    ) -> RequestContext:
        """Require the generated cookie and exact browser proof."""
        if not hmac.compare_digest(session_token, self._session):
            raise PermissionError("The administrator session is invalid.")
        if policy.mutation:
            proof = BrowserWriteProof(
                LOCAL_ADMIN_ORIGIN,
                origin or "",
                self._csrf,
                csrf_token or "",
            )
            if proof.request_origin != proof.allowed_origin or not hmac.compare_digest(
                proof.session_csrf_token, proof.request_csrf_token
            ):
                raise PermissionError("The browser write proof is invalid.")
        return RequestContext(
            request_id=request_id,
            actor_kind=PrincipalKind.ADMINISTRATOR,
            actor_id="local-development-administrator",
            authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
            authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
            machine_audience=None,
            operation=policy.operation,
            scope=scope,
            authorized_at=now,
            recent_authentication_at=now,
            mutation=policy.mutation,
        )


class DualAdministratorAuthority:
    """Keep localhost proof authority separate from public OIDC authority."""

    def __init__(
        self,
        local: LocalAdministratorAuthority,
        production: AdministratorAuthRepository,
    ) -> None:
        self._local = local
        self._production = production

    def authorize_session(self, session_token: str, **kwargs: object) -> RequestContext:
        mode, separator, token = session_token.partition(":")
        if not separator:
            raise PermissionError("The administrator session mode is invalid.")
        if mode == "local":
            return self._local.authorize_session(token, **kwargs)  # type: ignore[arg-type]
        if mode == "oidc":
            return self._production.authorize_session(token, **kwargs)  # type: ignore[arg-type]
        raise PermissionError("The administrator session mode is invalid.")


class LocalDistributionAdmission:
    """Refresh signed local configuration state before durable admission."""

    def __init__(
        self,
        database_url: str,
        repository: PostgresAdmissionRepository,
        distribution: ConfigurationRevisionDistribution,
        signer: RevisionAuthenticator,
    ) -> None:
        self._database_url = database_url
        self._repository = repository
        self._distribution = distribution
        self._signer = signer

    def admit(
        self, context: RequestContext, request: AdmissionRequest, *, now: datetime
    ) -> AdmissionResult:
        """Apply the exact active local revision, then admit."""
        service_id = context.scope.service_id
        if service_id is None:
            raise ValueError("A local model request needs one service.")
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                """WITH candidates AS (
                       SELECT active.revision_id, active.revision_number,
                              active.activated_at, 0 AS priority
                       FROM router.active_configurations AS active
                       WHERE active.scope_kind = 'workspace'
                         AND active.service_id = %s AND active.workspace_id = %s
                       UNION ALL
                       SELECT active.revision_id, active.revision_number,
                              active.activated_at, 1 AS priority
                       FROM router.active_configurations AS active
                       WHERE active.scope_kind = 'service' AND active.service_id = %s
                   )
                   SELECT candidate.revision_id::text, candidate.revision_number,
                          revision.content_sha256, candidate.activated_at
                   FROM candidates AS candidate
                   JOIN router.configuration_revisions AS revision
                     ON revision.id = candidate.revision_id
                   ORDER BY candidate.priority LIMIT 1""",
                (service_id, context.scope.workspace_id, service_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("The local configuration revision is unavailable.")
        revision = NormalConfigurationRevision(
            DistributionScope(service_id, context.scope.workspace_id),
            row[0],
            int(row[1]),
            bytes(row[2]),
            row[3],
        )
        signed = self._signer.normal(
            revision,
            authentication_challenge=self._distribution.authentication_challenge,
        )
        self._distribution.apply_normal(signed, received_at=now)
        return self._repository.admit(context, request, now=now)


class LocalBudgetGate:
    """Adapt durable budget reservations to the routing port."""

    def __init__(self, database_url: str, repository: PostgresBudgetRepository) -> None:
        self._database_url = database_url
        self._repository = repository
        self._reservations: dict[str, tuple[str, str]] = {}

    def reserve(self, plan: AttemptPlan) -> BudgetDecision:
        """Reserve the typed maximum cost for one attempt."""
        currency = plan.typed_prices[0].currency
        maximum = sum(
            (
                item.price
                * (Decimal(1) if item.unit is UsageUnit.REQUEST else Decimal(128))
                / item.unit_quantity
                for item in plan.typed_prices
            ),
            Decimal(0),
        )
        result = self._repository.reserve_candidate(
            _system("budget.reserve"),
            request_row_id=plan.request_row_id,
            candidate_id=plan.provider_model_route_id,
            reservation_key=plan.reservation_key,
            estimated_amount=maximum,
            reserved_amount=maximum,
            currency=currency,
            now=_now(),
        )
        if (
            result.state is not ReservationState.RESERVED
            or result.reservation_id is None
        ):
            failure = AttemptFailure(
                TerminalError(
                    TerminalErrorClass.BUDGET,
                    ErrorScope.LOGICAL_REQUEST,
                    "The applicable budget is exhausted.",
                ),
                plan.request_id,
                SafeFailureEvidence(detail_code="budget_exhausted"),
            )
            return BudgetDecision(permitted=False, failure=failure)
        if result.accounting_scope_id is None:
            raise RuntimeError("The local budget has no accounting scope.")
        self._reservations[plan.attempt_id] = (
            result.reservation_id,
            result.accounting_scope_id,
        )
        return BudgetDecision(permitted=True, reservation_id=result.reservation_id)

    def release(self, reservation_id: str) -> None:
        """Reconcile an unused reservation with zero cost."""
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                """SELECT created_at
                   FROM router.budget_candidate_reservations
                   WHERE id = %s""",
                (reservation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("The local budget reservation is unavailable.")
        self._repository.reconcile(
            _system("budget.reconcile"),
            reservation_id,
            accounting_event_id=str(
                uuid.uuid5(uuid.UUID(reservation_id), "unused-reservation")
            ),
            actual_amount=Decimal(0),
            now=row[0],
        )

    def evidence(self, plan: AttemptPlan) -> tuple[str, str]:
        """Return current or recovered reservation evidence."""
        current = self._reservations.get(plan.attempt_id)
        if current is not None:
            return current
        with psycopg.connect(self._database_url) as connection:
            recovered = connection.execute(
                """SELECT attempt.budget_reservation_id::text,
                          allocation.budget_scope_id::text
                   FROM router.provider_attempts AS attempt
                   JOIN router.budget_reservation_allocations AS allocation
                     ON allocation.reservation_id = attempt.budget_reservation_id
                   JOIN router.budget_scopes AS scope
                     ON scope.id = allocation.budget_scope_id
                   WHERE attempt.id = %s
                   ORDER BY CASE scope.scope_kind
                       WHEN 'assignment' THEN 1
                       WHEN 'workspace' THEN 2
                       WHEN 'service' THEN 3
                       WHEN 'global' THEN 4
                       ELSE 0
                   END, scope.id
                   LIMIT 1""",
                (plan.attempt_id,),
            ).fetchone()
        if recovered is not None:
            evidence = (recovered[0], recovered[1])
            self._reservations[plan.attempt_id] = evidence
            return evidence
        raise RuntimeError("The local attempt reservation is unavailable.")

    def finished_at(self, plan: AttemptPlan) -> datetime:
        """Return the durable attempt time for idempotent accounting retries."""
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                """SELECT finished_at
                   FROM router.provider_attempts
                   WHERE id = %s AND request_row_id = %s""",
                (plan.attempt_id, plan.request_row_id),
            ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("The local attempt completion time is unavailable.")
        return cast("datetime", row[0])


class LocalCancelableAdapter:
    """Expose only active plans as durable cancellation callbacks."""

    def __init__(
        self,
        adapter: OpenRouterAdapter,
        *,
        confirm_submitted_stop: bool = False,
    ) -> None:
        self._adapter = adapter
        self._confirm_submitted_stop = confirm_submitted_stop
        self._active: dict[str, AttemptPlan] = {}
        self._lock = threading.RLock()

    def execute(self, plan: AttemptPlan, progress: AdapterProgress) -> AdapterResult:
        with self._lock:
            self._active[plan.attempt_id] = plan
        try:
            return self._adapter.execute(plan, progress)
        finally:
            with self._lock:
                self._active.pop(plan.attempt_id, None)

    def cancel(self, plan: AttemptPlan) -> AdapterStopEvidence:
        with self._lock:
            active = self._active.get(plan.attempt_id)
        evidence = self._adapter.cancel(plan)
        if (
            isinstance(self._adapter, OpenRouterAdapter)
            and not self._confirm_submitted_stop
        ):
            return evidence
        if active is not None and evidence.stop_requested:
            return AdapterStopEvidence(
                evidence.operation_id,
                supported=True,
                stop_requested=True,
                confirmed_stopped=True,
                safe_code="local_deterministic_transport_stopped",
            )
        return evidence

    def active_stops(
        self, context: RequestContext, target: ExecutionTarget
    ) -> tuple[AdapterStop, ...]:
        del context
        with self._lock:
            plans = tuple(
                active
                for active in self._active.values()
                if active.request_id == target.public_id
            )
        return tuple(self._stop(plan) for plan in plans)

    def close(self) -> None:
        """Stop each active local provider stream during process shutdown."""
        with self._lock:
            plans = tuple(self._active.values())
        for plan in plans:
            try:
                self.cancel(plan)
            except Exception:
                _LOGGER.exception("A local provider stream did not stop cleanly.")

    def _stop(self, plan: AttemptPlan) -> Callable[[], AdapterStopEvidence]:
        return lambda: self.cancel(plan)


class LocalReplayProtector:
    """Keep encrypted local replay evidence before central accounting ingest."""

    def __init__(self, path: Path, key: bytes) -> None:
        self._journal = EncryptedFrameJournal(
            path,
            {"local-v1": key},
            "local-v1",
            trusted_root=path.parent,
        )
        self._journal.acquire_owner()
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, object]] = {}
        for record in self._journal.read_all():
            identity = record.get("event_id")
            if not isinstance(identity, str) or identity in self._records:
                raise RuntimeError("The local replay journal is invalid.")
            self._records[identity] = record

    def close(self) -> None:
        self._journal.close()

    def protect(self, event: CanonicalEvent, payload_sha256: bytes) -> str:
        encoded_payload = b64encode(event.payload).decode("ascii")
        expected = {
            "event_id": event.event_id,
            "source_node_id": event.source_node_id,
            "source_sequence": event.source_sequence,
            "payload_sha256": payload_sha256.hex(),
            "payload": encoded_payload,
        }
        with self._lock:
            existing = self._records.get(event.event_id)
            if existing is not None:
                if existing != expected:
                    raise RuntimeError("The local replay identity conflicts.")
            else:
                if (
                    hashlib.sha256(b64decode(encoded_payload)).digest()
                    != payload_sha256
                ):
                    raise RuntimeError("The local replay payload is invalid.")
                self._journal.append(expected)
                self._records[event.event_id] = expected
        return f"local-replay:{event.event_id}"


def _local_openrouter_transport(
    live_flag: str | None,
) -> httpx.BaseTransport | None:
    """Use real TLS only for the one exact local live-test flag."""
    if live_flag in {None, "", "0"}:
        return httpx.MockTransport(_local_openrouter)
    if live_flag == "1":
        return None
    message = "The local OpenRouter live-test flag is invalid."
    raise RuntimeError(message)


def install_local_runtime(
    app: FastAPI,
    *,
    database_url: str,
    digest_key: bytes,
    wrapping_key: bytes,
    idempotency_key: bytes,
    distribution_key: bytes,
    replay_key: bytes,
    replay_path: Path,
    admin_session: str,
    admin_csrf: str,
    production_administrator_authority: AdministratorAuthRepository | None = None,
    openrouter_live_flag: str | None = None,
) -> None:
    """Install all local MVP control-plane and data-plane components."""
    machine = MachineCredentialRepository(
        database_url,
        issuer="llmrouter-local-development",
        digest_keys={"local-v1": digest_key},
        current_digest_key_id="local-v1",
    )
    configuration = PostgresConfigurationRepository(
        database_url,
        schema_registry=SettingsSchemaRegistry(openrouter_registered_schemas()),
    )
    credentials = EncryptedCredentialRepository(
        database_url,
        wrapping_keys={"local-v1": wrapping_key},
        current_wrapping_key_id="local-v1",
        idempotency_digest_key=idempotency_key,
    )
    lifecycle = PostgresLifecycleRepository(database_url)
    execution = PostgresExecutionRepository(database_url)
    views = PostgresModelRequestViews(database_url)
    accounting = PostgresAccountingRepository(database_url)
    budget_repository = PostgresBudgetRepository(database_url)
    replay = LocalReplayProtector(replay_path, replay_key)
    local_authority = LocalAdministratorAuthority(admin_session, admin_csrf)
    authority = (
        local_authority
        if production_administrator_authority is None
        else DualAdministratorAuthority(
            local_authority, production_administrator_authority
        )
    )
    signer = RevisionAuthenticator(distribution_key)
    distribution = ConfigurationRevisionDistribution(signer)
    admission_repository = PostgresAdmissionRepository(
        database_url, distribution=distribution
    )
    admission = LocalDistributionAdmission(
        database_url, admission_repository, distribution, signer
    )
    routing_repository = PostgresRoutingRepository(database_url)
    budget = LocalBudgetGate(database_url, budget_repository)
    inputs = TransientModelInputRegistry(execution)

    def credential_source(plan: AttemptPlan) -> SecretLease:
        distributor = DataPlaneCredentialDistributor(
            database_url,
            wrapping_keys={"local-v1": wrapping_key},
            current_wrapping_key_id="local-v1",
            active_route_ids=frozenset({plan.provider_model_route_id}),
        )
        return distributor.secret_for_route(
            plan.provider_model_route_id,
            request_id=plan.request_id,
            now=_now(),
        )

    adapter = OpenRouterAdapter(
        requests=inputs.request_for_plan,
        credentials=credential_source,
        output=inputs.output_for_plan,
        transport=_local_openrouter_transport(openrouter_live_flag),
    )
    cancelable_adapter = LocalCancelableAdapter(
        adapter,
        confirm_submitted_stop=openrouter_live_flag in {None, "", "0"},
    )

    def account(plan: AttemptPlan, result: AdapterResult) -> None:
        reservation_id, accounting_scope_id = budget.evidence(plan)
        event_id = str(uuid.uuid5(uuid.UUID(plan.attempt_id), "accounting"))
        currency = plan.typed_prices[0].currency
        occurred_at = budget.finished_at(plan)
        event = AccountingEvent(
            event_id,
            event_id,
            plan.request_row_id,
            plan.service_id,
            plan.workspace_id,
            accounting_scope_id,
            AccountingSubjectKind.PROVIDER_ATTEMPT,
            plan.attempt_id,
            _accounting_outcome(result.outcome),
            currency,
            result.usage,
            occurred_at,
            price_version_id=plan.price_version_id,
            assignment_id=plan.assignment_id,
        )
        canonical = event.canonical_event(
            LOCAL_SOURCE_NODE_ID, _source_sequence(event.canonical_event_id)
        )
        with psycopg.connect(database_url, autocommit=True) as connection:
            CanonicalLedger(connection, replay).ingest(canonical)
        accounting.ingest(_system("accounting.ingest"), event)
        amount = sum(
            (
                usage.quantity * price.price / price.unit_quantity
                for usage in result.usage
                for price in plan.typed_prices
                if usage.unit is price.unit
            ),
            Decimal(0),
        )
        budget_repository.reconcile(
            _system("budget.reconcile"),
            reservation_id,
            accounting_event_id=event_id,
            actual_amount=amount,
            now=event.occurred_at,
        )

    def complete(plan: AttemptPlan, result: AdapterResult) -> None:
        context = _system("model.create", Scope(plan.service_id, plan.workspace_id))
        target = ExecutionTarget(ExecutionKind.MODEL, plan.request_id)
        with psycopg.connect(database_url) as connection:
            current = connection.execute(
                """SELECT state, state_revision
                   FROM router.logical_requests
                   WHERE request_id = %s AND service_id = %s
                     AND workspace_id IS NOT DISTINCT FROM %s""",
                (plan.request_id, plan.service_id, plan.workspace_id),
            ).fetchone()
        if current is None or current[0] not in {
            "admitted",
            "running",
            "cancel_requested",
        }:
            return
        if current[0] == "cancel_requested":
            return
        state = (
            ExecutionState.SUCCEEDED
            if result.outcome is AttemptOutcome.SUCCEEDED
            else ExecutionState.FAILED
        )
        execution.transition(
            context,
            target,
            expected_revision=int(current[1]),
            new_state=state,
            safe_error=None if result.failure is None else result.failure.error,
        )

    coordinator = RoutingCoordinator(
        routing_repository,
        eligibility=lambda _plan: None,
        budget=budget,
        adapter=lambda adapter_type: (
            cancelable_adapter
            if adapter_type == "openai_compatible.v1"
            else (_raise_adapter())
        ),
        accounting=account,
        completion=complete,
        clock=_now,
    )

    class LocalRoutingExecutor:
        def execute(
            self, context: RequestContext, *, request_id: str, owner_id: str
        ) -> AdapterResult:
            try:
                return coordinator.execute(
                    context, request_id=request_id, owner_id=owner_id
                )
            except Exception:
                _LOGGER.exception("Local routing failed without request content.")
                raise

    submitter = ThreadWorkSubmitter(maximum_workers=4)
    diagnostic_authenticator = TransientDiagnosticAuthenticator(machine)
    service = ModelRequestService(
        authenticator=diagnostic_authenticator,
        admission=admission,
        execution=execution,
        routing=LocalRoutingExecutor(),
        views=views,
        submit=submitter,
        owner_id="local-development-backend",
        inputs=inputs,
        active_stops=cancelable_adapter.active_stops,
    )
    install_model_request_service(app, service)
    install_administration_service(
        app,
        AdministrationService(
            authority=authority,
            configuration=configuration,
            credentials=credentials,
            lifecycle=lifecycle,
            requests=views,
            accounting=accounting,
            audit=PostgresAuditRepository(
                database_url,
                cursor_key=hmac.digest(
                    digest_key, b"llmrouter-audit-cursor-v1", "sha256"
                ),
            ),
            budgets=budget_repository,
            diagnostics=AdministratorDiagnosticRunner(
                routing_repository, service, diagnostic_authenticator
            ),
            machine=machine,
        ),
    )
    app.state.local_admin_authority = local_authority
    app.state.dual_administrator_authority = (
        production_administrator_authority is not None
    )
    app.state.local_adapter = adapter
    app.state.local_submitter = submitter
    app.include_router(_router)

    def shutdown_local_runtime() -> None:
        cancelable_adapter.close()
        try:
            submitter.close()
        finally:
            replay.close()

    app.router.add_event_handler("shutdown", shutdown_local_runtime)


@_router.get("/v1/admin/session", include_in_schema=False)
def local_admin_session(request: Request) -> Response:
    """Return CSRF only for the generated local administrator cookie."""
    if not _exact_local_request(request):
        return JSONResponse({"error": {"code": "invalid_session"}}, status_code=401)
    authority = request.app.state.local_admin_authority
    cookie = request.cookies.get(_COOKIE, "")
    if not authority.valid_session(cookie):
        return JSONResponse({"error": {"code": "invalid_session"}}, status_code=401)
    return JSONResponse(
        {"csrf_token": authority.csrf}, headers={"Cache-Control": "no-store"}
    )


@_router.post("/v1/admin/local-session", include_in_schema=False)
async def activate_local_admin_session(request: Request) -> Response:
    """Activate the generated localhost administrator session."""
    authority = request.app.state.local_admin_authority
    if (
        not _exact_local_request(request)
        or _single_header(request, b"origin") != LOCAL_ADMIN_ORIGIN
    ):
        return _local_activation_error(403)
    content_length = _single_header(request, b"content-length")
    content_type = _single_header(request, b"content-type")
    if (
        content_length is None
        or not content_length.isascii()
        or not content_length.isdecimal()
        or not 1 <= int(content_length) <= _MAXIMUM_LOCAL_ACTIVATION_BYTES
        or content_type is None
        or content_type.partition(";")[0].strip().lower() != "application/json"
        or request.headers.get("transfer-encoding") is not None
    ):
        return _local_activation_error(400)
    try:
        body = await request.body()
        if len(body) > _MAXIMUM_LOCAL_ACTIVATION_BYTES:
            return _local_activation_error(400)
        document = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        return _local_activation_error(400)
    secret = document.get("secret") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != {"secret"}
        or not isinstance(secret, str)
        or not _MINIMUM_LOCAL_SECRET_CHARACTERS
        <= len(secret)
        <= _MAXIMUM_LOCAL_SECRET_CHARACTERS
        or not authority.valid_session(secret)
    ):
        return _local_activation_error(401)
    response = JSONResponse(
        {"authenticated": True, "csrf_token": authority.csrf},
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        _COOKIE,
        secret,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@_router.head("/v1/admin/local-session", include_in_schema=False)
def local_admin_activation_capability(request: Request) -> Response:
    """Report only that the hidden localhost activation flow is installed."""
    if not _exact_local_request(request):
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def _exact_local_request(request: Request) -> bool:
    return (
        request.url.scheme == "http"
        and request.url.hostname == "127.0.0.1"
        and request.url.port == _LOCAL_ADMIN_PORT
    )


def _local_activation_error(status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": "local_administrator_activation_failed",
                "message": "The local administrator session was not activated.",
            }
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _single_header(request: Request, name: bytes) -> str | None:
    """Return one exact HTTP header, or reject a missing or duplicate value."""
    values = [
        value.decode("latin-1")
        for header_name, value in request.scope.get("headers", ())
        if header_name.lower() == name
    ]
    return values[0] if len(values) == 1 else None


def _local_openrouter(request: httpx.Request) -> httpx.Response:
    """Return a deterministic OpenRouter response without network access."""
    if request.url != "https://openrouter.ai/api/v1/chat/completions":
        return httpx.Response(404, json={"error": {"code": "not_found"}})
    if not request.headers.get("authorization", "").startswith("Bearer "):
        return httpx.Response(401, json={"error": {"code": "unauthorized"}})
    document = json.loads(request.content)
    if document.get("model") != "deepseek/deepseek-v4-flash":
        return httpx.Response(404, json={"error": {"code": "model_not_found"}})
    usage = {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    messages = document.get("messages", ())
    delayed = any(
        isinstance(message, dict)
        and isinstance(message.get("content"), str)
        and "Wait for" in message["content"]
        for message in messages
    )
    if document.get("stream") is True:
        documents = (
            {"choices": [{"delta": {"content": "local "}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "response"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage},
        )
        content = (
            "".join(
                f"data: {json.dumps(item, separators=(',', ':'))}\n\n"
                for item in documents
            )
            + "data: [DONE]\n\n"
        )
        stream = _DelayedOpenRouterStream(content.encode(), delayed=delayed)
        return httpx.Response(
            200, stream=stream, headers={"Content-Type": "text/event-stream"}
        )
    if delayed:
        stream = _DelayedOpenRouterStream(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "local response"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                }
            ).encode(),
            delayed=True,
        )
        return httpx.Response(
            200, stream=stream, headers={"Content-Type": "application/json"}
        )
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": "local response"}, "finish_reason": "stop"}
            ],
            "usage": usage,
        },
    )


class _DelayedOpenRouterStream(httpx.SyncByteStream):
    """Hold selected local responses until completion or a local stop."""

    def __init__(self, content: bytes, *, delayed: bool) -> None:
        self._content = content
        self._delayed = delayed
        self._closed = threading.Event()

    def __iter__(self) -> Iterator[bytes]:
        if self._delayed and self._closed.wait(timeout=30):
            return
        if not self._closed.is_set():
            yield self._content

    def close(self) -> None:
        self._closed.set()


def _system(operation: str, scope: Scope | None = None) -> RequestContext:
    return RequestContext(
        request_id=str(uuid.uuid4()),
        actor_kind=PrincipalKind.SYSTEM,
        actor_id="local-development-backend",
        authority_class=AuthorityClass.SYSTEM,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=None,
        operation=operation,
        scope=Scope() if scope is None else scope,
        authorized_at=_now(),
        recent_authentication_at=None,
        mutation=True,
    )


def _accounting_outcome(value: AttemptOutcome) -> AccountingOutcome:
    if value is AttemptOutcome.CANCELLED:
        return AccountingOutcome.FAILED
    return AccountingOutcome(value.value)


def _raise_adapter() -> Never:
    raise KeyError("The adapter type is unavailable.")


def _now() -> datetime:
    return datetime.now(UTC)


def _source_sequence(event_id: str) -> int:
    value = int.from_bytes(hashlib.sha256(event_id.encode()).digest()[:8], "big")
    return max(1, value & ((1 << 63) - 1))
