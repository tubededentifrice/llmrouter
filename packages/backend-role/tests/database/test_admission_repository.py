"""PostgreSQL atomic admission, replay, and scope tests."""
# ruff: noqa: D103, E501

from __future__ import annotations

import concurrent.futures
import hashlib
import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from llmrouter_backend.admission import (
    AdmissionError,
    AdmissionErrorCode,
    AdmissionRequest,
    AttachmentReference,
    FingerprintInput,
    PostgresAdmissionRepository,
    RequestKind,
    RequestState,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.execution import (
    ErrorScope,
    ExecutionKind,
    ExecutionState,
    ExecutionTarget,
    PostgresExecutionRepository,
    TerminalError,
    TerminalErrorClass,
)

from .helpers import CONFIGURATION_ID, SERVICE_ID, WORKSPACE_ID, seed_scope

NOW = datetime(2026, 8, 13, 18, tzinfo=UTC)
ASSIGNMENT_ID = "0198a080-0000-7000-8000-000000000110"
GLOBAL_CONFIGURATION_ID = "0198a080-0000-7000-8000-000000000116"
MODEL_ID = "0198a080-0000-7000-8000-000000000111"
CREDENTIAL_ID = "0198a080-0000-7000-8000-000000000112"
INSTANCE_ID = "0198a080-0000-7000-8000-000000000113"
ROUTE_ID = "0198a080-0000-7000-8000-000000000114"
PRICE_VERSION_ID = "0198a080-0000-7000-8000-000000000118"
DIAGNOSTIC_GRANT = "A" * 43
ATTACHMENT_ID = "0198a080-0000-7000-8000-000000000115"


def _uuidv7(at: datetime, random_bits: int = 1) -> str:
    milliseconds = int(at.timestamp() * 1000)
    value = (milliseconds << 80) | (7 << 76) | ((random_bits & 0xFFF) << 64)
    value |= (2 << 62) | (random_bits & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


def _context(
    operation: str = "model.create",
    *,
    workspace_id: str | None = WORKSPACE_ID,
    mutation: bool = True,
    authority_class: AuthorityClass = AuthorityClass.SERVICE,
) -> RequestContext:
    return RequestContext(
        request_id="transport-request",
        actor_kind=PrincipalKind.SERVICE,
        actor_id=SERVICE_ID,
        authority_class=authority_class,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=Audience.DATA_PLANE,
        operation=operation,
        scope=Scope(SERVICE_ID, workspace_id),
        authorized_at=NOW,
        recent_authentication_at=None,
        mutation=mutation,
    )


def _fingerprint(
    *,
    text: str = "Hello",
    workspace_id: str | None = WORKSPACE_ID,
    attachments: tuple[AttachmentReference, ...] = (),
) -> FingerprintInput:
    content: str | list[dict[str, str]] = text
    if attachments:
        content = [
            {
                "type": "file",
                "attachment_id": item.attachment_id,
                "sha256": item.sha256,
                "media_type": item.media_type,
            }
            for item in attachments
        ]
    return FingerprintInput(
        "model.create",
        1,
        SERVICE_ID,
        workspace_id,
        "service-data",
        {
            "api_version": "1",
            "assignment": "chat",
            "messages": [{"role": "user", "content": content}],
            "limits": {"logical_timeout_ms": 120000},
            "output": {"format": "text"},
        },
        attachments,
    )


def _request(
    request_id: str,
    *,
    text: str = "Hello",
    workspace_id: str | None = WORKSPACE_ID,
    attachments: tuple[AttachmentReference, ...] = (),
) -> AdmissionRequest:
    return AdmissionRequest(
        request_id,
        RequestKind.MODEL,
        _fingerprint(text=text, workspace_id=workspace_id, attachments=attachments),
        assignment="chat",
    )


def _seed_admission_target(connection: psycopg.Connection[object]) -> None:
    seed_scope(connection)
    connection.execute(
        """INSERT INTO router.configuration_revisions (
               id, scope_kind, revision_number, content, content_sha256,
               created_by_kind, created_by_id
           ) VALUES (%s, 'global', 1, '{}'::jsonb, %s, 'system', 'test')""",
        (GLOBAL_CONFIGURATION_ID, bytes.fromhex("55" * 32)),
    )
    connection.execute(
        "INSERT INTO router.provider_adapter_types (id, settings_schema_name, settings_schema_major, capabilities) VALUES ('provider.test', 'provider.settings', 1, '{}')"
    )
    connection.execute(
        "INSERT INTO router.canonical_models (id, stable_name, capabilities) VALUES (%s, 'model-test', '{}')",
        (MODEL_ID,),
    )
    connection.execute(
        """INSERT INTO router.encrypted_credentials (
               id, owner_kind, credential_kind, ciphertext, encrypted_data_key,
               wrapping_key_id, safe_fingerprint, current_revision, last_changed_at
           ) VALUES (%s, 'global', 'provider.test', %s, %s, 'wrap', 'safe', %s, %s)""",
        (CREDENTIAL_ID, bytes(32), bytes(32), CREDENTIAL_ID, NOW),
    )
    connection.execute(
        """INSERT INTO router.provider_instances (
               id, owner_kind, adapter_type_id, credential_id, stable_name,
               endpoint_origin, settings_schema_name, settings_schema_major, settings,
               current_revision
           ) VALUES (%s, 'global', 'provider.test', %s, 'instance-test',
                     'https://provider.example', 'provider.settings', 1, '{}', %s)""",
        (INSTANCE_ID, CREDENTIAL_ID, GLOBAL_CONFIGURATION_ID),
    )
    connection.execute(
        """INSERT INTO router.provider_model_routes (
               id, owner_kind, provider_instance_id, canonical_model_id,
               provider_lookup_id, settings_schema_name, settings_schema_major,
               settings, current_revision, wire_model
           ) VALUES (%s, 'global', %s, %s, 'wire-test', 'route.settings', 1,
                     '{}', %s, 'wire-test')""",
        (ROUTE_ID, INSTANCE_ID, MODEL_ID, GLOBAL_CONFIGURATION_ID),
    )
    connection.execute(
        """INSERT INTO router.assignment_definitions (
               id, configuration_revision_id, stable_name
           ) VALUES (%s, %s, 'chat')""",
        (ASSIGNMENT_ID, GLOBAL_CONFIGURATION_ID),
    )
    connection.execute(
        """INSERT INTO router.route_price_versions (
               id, provider_model_route_id, version_number, currency, status, accepted_at
           ) VALUES (%s, %s, 1, 'USD', 'current', %s)""",
        (PRICE_VERSION_ID, ROUTE_ID, NOW),
    )
    connection.execute(
        """INSERT INTO router.route_price_components (
               price_version_id, component_kind, unit_name, unit_quantity,
               unit_price, raw_source_value
           ) VALUES (%s, 'usage', 'input_token', 1000, 0.001, '0.001')""",
        (PRICE_VERSION_ID,),
    )
    connection.execute(
        """INSERT INTO router.configuration_price_bindings (
               configuration_revision_id, provider_model_route_id, price_version_id
           ) VALUES (%s, %s, %s)""",
        (GLOBAL_CONFIGURATION_ID, ROUTE_ID, PRICE_VERSION_ID),
    )
    connection.execute(
        """INSERT INTO router.assignment_candidates (
               assignment_id, configuration_revision_id, ordinal,
               provider_model_route_id, attempt_timeout_seconds,
               attempt_timeout_ms
           ) VALUES (%s, %s, 1, %s, 30, 30000)""",
        (ASSIGNMENT_ID, GLOBAL_CONFIGURATION_ID, ROUTE_ID),
    )
    connection.execute(
        """INSERT INTO router.active_configurations (
               scope_kind, service_id, workspace_id, revision_id, revision_number,
               activated_at
           ) VALUES ('global', NULL, NULL, %s, 1, %s)""",
        (GLOBAL_CONFIGURATION_ID, NOW),
    )


@pytest.fixture
def repository(database_url: str) -> PostgresAdmissionRepository:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed_admission_target(connection)
    return PostgresAdmissionRepository(database_url)


def test_atomic_create_replay_conflict_and_response_loss(
    database_url: str, repository: PostgresAdmissionRepository
) -> None:
    """Create one binding, return safe replay, and reject changed content."""
    request_id = _uuidv7(NOW)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: repository.admit(_context(), _request(request_id), now=NOW),
                range(2),
            )
        )
    assert sorted(item.created for item in results) == [False, True]
    assert [item.external_effects_permitted for item in results].count(True) == 1
    replay = repository.admit(
        _context(), _request(request_id), now=NOW + timedelta(hours=1)
    )
    assert not replay.created
    assert replay.receipt == results[0].receipt
    with pytest.raises(AdmissionError) as conflict:
        repository.admit(_context(), _request(request_id, text="Changed"), now=NOW)
    assert conflict.value.code is AdmissionErrorCode.REQUEST_IDENTITY_CONFLICT
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM router.logical_requests WHERE request_id = %s",
            (request_id,),
        ).fetchone()
    assert count == (1,)


def test_configured_disabled_capture_is_not_spool_pressure(
    database_url: str, repository: PostgresAdmissionRepository
) -> None:
    """Record configured disablement without a pressure reason."""
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO router.capture_policies (
                id, scope_kind, service_id, workspace_id, policy,
                revision, effective_at
            ) VALUES (%s, 'workspace', %s, %s, 'disabled', 1, %s)
            """,
            (uuid.uuid4(), SERVICE_ID, WORKSPACE_ID, NOW),
        )
    receipt = repository.admit(_context(), _request(_uuidv7(NOW, 99)), now=NOW).receipt
    assert not receipt.capture_enabled
    assert receipt.capture_policy == "disabled"
    assert receipt.capture_reason == "configured"
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT capture_pressure_reason, capture_reason
            FROM router.logical_requests WHERE request_id = %s
            """,
            (receipt.request_id,),
        ).fetchone()
    assert row == (None, "configured")


def test_uuid_age_scope_authority_status_and_expired_binding(  # noqa: PLR0915
    database_url: str, repository: PostgresAdmissionRepository
) -> None:
    """Apply age only to first submit and hide status outside the exact scope."""
    old_id = _uuidv7(NOW - timedelta(minutes=16), 2)
    with pytest.raises(AdmissionError) as old:
        repository.admit(_context(), _request(old_id), now=NOW)
    assert old.value.code is AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED
    future_id = _uuidv7(NOW + timedelta(minutes=6), 3)
    with pytest.raises(AdmissionError) as future:
        repository.admit(_context(), _request(future_id), now=NOW)
    assert future.value.code is AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED
    with pytest.raises(AdmissionError) as forged:
        repository.admit(
            _context(authority_class=AuthorityClass.SYSTEM),
            _request(_uuidv7(NOW, 4)),
            now=NOW,
        )
    assert forged.value.code is AdmissionErrorCode.INSUFFICIENT_SCOPE

    request_id = _uuidv7(NOW, 5)
    created = repository.admit(_context(), _request(request_id), now=NOW)
    status = repository.status(
        _context("model.read", mutation=False), request_id, now=NOW
    )
    assert status.receipt == created.receipt
    with pytest.raises(AdmissionError) as hidden:
        repository.status(
            _context("model.read", workspace_id=None, mutation=False),
            request_id,
            now=NOW,
        )
    assert hidden.value.code is AdmissionErrorCode.REQUEST_NOT_FOUND
    independent = repository.admit(
        _context(workspace_id=None),
        _request(request_id, workspace_id=None),
        now=NOW,
    )
    assert independent.created
    execution = PostgresExecutionRepository(database_url)
    target = ExecutionTarget(ExecutionKind.MODEL, request_id)
    execution.transition(
        _context(), target, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    running_status = repository.status(
        _context("model.read", mutation=False), request_id, now=NOW
    )
    running_replay = repository.admit(
        _context(), _request(request_id), now=NOW + timedelta(minutes=1)
    )
    assert running_status.receipt == created.receipt
    assert running_replay.receipt == created.receipt
    assert running_status.state is RequestState.RUNNING
    assert running_status.state_revision == 2  # noqa: PLR2004
    assert running_status.receipt.state is RequestState.ADMITTED
    assert running_status.receipt.state_revision == 1
    terminal_error = TerminalError(
        TerminalErrorClass.TIMEOUT,
        ErrorScope.LOGICAL_REQUEST,
        "The logical request timed out.",
    )
    execution.transition(
        _context(),
        target,
        expected_revision=2,
        new_state=ExecutionState.FAILED,
        safe_error=terminal_error,
    )
    terminal_status = repository.status(
        _context("model.read", mutation=False), request_id, now=NOW
    )
    terminal_replay = repository.admit(
        _context(), _request(request_id), now=NOW + timedelta(minutes=2)
    )
    assert terminal_status.receipt == created.receipt
    assert terminal_replay.receipt == created.receipt
    assert terminal_status.state is RequestState.FAILED
    assert terminal_status.state_revision == 3  # noqa: PLR2004
    assert terminal_status.receipt.state is RequestState.ADMITTED
    assert terminal_status.receipt.state_revision == 1
    assert terminal_status.safe_error == terminal_error
    assert terminal_status.safe_error is not None
    with pytest.raises(FrozenInstanceError):
        setattr(terminal_status.safe_error, "message", "changed")  # noqa: B010
    with psycopg.connect(database_url) as connection:
        timing = connection.execute(
            """SELECT terminal_at, expires_at FROM router.logical_requests
               WHERE request_id = %s AND service_id = %s
                 AND workspace_id = %s""",
            (request_id, SERVICE_ID, WORKSPACE_ID),
        ).fetchone()
    assert timing is not None
    terminal_at, expires_at = timing
    assert expires_at == terminal_at + timedelta(hours=24)
    with pytest.raises(AdmissionError) as expired:
        repository.admit(
            _context(), _request(request_id), now=expires_at + timedelta(microseconds=1)
        )
    assert expired.value.code is AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED


def test_disabled_service_ancestor_stops_descendant_admission(
    database_url: str, repository: PostgresAdmissionRepository
) -> None:
    """Stop new descendant work when one service ancestor is disabled."""
    parent_id = "0198a080-0000-7000-8000-000000000123"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO router.services (id, stable_name) VALUES (%s, 'parent')",
            (parent_id,),
        )
        connection.execute(
            """UPDATE router.services
               SET parent_service_id = %s, state_revision = 2
               WHERE id = %s""",
            (parent_id, SERVICE_ID),
        )
        connection.execute(
            """UPDATE router.services
               SET state = 'disabled', state_revision = 2
               WHERE id = %s""",
            (parent_id,),
        )
    with pytest.raises(AdmissionError) as unavailable:
        repository.admit(_context(), _request(_uuidv7(NOW, 9)), now=NOW)
    assert unavailable.value.code is AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE


def test_attachment_must_be_ready_immutable_current_and_in_scope(
    database_url: str, repository: PostgresAdmissionRepository
) -> None:
    """Bind verified attachment metadata and reject mutable lifecycle states."""
    digest = bytes.fromhex("66" * 32)
    reference = AttachmentReference(ATTACHMENT_ID, digest.hex(), "text/plain", 5)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.attachments (
                   id, service_id, workspace_id, media_type, byte_length,
                   content_sha256, object_manifest_id, expires_at
               ) VALUES (%s, %s, %s, 'text/plain', 5, %s, %s, %s)""",
            (
                ATTACHMENT_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                digest,
                "0198a080-0000-7000-8000-000000000117",
                NOW + timedelta(days=7),
            ),
        )
        connection.execute(
            """INSERT INTO router.attachment_status (
                   attachment_id, state, updated_at
               ) VALUES (%s, 'pending', %s)""",
            (ATTACHMENT_ID, NOW),
        )
        connection.execute(
            """INSERT INTO router.attachment_content (
                   attachment_id, ciphertext, encrypted_data_key,
                   wrapping_key_id, stored_at
               ) VALUES (%s, %s, %s, 'fixture-key', %s)""",
            (ATTACHMENT_ID, bytes(45), bytes(72), NOW),
        )
        connection.execute(
            """UPDATE router.attachment_status
               SET state = 'ready', verified_at = %s, revision = 2,
                   updated_at = %s WHERE attachment_id = %s""",
            (NOW, NOW, ATTACHMENT_ID),
        )
    request_id = _uuidv7(NOW, 10)
    created = repository.admit(
        _context(), _request(request_id, attachments=(reference,)), now=NOW
    )
    assert created.external_effects_permitted
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE router.attachment_status
               SET state = 'expired', verified_at = NULL, revision = 3,
                   updated_at = %s WHERE attachment_id = %s""",
            (NOW + timedelta(hours=2), ATTACHMENT_ID),
        )
        connection.execute(
            "DELETE FROM router.attachment_content WHERE attachment_id = %s",
            (ATTACHMENT_ID,),
        )
    replay = repository.admit(
        _context(),
        _request(request_id, attachments=(reference,)),
        now=NOW + timedelta(hours=2),
    )
    assert not replay.created
    with pytest.raises(AdmissionError) as invalid:
        repository.admit(
            _context(),
            _request(_uuidv7(NOW, 11), attachments=(reference,)),
            now=NOW,
        )
    assert invalid.value.code is AdmissionErrorCode.ATTACHMENT_INVALID


def test_exact_route_is_bound_to_active_configuration_and_blocks_lossy_rollback(
    database_url: str, repository: PostgresAdmissionRepository
) -> None:
    """Bind a validated diagnostic scope and retain its migration data."""
    grant_id = uuid.uuid4()
    audit_id = uuid.uuid4()
    with psycopg.connect(database_url) as connection:
        created_at = connection.execute("SELECT transaction_timestamp()").fetchone()[0]
        expires_at = created_at + timedelta(minutes=5)
        connection.execute(
            """INSERT INTO router.audit_events (
                   event_id, audit_class, actor_kind, actor_id, authority_class,
                   service_id, workspace_id, action, permission_result,
                   safe_details, occurred_at
               ) VALUES (%s, 'security', 'service', %s, 'service',
                         %s, %s, 'diagnostic.grant.create', 'permitted',
                         jsonb_build_object(
                             'diagnostic_grant_id', %s::uuid,
                             'exact_route_id', %s::uuid,
                             'route_configuration_revision_id', %s::uuid,
                             'reason', 'test', 'expires_at', %s::timestamptz
                         ), %s)""",
            (
                audit_id,
                SERVICE_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                grant_id,
                ROUTE_ID,
                GLOBAL_CONFIGURATION_ID,
                expires_at,
                created_at,
            ),
        )
        connection.execute(
            """INSERT INTO router.diagnostic_route_grants (
                   grant_id, grant_sha256, service_id, workspace_id, exact_route_id,
                   route_configuration_revision_id, credential_id,
                   credential_generation, credential_revision_id,
                   created_by_kind, created_by_id, reason, created_at, expires_at,
                   creation_audit_event_id
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, 'service', %s,
                         'test', %s, %s, %s)""",
            (
                grant_id,
                hashlib.sha256(DIAGNOSTIC_GRANT.encode()).digest(),
                SERVICE_ID,
                WORKSPACE_ID,
                ROUTE_ID,
                GLOBAL_CONFIGURATION_ID,
                CREDENTIAL_ID,
                CREDENTIAL_ID,
                SERVICE_ID,
                created_at,
                expires_at,
                audit_id,
            ),
        )
    fingerprint = FingerprintInput(
        "model.create",
        1,
        SERVICE_ID,
        WORKSPACE_ID,
        "service-data",
        {
            "api_version": "1",
            "exact_route": ROUTE_ID,
            "messages": [{"role": "user", "content": "Hello"}],
            "limits": {"logical_timeout_ms": 120000},
            "output": {"format": "text"},
        },
        resolved_exact_route_scope={
            "service_id": SERVICE_ID,
            "workspace_id": WORKSPACE_ID,
            "exact_route_id": ROUTE_ID,
        },
    )
    request_id = _uuidv7(NOW, 12)
    request = AdmissionRequest(
        request_id,
        RequestKind.MODEL,
        fingerprint,
        exact_route_id=ROUTE_ID,
        diagnostic_grant=DIAGNOSTIC_GRANT,
    )
    result = repository.admit(_context(), request, now=NOW)
    assert result.external_effects_permitted
    replay = repository.admit(_context(), request, now=NOW + timedelta(minutes=6))
    assert not replay.created
    with psycopg.connect(database_url) as connection:
        authorization_count = connection.execute(
            """SELECT count(*) FROM router.diagnostic_route_authorizations
               WHERE request_id = %s""",
            (request_id,),
        ).fetchone()
    assert authorization_count == (1,)
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error, match="cannot roll back without data loss"),
    ):
        migrate(connection, target=8)


@pytest.mark.usefixtures("repository")
def test_database_requires_one_target_and_exact_route_revision(
    database_url: str,
) -> None:
    """Reject direct SQL that bypasses the target and revision binding."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.logical_requests (
                       row_id, request_id, request_kind, service_id, workspace_id,
                       configuration_revision_id, fingerprint_version,
                       fingerprint_sha256, data_profile, capture_enabled,
                       operation_name, contract_major, status_location
                   ) VALUES (
                       %s, %s, 'model', %s, %s, %s, 1, %s, 'service-data', true,
                       'model.create', 1, %s
                   )""",
                (
                    uuid.uuid4(),
                    _uuidv7(NOW, 20),
                    SERVICE_ID,
                    WORKSPACE_ID,
                    GLOBAL_CONFIGURATION_ID,
                    bytes.fromhex("77" * 32),
                    "/v1/model-requests/no-target",
                ),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.logical_requests (
                       row_id, request_id, request_kind, service_id, workspace_id,
                       configuration_revision_id, fingerprint_version,
                       fingerprint_sha256, data_profile, capture_enabled,
                       operation_name, contract_major, status_location,
                       assignment_id, exact_route_id
                   ) VALUES (
                       %s, %s, 'model', %s, %s, %s, 1, %s, 'service-data', true,
                       'model.create', 1, %s, %s, %s
                   )""",
                (
                    uuid.uuid4(),
                    _uuidv7(NOW, 21),
                    SERVICE_ID,
                    WORKSPACE_ID,
                    GLOBAL_CONFIGURATION_ID,
                    bytes.fromhex("78" * 32),
                    "/v1/model-requests/two-targets",
                    ASSIGNMENT_ID,
                    ROUTE_ID,
                ),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.logical_requests (
                       row_id, request_id, request_kind, service_id, workspace_id,
                       configuration_revision_id, fingerprint_version,
                       fingerprint_sha256, data_profile, capture_enabled,
                       operation_name, contract_major, status_location,
                       exact_route_id
                   ) VALUES (
                       %s, %s, 'model', %s, %s, %s, 1, %s, 'service-data', true,
                       'model.create', 1, %s, %s
                   )""",
                (
                    uuid.uuid4(),
                    _uuidv7(NOW, 22),
                    SERVICE_ID,
                    WORKSPACE_ID,
                    CONFIGURATION_ID,
                    bytes.fromhex("79" * 32),
                    "/v1/model-requests/wrong-route-revision",
                    ROUTE_ID,
                ),
            )
