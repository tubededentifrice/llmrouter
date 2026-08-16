"""Fleet-wide PostgreSQL request admission and scoped status reads."""
# ruff: noqa: ARG001, E501, EM101, PLR0913, TRY003

from __future__ import annotations

import hashlib
import uuid
from contextlib import ExitStack
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
)
from llmrouter_backend.configuration.distribution import (
    AdmissionDistributionSnapshot,
    ConfigurationDistributionError,
    ConfigurationRevisionDistribution,
    CredentialGeneration,
    DistributionScope,
)
from llmrouter_backend.execution.model import TerminalError

from .errors import AdmissionError, AdmissionErrorCode
from .model import (
    DEFAULT_MAXIMUM_FUTURE_SKEW,
    DEFAULT_MAXIMUM_INITIAL_AGE,
    FINGERPRINT_VERSION,
    AdmissionReceipt,
    AdmissionRequest,
    AdmissionResult,
    RequestKind,
    RequestState,
    RequestStatus,
    uuidv7_time,
)

if TYPE_CHECKING:
    from datetime import datetime

    from psycopg import Connection

    from llmrouter_backend.authority import RequestContext


class PostgresAdmissionRepository:
    """Atomically create or find scoped logical request bindings."""

    def __init__(
        self,
        database_url: str,
        *,
        distribution: ConfigurationRevisionDistribution,
        maximum_initial_age: timedelta = DEFAULT_MAXIMUM_INITIAL_AGE,
        maximum_future_skew: timedelta = DEFAULT_MAXIMUM_FUTURE_SKEW,
    ) -> None:
        """Use central state and authenticated node-local revisions."""
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        if maximum_initial_age <= timedelta(0) or maximum_future_skew < timedelta(0):
            raise ValueError("The UUIDv7 time limits are invalid.")
        if not isinstance(distribution, ConfigurationRevisionDistribution):
            raise TypeError("A configuration revision distribution is required.")
        self._database_url = database_url
        self._maximum_initial_age = maximum_initial_age
        self._maximum_future_skew = maximum_future_skew
        self._distribution = distribution

    def admit(
        self,
        context: RequestContext,
        request: AdmissionRequest,
        *,
        now: datetime,
    ) -> AdmissionResult:
        """Return one committed create-or-equal-replay admission."""
        _require_create_authority(context, request)
        _require_aware(now)
        if request.fingerprint.service_id != context.scope.service_id or (
            request.fingerprint.workspace_id != context.scope.workspace_id
        ):
            raise AdmissionError(
                AdmissionErrorCode.INSUFFICIENT_SCOPE, context.request_id
            )
        digest = request.fingerprint.sha256()
        with (
            ExitStack() as distribution_stack,
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
        ):
            with connection.transaction():
                _lock_identity(connection, context, request.request_id)
                existing = _select_binding(connection, context, request.request_id)
                if existing is not None:
                    if (
                        existing["terminal_at"] is not None
                        and existing["expires_at"] is not None
                        and existing["expires_at"] <= now
                    ):
                        raise AdmissionError(
                            AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED,
                            context.request_id,
                        )
                    if (
                        existing["fingerprint_version"] != FINGERPRINT_VERSION
                        or bytes(existing["fingerprint_sha256"]) != digest
                    ):
                        raise AdmissionError(
                            AdmissionErrorCode.REQUEST_IDENTITY_CONFLICT,
                            context.request_id,
                        )
                    result = AdmissionResult(_receipt(existing), created=False)
                else:
                    request_time = uuidv7_time(request.request_id)
                    if (
                        request_time < now - self._maximum_initial_age
                        or request_time > now + self._maximum_future_skew
                    ):
                        raise AdmissionError(
                            AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED,
                            context.request_id,
                        )
                    ancestor_service_ids = _require_active_scope(connection, context)
                    distributed = _enter_distribution_admission(
                        distribution_stack,
                        self._distribution,
                        context,
                        now=now,
                        ancestor_service_ids=ancestor_service_ids,
                    )
                    target = _resolve_target(connection, context, request, now=now)
                    _require_distributed_revision(
                        distributed,
                        str(target[0]),
                        _configuration_revision_digest(
                            connection, target[0], context.request_id
                        ),
                        context.request_id,
                    )
                    attachments = _validated_attachments(
                        connection, context, request, now=now
                    )
                    row_id = uuid.uuid4()
                    locations = _locations(request.kind, request.request_id, context)
                    _lock_capture_configuration(connection, context)
                    capture_policy, capture_reason, capture_expiry = (
                        _resolve_capture_snapshot(
                            connection, context, request, admitted_at=now
                        )
                    )
                    if (
                        request.captured_content_expires_at is not None
                        and request.captured_content_expires_at != capture_expiry
                    ):
                        raise AdmissionError(
                            AdmissionErrorCode.INVALID_REQUEST,
                            context.request_id,
                        )
                    capture_enabled = capture_policy != "disabled"
                    row = connection.execute(
                        """
                            INSERT INTO router.logical_requests (
                                row_id, request_id, request_kind, service_id,
                                workspace_id, assignment_id, exact_route_id,
                                configuration_revision_id, operation_name,
                                contract_major, fingerprint_version,
                                fingerprint_sha256, data_profile, capture_enabled,
                                capture_pressure_reason, capture_policy,
                                capture_reason, captured_content_expires_at,
                                admitted_at, last_transition_at, status_location,
                                cancel_location, events_location
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                            ) RETURNING *
                            """,
                        (
                            row_id,
                            request.request_id,
                            request.kind.value,
                            context.scope.service_id,
                            context.scope.workspace_id,
                            target[1],
                            target[2],
                            target[0],
                            request.fingerprint.operation,
                            request.fingerprint.contract_major,
                            FINGERPRINT_VERSION,
                            digest,
                            request.fingerprint.data_profile,
                            capture_enabled,
                            "spool_pressure"
                            if capture_reason == "spool_pressure"
                            else None,
                            capture_policy,
                            capture_reason,
                            capture_expiry,
                            now,
                            now,
                            *locations,
                        ),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError(
                            "The durable admission insert did not return a row."
                        )
                    _snapshot_routing_chain(
                        connection,
                        request_row_id=row_id,
                        request_id=request.request_id,
                        service_id=context.scope.service_id,
                        workspace_id=context.scope.workspace_id,
                        assignment_revision_id=target[0],
                        assignment_id=target[1],
                        exact_route_id=target[2],
                        admitted_at=now,
                    )
                    _require_distributed_credentials(
                        distributed,
                        _snapshot_credential_generations(connection, row_id),
                        context.request_id,
                    )
                    for ordinal, attachment in enumerate(attachments, start=1):
                        connection.execute(
                            """
                                INSERT INTO router.request_attachments (
                                    request_row_id, service_id, workspace_id,
                                    attachment_id, ordinal, content_sha256,
                                    byte_length
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                                """,
                            (
                                row_id,
                                context.scope.service_id,
                                context.scope.workspace_id,
                                attachment[0],
                                ordinal,
                                attachment[1],
                                attachment[2],
                            ),
                        )
                    result = AdmissionResult(_receipt(row), created=True)
            if connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                raise RuntimeError("The durable admission transaction did not commit.")
        return result

    def status(
        self, context: RequestContext, request_id: str, *, now: datetime
    ) -> RequestStatus:
        """Return status only for the exact original service and workspace."""
        _require_read_authority(context)
        _require_aware(now)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = _select_binding(connection, context, request_id)
        if row is None or (
            row["terminal_at"] is not None
            and row["expires_at"] is not None
            and row["expires_at"] <= now
        ):
            raise AdmissionError(
                AdmissionErrorCode.REQUEST_NOT_FOUND, context.request_id
            )
        expected_kind = {
            "model.read": RequestKind.MODEL.value,
            "tool.read": RequestKind.SHARED_TOOL.value,
        }[context.operation]
        if row["request_kind"] != expected_kind:
            raise AdmissionError(
                AdmissionErrorCode.REQUEST_NOT_FOUND, context.request_id
            )
        return RequestStatus(
            receipt=_receipt(row),
            state=RequestState(row["state"]),
            state_revision=int(row["state_revision"]),
            last_transition_at=row["last_transition_at"],
            terminal_at=row["terminal_at"],
            configuration_revision_id=str(row["configuration_revision_id"]),
            assignment_id=(
                None if row["assignment_id"] is None else str(row["assignment_id"])
            ),
            exact_route_id=(
                None if row["exact_route_id"] is None else str(row["exact_route_id"])
            ),
            safe_error=TerminalError.from_document(row["safe_error"]),
            partial_output=bool(row["partial_output"]),
            committed_effects=bool(row["committed_effect"]),
        )


def _require_create_authority(
    context: RequestContext, request: AdmissionRequest
) -> None:
    operations = {
        RequestKind.MODEL: "model.create",
        RequestKind.SHARED_TOOL: "tool.create",
    }
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation == operations[request.kind]
        and context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise AdmissionError(AdmissionErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_read_authority(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation in {"model.read", "tool.read"}
        and not context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise AdmissionError(AdmissionErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _lock_identity(
    connection: Connection[Any], context: RequestContext, request_id: str
) -> None:
    key = f"admission:{context.scope.service_id}:{context.scope.workspace_id or '-'}:{request_id}"
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def _select_binding(
    connection: Connection[Any], context: RequestContext, request_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM router.logical_requests
        WHERE service_id = %s AND workspace_id IS NOT DISTINCT FROM %s
          AND request_id = %s
        """,
        (context.scope.service_id, context.scope.workspace_id, request_id),
    ).fetchone()
    return None if row is None else dict(row)


def _resolve_target(
    connection: Connection[Any],
    context: RequestContext,
    request: AdmissionRequest,
    *,
    now: datetime,
) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]:
    if request.exact_route_id is not None:
        row = connection.execute(
            """
            WITH RECURSIVE service_chain AS (
                SELECT id, parent_service_id, 1 AS depth
                FROM router.services WHERE id = %s
              UNION ALL
                SELECT parent.id, parent.parent_service_id, child.depth + 1
                FROM router.services AS parent
                JOIN service_chain AS child ON child.parent_service_id = parent.id
            ), scope_revisions AS (
                SELECT active.revision_id, revision.content, 0 AS priority
                FROM router.active_configurations AS active
                JOIN router.configuration_revisions AS revision
                  ON revision.id = active.revision_id
                WHERE active.scope_kind = 'workspace' AND active.service_id = %s
                  AND active.workspace_id = %s
              UNION ALL
                SELECT active.revision_id, revision.content, chain.depth
                FROM service_chain AS chain
                JOIN router.active_configurations AS active
                  ON active.scope_kind = 'service' AND active.service_id = chain.id
                JOIN router.configuration_revisions AS revision
                  ON revision.id = active.revision_id
              UNION ALL
                SELECT active.revision_id, revision.content, 1000000
                FROM router.active_configurations AS active
                JOIN router.configuration_revisions AS revision
                  ON revision.id = active.revision_id
                WHERE active.scope_kind = 'global'
            )
            SELECT route.current_revision, route.id
            FROM router.provider_model_routes AS route
            JOIN router.provider_instances AS instance
              ON instance.id = route.provider_instance_id
            JOIN scope_revisions AS owner
              ON owner.revision_id = route.current_revision
            WHERE route.id = %s AND route.state = 'active'
              AND instance.state = 'active'
              AND (route.owner_kind = 'global' OR route.owner_service_id IN (
                  SELECT id FROM service_chain
              ))
              AND (route.eligible_service_ids = '{}'::uuid[] OR EXISTS (
                  SELECT 1 FROM service_chain
                  WHERE id = ANY(route.eligible_service_ids)
              ))
              AND NOT EXISTS (
                  SELECT 1 FROM scope_revisions AS child
                  WHERE child.priority < owner.priority
                    AND child.content->'inherited_disables' @>
                        jsonb_build_array(jsonb_build_object(
                            'resource_kind', 'provider_model_route',
                            'resource_id', route.id::text
                        ))
              )
              AND router.provider_resource_is_enabled(
                  'provider_model_route', route.id, %s, %s
              )
              AND router.provider_resource_is_enabled(
                  'provider_instance', instance.id, %s, %s
              )
            FOR SHARE OF route
            """,
            (
                context.scope.service_id,
                context.scope.service_id,
                context.scope.workspace_id,
                request.exact_route_id,
                context.scope.service_id,
                context.scope.workspace_id,
                context.scope.service_id,
                context.scope.workspace_id,
            ),
        ).fetchone()
        if row is None or row["current_revision"] is None:
            raise AdmissionError(
                AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE, context.request_id
            )
        _authorize_diagnostic_route(
            connection,
            context,
            request=request,
            route_revision_id=row["current_revision"],
            now=now,
        )
        return row["current_revision"], None, row["id"]
    row = connection.execute(
        """
        WITH RECURSIVE service_chain AS (
            SELECT id, parent_service_id, 1 AS depth FROM router.services WHERE id = %s
          UNION ALL
            SELECT parent.id, parent.parent_service_id, child.depth + 1
            FROM router.services AS parent
            JOIN service_chain AS child ON child.parent_service_id = parent.id
        ), scope_revisions AS (
            SELECT active.revision_id, 0 AS priority
            FROM router.active_configurations AS active
            WHERE active.scope_kind = 'workspace' AND active.service_id = %s
              AND active.workspace_id = %s
          UNION ALL
            SELECT active.revision_id, chain.depth
            FROM service_chain AS chain
            JOIN router.active_configurations AS active
              ON active.scope_kind = 'service' AND active.service_id = chain.id
          UNION ALL
            SELECT active.revision_id, 1000000
            FROM router.active_configurations AS active WHERE active.scope_kind = 'global'
        )
        SELECT assignment.configuration_revision_id, assignment.id,
               assignment.state
        FROM scope_revisions AS scope
        JOIN router.assignment_definitions AS assignment
          ON assignment.configuration_revision_id = scope.revision_id
        WHERE assignment.stable_name = %s
        ORDER BY scope.priority LIMIT 1
        """,
        (
            context.scope.service_id,
            context.scope.service_id,
            context.scope.workspace_id,
            request.assignment,
        ),
    ).fetchone()
    if row is None or row["state"] != "active":
        raise AdmissionError(
            AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE, context.request_id
        )
    return row["configuration_revision_id"], row["id"], None


def _authorize_diagnostic_route(
    connection: Connection[Any],
    context: RequestContext,
    *,
    request: AdmissionRequest,
    route_revision_id: uuid.UUID,
    now: datetime,
) -> None:
    """Consume one exact short-lived grant and write its permitted audit event."""
    if request.exact_route_id is None or request.diagnostic_grant is None:
        message = "The diagnostic admission request is incomplete."
        raise RuntimeError(message)
    time_row = connection.execute("SELECT transaction_timestamp() AS value").fetchone()
    if time_row is None:
        message = "The scalar database query did not return a row."
        raise RuntimeError(message)
    authorized_at = time_row["value"]
    grant = connection.execute(
        """
        SELECT diagnostic_grant.grant_id
        FROM router.diagnostic_route_grants AS diagnostic_grant
        JOIN router.provider_model_routes AS route
          ON route.id = diagnostic_grant.exact_route_id
        JOIN router.provider_instances AS instance
          ON instance.id = route.provider_instance_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = instance.credential_id
        WHERE diagnostic_grant.grant_sha256 = %s
          AND diagnostic_grant.service_id = %s
          AND diagnostic_grant.workspace_id IS NOT DISTINCT FROM %s
          AND diagnostic_grant.exact_route_id = %s
          AND diagnostic_grant.route_configuration_revision_id = %s
          AND diagnostic_grant.created_at <= %s
          AND diagnostic_grant.expires_at > %s
          AND route.current_revision = diagnostic_grant.route_configuration_revision_id
          AND route.state = 'active' AND instance.state = 'active'
          AND credential.state = 'active'
          AND credential.id = diagnostic_grant.credential_id
          AND credential.generation = diagnostic_grant.credential_generation
          AND credential.current_revision = diagnostic_grant.credential_revision_id
          AND router.active_request_scope(
              diagnostic_grant.service_id, diagnostic_grant.workspace_id
          )
          AND router.provider_route_is_eligible(route.id, diagnostic_grant.service_id)
          AND router.provider_resource_is_enabled(
              'provider_model_route', route.id,
              diagnostic_grant.service_id, diagnostic_grant.workspace_id
          )
          AND router.provider_resource_is_enabled(
              'provider_instance', instance.id,
              diagnostic_grant.service_id, diagnostic_grant.workspace_id
          )
        FOR UPDATE OF diagnostic_grant
        """,
        (
            hashlib.sha256(request.diagnostic_grant.encode("ascii")).digest(),
            context.scope.service_id,
            context.scope.workspace_id,
            request.exact_route_id,
            route_revision_id,
            authorized_at,
            authorized_at,
        ),
    ).fetchone()
    if (
        grant is None
        or connection.execute(
            "SELECT 1 FROM router.diagnostic_route_authorizations WHERE grant_id = %s",
            (None if grant is None else grant["grant_id"],),
        ).fetchone()
        is not None
    ):
        raise AdmissionError(
            AdmissionErrorCode.DIAGNOSTIC_PERMISSION_REQUIRED,
            request.request_id,
        )
    event_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            service_id, workspace_id, action, permission_result, safe_details,
            occurred_at
        ) VALUES (%s, 'security', %s, %s, %s, %s, %s,
                  'diagnostic.route.use', 'permitted', %s, %s)
        """,
        (
            event_id,
            context.actor_kind.value,
            context.actor_id,
            context.authority_class.value,
            context.scope.service_id,
            context.scope.workspace_id,
            Jsonb(
                {
                    "diagnostic_grant_id": str(grant["grant_id"]),
                    "exact_route_id": request.exact_route_id,
                    "request_id": request.request_id,
                    "route_configuration_revision_id": str(route_revision_id),
                }
            ),
            authorized_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO router.diagnostic_route_authorizations (
            authorization_id, grant_id, request_id, service_id, workspace_id,
            exact_route_id, route_configuration_revision_id,
            authorized_by_kind, authorized_by_id, authorized_at,
            use_audit_event_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid.uuid4(),
            grant["grant_id"],
            request.request_id,
            context.scope.service_id,
            context.scope.workspace_id,
            request.exact_route_id,
            route_revision_id,
            context.actor_kind.value,
            context.actor_id,
            authorized_at,
            event_id,
        ),
    )


def _snapshot_routing_chain(
    connection: Connection[Any],
    *,
    request_row_id: uuid.UUID,
    request_id: str,
    service_id: str,
    workspace_id: str | None,
    assignment_revision_id: uuid.UUID,
    assignment_id: uuid.UUID | None,
    exact_route_id: uuid.UUID | None,
    admitted_at: datetime,
) -> None:
    """Store the complete ordered route chain in the admission transaction."""
    connection.execute(
        """
        WITH candidates AS (
            SELECT provider_model_route_id
            FROM router.assignment_candidates WHERE assignment_id = %s
          UNION ALL
            SELECT %s::uuid WHERE %s::uuid IS NULL
        )
        SELECT route.id
        FROM candidates AS candidate
        JOIN router.provider_model_routes AS route
          ON route.id = candidate.provider_model_route_id
        JOIN router.provider_instances AS instance
          ON instance.id = route.provider_instance_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = instance.credential_id
        FOR SHARE OF route, instance, credential
        """,
        (assignment_id, exact_route_id, assignment_id),
    ).fetchall()
    rows = connection.execute(
        """
        WITH candidates AS (
            SELECT candidate.ordinal, candidate.provider_model_route_id,
                   candidate.attempt_timeout_ms, candidate.candidate_policy
            FROM router.assignment_candidates AS candidate
            WHERE candidate.assignment_id = %(assignment_id)s
              AND candidate.configuration_revision_id = %(assignment_revision_id)s
          UNION ALL
            SELECT 1, %(exact_route_id)s::uuid, 120000, '{}'::jsonb
            WHERE %(assignment_id)s::uuid IS NULL
        ), values AS (
            SELECT %(request_row_id)s::uuid AS request_row_id,
                   candidate.ordinal AS candidate_ordinal,
                   %(assignment_revision_id)s::uuid AS assignment_revision_id,
                   candidate.attempt_timeout_ms, candidate.candidate_policy,
                   route.current_revision AS route_configuration_revision_id,
                   route.id AS provider_model_route_id,
                   route.generation AS route_generation,
                   instance.id AS provider_instance_id,
                   instance.generation AS provider_instance_generation,
                   COALESCE(instance.current_revision, route.current_revision)
                       AS instance_configuration_revision_id,
                   credential.id AS credential_id,
                   credential.generation AS credential_generation,
                   credential.current_revision AS credential_revision_id,
                   price.price_version_id, instance.adapter_type_id,
                   instance.endpoint_origin, route.wire_model, route.capabilities,
                   instance.settings AS instance_settings,
                   route.settings AS route_settings, prices.typed_prices
            FROM candidates AS candidate
            JOIN router.provider_model_routes AS route
              ON route.id = candidate.provider_model_route_id
            JOIN router.provider_instances AS instance
              ON instance.id = route.provider_instance_id
            JOIN router.encrypted_credentials AS credential
              ON credential.id = instance.credential_id
                JOIN router.configuration_price_bindings AS price
                  ON price.configuration_revision_id =
                     %(assignment_revision_id)s::uuid
                 AND price.provider_model_route_id = route.id
            CROSS JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'unit', component.unit_name,
                    'price', component.unit_price::text,
                    'currency', version.currency,
                    'raw_source_value', component.raw_source_value,
                    'unit_quantity', component.unit_quantity::text
                ) ORDER BY component.unit_name) AS typed_prices
                FROM router.route_price_components AS component
                JOIN router.route_price_versions AS version
                  ON version.id = component.price_version_id
                WHERE component.price_version_id = price.price_version_id
            ) AS prices
            WHERE route.state = 'active' AND instance.state = 'active'
              AND credential.state = 'active'
              AND router.provider_route_is_eligible(route.id, %(service_id)s)
              AND router.provider_resource_is_enabled(
                  'provider_model_route', route.id,
                  %(service_id)s, %(workspace_id)s
              )
              AND router.provider_resource_is_enabled(
                  'provider_instance', instance.id,
                  %(service_id)s, %(workspace_id)s
              )
        ), documents AS (
            SELECT values.*, jsonb_build_object(
                'request_row_id', request_row_id,
                'candidate_ordinal', candidate_ordinal,
                'assignment_revision_id', assignment_revision_id,
                'attempt_timeout_ms', attempt_timeout_ms,
                'candidate_policy', candidate_policy,
                'route_configuration_revision_id', route_configuration_revision_id,
                'provider_model_route_id', provider_model_route_id,
                'route_generation', route_generation,
                'provider_instance_id', provider_instance_id,
                'provider_instance_generation', provider_instance_generation,
                'instance_configuration_revision_id', instance_configuration_revision_id,
                'credential_id', credential_id,
                'credential_generation', credential_generation,
                'credential_revision_id', credential_revision_id,
                'price_version_id', price_version_id,
                'adapter_type_id', adapter_type_id,
                'endpoint_origin', endpoint_origin,
                'wire_model', wire_model,
                'capabilities', capabilities,
                'instance_settings', instance_settings,
                'route_settings', route_settings,
                'typed_prices', typed_prices
            ) AS document
            FROM values
        )
        INSERT INTO router.provider_route_execution_snapshots (
            request_row_id, candidate_ordinal, assignment_revision_id,
            attempt_timeout_ms, candidate_policy, content_sha256,
            route_configuration_revision_id, provider_model_route_id,
            route_generation, provider_instance_id, provider_instance_generation,
            instance_configuration_revision_id, credential_id,
            credential_generation, credential_revision_id, price_version_id,
            adapter_type_id, endpoint_origin, wire_model, capabilities,
            instance_settings, route_settings, typed_prices, created_at
        )
        SELECT request_row_id, candidate_ordinal, assignment_revision_id,
               attempt_timeout_ms, candidate_policy,
               sha256(convert_to(document::text, 'UTF8')),
               route_configuration_revision_id, provider_model_route_id,
               route_generation, provider_instance_id, provider_instance_generation,
               instance_configuration_revision_id, credential_id,
               credential_generation, credential_revision_id, price_version_id,
               adapter_type_id, endpoint_origin, wire_model, capabilities,
               instance_settings, route_settings, typed_prices, %(admitted_at)s
        FROM documents
        ORDER BY candidate_ordinal
        RETURNING candidate_ordinal
        """,
        {
            "request_row_id": request_row_id,
            "service_id": service_id,
            "workspace_id": workspace_id,
            "assignment_revision_id": assignment_revision_id,
            "assignment_id": assignment_id,
            "exact_route_id": exact_route_id,
            "admitted_at": admitted_at,
        },
    ).fetchall()
    expected = 1
    if assignment_id is not None:
        expected_row = connection.execute(
            "SELECT count(*) AS candidate_count FROM router.assignment_candidates WHERE assignment_id = %s",
            (assignment_id,),
        ).fetchone()
        if expected_row is None:
            message = "A required database row is missing."
            raise RuntimeError(message)
        expected = expected_row["candidate_count"]
    ordinals = sorted(row["candidate_ordinal"] for row in rows)
    if len(rows) != expected or ordinals != list(range(1, expected + 1)):
        raise AdmissionError(AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE, request_id)


def _enter_distribution_admission(
    stack: ExitStack,
    distribution: ConfigurationRevisionDistribution,
    context: RequestContext,
    *,
    now: datetime,
    ancestor_service_ids: tuple[str, ...],
) -> AdmissionDistributionSnapshot:
    service_id = context.scope.service_id
    if service_id is None:
        raise AdmissionError(AdmissionErrorCode.INSUFFICIENT_SCOPE, context.request_id)
    try:
        return stack.enter_context(
            distribution.admission(
                DistributionScope(service_id, context.scope.workspace_id),
                now=now,
                ancestor_service_ids=ancestor_service_ids,
            )
        )
    except ConfigurationDistributionError as error:
        raise AdmissionError(
            AdmissionErrorCode.CONFIGURATION_UNAVAILABLE, context.request_id
        ) from error


def _require_distributed_revision(
    snapshot: AdmissionDistributionSnapshot,
    revision_id: str,
    content_sha256: bytes,
    request_id: str,
) -> None:
    try:
        snapshot.require_revision(revision_id, content_sha256)
    except ConfigurationDistributionError as error:
        raise AdmissionError(
            AdmissionErrorCode.CONFIGURATION_UNAVAILABLE, request_id
        ) from error


def _require_distributed_credentials(
    snapshot: AdmissionDistributionSnapshot,
    credentials: tuple[CredentialGeneration, ...],
    request_id: str,
) -> None:
    try:
        snapshot.require_credentials(credentials)
    except ConfigurationDistributionError as error:
        raise AdmissionError(
            AdmissionErrorCode.CONFIGURATION_UNAVAILABLE, request_id
        ) from error


def _configuration_revision_digest(
    connection: Connection[Any], revision_id: uuid.UUID, request_id: str
) -> bytes:
    row = connection.execute(
        """SELECT content_sha256 FROM router.configuration_revisions
           WHERE id = %s FOR SHARE""",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise AdmissionError(AdmissionErrorCode.CONFIGURATION_UNAVAILABLE, request_id)
    return bytes(row["content_sha256"])


def _snapshot_credential_generations(
    connection: Connection[Any], request_row_id: uuid.UUID
) -> tuple[CredentialGeneration, ...]:
    rows = connection.execute(
        """
        SELECT DISTINCT snapshot.credential_id::text AS credential_id,
               snapshot.credential_generation,
               credential.owner_service_id::text AS owner_service_id
        FROM router.provider_route_execution_snapshots AS snapshot
        JOIN router.encrypted_credentials AS credential
          ON credential.id = snapshot.credential_id
        WHERE snapshot.request_row_id = %s
        ORDER BY snapshot.credential_id::text, snapshot.credential_generation
        """,
        (request_row_id,),
    ).fetchall()
    return tuple(
        CredentialGeneration(
            row["credential_id"],
            int(row["credential_generation"]),
            row["owner_service_id"],
        )
        for row in rows
    )


def _require_active_scope(
    connection: Connection[Any], context: RequestContext
) -> tuple[str, ...]:
    services = connection.execute(
        """WITH RECURSIVE service_chain AS (
               SELECT id, parent_service_id
               FROM router.services WHERE id = %s
             UNION ALL
               SELECT parent.id, parent.parent_service_id
               FROM router.services AS parent
               JOIN service_chain AS child ON child.parent_service_id = parent.id
           )
           SELECT service.id::text AS service_id, service.state
           FROM router.services AS service
           JOIN service_chain AS chain ON chain.id = service.id
           FOR SHARE OF service""",
        (context.scope.service_id,),
    ).fetchall()
    if not services or any(service["state"] != "active" for service in services):
        raise AdmissionError(
            AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE, context.request_id
        )
    service_ids = tuple(service["service_id"] for service in services)
    if context.scope.workspace_id is None:
        return service_ids
    workspace = connection.execute(
        """SELECT state FROM router.workspaces
           WHERE id = %s AND service_id = %s FOR SHARE""",
        (context.scope.workspace_id, context.scope.service_id),
    ).fetchone()
    if workspace is None or workspace["state"] != "active":
        raise AdmissionError(
            AdmissionErrorCode.WORKSPACE_UNAVAILABLE, context.request_id
        )
    return service_ids


def _validated_attachments(
    connection: Connection[Any],
    context: RequestContext,
    request: AdmissionRequest,
    *,
    now: datetime,
) -> list[tuple[uuid.UUID, bytes, int]]:
    result: list[tuple[uuid.UUID, bytes, int]] = []
    for reference in sorted(
        request.fingerprint.attachments, key=lambda item: item.attachment_id
    ):
        row = connection.execute(
            """
            SELECT attachment.id, attachment.content_sha256, attachment.byte_length
            FROM router.attachments AS attachment
            JOIN router.attachment_status AS status
              ON status.attachment_id = attachment.id
            WHERE attachment.id = %s AND attachment.service_id = %s
              AND attachment.workspace_id IS NOT DISTINCT FROM %s
              AND attachment.content_sha256 = %s AND attachment.media_type = %s
              AND attachment.byte_length = %s AND attachment.expires_at > %s
              AND status.state = 'ready'
            FOR SHARE OF attachment, status
            """,
            (
                reference.attachment_id,
                context.scope.service_id,
                context.scope.workspace_id,
                bytes.fromhex(reference.sha256),
                reference.media_type,
                reference.byte_length,
                now,
            ),
        ).fetchone()
        if row is None:
            raise AdmissionError(
                AdmissionErrorCode.ATTACHMENT_INVALID, context.request_id
            )
        result.append(
            (
                row["id"],
                bytes(row["content_sha256"]),
                int(row["byte_length"]),
            )
        )
    return result


def _resolve_capture_snapshot(
    connection: Connection[Any],
    context: RequestContext,
    request: AdmissionRequest,
    *,
    admitted_at: datetime,
) -> tuple[str, str, datetime | None]:
    if request.capture_reason == "spool_pressure":
        return "disabled", "spool_pressure", None
    rows = connection.execute(
        """
        SELECT DISTINCT ON (scope_kind) scope_kind, policy,
               minimum_policy, maximum_policy
        FROM router.capture_policies
        WHERE effective_at <= %s AND (
            scope_kind = 'global'
            OR (scope_kind = 'service' AND service_id = %s)
            OR (scope_kind = 'workspace' AND service_id = %s AND workspace_id = %s)
        )
        ORDER BY scope_kind, revision DESC
        """,
        (
            admitted_at,
            context.scope.service_id,
            context.scope.service_id,
            context.scope.workspace_id,
        ),
    ).fetchall()
    by_scope = {row["scope_kind"]: row for row in rows}
    global_row = by_scope.get("global")
    if global_row is None:
        raise AdmissionError(AdmissionErrorCode.INVALID_REQUEST, context.request_id)
    selected = (
        by_scope["workspace"]
        if context.scope.workspace_id is not None and "workspace" in by_scope
        else by_scope.get("service", global_row)
    )
    order = {"disabled": 0, "metadata_only": 1, "complete": 2}
    policy = selected["policy"]
    if (
        not order[global_row["minimum_policy"]]
        <= order[policy]
        <= order[global_row["maximum_policy"]]
    ):
        raise AdmissionError(AdmissionErrorCode.INVALID_REQUEST, context.request_id)
    if policy == "disabled":
        return policy, "configured", None
    retention = connection.execute(
        """
        SELECT retention_days FROM router.retention_policies
        WHERE data_class = 'captured_content' AND effective_at <= %s AND (
            scope_kind = 'global'
            OR (scope_kind = 'service' AND service_id = %s)
            OR (scope_kind = 'workspace' AND service_id = %s AND workspace_id = %s)
        )
        ORDER BY CASE scope_kind
            WHEN 'workspace' THEN 3 WHEN 'service' THEN 2 ELSE 1 END DESC,
            revision DESC
        LIMIT 1 FOR SHARE
        """,
        (
            admitted_at,
            context.scope.service_id,
            context.scope.service_id,
            context.scope.workspace_id,
        ),
    ).fetchone()
    if retention is None:
        raise AdmissionError(AdmissionErrorCode.INVALID_REQUEST, context.request_id)
    return (
        policy,
        "configured",
        admitted_at + timedelta(days=retention["retention_days"]),
    )


def _lock_capture_configuration(
    connection: Connection[Any], context: RequestContext
) -> None:
    scopes: list[tuple[str, str | None, str | None]] = [("global", None, None)]
    if context.scope.service_id is not None:
        scopes.append(("service", context.scope.service_id, None))
    if context.scope.workspace_id is not None:
        scopes.append(
            ("workspace", context.scope.service_id, context.scope.workspace_id)
        )
    for namespace in ("capture-policy", "retention-policy"):
        for kind, service_id, workspace_id in scopes:
            scope_key = ":".join(
                (namespace, kind, service_id or "-", workspace_id or "-")
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (scope_key,),
            )


def _locations(
    kind: RequestKind, request_id: str, context: RequestContext
) -> tuple[str, str | None, str | None]:
    collection = {
        RequestKind.MODEL: "model-requests",
        RequestKind.SHARED_TOOL: "shared-tool-requests",
    }[kind]
    base = f"/v1/{collection}/{request_id}"
    return (
        base,
        f"{base}/cancel",
        None if kind is RequestKind.SHARED_TOOL else f"{base}/events",
    )


def _receipt(row: dict[str, Any]) -> AdmissionReceipt:
    enabled = bool(row["capture_enabled"])
    return AdmissionReceipt(
        request_id=str(row["request_id"]),
        admitted_at=row["admitted_at"],
        state=RequestState.ADMITTED,
        state_revision=1,
        status_url=row["status_location"],
        cancel_url=row["cancel_location"],
        events_url=row["events_location"],
        capture_enabled=enabled,
        capture_reason=row["capture_reason"],
        capture_policy=row["capture_policy"],
        captured_content_expires_at=row["captured_content_expires_at"],
    )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("The admission time must include a time zone.")
