"""Atomic PostgreSQL configuration publication and resolution."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import psycopg
from psycopg.types.json import Jsonb

from llmrouter_backend.accounting.model import (
    PriceComponent,
    SynchronizationState,
    UsageUnit,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    ScopeKind,
)

from .errors import ConfigurationError, ConfigurationErrorCode, ValidationIssue
from .model import (
    Assignment,
    AssignmentCandidate,
    CatalogEntry,
    CatalogKind,
    ConfigurationScope,
    ConfigurationState,
    ConfigurationWriteResult,
    DistributionState,
    EffectiveConfiguration,
    EffectiveItem,
    InheritedDisable,
    PriceAuthority,
    PriceAuthorityMode,
    ProviderInstance,
    ProviderModelRoute,
    RegisteredDocument,
    ResourceKind,
    RevisionLayer,
    ScopeConfiguration,
)
from .resolver import resolve_configuration, validate_layers
from .schema import SettingsSchemaRegistry, validate_endpoint

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from psycopg import Connection

    from llmrouter_backend.authority import RequestContext

_MAXIMUM_REASON_CHARACTERS = 500
_MINIMUM_IDEMPOTENCY_KEY_CHARACTERS = 16
_MAXIMUM_IDEMPOTENCY_KEY_CHARACTERS = 200
_MAXIMUM_DESCENDANT_SCOPES = 10_000
_MAXIMUM_OPAQUE_ID_CHARACTERS = 200
_MAXIMUM_WIRE_MODEL_CHARACTERS = 500


class PostgresConfigurationRepository:
    """Publish immutable scope revisions and resolve effective state."""

    def __init__(
        self,
        database_url: str,
        *,
        schema_registry: SettingsSchemaRegistry,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        """Use one database and one closed registered-schema set."""
        self._database_url = database_url
        self._registry = schema_registry
        self._identity_factory = identity_factory

    def publish(  # noqa: PLR0913
        self,
        context: RequestContext,
        scope: ConfigurationScope,
        content: ScopeConfiguration,
        *,
        expected_active_revision: str | None,
        reason: str,
        now: datetime,
        resource_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ConfigurationWriteResult:
        """Validate and immediately publish one complete local layer."""
        _require_write_authority(context, scope)
        _require_reason(reason)
        _require_aware(now)
        _require_scope_identities(scope, context.request_id)
        if idempotency_key is not None and not (
            _MINIMUM_IDEMPOTENCY_KEY_CHARACTERS
            <= len(idempotency_key)
            <= _MAXIMUM_IDEMPOTENCY_KEY_CHARACTERS
        ):
            raise ConfigurationError(
                ConfigurationErrorCode.INVALID_REQUEST, context.request_id
            )
        expected = _parse_optional_uuid(expected_active_revision, context.request_id)
        public_resource_id = resource_id or scope.source_layer
        request_fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "content": _encode_content(content),
                    "expected_active_revision": expected_active_revision,
                    "reason": reason,
                    "resource_id": public_resource_id,
                }
            )
        ).digest()
        revision_id = self._identity_factory()
        operation_id = self._identity_factory()
        with self._connect() as connection:
            _lock_scope(connection, scope)
            if idempotency_key is not None:
                replay = _configuration_replay(
                    connection,
                    context,
                    scope,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
                if replay is not None:
                    return replay
            _require_active_scope(connection, scope, context.request_id)
            current = _active_revision(connection, scope, lock=True)
            _check_expected_revision(
                current,
                expected,
                request_id=context.request_id,
            )
            old_content = (
                ScopeConfiguration()
                if current is None
                else _revision_content(connection, current[0], context.request_id)
            )
            content = _prepare_price_content(
                content,
                old_content,
                identity_factory=self._identity_factory,
            )
            issues = list(_shape_issues(content))
            issues.extend(_transition_issues(old_content, content))
            if not issues:
                issues.extend(
                    _projection_identity_issues(connection, old_content, content)
                )
            issues.extend(_eligibility_issues(connection, scope, content))
            issues.extend(_price_currency_issues(connection, scope, content))
            proposed = RevisionLayer(scope, str(revision_id), content)
            issues.extend(self._affected_validation(connection, scope, proposed))
            issues.extend(_credential_issues(connection, scope, content))
            if issues:
                raise ConfigurationError(
                    ConfigurationErrorCode.VALIDATION_FAILED,
                    context.request_id,
                    issues=_deduplicate_issues(issues),
                )
            revision_number = 1 if current is None else current[1] + 1
            payload = _encode_content(content)
            digest = hashlib.sha256(_canonical_json(payload)).digest()
            connection.execute(
                """
                INSERT INTO router.configuration_revisions (
                    id, scope_kind, service_id, workspace_id, revision_number,
                    content, content_sha256, created_at, created_by_kind, created_by_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    revision_id,
                    scope.kind,
                    _parse_optional_uuid(scope.service_id, context.request_id),
                    _parse_optional_uuid(scope.workspace_id, context.request_id),
                    revision_number,
                    Jsonb(payload),
                    digest,
                    now,
                    _created_by_kind(context),
                    context.actor_id,
                ),
            )
            _synchronize_projections(
                connection,
                scope,
                old_content,
                content,
                revision_id=revision_id,
                now=now,
                identity_factory=self._identity_factory,
            )
            _insert_assignment_rows(
                connection,
                content.assignments,
                revision_id=revision_id,
                now=now,
                identity_factory=self._identity_factory,
            )
            _activate_revision(
                connection,
                scope,
                revision_id=revision_id,
                revision_number=revision_number,
                now=now,
            )
            connection.execute(
                """
                INSERT INTO router.configuration_distribution_states (
                    revision_id, state, current_nodes, total_nodes,
                    published_at, observed_at
                ) VALUES (%s, 'distributing', 0, 0, %s, %s)
                """,
                (revision_id, now, now),
            )
            _insert_audit(
                connection,
                context,
                event_id=operation_id,
                revision_id=revision_id,
                scope=scope,
                reason=reason,
                action="configuration.publish",
                now=now,
            )
            if idempotency_key is not None:
                connection.execute(
                    """
                    INSERT INTO router.configuration_write_idempotency_bindings (
                        actor_id, operation, scope_key, idempotency_key,
                        request_fingerprint, resource_id, active_revision,
                        distribution_state, operation_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        context.actor_id,
                        context.operation,
                        _scope_key(scope),
                        idempotency_key,
                        request_fingerprint,
                        public_resource_id,
                        revision_id,
                        DistributionState.DISTRIBUTING.value,
                        operation_id,
                        now,
                    ),
                )
        return ConfigurationWriteResult(
            resource_id=public_resource_id,
            active_revision=str(revision_id),
            distribution_state=DistributionState.DISTRIBUTING,
            operation_id=str(operation_id),
        )

    def rollback(  # noqa: PLR0913
        self,
        context: RequestContext,
        scope: ConfigurationScope,
        revision: str,
        *,
        expected_active_revision: str,
        reason: str,
        now: datetime,
    ) -> ConfigurationWriteResult:
        """Restore selected content by publishing a new immutable revision."""
        _require_write_authority(context, scope)
        _require_reason(reason)
        _require_aware(now)
        _require_scope_identities(scope, context.request_id)
        target_revision = _parse_uuid(revision, context.request_id)
        expected = _parse_uuid(expected_active_revision, context.request_id)
        new_revision = self._identity_factory()
        operation_id = self._identity_factory()
        with self._connect() as connection:
            _lock_scope(connection, scope)
            _require_active_scope(connection, scope, context.request_id)
            current = _active_revision(connection, scope, lock=True)
            _check_expected_revision(current, expected, request_id=context.request_id)
            target = connection.execute(
                """
                SELECT content
                FROM router.configuration_revisions
                WHERE id = %s AND scope_kind = %s
                  AND service_id IS NOT DISTINCT FROM %s
                  AND workspace_id IS NOT DISTINCT FROM %s
                """,
                (
                    target_revision,
                    scope.kind,
                    _parse_optional_uuid(scope.service_id, context.request_id),
                    _parse_optional_uuid(scope.workspace_id, context.request_id),
                ),
            ).fetchone()
            if target is None or current is None:
                raise ConfigurationError(
                    ConfigurationErrorCode.NOT_FOUND, context.request_id
                )
            content = _decode_content(
                _with_legacy_price_content(
                    connection,
                    target_revision,
                    cast("dict[str, Any]", target[0]),
                )
            )
            old_content = _revision_content(connection, current[0], context.request_id)
            issues = list(
                _transition_issues(old_content, content, allow_omissions=True)
            )
            proposed = RevisionLayer(scope, str(new_revision), content)
            issues.extend(self._affected_validation(connection, scope, proposed))
            issues.extend(_credential_issues(connection, scope, content))
            issues.extend(_eligibility_issues(connection, scope, content))
            issues.extend(_price_currency_issues(connection, scope, content))
            if issues:
                raise ConfigurationError(
                    ConfigurationErrorCode.VALIDATION_FAILED,
                    context.request_id,
                    issues=_deduplicate_issues(issues),
                )
            revision_number = current[1] + 1
            payload = _encode_content(content)
            connection.execute(
                """
                INSERT INTO router.configuration_revisions (
                    id, scope_kind, service_id, workspace_id, revision_number,
                    restored_from_revision_id, content, content_sha256,
                    created_at, created_by_kind, created_by_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_revision,
                    scope.kind,
                    _parse_optional_uuid(scope.service_id, context.request_id),
                    _parse_optional_uuid(scope.workspace_id, context.request_id),
                    revision_number,
                    target_revision,
                    Jsonb(payload),
                    hashlib.sha256(_canonical_json(payload)).digest(),
                    now,
                    _created_by_kind(context),
                    context.actor_id,
                ),
            )
            _synchronize_projections(
                connection,
                scope,
                old_content,
                content,
                revision_id=new_revision,
                now=now,
                retire_omitted=True,
                identity_factory=self._identity_factory,
            )
            _insert_assignment_rows(
                connection,
                content.assignments,
                revision_id=new_revision,
                now=now,
                identity_factory=self._identity_factory,
            )
            _activate_revision(
                connection,
                scope,
                revision_id=new_revision,
                revision_number=revision_number,
                now=now,
            )
            connection.execute(
                """
                INSERT INTO router.configuration_distribution_states (
                    revision_id, state, published_at, observed_at
                ) VALUES (%s, 'distributing', %s, %s)
                """,
                (new_revision, now, now),
            )
            _insert_audit(
                connection,
                context,
                event_id=operation_id,
                revision_id=new_revision,
                scope=scope,
                reason=reason,
                action="configuration.rollback",
                now=now,
            )
        return ConfigurationWriteResult(
            resource_id=scope.source_layer,
            active_revision=str(new_revision),
            distribution_state=DistributionState.DISTRIBUTING,
            operation_id=str(operation_id),
        )

    def effective(
        self, context: RequestContext, scope: ConfigurationScope
    ) -> EffectiveConfiguration:
        """Return effective configuration only for the authorized exact scope."""
        _require_read_authority(context, scope)
        _require_scope_identities(scope, context.request_id)
        if scope.kind == "global":
            raise ConfigurationError(
                ConfigurationErrorCode.INSUFFICIENT_SCOPE, context.request_id
            )
        with self._connect() as connection:
            _require_active_scope(
                connection, scope, context.request_id, permit_disabled=True
            )
            layers = _load_layers(connection, scope)
            if not layers or all(
                layer.revision_id.startswith("none:") for layer in layers
            ):
                raise ConfigurationError(
                    ConfigurationErrorCode.NOT_FOUND, context.request_id
                )
            state = _effective_distribution_state(connection, layers)
            try:
                resolved = resolve_configuration(
                    layers,
                    registry=self._registry,
                    distribution_state=state,
                )
                return _with_current_price_states(connection, resolved)
            except ValueError as error:
                raise ConfigurationError(
                    ConfigurationErrorCode.VALIDATION_FAILED,
                    context.request_id,
                ) from error

    def owned(
        self, context: RequestContext, scope: ConfigurationScope
    ) -> RevisionLayer | None:
        """Return one exact local layer without inherited content."""
        if context.mutation:
            _require_write_authority(context, scope)
        else:
            _require_read_authority(context, scope)
        _require_scope_identities(scope, context.request_id)
        with self._connect() as connection:
            _require_active_scope(
                connection, scope, context.request_id, permit_disabled=True
            )
            current = _active_revision(connection, scope, lock=False)
            if current is None:
                return None
            return RevisionLayer(
                scope,
                str(current[0]),
                _revision_content(connection, current[0], context.request_id),
            )

    def mark_distribution(  # noqa: PLR0913
        self,
        context: RequestContext,
        revision: str,
        *,
        current_nodes: int,
        total_nodes: int,
        degraded: bool,
        observed_at: datetime,
    ) -> DistributionState:
        """Record bounded internal node observations for one revision."""
        _require_distribution_authority(context)
        revision_id = _parse_uuid(revision, context.request_id)
        _require_aware(observed_at)
        if current_nodes < 0 or total_nodes < current_nodes:
            msg = "Distribution node counts are invalid."
            raise ValueError(msg)
        state = (
            DistributionState.DEGRADED
            if degraded
            else DistributionState.CURRENT
            if total_nodes > 0 and current_nodes == total_nodes
            else DistributionState.DISTRIBUTING
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state, current_nodes, total_nodes, published_at, observed_at
                FROM router.configuration_distribution_states
                WHERE revision_id = %s
                FOR UPDATE
                """,
                (revision_id,),
            ).fetchone()
            if row is None:
                msg = "The configuration revision is unknown."
                raise ValueError(msg)
            if observed_at < row[3] or observed_at < row[4]:
                msg = "The distribution observation is stale."
                raise ValueError(msg)
            if observed_at == row[4]:
                if (state.value, current_nodes, total_nodes) != tuple(row[:3]):
                    msg = "The distribution observation conflicts with stored state."
                    raise ValueError(msg)
                return state
            connection.execute(
                """
                UPDATE router.configuration_distribution_states
                SET state = %s, current_nodes = %s, total_nodes = %s,
                    observed_at = %s
                WHERE revision_id = %s
                """,
                (state.value, current_nodes, total_nodes, observed_at, revision_id),
            )
        return state

    def _affected_validation(
        self,
        connection: Connection[Any],
        scope: ConfigurationScope,
        proposed: RevisionLayer,
    ) -> tuple[ValidationIssue, ...]:
        targets = _affected_scopes(connection, scope)
        if targets is None:
            return (
                ValidationIssue(
                    "scope",
                    "The affected scope count exceeds the bounded validation limit.",
                ),
            )
        issues: list[ValidationIssue] = []
        for target in targets:
            layers = _load_layers(connection, target, proposed=proposed)
            issues.extend(validate_layers(layers, registry=self._registry))
        return tuple(issues)

    def _connect(self) -> Connection[Any]:
        return psycopg.connect(self._database_url)


def _load_layers(
    connection: Connection[Any],
    scope: ConfigurationScope,
    *,
    proposed: RevisionLayer | None = None,
) -> tuple[RevisionLayer, ...]:
    scopes = _scope_chain(connection, scope)
    layers: list[RevisionLayer] = []
    inherited_revision = "none:global"
    for item in scopes:
        if proposed is not None and item == proposed.scope:
            layers.append(proposed)
            inherited_revision = proposed.revision_id
            continue
        row = _active_revision(connection, item, lock=False)
        if row is None:
            layers.append(RevisionLayer(item, inherited_revision, ScopeConfiguration()))
        else:
            inherited_revision = str(row[0])
            layers.append(
                RevisionLayer(
                    item,
                    inherited_revision,
                    _revision_content(connection, row[0], "configuration-read"),
                )
            )
    return tuple(layers)


def _with_current_price_states(
    connection: Connection[Any], value: EffectiveConfiguration
) -> EffectiveConfiguration:
    """Show the latest safe synchronization state with immutable route prices."""
    route_ids = [uuid.UUID(item.stable_id) for item in value.provider_model_routes]
    if not route_ids:
        return value
    states = {
        str(row[0]): SynchronizationState(row[1])
        for row in connection.execute(
            """SELECT state.provider_model_route_id,
                      CASE
                        WHEN state.synchronization_state = 'current'
                         AND source.authority_kind = 'synchronized'
                         AND state.observed_at + source.stale_after <=
                             transaction_timestamp()
                        THEN 'stale'
                        ELSE state.synchronization_state
                      END
               FROM router.route_price_synchronization_states AS state
               JOIN router.route_price_sources AS source
                 ON source.provider_model_route_id = state.provider_model_route_id
               WHERE state.provider_model_route_id = ANY(%s)""",
            (route_ids,),
        ).fetchall()
    }
    routes: list[EffectiveItem] = []
    for item in value.provider_model_routes:
        route = cast("ProviderModelRoute", item.value)
        state = states.get(item.stable_id, route.synchronization_state)
        routes.append(
            replace(
                item,
                value=replace(route, synchronization_state=state),
            )
        )
    return replace(value, provider_model_routes=tuple(routes))


def _scope_chain(
    connection: Connection[Any], scope: ConfigurationScope
) -> tuple[ConfigurationScope, ...]:
    result: list[ConfigurationScope] = [ConfigurationScope()]
    if scope.service_id is None:
        return tuple(result)
    service_id = uuid.UUID(scope.service_id)
    rows = connection.execute(
        """
        WITH RECURSIVE ancestors AS (
            SELECT id, parent_service_id, 0 AS depth, ARRAY[id] AS path, false AS cycle
            FROM router.services WHERE id = %s
          UNION ALL
            SELECT parent.id, parent.parent_service_id, child.depth + 1,
                   child.path || parent.id, parent.id = ANY(child.path)
            FROM router.services AS parent
            JOIN ancestors AS child ON parent.id = child.parent_service_id
            WHERE NOT child.cycle AND child.depth < 1000
        )
        SELECT id, cycle, parent_service_id FROM ancestors ORDER BY depth DESC
        """,
        (service_id,),
    ).fetchall()
    if not rows or any(bool(row[1]) for row in rows) or rows[0][2] is not None:
        return ()
    result.extend(ConfigurationScope(service_id=str(row[0])) for row in rows)
    if scope.workspace_id is not None:
        result.append(scope)
    return tuple(result)


def _affected_scopes(
    connection: Connection[Any], scope: ConfigurationScope
) -> tuple[ConfigurationScope, ...] | None:
    if scope.workspace_id is not None:
        return (scope,)
    if scope.service_id is None:
        service_rows = connection.execute(
            """
            SELECT id FROM router.services
            WHERE state <> 'retired' ORDER BY id LIMIT %s
            """,
            (_MAXIMUM_DESCENDANT_SCOPES + 1,),
        ).fetchall()
    else:
        service_rows = connection.execute(
            """
            WITH RECURSIVE descendants AS (
                SELECT id FROM router.services WHERE id = %s
              UNION ALL
                SELECT child.id FROM router.services AS child
                JOIN descendants AS parent ON child.parent_service_id = parent.id
                WHERE child.state <> 'retired'
            )
            SELECT id FROM descendants ORDER BY id LIMIT %s
            """,
            (uuid.UUID(scope.service_id), _MAXIMUM_DESCENDANT_SCOPES + 1),
        ).fetchall()
    if len(service_rows) > _MAXIMUM_DESCENDANT_SCOPES:
        return None
    result: list[ConfigurationScope] = []
    for row in service_rows:
        service_scope = ConfigurationScope(service_id=str(row[0]))
        result.append(service_scope)
        workspaces = connection.execute(
            """
            SELECT id FROM router.workspaces
            WHERE service_id = %s AND state <> 'retired'
            ORDER BY id LIMIT %s
            """,
            (row[0], _MAXIMUM_DESCENDANT_SCOPES + 1 - len(result)),
        ).fetchall()
        result.extend(
            ConfigurationScope(service_id=str(row[0]), workspace_id=str(item[0]))
            for item in workspaces
        )
        if len(result) > _MAXIMUM_DESCENDANT_SCOPES:
            return None
    if scope.service_id is None:
        result.insert(0, scope)
        if len(result) > _MAXIMUM_DESCENDANT_SCOPES:
            return None
    return tuple(result)


def _active_revision(
    connection: Connection[Any],
    scope: ConfigurationScope,
    *,
    lock: bool,
) -> tuple[uuid.UUID, int] | None:
    statement = (
        """
        SELECT revision_id, revision_number
        FROM router.active_configurations
        WHERE scope_kind = %s
          AND service_id IS NOT DISTINCT FROM %s
          AND workspace_id IS NOT DISTINCT FROM %s
        FOR UPDATE
        """
        if lock
        else """
        SELECT revision_id, revision_number
        FROM router.active_configurations
        WHERE scope_kind = %s
          AND service_id IS NOT DISTINCT FROM %s
          AND workspace_id IS NOT DISTINCT FROM %s
        """
    )
    row = connection.execute(
        statement,
        (
            scope.kind,
            uuid.UUID(scope.service_id) if scope.service_id is not None else None,
            uuid.UUID(scope.workspace_id) if scope.workspace_id is not None else None,
        ),
    ).fetchone()
    return None if row is None else (row[0], int(row[1]))


def _scope_key(scope: ConfigurationScope) -> str:
    if scope.workspace_id is not None:
        return f"workspace:{scope.service_id}:{scope.workspace_id}"
    if scope.service_id is not None:
        return f"service:{scope.service_id}"
    return "global"


def _configuration_replay(
    connection: Connection[Any],
    context: RequestContext,
    scope: ConfigurationScope,
    *,
    idempotency_key: str,
    request_fingerprint: bytes,
) -> ConfigurationWriteResult | None:
    row = connection.execute(
        """
        SELECT request_fingerprint, resource_id, active_revision::text,
               distribution_state, operation_id::text
        FROM router.configuration_write_idempotency_bindings
        WHERE actor_id = %s AND operation = %s AND scope_key = %s
          AND idempotency_key = %s
        """,
        (
            context.actor_id,
            context.operation,
            _scope_key(scope),
            idempotency_key,
        ),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(bytes(row[0]), request_fingerprint):
        raise ConfigurationError(
            ConfigurationErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
        )
    return ConfigurationWriteResult(
        resource_id=str(row[1]),
        active_revision=str(row[2]),
        distribution_state=DistributionState(str(row[3])),
        operation_id=str(row[4]),
    )


def _revision_content(
    connection: Connection[Any], revision_id: uuid.UUID, request_id: str
) -> ScopeConfiguration:
    row = connection.execute(
        "SELECT content FROM router.configuration_revisions WHERE id = %s",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ConfigurationError(ConfigurationErrorCode.NOT_FOUND, request_id)
    content = cast("dict[str, Any]", row[0])
    return _decode_content(_with_legacy_price_content(connection, revision_id, content))


def _with_legacy_price_content(
    connection: Connection[Any], revision_id: uuid.UUID, value: dict[str, Any]
) -> dict[str, Any]:
    """Read pre-pricing revision content through immutable migration bindings."""
    routes = value.get("provider_model_routes", [])
    legacy_ids = [
        uuid.UUID(item["provider_model_route_id"])
        for item in routes
        if "price_authority" not in item
    ]
    if not legacy_ids:
        return value
    rows = connection.execute(
        """
        SELECT legacy.provider_model_route_id::text,
               COALESCE(source.authority_kind, 'synchronized'),
               CASE WHEN source.authority_kind = 'manual' THEN NULL
                    ELSE COALESCE(source.source_name, 'legacy-unconfigured') END,
               CASE WHEN source.authority_kind = 'manual' THEN NULL
                    ELSE COALESCE(source.lookup_identifier,
                                  route.wire_model) END,
               COALESCE(source.synchronization_schedule, '0 0 * * 0'),
               COALESCE(extract(epoch FROM source.stale_after)::bigint,
                        1209600),
               binding.price_version_id::text,
               CASE WHEN binding.price_version_id IS NULL
                         AND source.authority_kind = 'manual' THEN 'manual'
                    WHEN binding.price_version_id IS NULL THEN 'missing'
                    ELSE COALESCE(
                        state.synchronization_state,
                        CASE WHEN source.authority_kind = 'manual'
                             THEN 'manual' ELSE 'current' END)
               END,
               version.currency::text, component.unit_name,
               component.unit_price, component.raw_source_value,
               component.unit_quantity
        FROM unnest(%s::uuid[]) AS legacy(provider_model_route_id)
        JOIN router.provider_model_routes AS route
          ON route.id = legacy.provider_model_route_id
        LEFT JOIN router.route_price_sources AS source
          ON source.provider_model_route_id = legacy.provider_model_route_id
        LEFT JOIN router.configuration_price_bindings AS binding
          ON binding.configuration_revision_id = %s
         AND binding.provider_model_route_id = legacy.provider_model_route_id
        LEFT JOIN router.route_price_versions AS version
          ON version.id = binding.price_version_id
         AND version.provider_model_route_id = legacy.provider_model_route_id
        LEFT JOIN router.route_price_components AS component
          ON component.price_version_id = version.id
        LEFT JOIN router.route_price_synchronization_states AS state
          ON state.provider_model_route_id = legacy.provider_model_route_id
        ORDER BY legacy.provider_model_route_id, component.unit_name
        """,
        (legacy_ids, revision_id),
    ).fetchall()
    policies: dict[str, dict[str, Any]] = {}
    for row in rows:
        policy = policies.setdefault(
            row[0],
            {
                "price_authority": {
                    "mode": "manual" if row[1] == "manual" else "source",
                    "source_name": row[2],
                    "lookup_identifier": row[3],
                },
                "prices": [],
                "synchronization_schedule": row[4],
                "stale_after_seconds": row[5],
                "price_version": row[6],
                "synchronization_state": row[7],
            },
        )
        if row[9] is not None:
            policy["prices"].append(
                {
                    "unit": row[9],
                    "price": str(row[10]),
                    "currency": row[8],
                    "raw_source_value": row[11],
                    "unit_quantity": str(row[12]),
                }
            )
    if set(policies) != {str(item) for item in legacy_ids}:
        msg = "A legacy configuration route has no accepted price binding."
        raise RuntimeError(msg)
    upgraded = dict(value)
    upgraded["provider_model_routes"] = [
        item
        if "price_authority" in item
        else {**item, **policies[item["provider_model_route_id"]]}
        for item in routes
    ]
    return upgraded


def _transition_issues(
    old: ScopeConfiguration,
    new: ScopeConfiguration,
    *,
    allow_omissions: bool = False,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    groups: tuple[tuple[str, dict[str, object], dict[str, object]], ...] = (
        (
            "catalog",
            {item.stable_id: item for item in old.catalog},
            {item.stable_id: item for item in new.catalog},
        ),
        (
            "provider_instances",
            {item.provider_instance_id: item for item in old.provider_instances},
            {item.provider_instance_id: item for item in new.provider_instances},
        ),
        (
            "provider_model_routes",
            {item.provider_model_route_id: item for item in old.provider_model_routes},
            {item.provider_model_route_id: item for item in new.provider_model_routes},
        ),
        (
            "assignments",
            {item.name: item for item in old.assignments},
            {item.name: item for item in new.assignments},
        ),
    )
    for path, old_items, new_items in groups:
        if not allow_omissions:
            issues.extend(
                ValidationIssue(
                    f"{path}.{stable_id}",
                    "An existing identity must remain in the complete document.",
                )
                for stable_id in sorted(old_items.keys() - new_items.keys())
            )
        for stable_id in sorted(old_items.keys() & new_items.keys()):
            previous = old_items[stable_id]
            current = new_items[stable_id]
            if (
                getattr(previous, "state", None) is ConfigurationState.RETIRED
                and current != previous
            ):
                issues.append(
                    ValidationIssue(
                        f"{path}.{stable_id}.state",
                        "A retired identity is terminal.",
                    )
                )
            issues.extend(
                _immutable_identity_issues(path, stable_id, previous, current)
            )
            if (
                not allow_omissions
                and isinstance(previous, ProviderModelRoute)
                and isinstance(current, ProviderModelRoute)
                and current.price_authority.mode is PriceAuthorityMode.SOURCE
                and current.prices != previous.prices
            ):
                issues.append(
                    ValidationIssue(
                        f"{path}.{stable_id}.prices",
                        "A source-owned price can change only by synchronization.",
                    )
                )
    return tuple(issues)


def _immutable_identity_issues(
    path: str, stable_id: str, previous: object, current: object
) -> tuple[ValidationIssue, ...]:
    fields: tuple[str, ...]
    if isinstance(previous, CatalogEntry):
        fields = ("kind",)
    elif isinstance(previous, ProviderInstance):
        fields = ("provider_catalog_id",)
    elif isinstance(previous, ProviderModelRoute):
        fields = ("provider_instance_id", "canonical_model_id")
    else:
        fields = ()
    return tuple(
        ValidationIssue(
            f"{path}.{stable_id}.{field}",
            "The stable identity field is immutable.",
        )
        for field in fields
        if getattr(previous, field) != getattr(current, field)
    )


def _credential_issues(
    connection: Connection[Any],
    scope: ConfigurationScope,
    content: ScopeConfiguration,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for index, item in enumerate(content.provider_instances):
        path = f"provider_instances[{index}]"
        issues.extend(validate_endpoint(item.endpoint, field_path=f"{path}.endpoint"))
        try:
            credential_id = uuid.UUID(item.credential_id)
        except ValueError:
            issues.append(
                ValidationIssue(
                    f"{path}.credential_id",
                    "The credential reference is unknown or not eligible.",
                )
            )
            continue
        row = connection.execute(
            """
            SELECT owner_kind, owner_service_id, credential_kind, state
            FROM router.encrypted_credentials
            WHERE id = %s
            FOR KEY SHARE
            """,
            (credential_id,),
        ).fetchone()
        owner_service = None if scope.kind == "global" else scope.service_id
        unavailable = (
            row is None
            or (
                item.state is ConfigurationState.ACTIVE
                and row[3] != ConfigurationState.ACTIVE.value
            )
            or row[2] != item.provider_catalog_id
            or (row[0] == "service" and str(row[1]) != owner_service)
            or (scope.kind == "global" and row[0] != "global")
        )
        if unavailable:
            issues.append(
                ValidationIssue(
                    f"{path}.credential_id",
                    "The credential reference is unknown or not eligible.",
                )
            )
    return tuple(issues)


def _eligibility_issues(
    connection: Connection[Any],
    scope: ConfigurationScope,
    content: ScopeConfiguration,
) -> tuple[ValidationIssue, ...]:
    eligible_ids = {
        value
        for item in content.provider_instances
        for value in item.eligible_service_ids
    }
    eligible_ids.update(
        value
        for item in content.provider_model_routes
        for value in item.eligible_service_ids
    )
    if not eligible_ids:
        return ()
    if scope.service_id is None:
        rows = connection.execute(
            """
            SELECT id::text FROM router.services
            WHERE id = ANY(%s) AND state <> 'retired'
            """,
            ([uuid.UUID(value) for value in eligible_ids],),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            WITH RECURSIVE descendants AS (
                SELECT id FROM router.services WHERE id = %s
              UNION ALL
                SELECT child.id FROM router.services AS child
                JOIN descendants AS parent ON child.parent_service_id = parent.id
                WHERE child.state <> 'retired'
            )
            SELECT id::text FROM descendants WHERE id = ANY(%s)
            """,
            (
                uuid.UUID(scope.service_id),
                [uuid.UUID(value) for value in eligible_ids],
            ),
        ).fetchall()
    valid = {str(row[0]) for row in rows}
    return tuple(
        ValidationIssue(
            "eligible_service_ids",
            "An eligible service is unknown or outside the owning service tree.",
        )
        for _value in sorted(eligible_ids - valid)
    )


def _price_currency_issues(
    connection: Connection[Any],
    scope: ConfigurationScope,
    content: ScopeConfiguration,
) -> tuple[ValidationIssue, ...]:
    """Require each route price to match every applicable hard-budget currency."""
    issues: list[ValidationIssue] = []
    for index, item in enumerate(content.provider_model_routes):
        try:
            route_id = uuid.UUID(item.provider_model_route_id)
            eligible_ids = [uuid.UUID(value) for value in item.eligible_service_ids]
            owner_service_id = (
                None if scope.service_id is None else uuid.UUID(scope.service_id)
            )
        except ValueError:
            continue
        currencies = connection.execute(
            """
            WITH RECURSIVE roots AS (
                SELECT service.id
                FROM router.services AS service
                WHERE service.state <> 'retired'
                  AND (
                    (%s::boolean AND %s::uuid[] = '{}'::uuid[])
                    OR service.id = ANY(%s::uuid[])
                    OR (NOT %s::boolean
                        AND %s::uuid[] = '{}'::uuid[]
                        AND service.id = %s)
                  )
            ), permitted_services AS (
                SELECT id FROM roots
              UNION
                SELECT child.id
                FROM router.services AS child
                JOIN permitted_services AS parent
                  ON child.parent_service_id = parent.id
                WHERE child.state <> 'retired'
            ), applicable AS (
                SELECT budget.currency
                FROM router.budget_scopes AS budget
                WHERE budget.scope_kind = 'global'
              UNION
                SELECT budget.currency
                FROM router.budget_scopes AS budget
                WHERE budget.scope_kind IN ('service', 'workspace')
                  AND (budget.service_id = %s
                       OR budget.service_id IN (SELECT id FROM permitted_services))
              UNION
                SELECT budget.currency
                FROM router.assignment_candidates AS candidate
                JOIN router.assignment_definitions AS assignment
                  ON assignment.id = candidate.assignment_id
                 AND assignment.configuration_revision_id =
                     candidate.configuration_revision_id
                JOIN router.active_configurations AS active
                  ON active.revision_id = assignment.configuration_revision_id
                JOIN router.budget_scopes AS budget
                  ON budget.assignment_id = candidate.assignment_id
                WHERE candidate.provider_model_route_id = %s
            )
            SELECT DISTINCT currency::text FROM applicable
            """,
            (
                scope.service_id is None,
                eligible_ids,
                eligible_ids,
                scope.service_id is None,
                eligible_ids,
                owner_service_id,
                owner_service_id,
                route_id,
            ),
        ).fetchall()
        if not item.prices:
            continue
        currency = item.prices[0].currency
        if any(row[0] != currency for row in currencies):
            issues.append(
                ValidationIssue(
                    f"provider_model_routes[{index}].prices.currency",
                    "The price currency does not match an eligible hard-budget scope.",
                )
            )
    return tuple(issues)


def _shape_issues(  # noqa: C901, PLR0912
    content: ScopeConfiguration,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for index, item in enumerate(content.catalog):
        path = f"catalog[{index}]"
        if not 1 <= len(item.stable_id) <= _MAXIMUM_OPAQUE_ID_CHARACTERS:
            issues.append(
                ValidationIssue(f"{path}.stable_id", "The stable identity is invalid.")
            )
        if item.kind is CatalogKind.MODEL and not _is_uuid(item.stable_id):
            issues.append(
                ValidationIssue(f"{path}.stable_id", "The stable identity is invalid.")
            )
        if not 1 <= len(item.display_name) <= _MAXIMUM_OPAQUE_ID_CHARACTERS:
            issues.append(
                ValidationIssue(f"{path}.display_name", "The display name is invalid.")
            )
    for index, instance_item in enumerate(content.provider_instances):
        path = f"provider_instances[{index}]"
        for field, value in (
            ("provider_instance_id", instance_item.provider_instance_id),
            ("credential_id", instance_item.credential_id),
        ):
            if not _is_uuid(value):
                issues.append(
                    ValidationIssue(
                        f"{path}.{field}", "The stable identity is invalid."
                    )
                )
        if not (
            1 <= len(instance_item.provider_catalog_id) <= _MAXIMUM_OPAQUE_ID_CHARACTERS
        ):
            issues.append(
                ValidationIssue(
                    f"{path}.provider_catalog_id", "The stable identity is invalid."
                )
            )
        if not 1 <= len(instance_item.display_name) <= _MAXIMUM_OPAQUE_ID_CHARACTERS:
            issues.append(
                ValidationIssue(f"{path}.display_name", "The display name is invalid.")
            )
        issues.extend(
            ValidationIssue(
                f"{path}.eligible_service_ids",
                "An eligible service identity is invalid.",
            )
            for eligible in instance_item.eligible_service_ids
            if not _is_uuid(eligible)
        )
    for index, route_item in enumerate(content.provider_model_routes):
        path = f"provider_model_routes[{index}]"
        for field, value in (
            ("provider_model_route_id", route_item.provider_model_route_id),
            ("provider_instance_id", route_item.provider_instance_id),
            ("canonical_model_id", route_item.canonical_model_id),
        ):
            if not _is_uuid(value):
                issues.append(
                    ValidationIssue(
                        f"{path}.{field}", "The stable identity is invalid."
                    )
                )
        if not 1 <= len(route_item.wire_model) <= _MAXIMUM_WIRE_MODEL_CHARACTERS:
            issues.append(
                ValidationIssue(f"{path}.wire_model", "The wire model is invalid.")
            )
        issues.extend(
            ValidationIssue(
                f"{path}.eligible_service_ids",
                "An eligible service identity is invalid.",
            )
            for eligible in route_item.eligible_service_ids
            if not _is_uuid(eligible)
        )
    for index, assignment_item in enumerate(content.assignments):
        for candidate_index, candidate in enumerate(assignment_item.candidates):
            if not _is_uuid(candidate.provider_model_route_id):
                issues.append(
                    ValidationIssue(
                        f"assignments[{index}].candidates[{candidate_index}].provider_model_route_id",
                        "The stable identity is invalid.",
                    )
                )
    for index, disabled_item in enumerate(content.inherited_disables):
        if not _is_uuid(disabled_item.resource_id):
            issues.append(
                ValidationIssue(
                    f"inherited_disables[{index}].resource_id",
                    "The stable identity is invalid.",
                )
            )
    return tuple(issues)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _projection_identity_issues(
    connection: Connection[Any],
    old: ScopeConfiguration,
    new: ScopeConfiguration,
) -> tuple[ValidationIssue, ...]:
    checks: tuple[tuple[str, str, set[str], tuple[object, ...]], ...] = (
        (
            "catalog",
            "SELECT id::text FROM router.provider_adapter_types WHERE id = ANY(%s)",
            {
                item.stable_id
                for item in old.catalog
                if item.kind is CatalogKind.PROVIDER
            },
            tuple(
                item.stable_id
                for item in new.catalog
                if item.kind is CatalogKind.PROVIDER
            ),
        ),
        (
            "catalog",
            "SELECT id::text FROM router.canonical_models WHERE id = ANY(%s)",
            {item.stable_id for item in old.catalog if item.kind is CatalogKind.MODEL},
            tuple(
                uuid.UUID(item.stable_id)
                for item in new.catalog
                if item.kind is CatalogKind.MODEL
            ),
        ),
        (
            "provider_instances",
            "SELECT id::text FROM router.provider_instances WHERE id = ANY(%s)",
            {item.provider_instance_id for item in old.provider_instances},
            tuple(
                uuid.UUID(item.provider_instance_id) for item in new.provider_instances
            ),
        ),
        (
            "provider_model_routes",
            "SELECT id::text FROM router.provider_model_routes WHERE id = ANY(%s)",
            {item.provider_model_route_id for item in old.provider_model_routes},
            tuple(
                uuid.UUID(item.provider_model_route_id)
                for item in new.provider_model_routes
            ),
        ),
    )
    issues: list[ValidationIssue] = []
    for path, statement, old_ids, candidates in checks:
        if not candidates:
            continue
        rows = connection.execute(statement, (list(candidates),)).fetchall()
        for row in rows:
            stable_id = str(row[0])
            if stable_id not in old_ids:
                issues.append(
                    ValidationIssue(
                        f"{path}.{stable_id}",
                        "The stable identity already exists and cannot be reused.",
                    )
                )
    return tuple(issues)


def _retire_omitted_projections(  # noqa: PLR0913
    connection: Connection[Any],
    scope: ConfigurationScope,
    old: ScopeConfiguration,
    new: ScopeConfiguration,
    *,
    revision_id: uuid.UUID,
    now: datetime,
) -> None:
    new_routes = {item.provider_model_route_id for item in new.provider_model_routes}
    for route_item in old.provider_model_routes:
        if (
            route_item.provider_model_route_id not in new_routes
            and route_item.state is not ConfigurationState.RETIRED
        ):
            connection.execute(
                """
                UPDATE router.provider_model_routes
                SET state = 'retired', generation = generation + 1,
                    current_revision = %s, last_changed_at = %s, retired_at = %s
                WHERE id = %s
                """,
                (revision_id, now, now, uuid.UUID(route_item.provider_model_route_id)),
            )
    new_instances = {item.provider_instance_id for item in new.provider_instances}
    for instance_item in old.provider_instances:
        if (
            instance_item.provider_instance_id not in new_instances
            and instance_item.state is not ConfigurationState.RETIRED
        ):
            connection.execute(
                """
                UPDATE router.provider_instances
                SET state = 'retired', generation = generation + 1,
                    current_revision = %s, last_changed_at = %s, retired_at = %s
                WHERE id = %s
                """,
                (revision_id, now, now, uuid.UUID(instance_item.provider_instance_id)),
            )
    if scope.kind != "global":
        return
    new_catalog = {item.stable_id for item in new.catalog}
    for catalog_item in old.catalog:
        if (
            catalog_item.stable_id in new_catalog
            or catalog_item.state is ConfigurationState.RETIRED
        ):
            continue
        identity: object = (
            catalog_item.stable_id
            if catalog_item.kind is CatalogKind.PROVIDER
            else uuid.UUID(catalog_item.stable_id)
        )
        if catalog_item.kind is CatalogKind.PROVIDER:
            connection.execute(
                """
                UPDATE router.provider_adapter_types
                SET state = 'retired', generation = generation + 1,
                    current_revision = %s, retired_at = %s
                WHERE id = %s
                """,
                (revision_id, now, identity),
            )
        else:
            connection.execute(
                """
                UPDATE router.canonical_models
                SET state = 'retired', generation = generation + 1,
                    current_revision = %s, retired_at = %s
                WHERE id = %s
                """,
                (revision_id, now, identity),
            )


def _synchronize_projections(  # noqa: C901, PLR0913
    connection: Connection[Any],
    scope: ConfigurationScope,
    old: ScopeConfiguration,
    new: ScopeConfiguration,
    *,
    revision_id: uuid.UUID,
    now: datetime,
    identity_factory: Callable[[], uuid.UUID],
    retire_omitted: bool = False,
) -> None:
    if retire_omitted:
        _retire_omitted_projections(
            connection,
            scope,
            old,
            new,
            revision_id=revision_id,
            now=now,
        )
    if scope.kind == "global":
        old_catalog = {item.stable_id: item for item in old.catalog}
        for catalog_item in new.catalog:
            catalog_previous = old_catalog.get(catalog_item.stable_id)
            if catalog_previous == catalog_item:
                continue
            if catalog_item.kind is CatalogKind.PROVIDER:
                _upsert_provider_catalog(
                    connection,
                    catalog_item,
                    catalog_previous,
                    revision_id=revision_id,
                    now=now,
                )
            else:
                _upsert_model_catalog(
                    connection,
                    catalog_item,
                    catalog_previous,
                    revision_id=revision_id,
                    now=now,
                )
    owner_kind = "global" if scope.kind == "global" else "service"
    owner_service = (
        None if scope.kind == "global" else uuid.UUID(cast("str", scope.service_id))
    )
    old_instances = {item.provider_instance_id: item for item in old.provider_instances}
    for instance_item in new.provider_instances:
        instance_previous = old_instances.get(instance_item.provider_instance_id)
        if instance_previous == instance_item:
            continue
        _upsert_provider_instance(
            connection,
            instance_item,
            instance_previous,
            owner_kind=owner_kind,
            owner_service_id=owner_service,
            revision_id=revision_id,
            now=now,
        )
    old_routes = {
        item.provider_model_route_id: item for item in old.provider_model_routes
    }
    for route_item in new.provider_model_routes:
        route_previous = old_routes.get(route_item.provider_model_route_id)
        if route_previous == route_item:
            continue
        _upsert_provider_route(
            connection,
            route_item,
            route_previous,
            owner_kind=owner_kind,
            owner_service_id=owner_service,
            revision_id=revision_id,
            now=now,
        )
    old_routes = {
        item.provider_model_route_id: item for item in old.provider_model_routes
    }
    for route_item in new.provider_model_routes:
        _project_route_price(
            connection,
            route_item,
            previous=old_routes.get(route_item.provider_model_route_id),
            revision_id=revision_id,
            now=now,
            identity_factory=identity_factory,
        )


def _upsert_provider_catalog(
    connection: Connection[Any],
    item: CatalogEntry,
    previous: CatalogEntry | None,
    *,
    revision_id: uuid.UUID,
    now: datetime,
) -> None:
    settings = cast("RegisteredDocument", item.settings)
    retired_at = now if item.state is ConfigurationState.RETIRED else None
    capabilities = Jsonb(dict.fromkeys(sorted(item.capabilities), True))
    if previous is None:
        connection.execute(
            """
            INSERT INTO router.provider_adapter_types (
                id, settings_schema_name, settings_schema_major, capabilities,
                state, display_name, generation, current_revision, retired_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)
            """,
            (
                item.stable_id,
                settings.schema_name,
                settings.major_version,
                capabilities,
                item.state.value,
                item.display_name,
                revision_id,
                retired_at,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE router.provider_adapter_types
            SET settings_schema_name = %s, settings_schema_major = %s,
                capabilities = %s, state = %s, display_name = %s,
                generation = generation + 1, current_revision = %s,
                retired_at = %s
            WHERE id = %s
            """,
            (
                settings.schema_name,
                settings.major_version,
                capabilities,
                item.state.value,
                item.display_name,
                revision_id,
                retired_at,
                item.stable_id,
            ),
        )


def _upsert_model_catalog(
    connection: Connection[Any],
    item: CatalogEntry,
    previous: CatalogEntry | None,
    *,
    revision_id: uuid.UUID,
    now: datetime,
) -> None:
    item_id = uuid.UUID(item.stable_id)
    capabilities = Jsonb(dict.fromkeys(sorted(item.capabilities), True))
    retired_at = now if item.state is ConfigurationState.RETIRED else None
    metadata = Jsonb(
        _thaw_json(item.settings.document) if item.settings is not None else {}
    )
    if previous is None:
        connection.execute(
            """
            INSERT INTO router.canonical_models (
                id, stable_name, capabilities, metadata, state, display_name,
                generation, current_revision, retired_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)
            """,
            (
                item_id,
                item.stable_id,
                capabilities,
                metadata,
                item.state.value,
                item.display_name,
                revision_id,
                retired_at,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE router.canonical_models
            SET capabilities = %s, metadata = %s, state = %s,
                display_name = %s, generation = generation + 1,
                current_revision = %s, retired_at = %s
            WHERE id = %s
            """,
            (
                capabilities,
                metadata,
                item.state.value,
                item.display_name,
                revision_id,
                retired_at,
                item_id,
            ),
        )


def _upsert_provider_instance(  # noqa: PLR0913
    connection: Connection[Any],
    item: ProviderInstance,
    previous: ProviderInstance | None,
    *,
    owner_kind: str,
    owner_service_id: uuid.UUID | None,
    revision_id: uuid.UUID,
    now: datetime,
) -> None:
    item_id = uuid.UUID(item.provider_instance_id)
    credential_id = uuid.UUID(item.credential_id)
    retired_at = now if item.state is ConfigurationState.RETIRED else None
    eligible = [uuid.UUID(value) for value in sorted(item.eligible_service_ids)]
    values = (
        item.provider_catalog_id,
        credential_id,
        item.display_name,
        item.endpoint,
        item.settings.schema_name,
        item.settings.major_version,
        Jsonb(_thaw_json(item.settings.document)),
        item.state.value,
        eligible,
        revision_id,
        now,
        retired_at,
    )
    if previous is None:
        connection.execute(
            """
            INSERT INTO router.provider_instances (
                id, owner_kind, owner_service_id, adapter_type_id,
                credential_id, stable_name, display_name, endpoint_origin,
                settings_schema_name, settings_schema_major, settings,
                state, eligible_service_ids, current_revision,
                last_changed_at, retired_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                item_id,
                owner_kind,
                owner_service_id,
                item.provider_catalog_id,
                credential_id,
                item.provider_instance_id,
                item.display_name,
                item.endpoint,
                item.settings.schema_name,
                item.settings.major_version,
                Jsonb(_thaw_json(item.settings.document)),
                item.state.value,
                eligible,
                revision_id,
                now,
                retired_at,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE router.provider_instances
            SET credential_id = %s, display_name = %s, endpoint_origin = %s,
                settings_schema_name = %s, settings_schema_major = %s,
                settings = %s, state = %s, eligible_service_ids = %s,
                generation = generation + 1, current_revision = %s,
                last_changed_at = %s, retired_at = %s
            WHERE id = %s
            """,
            (values[1], *values[2:], item_id),
        )


def _upsert_provider_route(  # noqa: PLR0913
    connection: Connection[Any],
    item: ProviderModelRoute,
    previous: ProviderModelRoute | None,
    *,
    owner_kind: str,
    owner_service_id: uuid.UUID | None,
    revision_id: uuid.UUID,
    now: datetime,
) -> None:
    item_id = uuid.UUID(item.provider_model_route_id)
    retired_at = now if item.state is ConfigurationState.RETIRED else None
    eligible = [uuid.UUID(value) for value in sorted(item.eligible_service_ids)]
    if previous is None:
        connection.execute(
            """
            INSERT INTO router.provider_model_routes (
                id, owner_kind, owner_service_id, provider_instance_id,
                canonical_model_id, provider_lookup_id, wire_model,
                settings_schema_name,
                settings_schema_major, settings, state, eligible_service_ids,
                capabilities, embedding_model_space_id, embedding_dimensions,
                current_revision, last_changed_at, retired_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            """,
            (
                item_id,
                owner_kind,
                owner_service_id,
                uuid.UUID(item.provider_instance_id),
                uuid.UUID(item.canonical_model_id),
                item.provider_model_route_id,
                item.wire_model,
                item.settings.schema_name,
                item.settings.major_version,
                Jsonb(_thaw_json(item.settings.document)),
                item.state.value,
                eligible,
                Jsonb(sorted(item.capabilities)),
                item.embedding_model_space_id,
                item.embedding_dimensions,
                revision_id,
                now,
                retired_at,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE router.provider_model_routes
            SET wire_model = %s, settings_schema_name = %s,
                settings_schema_major = %s,
                settings = %s, state = %s, eligible_service_ids = %s,
                capabilities = %s, embedding_model_space_id = %s,
                embedding_dimensions = %s, generation = generation + 1,
                current_revision = %s, last_changed_at = %s, retired_at = %s
            WHERE id = %s
            """,
            (
                item.wire_model,
                item.settings.schema_name,
                item.settings.major_version,
                Jsonb(_thaw_json(item.settings.document)),
                item.state.value,
                eligible,
                Jsonb(sorted(item.capabilities)),
                item.embedding_model_space_id,
                item.embedding_dimensions,
                revision_id,
                now,
                retired_at,
                item_id,
            ),
        )


def _project_route_price(  # noqa: PLR0913
    connection: Connection[Any],
    item: ProviderModelRoute,
    *,
    previous: ProviderModelRoute | None,
    revision_id: uuid.UUID,
    now: datetime,
    identity_factory: Callable[[], uuid.UUID],
) -> None:
    """Project one revision-bound price authority and exact price version."""
    route_id = uuid.UUID(item.provider_model_route_id)
    price_version_id = (
        None if item.price_version is None else uuid.UUID(item.price_version)
    )
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"price-version:{item.provider_model_route_id}",),
    )
    source = connection.execute(
        """SELECT id FROM router.route_price_sources
           WHERE provider_model_route_id = %s FOR UPDATE""",
        (route_id,),
    ).fetchone()
    authority_kind = (
        "manual"
        if item.price_authority.mode is PriceAuthorityMode.MANUAL
        else "synchronized"
    )
    source_name = item.price_authority.source_name
    lookup_identifier = item.price_authority.lookup_identifier
    source_id = identity_factory() if source is None else source[0]
    connection.execute(
        """
        INSERT INTO router.route_price_sources (
            id, provider_model_route_id, authority_kind, source_name,
            lookup_identifier, synchronization_schedule, stale_after
        ) VALUES (%s, %s, %s, %s, %s, %s, %s * interval '1 second')
        ON CONFLICT (provider_model_route_id) DO UPDATE
        SET authority_kind = EXCLUDED.authority_kind,
            source_name = EXCLUDED.source_name,
            lookup_identifier = EXCLUDED.lookup_identifier,
            synchronization_schedule = EXCLUDED.synchronization_schedule,
            stale_after = EXCLUDED.stale_after
        """,
        (
            source_id,
            route_id,
            authority_kind,
            source_name,
            lookup_identifier,
            item.synchronization_schedule,
            item.stale_after_seconds,
        ),
    )
    version = (
        None
        if price_version_id is None
        else connection.execute(
            """SELECT provider_model_route_id FROM router.route_price_versions
           WHERE id = %s""",
            (price_version_id,),
        ).fetchone()
    )
    if price_version_id is not None and version is None:
        next_version = connection.execute(
            """SELECT COALESCE(max(version_number), 0) + 1
               FROM router.route_price_versions
               WHERE provider_model_route_id = %s""",
            (route_id,),
        ).fetchone()
        if next_version is None:
            msg = "The route price version sequence is unavailable."
            raise RuntimeError(msg)
        connection.execute(
            """
            INSERT INTO router.route_price_versions (
                id, provider_model_route_id, source_snapshot_id, version_number,
                currency, status, accepted_at
            ) VALUES (%s, %s, NULL, %s, %s, 'current', %s)
            """,
            (
                price_version_id,
                route_id,
                next_version[0],
                item.prices[0].currency,
                now,
            ),
        )
        for component in item.prices:
            connection.execute(
                """
                INSERT INTO router.route_price_components (
                    price_version_id, component_kind, unit_name, unit_quantity,
                    unit_price, raw_source_value
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    price_version_id,
                    component.unit.value,
                    component.unit.value,
                    component.unit_quantity,
                    component.price,
                    component.raw_source_value,
                ),
            )
    elif version is not None and version[0] != route_id:
        msg = "The route price version belongs to a different route."
        raise RuntimeError(msg)
    elif version is not None:
        stored_rows = connection.execute(
            """
            SELECT component.unit_name, component.unit_price,
                   version.currency::text, component.raw_source_value,
                   component.unit_quantity
            FROM router.route_price_versions AS version
            JOIN router.route_price_components AS component
              ON component.price_version_id = version.id
            WHERE version.id = %s
            ORDER BY component.unit_name
            """,
            (price_version_id,),
        ).fetchall()
        stored_prices = tuple(
            PriceComponent(UsageUnit(row[0]), row[1], row[2], row[3], row[4])
            for row in stored_rows
        )
        if stored_prices != item.prices:
            msg = "The immutable route price version does not match its content."
            raise RuntimeError(msg)
    if price_version_id is not None:
        connection.execute(
            """
            INSERT INTO router.configuration_price_bindings (
                configuration_revision_id, provider_model_route_id, price_version_id
            ) VALUES (%s, %s, %s)
            """,
            (revision_id, route_id, price_version_id),
        )
    state = cast("SynchronizationState", item.synchronization_state)
    must_replace_state = item.price_authority.mode is PriceAuthorityMode.MANUAL
    if (
        item.price_authority.mode is PriceAuthorityMode.SOURCE
        and previous is not None
        and (
            previous.price_authority != item.price_authority
            or previous.price_version != item.price_version
        )
    ):
        state = SynchronizationState.STALE
        must_replace_state = True
    statement = (
        """
        INSERT INTO router.route_price_synchronization_states (
            provider_model_route_id, synchronization_state,
            last_price_version_id, last_error_class, observed_at
        ) VALUES (%s, %s, %s, NULL, %s)
        ON CONFLICT (provider_model_route_id) DO UPDATE
        SET synchronization_state = EXCLUDED.synchronization_state,
            last_price_version_id = EXCLUDED.last_price_version_id,
            last_error_class = NULL,
            observed_at = EXCLUDED.observed_at
        WHERE EXCLUDED.observed_at >=
              router.route_price_synchronization_states.observed_at
        """
        if must_replace_state
        else """
        INSERT INTO router.route_price_synchronization_states (
            provider_model_route_id, synchronization_state,
            last_price_version_id, last_error_class, observed_at
        ) VALUES (%s, %s, %s, NULL, %s)
        ON CONFLICT (provider_model_route_id) DO NOTHING
        """
    )
    connection.execute(
        statement,
        (
            route_id,
            state.value,
            price_version_id,
            now,
        ),
    )


def _insert_assignment_rows(
    connection: Connection[Any],
    assignments: tuple[Assignment, ...],
    *,
    revision_id: uuid.UUID,
    now: datetime,
    identity_factory: Callable[[], uuid.UUID],
) -> None:
    for assignment in assignments:
        assignment_id = identity_factory()
        connection.execute(
            """
            INSERT INTO router.assignment_definitions (
                id, configuration_revision_id, stable_name, state, created_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                assignment_id,
                revision_id,
                assignment.name,
                assignment.state.value,
                now,
            ),
        )
        for ordinal, candidate in enumerate(assignment.candidates, start=1):
            connection.execute(
                """
                INSERT INTO router.assignment_candidates (
                    assignment_id, configuration_revision_id, ordinal,
                    provider_model_route_id, attempt_timeout_seconds,
                    attempt_timeout_ms,
                    candidate_policy
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    assignment_id,
                    revision_id,
                    ordinal,
                    uuid.UUID(candidate.provider_model_route_id),
                    (candidate.attempt_timeout_ms + 999) // 1_000,
                    candidate.attempt_timeout_ms,
                    Jsonb(
                        {
                            "required_capabilities": sorted(
                                assignment.required_capabilities
                            )
                        }
                    ),
                ),
            )


def _activate_revision(
    connection: Connection[Any],
    scope: ConfigurationScope,
    *,
    revision_id: uuid.UUID,
    revision_number: int,
    now: datetime,
) -> None:
    changed = connection.execute(
        """
        UPDATE router.active_configurations
        SET revision_id = %s, revision_number = %s, activated_at = %s
        WHERE scope_kind = %s
          AND service_id IS NOT DISTINCT FROM %s
          AND workspace_id IS NOT DISTINCT FROM %s
        """,
        (
            revision_id,
            revision_number,
            now,
            scope.kind,
            uuid.UUID(scope.service_id) if scope.service_id is not None else None,
            uuid.UUID(scope.workspace_id) if scope.workspace_id is not None else None,
        ),
    )
    if changed.rowcount == 1:
        return
    connection.execute(
        """
        INSERT INTO router.active_configurations (
            scope_kind, service_id, workspace_id, revision_id,
            revision_number, activated_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            scope.kind,
            uuid.UUID(scope.service_id) if scope.service_id is not None else None,
            uuid.UUID(scope.workspace_id) if scope.workspace_id is not None else None,
            revision_id,
            revision_number,
            now,
        ),
    )


def _insert_audit(  # noqa: PLR0913
    connection: Connection[Any],
    context: RequestContext,
    *,
    event_id: uuid.UUID,
    revision_id: uuid.UUID,
    scope: ConfigurationScope,
    reason: str,
    action: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            service_id, workspace_id, action, permission_result,
            safe_details, occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'permitted', %s, %s)
        """,
        (
            event_id,
            (
                "global_administration"
                if context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
                else "security"
            ),
            context.actor_kind.value,
            context.actor_id,
            context.authority_class.value,
            uuid.UUID(scope.service_id) if scope.service_id is not None else None,
            uuid.UUID(scope.workspace_id) if scope.workspace_id is not None else None,
            action,
            Jsonb(
                {
                    "resource_type": "configuration_revision",
                    "resource_id": str(revision_id),
                    "reason": reason,
                }
            ),
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO router.configuration_audit_bindings (revision_id, event_id)
        VALUES (%s, %s)
        """,
        (revision_id, event_id),
    )


def _effective_distribution_state(
    connection: Connection[Any], layers: tuple[RevisionLayer, ...]
) -> DistributionState:
    revision_ids = [
        uuid.UUID(layer.revision_id)
        for layer in layers
        if not layer.revision_id.startswith("none:")
    ]
    if not revision_ids:
        return DistributionState.DEGRADED
    rows = connection.execute(
        """
        SELECT state FROM router.configuration_distribution_states
        WHERE revision_id = ANY(%s)
        """,
        (revision_ids,),
    ).fetchall()
    states = {DistributionState(row[0]) for row in rows}
    if DistributionState.DEGRADED in states:
        return DistributionState.DEGRADED
    if len(rows) == len(set(revision_ids)) and states == {DistributionState.CURRENT}:
        return DistributionState.CURRENT
    return DistributionState.DISTRIBUTING


def _check_expected_revision(
    current: tuple[uuid.UUID, int] | None,
    expected: uuid.UUID | None,
    *,
    request_id: str,
) -> None:
    current_id = None if current is None else current[0]
    if current_id != expected:
        raise ConfigurationError(
            ConfigurationErrorCode.REVISION_CONFLICT,
            request_id,
            current_revision=None if current_id is None else str(current_id),
        )


def _lock_scope(connection: Connection[Any], scope: ConfigurationScope) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"configuration:{scope.kind}:{scope.service_id}:{scope.workspace_id}",),
    )


def _require_active_scope(
    connection: Connection[Any],
    scope: ConfigurationScope,
    request_id: str,
    *,
    permit_disabled: bool = False,
) -> None:
    if scope.service_id is None:
        return
    allowed_states = ("active", "disabled") if permit_disabled else ("active",)
    row = connection.execute(
        "SELECT state FROM router.services WHERE id = %s",
        (uuid.UUID(scope.service_id),),
    ).fetchone()
    if row is None or row[0] not in allowed_states:
        raise ConfigurationError(ConfigurationErrorCode.NOT_FOUND, request_id)
    if scope.workspace_id is not None:
        workspace = connection.execute(
            """
            SELECT state FROM router.workspaces
            WHERE id = %s AND service_id = %s
            """,
            (uuid.UUID(scope.workspace_id), uuid.UUID(scope.service_id)),
        ).fetchone()
        if workspace is None or workspace[0] not in allowed_states:
            raise ConfigurationError(ConfigurationErrorCode.NOT_FOUND, request_id)


def _require_write_authority(
    context: RequestContext, scope: ConfigurationScope
) -> None:
    if not context.mutation:
        raise ConfigurationError(
            ConfigurationErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )
    if scope.kind == "global":
        allowed = (
            context.actor_kind is PrincipalKind.ADMINISTRATOR
            and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
            and context.scope.kind is ScopeKind.GLOBAL
            and context.operation
            in {
                "catalog.manage",
                "provider_instance.manage",
                "provider_route.manage",
                "assignment.manage",
            }
        )
    else:
        exact_scope = (
            context.scope.kind
            is (ScopeKind.WORKSPACE if scope.workspace_id else ScopeKind.SERVICE)
            and context.scope.service_id == scope.service_id
            and context.scope.workspace_id == scope.workspace_id
        )
        service_machine = (
            context.actor_kind is PrincipalKind.SERVICE
            and context.actor_id == scope.service_id
            and context.authority_class is AuthorityClass.SERVICE
            and context.authority_path is AuthorityPath.MACHINE
            and context.machine_audience is Audience.CONFIGURATION
            and context.operation == "configuration.write"
        )
        service_embed = (
            context.actor_kind is PrincipalKind.EMBED
            and context.authority_class is AuthorityClass.SERVICE
            and context.authority_path is AuthorityPath.EMBED
            and context.machine_audience is None
            and context.operation == "configuration.write"
        )
        administrator = (
            context.actor_kind is PrincipalKind.ADMINISTRATOR
            and context.authority_class
            in {AuthorityClass.GLOBAL_ADMINISTRATOR, AuthorityClass.SERVICE}
            and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
            and context.machine_audience is None
            and context.operation
            in {
                "provider_instance.manage",
                "provider_route.manage",
                "assignment.manage",
            }
        )
        allowed = exact_scope and (service_machine or service_embed or administrator)
    if not allowed:
        raise ConfigurationError(
            ConfigurationErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )


def _require_read_authority(context: RequestContext, scope: ConfigurationScope) -> None:
    if scope.kind == "global":
        allowed = (
            context.actor_kind is PrincipalKind.ADMINISTRATOR
            and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
            and context.scope.kind is ScopeKind.GLOBAL
            and not context.mutation
            and context.operation
            in {
                "catalog.manage",
                "provider_instance.manage",
                "provider_route.manage",
                "assignment.manage",
            }
        )
    else:
        exact_scope = (
            context.scope.kind
            is (ScopeKind.WORKSPACE if scope.workspace_id else ScopeKind.SERVICE)
            and context.scope.service_id == scope.service_id
            and context.scope.workspace_id == scope.workspace_id
        )
        service_machine = (
            context.actor_kind is PrincipalKind.SERVICE
            and context.actor_id == scope.service_id
            and context.authority_class is AuthorityClass.SERVICE
            and context.authority_path is AuthorityPath.MACHINE
            and context.machine_audience is Audience.CONFIGURATION
            and context.operation == "configuration.read"
        )
        service_embed = (
            context.actor_kind is PrincipalKind.EMBED
            and context.authority_class is AuthorityClass.SERVICE
            and context.authority_path is AuthorityPath.EMBED
            and context.machine_audience is None
            and context.operation == "configuration.read"
        )
        administrator = (
            context.actor_kind is PrincipalKind.ADMINISTRATOR
            and context.authority_class
            in {AuthorityClass.GLOBAL_ADMINISTRATOR, AuthorityClass.SERVICE}
            and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
            and context.machine_audience is None
            and context.operation
            in {
                "provider_instance.manage",
                "provider_route.manage",
                "assignment.manage",
            }
        )
        allowed = (
            exact_scope
            and not context.mutation
            and (service_machine or service_embed or administrator)
        )
    if not allowed:
        raise ConfigurationError(
            ConfigurationErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )


def _require_distribution_authority(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.SYSTEM
        and context.authority_class is AuthorityClass.SYSTEM
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is None
        and context.scope.kind is ScopeKind.GLOBAL
        and context.operation == "configuration.distribution.observe"
        and context.mutation
    ):
        raise ConfigurationError(
            ConfigurationErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )


def _created_by_kind(context: RequestContext) -> str:
    if context.actor_kind is PrincipalKind.ADMINISTRATOR:
        return "administrator"
    return "system" if context.actor_kind is PrincipalKind.SYSTEM else "service"


def _require_reason(value: str) -> None:
    if not 1 <= len(value) <= _MAXIMUM_REASON_CHARACTERS:
        msg = "A configuration reason must contain from 1 to 500 characters."
        raise ValueError(msg)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "A configuration time must include a time zone."
        raise ValueError(msg)


def _parse_optional_uuid(value: str | None, request_id: str) -> uuid.UUID | None:
    return None if value is None else _parse_uuid(value, request_id)


def _parse_uuid(value: str, request_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise ConfigurationError(
            ConfigurationErrorCode.NOT_FOUND, request_id
        ) from error


def _deduplicate_issues(issues: list[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    return tuple(dict.fromkeys(issues))


def _require_scope_identities(scope: ConfigurationScope, request_id: str) -> None:
    _parse_optional_uuid(scope.service_id, request_id)
    _parse_optional_uuid(scope.workspace_id, request_id)


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _prepare_price_content(
    value: ScopeConfiguration,
    previous: ScopeConfiguration,
    *,
    identity_factory: Callable[[], uuid.UUID],
) -> ScopeConfiguration:
    """Assign server-owned immutable price identities before publication."""
    old_routes = {
        item.provider_model_route_id: item for item in previous.provider_model_routes
    }
    routes: list[ProviderModelRoute] = []
    for item in value.provider_model_routes:
        old = old_routes.get(item.provider_model_route_id)
        same_prices = old is not None and old.prices == item.prices
        price_version_id = str(identity_factory()) if item.prices else None
        if old is not None and same_prices and old.price_version is not None:
            price_version_id = old.price_version
        if item.price_authority.mode is PriceAuthorityMode.MANUAL:
            state = SynchronizationState.MANUAL
        elif (
            old is not None
            and old.price_authority == item.price_authority
            and same_prices
        ):
            state = cast("SynchronizationState", old.synchronization_state)
        else:
            state = (
                SynchronizationState.CURRENT
                if item.prices
                else SynchronizationState.MISSING
            )
        routes.append(
            replace(
                item,
                price_version=price_version_id,
                synchronization_state=state,
            )
        )
    return replace(value, provider_model_routes=tuple(routes))


def _encode_content(value: ScopeConfiguration) -> dict[str, Any]:
    return {
        "catalog": [
            {
                "kind": item.kind.value,
                "stable_id": item.stable_id,
                "display_name": item.display_name,
                "capabilities": sorted(item.capabilities),
                "state": item.state.value,
                "settings": None
                if item.settings is None
                else _encode_document(item.settings),
            }
            for item in sorted(value.catalog, key=lambda item: item.stable_id)
        ],
        "provider_instances": [
            {
                "provider_instance_id": item.provider_instance_id,
                "provider_catalog_id": item.provider_catalog_id,
                "display_name": item.display_name,
                "endpoint": item.endpoint,
                "credential_id": item.credential_id,
                "settings": _encode_document(item.settings),
                "state": item.state.value,
                "eligible_service_ids": sorted(item.eligible_service_ids),
            }
            for item in sorted(
                value.provider_instances,
                key=lambda item: item.provider_instance_id,
            )
        ],
        "provider_model_routes": [
            {
                "provider_model_route_id": item.provider_model_route_id,
                "provider_instance_id": item.provider_instance_id,
                "canonical_model_id": item.canonical_model_id,
                "wire_model": item.wire_model,
                "capabilities": sorted(item.capabilities),
                "settings": _encode_document(item.settings),
                "price_authority": {
                    "mode": item.price_authority.mode.value,
                    "source_name": item.price_authority.source_name,
                    "lookup_identifier": item.price_authority.lookup_identifier,
                },
                "prices": [
                    {
                        "unit": price.unit.value,
                        "price": str(price.price),
                        "currency": price.currency,
                        "raw_source_value": price.raw_source_value,
                        "unit_quantity": str(price.unit_quantity),
                    }
                    for price in item.prices
                ],
                "synchronization_schedule": item.synchronization_schedule,
                "stale_after_seconds": item.stale_after_seconds,
                "price_version": item.price_version,
                "synchronization_state": cast(
                    "SynchronizationState", item.synchronization_state
                ).value,
                "state": item.state.value,
                "eligible_service_ids": sorted(item.eligible_service_ids),
                "embedding_model_space_id": item.embedding_model_space_id,
                "embedding_dimensions": item.embedding_dimensions,
            }
            for item in sorted(
                value.provider_model_routes,
                key=lambda item: item.provider_model_route_id,
            )
        ],
        "assignments": [
            {
                "name": item.name,
                "candidates": [
                    {
                        "provider_model_route_id": candidate.provider_model_route_id,
                        "attempt_timeout_ms": candidate.attempt_timeout_ms,
                    }
                    for candidate in item.candidates
                ],
                "required_capabilities": sorted(item.required_capabilities),
                "state": item.state.value,
            }
            for item in sorted(value.assignments, key=lambda item: item.name)
        ],
        "inherited_disables": [
            {
                "resource_kind": item.resource_kind.value,
                "resource_id": item.resource_id,
            }
            for item in sorted(
                value.inherited_disables,
                key=lambda item: (item.resource_kind.value, item.resource_id),
            )
        ],
    }


def _encode_document(value: RegisteredDocument) -> dict[str, Any]:
    return {
        "schema_name": value.schema_name,
        "major_version": value.major_version,
        "document": _thaw_json(value.document),
    }


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_thaw_json(item) for item in value]
    return value


def _decode_content(value: dict[str, Any]) -> ScopeConfiguration:
    try:
        return ScopeConfiguration(
            catalog=tuple(
                CatalogEntry(
                    kind=CatalogKind(item["kind"]),
                    stable_id=item["stable_id"],
                    display_name=item["display_name"],
                    capabilities=frozenset(item["capabilities"]),
                    state=ConfigurationState(item["state"]),
                    settings=(
                        None
                        if item["settings"] is None
                        else _decode_document(item["settings"])
                    ),
                )
                for item in value.get("catalog", [])
            ),
            provider_instances=tuple(
                ProviderInstance(
                    provider_instance_id=item["provider_instance_id"],
                    provider_catalog_id=item["provider_catalog_id"],
                    display_name=item["display_name"],
                    endpoint=item["endpoint"],
                    credential_id=item["credential_id"],
                    settings=_decode_document(item["settings"]),
                    state=ConfigurationState(item["state"]),
                    eligible_service_ids=frozenset(item["eligible_service_ids"]),
                )
                for item in value.get("provider_instances", [])
            ),
            provider_model_routes=tuple(
                ProviderModelRoute(
                    provider_model_route_id=item["provider_model_route_id"],
                    provider_instance_id=item["provider_instance_id"],
                    canonical_model_id=item["canonical_model_id"],
                    wire_model=item["wire_model"],
                    capabilities=frozenset(item["capabilities"]),
                    settings=_decode_document(item["settings"]),
                    price_authority=PriceAuthority(
                        mode=PriceAuthorityMode(item["price_authority"]["mode"]),
                        source_name=item["price_authority"].get("source_name"),
                        lookup_identifier=item["price_authority"].get(
                            "lookup_identifier"
                        ),
                    ),
                    prices=tuple(
                        PriceComponent(
                            unit=UsageUnit(price["unit"]),
                            price=price["price"],
                            currency=price["currency"],
                            raw_source_value=price["raw_source_value"],
                            unit_quantity=price.get("unit_quantity", "1"),
                        )
                        for price in item["prices"]
                    ),
                    synchronization_schedule=item["synchronization_schedule"],
                    stale_after_seconds=item["stale_after_seconds"],
                    price_version=item["price_version"],
                    synchronization_state=SynchronizationState(
                        item["synchronization_state"]
                    ),
                    state=ConfigurationState(item["state"]),
                    eligible_service_ids=frozenset(item["eligible_service_ids"]),
                    embedding_model_space_id=item.get("embedding_model_space_id"),
                    embedding_dimensions=item.get("embedding_dimensions"),
                )
                for item in value.get("provider_model_routes", [])
            ),
            assignments=tuple(
                Assignment(
                    name=item["name"],
                    candidates=tuple(
                        AssignmentCandidate(
                            provider_model_route_id=candidate[
                                "provider_model_route_id"
                            ],
                            attempt_timeout_ms=candidate["attempt_timeout_ms"],
                        )
                        for candidate in item["candidates"]
                    ),
                    required_capabilities=frozenset(item["required_capabilities"]),
                    state=ConfigurationState(item["state"]),
                )
                for item in value.get("assignments", [])
            ),
            inherited_disables=tuple(
                InheritedDisable(
                    resource_kind=ResourceKind(item["resource_kind"]),
                    resource_id=item["resource_id"],
                )
                for item in value.get("inherited_disables", [])
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        msg = "Stored configuration content is invalid."
        raise RuntimeError(msg) from error


def _decode_document(value: dict[str, Any]) -> RegisteredDocument:
    return RegisteredDocument(
        schema_name=value["schema_name"],
        major_version=value["major_version"],
        document=value["document"],
    )
