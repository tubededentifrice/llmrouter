"""Direct current assignment storage, inheritance, and runtime selection."""
# ruff: noqa: ANN401, EM101, PLR0913, TRY003

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from llmrouter_backend import catalog
from llmrouter_backend.errors import (
    ApiError,
    assignment_cycle,
    invalid_request,
    not_found,
    provider_unavailable,
)
from llmrouter_backend.store import record_activity

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from psycopg import Connection

    from llmrouter_backend.catalog import ProviderRoute
    from llmrouter_backend.models import AssignmentWrite, ReasoningLevel

# Catalog and assignment writes share one lock because each validates the other.
_ASSIGNMENT_WRITE_LOCK = 4_993_044_345_823
_MAXIMUM_API_NAME_LENGTH = 63
_MAXIMUM_ACTOR_SUBJECT_LENGTH = 500
_MAXIMUM_EMBEDDING_DIMENSION = 65_536
_MAXIMUM_INPUT_IMAGES = 8
_MAXIMUM_INPUT_IMAGE_BYTES = 20 * 1024 * 1024
_MAXIMUM_TOTAL_IMAGE_BYTES = 50 * 1024 * 1024
_MAXIMUM_OUTPUT_DURATION_SECONDS = 86_400
_ASSIGNMENT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$", re.ASCII)
_ACTIVITY_NAMESPACE = uuid.UUID("4ee89ff5-9be7-4e0e-a040-b2c9b70deca5")
_OBSERVED_REQUIREMENTS = frozenset(
    {
        "text_input",
        "image_input",
        "text_output",
        "structured_json_output",
        "tool_calling",
        "streaming",
        "reasoning",
        "embedding_output",
        "image_output",
        "video_output",
        "audio_output",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedAssignment:
    """One deterministic effective assignment before runtime filtering."""

    api_name: str
    display_name: str
    definition_kind: str
    defined_by_service_api_name: str | None
    inherits_assignment_api_name: str | None
    direct_chain: tuple[str, ...] | None
    effective_chain: tuple[str, ...]
    reasoning_level: ReasoningLevel | None
    observed_requirements: tuple[str, ...]
    last_used_at: datetime | None
    created_at: datetime | None
    definition_id: uuid.UUID | None

    def response(self) -> dict[str, Any]:
        """Build the closed native assignment response."""
        result: dict[str, Any] = {
            "api_name": self.api_name,
            "display_name": self.display_name,
            "definition_kind": self.definition_kind,
            "effective_chain": [
                {"provider_model_api_name": name} for name in self.effective_chain
            ],
            "observed_requirements": list(self.observed_requirements),
        }
        optional = {
            "defined_by_service_api_name": self.defined_by_service_api_name,
            "inherits_assignment_api_name": self.inherits_assignment_api_name,
            "reasoning_level": self.reasoning_level,
            "last_used_at": self.last_used_at,
            "created_at": self.created_at,
        }
        result.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        if self.direct_chain is not None:
            result["direct_chain"] = [
                {"provider_model_api_name": name} for name in self.direct_chain
            ]
        return result


def get_assignment(
    connection: Connection[Any], *, service_id: uuid.UUID, api_name: str
) -> dict[str, Any]:
    """Read one effective assignment only in the selected service scope."""
    resolved = resolve_assignment(connection, service_id=service_id, api_name=api_name)
    if resolved is None:
        raise not_found("assignment")
    return resolved.response()


def list_assignments(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """List the deterministic effective assignment names for one service."""
    chain = _service_chain(connection, service_id)
    if not chain:
        raise not_found("service")
    service_ids = [item[0] for item in chain]
    names = connection.execute(
        """SELECT api_name FROM (
               SELECT api_name FROM router.assignment_definitions
               WHERE service_id = ANY(%s)
               UNION SELECT 'default'::router.assignment_name
           ) AS names
           WHERE (%s::text IS NULL OR api_name > %s)
           ORDER BY api_name LIMIT %s""",
        (service_ids, cursor, cursor, limit + 1),
    ).fetchall()
    selected = names[:limit]
    items = [
        cast(
            "ResolvedAssignment",
            resolve_assignment(
                connection, service_id=service_id, api_name=row["api_name"]
            ),
        ).response()
        for row in selected
    ]
    next_cursor = str(selected[-1]["api_name"]) if len(names) > limit else None
    return items, next_cursor


def put_assignment(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    api_name: str,
    value: AssignmentWrite,
) -> dict[str, Any]:
    """Create or replace one complete local definition atomically."""
    _lock_writes(connection)
    _require_service(connection, service_id)
    candidates = tuple(
        item.provider_model_api_name for item in (value.direct_chain or ())
    )
    if candidates:
        _validate_direct_candidates(connection, candidates, value.reasoning_level)
    display_name = (
        value.display_name
        or _existing_display_name(connection, service_id, api_name)
        or _default_display_name(api_name)
    )
    row = connection.execute(
        """INSERT INTO router.assignment_definitions
               (service_id, api_name, display_name,
                inherits_assignment_api_name, reasoning_level)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (service_id, api_name) DO UPDATE SET
               display_name = EXCLUDED.display_name,
               inherits_assignment_api_name = EXCLUDED.inherits_assignment_api_name,
               reasoning_level = EXCLUDED.reasoning_level
           RETURNING id""",
        (
            service_id,
            api_name,
            display_name,
            value.inherits_assignment_api_name,
            value.reasoning_level,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("The assignment write did not return its identity.")
    assignment_id = cast("uuid.UUID", row["id"])
    connection.execute(
        "DELETE FROM router.assignment_candidates WHERE assignment_id = %s",
        (assignment_id,),
    )
    for position, candidate in enumerate(candidates):
        inserted = connection.execute(
            """INSERT INTO router.assignment_candidates
                   (assignment_id, position, provider_model_id)
               SELECT %s, %s, id FROM router.provider_models WHERE api_name = %s""",
            (assignment_id, position, candidate),
        )
        if inserted.rowcount != 1:
            raise invalid_request(
                "direct_chain", "A provider-model candidate does not exist."
            )
    validate_all_assignments(connection)
    resolved = resolve_assignment(connection, service_id=service_id, api_name=api_name)
    if resolved is None:
        raise RuntimeError("The assignment write did not resolve.")
    return resolved.response()


def delete_assignment(
    connection: Connection[Any], *, service_id: uuid.UUID, api_name: str
) -> uuid.UUID:
    """Delete one local definition and validate every dependent assignment."""
    _lock_writes(connection)
    row = connection.execute(
        """DELETE FROM router.assignment_definitions
           WHERE service_id = %s AND api_name = %s RETURNING id""",
        (service_id, api_name),
    ).fetchone()
    if row is None:
        raise not_found("assignment")
    validate_all_assignments(connection)
    return cast("uuid.UUID", row["id"])


def remove_observed_requirement(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    api_name: str,
    observed_requirement: str,
) -> uuid.UUID:
    """Remove one local observed item without changing runtime policy."""
    if observed_requirement not in _OBSERVED_REQUIREMENTS:
        raise invalid_request(
            "observed_requirement", "The observed requirement is not supported."
        )
    resolved = resolve_assignment(connection, service_id=service_id, api_name=api_name)
    if resolved is None:
        raise not_found("assignment")
    connection.execute(
        """UPDATE router.assignment_usage
           SET observed_requirements = array_remove(observed_requirements, %s)
           WHERE service_id = %s AND api_name = %s""",
        (observed_requirement, service_id, api_name),
    )
    return resolved.definition_id or _activity_resource_id(service_id, api_name)


def resolve_assignment(
    connection: Connection[Any], *, service_id: uuid.UUID, api_name: str
) -> ResolvedAssignment | None:
    """Resolve nearest definitions through the called service parent chain."""
    chain = _service_chain(connection, service_id)
    if not chain:
        raise not_found("service")
    usage = _usage(connection, service_id, api_name)
    return _resolve_name(
        connection,
        chain=chain,
        api_name=api_name,
        usage=usage,
        active=(),
    )


def resolve_assignment_for_call(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    workspace_api_name: str,
    assignment_api_name: str,
    required_inputs: frozenset[str],
    required_output: str,
    required_capabilities: frozenset[str],
    actor_subject: str,
    embedding_dimension: int | None = None,
    input_image_sizes: Sequence[int] = (),
    output_duration_seconds: int | None = None,
    excluded_provider_model_api_names: Sequence[str] = (),
    commit_evidence: bool = True,
) -> tuple[ResolvedAssignment, tuple[ProviderRoute, ...]]:
    """Observe one validated call and filter candidates by its actual shape."""
    _require_assignment_name(assignment_api_name)
    _require_actor_subject(actor_subject)
    _validate_actual_bounds(
        required_inputs=required_inputs,
        required_output=required_output,
        embedding_dimension=embedding_dimension,
        input_image_sizes=input_image_sizes,
        output_duration_seconds=output_duration_seconds,
    )
    observed = _actual_observations(
        required_inputs,
        required_output,
        required_capabilities,
    )
    workspace = connection.execute(
        """SELECT service.api_name AS service_api_name
           FROM router.workspaces AS workspace
           JOIN router.services AS service ON service.id = workspace.service_id
           WHERE workspace.service_id = %s AND workspace.api_name = %s
           FOR KEY SHARE OF workspace, service""",
        (service_id, workspace_api_name),
    ).fetchone()
    if workspace is None:
        raise not_found("workspace")
    # Admission uses one current assignment and catalog snapshot. Configuration
    # writes that start later wait until this transaction records its evidence.
    _lock_writes(connection)
    resolved = resolve_assignment(
        connection, service_id=service_id, api_name=assignment_api_name
    )
    if resolved is None:
        resolved = _create_automatic_assignment(
            connection,
            service_id=service_id,
            service_api_name=workspace["service_api_name"],
            api_name=assignment_api_name,
            actor_subject=actor_subject,
        )
    if resolved is None:
        raise RuntimeError("The automatic assignment did not resolve.")
    routes: list[ProviderRoute] = []
    excluded = frozenset(excluded_provider_model_api_names)
    for candidate in resolved.effective_chain:
        if candidate in excluded:
            continue
        try:
            route = catalog.resolve_provider_route(
                connection,
                candidate,
                required_inputs=required_inputs,
                required_output=required_output,
                required_capabilities=required_capabilities,
                reasoning_level=resolved.reasoning_level,
            )
            catalog.validate_route_constraints(
                route,
                embedding_dimension=embedding_dimension,
                input_image_sizes=input_image_sizes,
                output_duration_seconds=output_duration_seconds,
            )
            routes.append(route)
        except ApiError as error:
            if error.code not in {"invalid_request", "provider_unavailable"}:
                raise
    _record_use(
        connection,
        service_id=service_id,
        api_name=assignment_api_name,
        observed_requirements=observed,
    )
    # Admission evidence and an automatic first-use definition must survive a
    # later empty-chain, eligibility, or provider failure.
    if commit_evidence:
        connection.commit()
    if not routes:
        raise provider_unavailable()
    return resolved, tuple(routes)


def resolve_assignment_snapshot_for_administrator(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    assignment_api_name: str,
    required_inputs: frozenset[str],
    required_output: str,
    required_capabilities: frozenset[str],
    embedding_dimension: int | None = None,
    input_image_sizes: Sequence[int] = (),
    output_duration_seconds: int | None = None,
    excluded_provider_model_api_names: Sequence[str] = (),
    allow_empty: bool = False,
) -> tuple[ResolvedAssignment, tuple[ProviderRoute, ...]]:
    """Resolve one read-only administrator assignment admission snapshot."""
    _require_assignment_name(assignment_api_name)
    _validate_actual_bounds(
        required_inputs=required_inputs,
        required_output=required_output,
        embedding_dimension=embedding_dimension,
        input_image_sizes=input_image_sizes,
        output_duration_seconds=output_duration_seconds,
    )
    # Use the same serialization boundary as assignment and catalog writes. The
    # selected chain and all routes therefore come from one current state.
    _lock_writes(connection)
    resolved = resolve_assignment(
        connection, service_id=service_id, api_name=assignment_api_name
    )
    if resolved is None:
        raise not_found("assignment")
    routes: list[ProviderRoute] = []
    excluded = frozenset(excluded_provider_model_api_names)
    for candidate in resolved.effective_chain:
        if candidate in excluded:
            continue
        try:
            route = catalog.resolve_provider_route(
                connection,
                candidate,
                required_inputs=required_inputs,
                required_output=required_output,
                required_capabilities=required_capabilities,
                reasoning_level=resolved.reasoning_level,
            )
            catalog.validate_route_constraints(
                route,
                embedding_dimension=embedding_dimension,
                input_image_sizes=input_image_sizes,
                output_duration_seconds=output_duration_seconds,
            )
            routes.append(route)
        except ApiError as error:
            if error.code not in {"invalid_request", "provider_unavailable"}:
                raise
    if not routes and not allow_empty:
        raise provider_unavailable()
    return resolved, tuple(routes)


def validate_all_assignments(connection: Connection[Any]) -> None:
    """Reject cycles, missing parents, and invalid effective reasoning."""
    _lock_writes(connection)
    rows = connection.execute(
        "SELECT id FROM router.services ORDER BY api_name"
    ).fetchall()
    for row in rows:
        service_id = cast("uuid.UUID", row["id"])
        names = connection.execute(
            """SELECT DISTINCT definition.api_name
               FROM router.assignment_definitions AS definition
               JOIN (
                   WITH RECURSIVE chain(id, parent_service_id, path) AS (
                       SELECT id, parent_service_id, ARRAY[id]
                       FROM router.services WHERE id = %s
                     UNION ALL
                       SELECT parent.id, parent.parent_service_id,
                              chain.path || parent.id
                       FROM router.services AS parent
                       JOIN chain ON parent.id = chain.parent_service_id
                       WHERE NOT parent.id = ANY(chain.path)
                   ) SELECT id FROM chain
               ) AS service_chain ON service_chain.id = definition.service_id""",
            (service_id,),
        ).fetchall()
        for name_row in names:
            resolved = resolve_assignment(
                connection, service_id=service_id, api_name=name_row["api_name"]
            )
            if resolved is None:
                raise invalid_request(
                    "inherits_assignment_api_name",
                    "An inherited assignment does not resolve.",
                )
            if resolved.effective_chain:
                _validate_direct_candidates(
                    connection,
                    resolved.effective_chain,
                    resolved.reasoning_level,
                )
    _prune_absent_usage(connection)


def configuration_change(
    connection: Connection[Any],
    *,
    actor_subject: str,
    service_id: uuid.UUID,
    service_api_name: str,
    action: str,
    assignment_api_name: str,
    operation: Callable[[], Any],
) -> Any:
    """Record one successful or failed assignment change without field values."""
    try:
        result = operation()
        resource_id = _operation_resource_id(
            connection, service_id, assignment_api_name, result
        )
        record_activity(
            connection,
            actor_subject,
            action,
            "assignment",
            service_api_name=service_api_name,
            resource_api_name=(
                assignment_api_name if _is_api_name(assignment_api_name) else None
            ),
            resource_id=resource_id,
        )
    except Exception:
        connection.rollback()
        resource_id = _operation_resource_id(
            connection, service_id, assignment_api_name, None
        )
        record_activity(
            connection,
            actor_subject,
            action,
            "assignment",
            service_api_name=service_api_name,
            resource_api_name=(
                assignment_api_name if _is_api_name(assignment_api_name) else None
            ),
            resource_id=resource_id,
            result="failed",
        )
        connection.commit()
        raise
    return result


def _resolve_name(
    connection: Connection[Any],
    *,
    chain: Sequence[tuple[uuid.UUID, str]],
    api_name: str,
    usage: dict[str, Any] | None,
    active: tuple[str, ...],
) -> ResolvedAssignment | None:
    if api_name in active:
        raise assignment_cycle()
    definition = _nearest_definition(connection, chain, api_name)
    if definition is None:
        if api_name != "default":
            return None
        return ResolvedAssignment(
            api_name="default",
            display_name="Default",
            definition_kind="implicit",
            defined_by_service_api_name=chain[-1][1],
            inherits_assignment_api_name=None,
            direct_chain=None,
            effective_chain=(),
            reasoning_level=None,
            observed_requirements=tuple(
                sorted((usage or {}).get("observed_requirements", ()))
            ),
            last_used_at=(usage or {}).get("last_used_at"),
            created_at=None,
            definition_id=None,
        )
    direct_chain = _candidate_names(connection, definition["id"])
    inherited_name = definition["inherits_assignment_api_name"]
    if inherited_name is not None:
        inherited = _resolve_name(
            connection,
            chain=chain,
            api_name=inherited_name,
            usage=None,
            active=(*active, api_name),
        )
        if inherited is None:
            raise invalid_request(
                "inherits_assignment_api_name",
                "An inherited assignment does not resolve.",
            )
        effective_chain = inherited.effective_chain
        reasoning = definition["reasoning_level"] or inherited.reasoning_level
        kind = "inherited_assignment"
        direct: tuple[str, ...] | None = None
    else:
        if not direct_chain:
            raise invalid_request(
                "direct_chain", "A direct assignment must have one candidate."
            )
        effective_chain = direct_chain
        reasoning = definition["reasoning_level"]
        kind = "direct_chain"
        direct = direct_chain
    return ResolvedAssignment(
        api_name=api_name,
        display_name=definition["display_name"],
        definition_kind=kind,
        defined_by_service_api_name=definition["service_api_name"],
        inherits_assignment_api_name=inherited_name,
        direct_chain=direct,
        effective_chain=effective_chain,
        reasoning_level=reasoning,
        observed_requirements=tuple(
            sorted((usage or {}).get("observed_requirements", ()))
        ),
        last_used_at=(usage or {}).get("last_used_at"),
        created_at=definition["created_at"],
        definition_id=definition["id"],
    )


def _service_chain(
    connection: Connection[Any], service_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    rows = connection.execute(
        """WITH RECURSIVE chain(id, api_name, parent_service_id, depth, path) AS (
               SELECT id, api_name, parent_service_id, 0, ARRAY[id]
               FROM router.services WHERE id = %s
             UNION ALL
               SELECT parent.id, parent.api_name, parent.parent_service_id,
                      chain.depth + 1, chain.path || parent.id
               FROM router.services AS parent
               JOIN chain ON parent.id = chain.parent_service_id
               WHERE NOT parent.id = ANY(chain.path)
           )
           SELECT id, api_name FROM chain ORDER BY depth""",
        (service_id,),
    ).fetchall()
    return [(row["id"], row["api_name"]) for row in rows]


def _nearest_definition(
    connection: Connection[Any],
    chain: Sequence[tuple[uuid.UUID, str]],
    api_name: str,
) -> dict[str, Any] | None:
    for service_id, service_api_name in chain:
        row = connection.execute(
            """SELECT id, display_name, inherits_assignment_api_name,
                      reasoning_level, created_at
               FROM router.assignment_definitions
               WHERE service_id = %s AND api_name = %s""",
            (service_id, api_name),
        ).fetchone()
        if row is not None:
            row["service_api_name"] = service_api_name
            return cast("dict[str, Any]", row)
    return None


def _candidate_names(
    connection: Connection[Any], assignment_id: uuid.UUID
) -> tuple[str, ...]:
    rows = connection.execute(
        """SELECT mapping.api_name
           FROM router.assignment_candidates AS candidate
           JOIN router.provider_models AS mapping
             ON mapping.id = candidate.provider_model_id
           WHERE candidate.assignment_id = %s ORDER BY candidate.position""",
        (assignment_id,),
    ).fetchall()
    return tuple(row["api_name"] for row in rows)


def _validate_direct_candidates(
    connection: Connection[Any],
    candidates: Sequence[str],
    reasoning_level: ReasoningLevel | None,
) -> None:
    try:
        catalog.validate_assignment_reasoning(connection, candidates, reasoning_level)
    except ApiError as error:
        if error.code == "provider_unavailable":
            raise invalid_request(
                "direct_chain",
                "Each provider-model candidate must exist and be enabled.",
            ) from error
        raise


def _create_automatic_assignment(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    service_api_name: str,
    api_name: str,
    actor_subject: str,
) -> ResolvedAssignment:
    _lock_writes(connection)
    current = resolve_assignment(connection, service_id=service_id, api_name=api_name)
    if current is not None:
        return current
    if api_name == "default":
        raise RuntimeError("The implicit default assignment did not resolve.")
    row = connection.execute(
        """INSERT INTO router.assignment_definitions
               (service_id, api_name, display_name, inherits_assignment_api_name)
           VALUES (%s, %s, %s, 'default')
           ON CONFLICT (service_id, api_name) DO NOTHING
           RETURNING id""",
        (service_id, api_name, _default_display_name(api_name)),
    ).fetchone()
    if row is not None:
        record_activity(
            connection,
            actor_subject,
            "assignment.create",
            "assignment",
            service_api_name=service_api_name,
            resource_api_name=api_name if _is_api_name(api_name) else None,
            resource_id=row["id"],
        )
    resolved = resolve_assignment(connection, service_id=service_id, api_name=api_name)
    if resolved is None:
        raise RuntimeError("The automatic assignment did not resolve.")
    return resolved


def _record_use(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    api_name: str,
    observed_requirements: frozenset[str],
) -> None:
    connection.execute(
        """INSERT INTO router.assignment_usage
               (service_id, api_name, observed_requirements, last_used_at)
           VALUES (%s, %s, %s, statement_timestamp())
           ON CONFLICT (service_id, api_name) DO UPDATE SET
               observed_requirements = ARRAY(
                   SELECT DISTINCT item FROM unnest(
                       assignment_usage.observed_requirements ||
                       EXCLUDED.observed_requirements
                   ) AS item ORDER BY item
               ),
               last_used_at = statement_timestamp()""",
        (service_id, api_name, sorted(observed_requirements)),
    )


def _actual_observations(
    required_inputs: frozenset[str],
    required_output: str,
    required_capabilities: frozenset[str],
) -> frozenset[str]:
    observations = {
        *(f"{item}_input" for item in required_inputs),
        f"{required_output}_output",
        *required_capabilities,
    }
    if not observations <= _OBSERVED_REQUIREMENTS:
        raise invalid_request(
            "requirements", "The call requirements are not supported."
        )
    return frozenset(observations)


def _validate_actual_bounds(
    *,
    required_inputs: frozenset[str],
    required_output: str,
    embedding_dimension: int | None,
    input_image_sizes: Sequence[int],
    output_duration_seconds: int | None,
) -> None:
    has_image_input = bool(input_image_sizes)
    if has_image_input != ("image" in required_inputs):
        raise invalid_request(
            "images", "Image sizes must match the actual input modalities."
        )
    if embedding_dimension is not None and required_output != "embedding":
        raise invalid_request(
            "embedding_dimension",
            "An embedding dimension requires embedding output.",
        )
    if output_duration_seconds is not None and required_output not in {
        "video",
        "audio",
    }:
        raise invalid_request(
            "duration", "An output duration requires video or audio output."
        )
    if embedding_dimension is not None and (
        type(embedding_dimension) is not int
        or not 1 <= embedding_dimension <= _MAXIMUM_EMBEDDING_DIMENSION
    ):
        raise invalid_request(
            "embedding_dimension", "The embedding dimension is invalid."
        )
    if (
        len(input_image_sizes) > _MAXIMUM_INPUT_IMAGES
        or any(
            type(size) is not int or not 1 <= size <= _MAXIMUM_INPUT_IMAGE_BYTES
            for size in input_image_sizes
        )
        or sum(input_image_sizes) > _MAXIMUM_TOTAL_IMAGE_BYTES
    ):
        raise invalid_request("images", "The input image bounds are exceeded.")
    if output_duration_seconds is not None and (
        type(output_duration_seconds) is not int
        or not 1 <= output_duration_seconds <= _MAXIMUM_OUTPUT_DURATION_SECONDS
    ):
        raise invalid_request("duration", "The output duration is invalid.")


def _prune_absent_usage(connection: Connection[Any]) -> None:
    rows = connection.execute(
        """SELECT service_id, api_name FROM router.assignment_usage
           WHERE api_name <> 'default' ORDER BY service_id, api_name"""
    ).fetchall()
    for row in rows:
        if (
            resolve_assignment(
                connection,
                service_id=row["service_id"],
                api_name=row["api_name"],
            )
            is None
        ):
            connection.execute(
                """DELETE FROM router.assignment_usage
                   WHERE service_id = %s AND api_name = %s""",
                (row["service_id"], row["api_name"]),
            )


def _usage(
    connection: Connection[Any], service_id: uuid.UUID, api_name: str
) -> dict[str, Any] | None:
    return connection.execute(
        """SELECT observed_requirements, last_used_at
           FROM router.assignment_usage WHERE service_id = %s AND api_name = %s""",
        (service_id, api_name),
    ).fetchone()


def _lock_writes(connection: Connection[Any]) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ASSIGNMENT_WRITE_LOCK,))


def _require_service(connection: Connection[Any], service_id: uuid.UUID) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM router.services WHERE id = %s", (service_id,)
        ).fetchone()
        is None
    ):
        raise not_found("service")


def _require_assignment_name(value: str) -> None:
    if _ASSIGNMENT_NAME.fullmatch(value) is None:
        raise invalid_request("assignment_api_name", "The assignment name is invalid.")


def _require_actor_subject(value: str) -> None:
    if not 1 <= len(value) <= _MAXIMUM_ACTOR_SUBJECT_LENGTH:
        raise invalid_request("actor", "The service actor identity is invalid.")


def _existing_display_name(
    connection: Connection[Any], service_id: uuid.UUID, api_name: str
) -> str | None:
    row = connection.execute(
        """SELECT display_name FROM router.assignment_definitions
           WHERE service_id = %s AND api_name = %s""",
        (service_id, api_name),
    ).fetchone()
    return cast("str", row["display_name"]) if row is not None else None


def _default_display_name(api_name: str) -> str:
    return api_name.replace("_", " ").replace("-", " ").replace(".", " ").title()


def _operation_resource_id(
    connection: Connection[Any],
    service_id: uuid.UUID,
    api_name: str,
    result: Any,
) -> uuid.UUID:
    if isinstance(result, uuid.UUID):
        return result
    row = connection.execute(
        """SELECT id FROM router.assignment_definitions
           WHERE service_id = %s AND api_name = %s""",
        (service_id, api_name),
    ).fetchone()
    return (
        cast("uuid.UUID", row["id"])
        if row is not None
        else _activity_resource_id(service_id, api_name)
    )


def _activity_resource_id(service_id: uuid.UUID, api_name: str) -> uuid.UUID:
    return uuid.uuid5(_ACTIVITY_NAMESPACE, f"{service_id}:{api_name}")


def _is_api_name(value: str) -> bool:
    return (
        1 <= len(value) <= _MAXIMUM_API_NAME_LENGTH
        and value[0].islower()
        and value[0].isascii()
        and value[-1].isalnum()
        and value[-1].isascii()
        and all(
            character.islower() or character.isdigit() or character == "-"
            for character in value
        )
    )
