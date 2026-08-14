"""PostgreSQL content capture, retention, export, and fencing tests."""
# ruff: noqa: E501, PLR2004, PT018

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.content import (
    CapturePolicy,
    ContentError,
    ContentErrorCode,
    ExportDataClass,
    ExportRequest,
    ExportState,
    MemoryObjectStore,
    PostgresContentRepository,
    RedeemedExport,
    RetentionDataClass,
    RetentionLimit,
    RetentionSelection,
)
from llmrouter_backend.credential_store.crypto import EnvelopeCipher
from llmrouter_backend.database import migrate

from .helpers import (
    CONFIGURATION_ID,
    FIXTURE_ROUTE_ID,
    OTHER_SERVICE_ID,
    OTHER_WORKSPACE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_request,
    seed_scope,
)

NOW = datetime(2026, 8, 13, 18, tzinfo=UTC)
CONTENT_ID = "0198a080-0000-7000-8000-000000000130"
NODE_ONE = "0198a080-0000-7000-8000-000000000131"
NODE_TWO = "0198a080-0000-7000-8000-000000000132"


def _context(
    operation: str,
    *,
    mutation: bool,
    global_authority: bool = True,
    actor_id: str = "administrator",
    recent: datetime | None = NOW,
) -> RequestContext:
    return RequestContext(
        request_id=f"transport-{operation}-{mutation}",
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id=actor_id,
        authority_class=(
            AuthorityClass.GLOBAL_ADMINISTRATOR
            if global_authority
            else AuthorityClass.SERVICE
        ),
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation=operation,
        scope=Scope() if global_authority else Scope(SERVICE_ID, WORKSPACE_ID),
        authorized_at=NOW,
        recent_authentication_at=recent,
        mutation=mutation,
    )


def _service_context(operation: str, *, mutation: bool) -> RequestContext:
    return RequestContext(
        request_id=f"service-{operation}",
        actor_kind=PrincipalKind.SERVICE,
        actor_id=SERVICE_ID,
        authority_class=AuthorityClass.SERVICE,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=Audience.CONFIGURATION,
        operation=operation,
        scope=Scope(SERVICE_ID, WORKSPACE_ID),
        authorized_at=NOW,
        recent_authentication_at=None,
        mutation=mutation,
    )


def _database_now(database_url: str) -> datetime:
    with psycopg.connect(database_url) as connection:
        row = connection.execute("SELECT transaction_timestamp()").fetchone()
    assert row is not None and isinstance(row[0], datetime)
    return row[0]


@pytest.fixture
def store() -> MemoryObjectStore:
    """Create one isolated deterministic object store."""
    return MemoryObjectStore()


@pytest.fixture
def repository(
    database_url: str, store: MemoryObjectStore
) -> PostgresContentRepository:
    """Create current schema, one admitted request, and content custody."""
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
        connection.execute(
            "ALTER TABLE router.logical_requests DISABLE TRIGGER logical_requests_stable_identity"
        )
        connection.execute(
            """
            UPDATE router.logical_requests SET admitted_at = %s,
                captured_content_expires_at = %s WHERE request_id = %s
            """,
            (NOW, NOW + timedelta(days=7), REQUEST_ID),
        )
        connection.execute(
            """
            ALTER TABLE router.logical_requests ENABLE TRIGGER logical_requests_stable_identity
            """
        )
    return PostgresContentRepository(
        database_url,
        cipher=EnvelopeCipher(
            {"wrap": bytes(range(32))},
            current_key_id="wrap",
            random_bytes=lambda size: bytes(index % 251 for index in range(size)),
        ),
        object_store=store,
        token_digest_key=b"d" * 32,
    )


def test_capture_read_audit_secret_controls_and_source_independence(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Keep encrypted capture independent and permit only audited global reads."""
    metadata = repository.capture(
        REQUEST_ROW_ID,
        "model.request",
        {"message": "hello known-control"},
        content_id=CONTENT_ID,
        authenticated_control_values=("known-control",),
        now=NOW,
    )
    assert metadata.capture_policy is CapturePolicy.COMPLETE
    protected = repository.read(
        _context("content.read", mutation=False), CONTENT_ID, now=NOW
    )
    assert protected.value == {"message": "hello [REDACTED]"}
    with pytest.raises(ContentError) as service_read:
        repository.read(
            _context("content.read", mutation=False, global_authority=False),
            CONTENT_ID,
            now=NOW,
        )
    assert service_read.value.code is ContentErrorCode.INSUFFICIENT_SCOPE
    with pytest.raises(ContentError) as stale_auth:
        repository.read(
            _context("content.read", mutation=False, recent=NOW - timedelta(minutes=6)),
            CONTENT_ID,
            now=NOW,
        )
    assert stale_auth.value.code is ContentErrorCode.INSUFFICIENT_SCOPE
    with pytest.raises(ContentError) as structured_secret:
        repository.capture(
            REQUEST_ROW_ID,
            "model.request",
            {"authorization": "must-not-leave"},
            content_id="0198a080-0000-7000-8000-000000000133",
            authenticated_control_values=(),
            now=NOW,
        )
    assert structured_secret.value.code is ContentErrorCode.INVALID
    with psycopg.connect(database_url) as connection:
        events = connection.execute(
            "SELECT action FROM router.audit_events ORDER BY occurred_at"
        ).fetchall()
        request_source_fields = connection.execute(
            "SELECT service_id, workspace_id, request_id FROM router.captured_content WHERE id = %s",
            (CONTENT_ID,),
        ).fetchone()
    assert ("captured_content.read",) in events
    assert request_source_fields is not None


def test_capture_uses_request_row_identity_and_serializes_content_identity(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Do not bind an equal request identity from another service."""
    second_row = uuid.uuid4()
    second_configuration = uuid.uuid4()
    second_assignment = uuid.uuid4()
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO router.configuration_revisions (
                id, scope_kind, service_id, workspace_id, revision_number,
                content, content_sha256, created_by_kind, created_by_id
            ) VALUES (%s, 'workspace', %s, %s, 1, '{}'::jsonb,
                      %s, 'system', 'test')
            """,
            (
                second_configuration,
                OTHER_SERVICE_ID,
                OTHER_WORKSPACE_ID,
                bytes.fromhex("77" * 32),
            ),
        )
        connection.execute(
            """
            INSERT INTO router.assignment_definitions (
                id, configuration_revision_id, stable_name
            ) VALUES (%s, %s, 'second-service')
            """,
            (second_assignment, second_configuration),
        )
        connection.execute(
            """
            INSERT INTO router.assignment_candidates (
                assignment_id, configuration_revision_id, ordinal,
                provider_model_route_id, attempt_timeout_seconds,
                attempt_timeout_ms
            ) VALUES (%s, %s, 1, %s, 30, 30000)
            """,
            (second_assignment, second_configuration, FIXTURE_ROUTE_ID),
        )
        connection.execute(
            """
            INSERT INTO router.logical_requests (
                row_id, request_id, request_kind, service_id, workspace_id,
                assignment_id, configuration_revision_id, fingerprint_version,
                fingerprint_sha256, data_profile, capture_enabled, admitted_at,
                last_transition_at
                ) SELECT %s, request_id, request_kind, %s, %s,
                         %s, %s, fingerprint_version,
                     fingerprint_sha256, data_profile, capture_enabled, admitted_at,
                     last_transition_at
              FROM router.logical_requests WHERE row_id = %s
            """,
            (
                second_row,
                OTHER_SERVICE_ID,
                OTHER_WORKSPACE_ID,
                second_assignment,
                second_configuration,
                REQUEST_ROW_ID,
            ),
        )

    def capture_once() -> str:
        return repository.capture(
            str(second_row),
            "model.response",
            {"answer": "second"},
            content_id=CONTENT_ID,
            authenticated_control_values=(),
            now=NOW,
        ).service_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: capture_once(), range(2))) == [
            OTHER_SERVICE_ID,
            OTHER_SERVICE_ID,
        ]
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT request_row_id, service_id FROM router.captured_content WHERE id = %s",
            (CONTENT_ID,),
        ).fetchone()
    assert row == (second_row, uuid.UUID(OTHER_SERVICE_ID))


def test_wrong_manifest_and_expiry_takeover_are_fail_closed(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Reject manifest change and fence an old expiry worker after takeover."""
    repository.capture(
        REQUEST_ROW_ID,
        "model.response",
        {"answer": "safe"},
        content_id=CONTENT_ID,
        authenticated_control_values=(),
        now=NOW,
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "ALTER TABLE router.content_manifests DISABLE TRIGGER content_manifests_append_only"
        )
        connection.execute(
            "UPDATE router.content_manifests SET manifest_sha256 = %s WHERE id = (SELECT manifest_id FROM router.captured_content WHERE id = %s)",
            (bytes.fromhex("11" * 32), CONTENT_ID),
        )
        connection.execute(
            "ALTER TABLE router.content_manifests ENABLE TRIGGER content_manifests_append_only"
        )
    with pytest.raises(ContentError) as wrong_manifest:
        repository.read(_context("content.read", mutation=False), CONTENT_ID, now=NOW)
    assert wrong_manifest.value.code is ContentErrorCode.INTEGRITY
    worker_now = _database_now(database_url)
    first_now = worker_now - timedelta(seconds=2)
    job_id = repository.enqueue_lifecycle_job(
        "expiry", CONTENT_ID, {"content_id": CONTENT_ID}, now=first_now
    )
    assert (
        repository.enqueue_lifecycle_job(
            "expiry", CONTENT_ID, {"content_id": CONTENT_ID}, now=first_now
        )
        == job_id
    )
    first = repository.claim_lifecycle_job(
        NODE_ONE, now=first_now, lease_lifetime=timedelta(seconds=1)
    )
    assert first is not None and first.job_id == job_id
    second = repository.claim_lifecycle_job(
        NODE_TWO,
        now=worker_now,
        lease_lifetime=timedelta(minutes=1),
    )
    assert second is not None and second.generation > first.generation
    with pytest.raises(ContentError) as stale:
        repository.run_lifecycle_job(first, now=worker_now)
    assert stale.value.code is ContentErrorCode.STALE_LEASE
    repository.run_lifecycle_job(second, now=worker_now)
    with pytest.raises(ContentError) as gone:
        repository.read(
            _context("content.read", mutation=False),
            CONTENT_ID,
            now=NOW,
        )
    assert gone.value.code is ContentErrorCode.NOT_FOUND


def test_cleanup_fence_uses_database_time_and_exact_manifest_scope(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Reject a fake clock and deletion of a manifest from a different scope."""
    other_content_id = "0198a080-0000-7000-8000-000000000133"
    for content_id in (CONTENT_ID, other_content_id):
        repository.capture(
            REQUEST_ROW_ID,
            "model.response",
            {"content_id": content_id},
            content_id=content_id,
            authenticated_control_values=(),
            now=NOW,
        )
    worker_now = _database_now(database_url)
    job_id = repository.enqueue_lifecycle_job(
        "expiry", CONTENT_ID, {"content_id": CONTENT_ID}, now=worker_now
    )
    lease = repository.claim_lifecycle_job(NODE_ONE, now=worker_now)
    assert lease is not None and lease.job_id == job_id

    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            SELECT
                set_config('llmrouter.lifecycle_job_id', %s, true),
                set_config('llmrouter.lifecycle_owner_node_id', %s, true),
                set_config('llmrouter.lifecycle_generation', %s, true),
                set_config('llmrouter.lifecycle_manifest_id', '', true)
            """,
            (lease.job_id, lease.owner_node_id, str(lease.generation)),
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                DELETE FROM router.content_segments
                WHERE manifest_id = (
                    SELECT manifest_id FROM router.captured_content WHERE id = %s
                )
                """,
                (CONTENT_ID,),
            )
        connection.rollback()
        row = connection.execute(
            "SELECT manifest_id FROM router.captured_content WHERE id = %s",
            (other_content_id,),
        ).fetchone()
        assert row is not None and isinstance(row[0], uuid.UUID)
        other_manifest_id = row[0]
        connection.execute(
            """
            SELECT
                set_config('llmrouter.lifecycle_job_id', %s, true),
                set_config('llmrouter.lifecycle_owner_node_id', %s, true),
                set_config('llmrouter.lifecycle_generation', %s, true),
                set_config('llmrouter.lifecycle_manifest_id', %s, true)
            """,
            (
                lease.job_id,
                lease.owner_node_id,
                str(lease.generation),
                str(other_manifest_id),
            ),
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "DELETE FROM router.content_segments WHERE manifest_id = %s",
                (other_manifest_id,),
            )
        connection.rollback()
        connection.execute(
            """
            SELECT
                set_config('llmrouter.lifecycle_job_id', %s, true),
                set_config('llmrouter.lifecycle_owner_node_id', %s, true),
                set_config('llmrouter.lifecycle_generation', %s, true),
                set_config('llmrouter.lifecycle_manifest_id', %s, true)
            """,
            (
                lease.job_id,
                lease.owner_node_id,
                str(lease.generation),
                str(other_manifest_id),
            ),
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "DELETE FROM router.content_manifests WHERE id = %s",
                (other_manifest_id,),
            )
        connection.rollback()

    stale_now = _database_now(database_url) - timedelta(minutes=2)
    stale_job_id = repository.enqueue_lifecycle_job(
        "delete",
        other_content_id,
        {"content_id": other_content_id},
        now=stale_now,
    )
    stale_lease = repository.claim_lifecycle_job(
        NODE_TWO, now=stale_now, lease_lifetime=timedelta(seconds=1)
    )
    assert stale_lease is not None and stale_lease.job_id == stale_job_id
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            SELECT
                set_config('llmrouter.lifecycle_job_id', %s, true),
                set_config('llmrouter.lifecycle_owner_node_id', %s, true),
                set_config('llmrouter.lifecycle_generation', %s, true),
                set_config('llmrouter.lifecycle_manifest_id', '', true),
                set_config('llmrouter.lifecycle_now', %s, true)
            """,
            (
                stale_lease.job_id,
                stale_lease.owner_node_id,
                str(stale_lease.generation),
                stale_now.isoformat(),
            ),
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE router.captured_content
                SET lifecycle_state = 'deleting', deletion_started_at = %s
                WHERE id = %s
                """,
                (stale_now, other_content_id),
            )
        connection.rollback()
        assert connection.execute(
            """
            SELECT lifecycle_state, count(*) OVER ()
            FROM router.captured_content
            WHERE id IN (%s, %s) ORDER BY id
            """,
            (CONTENT_ID, other_content_id),
        ).fetchall() == [("live", 2), ("live", 2)]


def test_retention_nearest_replacement_preview_and_count_plus_days(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Replace by nearest class and keep a revision while either rule applies."""
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO router.retention_policies (
                id, scope_kind, service_id, workspace_id, data_class,
                retention_days, revision, effective_at
            ) VALUES
              (%s, 'service', %s, NULL, 'captured_content', 9, 1, %s),
              (%s, 'workspace', %s, %s, 'captured_content', 8, 1, %s)
            """,
            (
                uuid.uuid4(),
                SERVICE_ID,
                NOW,
                uuid.uuid4(),
                SERVICE_ID,
                WORKSPACE_ID,
                NOW,
            ),
        )
    assert (
        repository.resolve_retention(
            SERVICE_ID,
            WORKSPACE_ID,
            RetentionDataClass.CAPTURED_CONTENT,
            effective_at=NOW,
        ).days
        == 8
    )
    repository.put_capture_policy(
        _service_context("retention.write", mutation=True),
        CapturePolicy.METADATA_ONLY,
        now=NOW,
    )
    capture = repository.resolve_capture(SERVICE_ID, WORKSPACE_ID, admitted_at=NOW)
    assert capture.policy is CapturePolicy.METADATA_ONLY
    assert capture.expires_at == NOW + timedelta(days=8)
    pressure = repository.resolve_capture(
        SERVICE_ID, WORKSPACE_ID, admitted_at=NOW, spool_pressure=True
    )
    assert pressure.policy is CapturePolicy.DISABLED
    assert pressure.expires_at is None
    context = _context("retention.manage", mutation=False)
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT data_class, revision FROM router.retention_policies
            WHERE scope_kind = 'global' ORDER BY data_class, revision
            """
        ).fetchall()
    revision = hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode()
    ).hexdigest()
    preview = repository.preview_retention(
        context,
        (RetentionSelection(RetentionDataClass.CAPTURED_CONTENT, 6),),
        expected_revision=revision,
        now=NOW,
    )
    assert len(preview.effects) == 1
    revisions = [
        ("new", NOW),
        ("young", NOW - timedelta(days=10)),
        ("old", NOW - timedelta(days=900)),
    ]
    oldest, retained_by = repository.revision_retention_evidence(
        revisions,
        RetentionSelection(RetentionDataClass.CONFIGURATION_REVISIONS, 730, 2),
        now=NOW,
    )
    assert (oldest, retained_by) == ("young", "both")


@pytest.mark.parametrize("export_format", ["jsonl", "csv"])
def test_protected_export_current_session_one_use_and_token_race(
    repository: PostgresContentRepository,
    export_format: str,
) -> None:
    """Proxy content once with current session, both grants, and response controls."""
    repository.capture(
        REQUEST_ROW_ID,
        "model.response",
        {"answer": "exported"},
        content_id=CONTENT_ID,
        authenticated_control_values=(),
        now=NOW,
    )
    export_context = _context("export.create", mutation=True)
    content_context = _context("content.read", mutation=False)
    request = ExportRequest(
        ExportDataClass.CAPTURED_CONTENT,
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=1),
        export_format,
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
    )
    operation = repository.create_export(
        export_context,
        request,
        idempotency_key="export-test-key-0001",
        administrator_session_id="session-one",
        now=NOW,
        content_context=content_context,
    )
    assert operation.state is ExportState.QUEUED
    replay = repository.create_export(
        export_context,
        request,
        idempotency_key="export-test-key-0001",
        administrator_session_id="session-one",
        now=NOW,
        content_context=content_context,
    )
    assert replay.operation_id == operation.operation_id
    conflicting_format = "csv" if export_format == "jsonl" else "jsonl"
    with pytest.raises(ContentError) as idempotency_conflict:
        repository.create_export(
            export_context,
            ExportRequest(
                ExportDataClass.CAPTURED_CONTENT,
                NOW - timedelta(minutes=1),
                NOW + timedelta(minutes=1),
                conflicting_format,
                service_id=SERVICE_ID,
                workspace_id=WORKSPACE_ID,
            ),
            idempotency_key="export-test-key-0001",
            administrator_session_id="session-one",
            now=NOW,
            content_context=content_context,
        )
    assert idempotency_conflict.value.code is ContentErrorCode.CONFLICT
    lease = repository.claim_lifecycle_job(NODE_ONE, now=NOW)
    assert lease is not None and lease.job_kind == "export"
    repository.run_lifecycle_job(lease, now=NOW)
    status = repository.export_status(
        _context("export.create", mutation=False, recent=None),
        operation.operation_id,
        administrator_session_id="session-one",
        now=NOW,
    )
    assert (
        status.redemption_path == f"/v1/admin/exports/{operation.operation_id}/redeem"
    )
    assert status.redemption_token is not None
    with pytest.raises(ContentError):
        repository.redeem_export(
            export_context,
            operation.operation_id,
            status.redemption_token,
            administrator_session_id="session-two",
            now=NOW,
            content_context=content_context,
        )

    def redeem() -> RedeemedExport | ContentErrorCode:
        try:
            return repository.redeem_export(
                export_context,
                operation.operation_id,
                status.redemption_token or "",
                administrator_session_id="session-one",
                now=NOW,
                content_context=content_context,
            )
        except ContentError as error:
            return error.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: redeem(), range(2)))
    successful = [item for item in results if isinstance(item, RedeemedExport)]
    assert len(successful) == 1
    result = successful[0]
    assert result.cache_control == "no-store"
    assert result.referrer_policy == "no-referrer"
    if export_format == "jsonl":
        assert b'"answer":"exported"' in result.value
    else:
        assert b"answer" in result.value
        assert b"exported" in result.value


def test_non_content_export_classes_build_and_redeem(
    repository: PostgresContentRepository,
) -> None:
    """Build each protected non-content export through its real query."""
    for index, data_class in enumerate(
        (
            ExportDataClass.ACCOUNTING,
            ExportDataClass.AUDIT,
            ExportDataClass.CONFIGURATION,
        )
    ):
        operation = repository.create_export(
            _context("export.create", mutation=True),
            ExportRequest(
                data_class,
                NOW - timedelta(minutes=1),
                NOW + timedelta(minutes=1),
                "jsonl",
            ),
            idempotency_key=f"export-non-content-{index:02d}",
            administrator_session_id="session-one",
            now=NOW,
        )
        lease = repository.claim_lifecycle_job(NODE_ONE, now=NOW)
        assert lease is not None and lease.scope_key == operation.operation_id
        repository.run_lifecycle_job(lease, now=NOW)
        status = repository.export_status(
            _context("export.create", mutation=False, recent=None),
            operation.operation_id,
            administrator_session_id="session-one",
            now=NOW,
        )
        assert status.redemption_token is not None
        redeemed = repository.redeem_export(
            _context("export.create", mutation=True),
            operation.operation_id,
            status.redemption_token,
            administrator_session_id="session-one",
            now=NOW,
        )
        if data_class is ExportDataClass.AUDIT:
            assert b'"action":"export.create"' in redeemed.value


def test_capture_and_export_expiry_remove_objects_keys_and_manifests(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Remove expired encrypted custody data with retry-safe stages."""
    repository.capture(
        REQUEST_ROW_ID,
        "model.response",
        {"answer": "expire"},
        content_id=CONTENT_ID,
        authenticated_control_values=(),
        now=NOW,
    )
    cleanup_now = _database_now(database_url) + timedelta(days=8)
    assert repository.expire_due_content(now=cleanup_now) == 1
    lease = repository.claim_lifecycle_job(NODE_ONE, now=cleanup_now)
    assert lease is not None and lease.job_kind == "expiry"
    repository.run_lifecycle_job(lease, now=cleanup_now)
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.captured_content"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.content_segments"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.content_manifests"
        ).fetchone() == (0,)

    operation = repository.create_export(
        _context("export.create", mutation=True),
        ExportRequest(
            ExportDataClass.AUDIT,
            NOW - timedelta(minutes=1),
            NOW + timedelta(minutes=1),
            "jsonl",
        ),
        idempotency_key="export-expiry-test-01",
        administrator_session_id="session-one",
        now=NOW,
    )
    build = repository.claim_lifecycle_job(NODE_ONE, now=NOW)
    assert build is not None and build.job_kind == "export"
    repository.run_lifecycle_job(build, now=NOW)
    cleanup_now = _database_now(database_url) + timedelta(hours=2)
    assert repository.expire_due_exports(now=cleanup_now) == 1
    expiry = repository.claim_lifecycle_job(NODE_ONE, now=cleanup_now)
    assert expiry is not None and expiry.job_kind == "export_expiry"
    repository.run_lifecycle_job(expiry, now=cleanup_now)
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.protected_exports WHERE id = %s",
            (operation.operation_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.content_segments"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.content_manifests"
        ).fetchone() == (0,)


def test_queued_export_build_finishes_after_expiry_cleanup(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Complete both jobs when an export expires before its build starts."""
    operation = repository.create_export(
        _context("export.create", mutation=True),
        ExportRequest(
            ExportDataClass.AUDIT,
            NOW - timedelta(minutes=1),
            NOW + timedelta(minutes=1),
            "jsonl",
        ),
        idempotency_key="queued-export-expiry-test-01",
        administrator_session_id="session-one",
        now=NOW,
    )
    later = _database_now(database_url) + timedelta(hours=2)
    assert repository.expire_due_exports(now=later) == 1
    build = repository.claim_lifecycle_job(NODE_ONE, now=later)
    assert build is not None and build.job_kind == "export"
    repository.run_lifecycle_job(build, now=later)
    expiry = repository.claim_lifecycle_job(NODE_ONE, now=later)
    assert expiry is not None and expiry.job_kind == "export_expiry"
    repository.run_lifecycle_job(expiry, now=later)
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.protected_exports WHERE id = %s",
            (operation.operation_id,),
        ).fetchone() == (0,)
        states = connection.execute(
            """
            SELECT state FROM router.content_lifecycle_jobs
            WHERE scope_key = %s ORDER BY job_kind
            """,
            (operation.operation_id,),
        ).fetchall()
    assert states == [("succeeded",), ("succeeded",)]


class _FailDeleteOnceStore(MemoryObjectStore):
    failed = False

    def delete(self, key: str, *, sha256: str) -> None:
        if not self.failed:
            self.failed = True
            message = "The injected object delete failed."
            raise RuntimeError(message)
        super().delete(key, sha256=sha256)


def test_partial_object_delete_keeps_content_unreadable_for_retry(
    database_url: str,
) -> None:
    """Keep a staged row and keys after a partial object-delete failure."""
    store = _FailDeleteOnceStore()
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
        connection.execute(
            "ALTER TABLE router.logical_requests DISABLE TRIGGER logical_requests_stable_identity"
        )
        connection.execute(
            """
            UPDATE router.logical_requests SET admitted_at = %s,
                captured_content_expires_at = %s WHERE row_id = %s
            """,
            (NOW, NOW + timedelta(days=7), REQUEST_ROW_ID),
        )
        connection.execute(
            "ALTER TABLE router.logical_requests ENABLE TRIGGER logical_requests_stable_identity"
        )
    repository = PostgresContentRepository(
        database_url,
        cipher=EnvelopeCipher(
            {"wrap": bytes(range(32))},
            current_key_id="wrap",
            random_bytes=lambda size: bytes(index % 251 for index in range(size)),
        ),
        object_store=store,
        token_digest_key=b"d" * 32,
    )
    repository.capture(
        REQUEST_ROW_ID,
        "model.response",
        {"answer": "retry"},
        content_id=CONTENT_ID,
        authenticated_control_values=(),
        now=NOW,
    )
    job = repository.enqueue_lifecycle_job(
        "expiry",
        CONTENT_ID,
        {"content_id": CONTENT_ID},
        now=_database_now(database_url),
    )
    worker_now = _database_now(database_url)
    lease = repository.claim_lifecycle_job(NODE_ONE, now=worker_now)
    assert lease is not None and lease.job_id == job
    with pytest.raises(RuntimeError):
        repository.run_lifecycle_job(lease, now=worker_now)
    with pytest.raises(ContentError) as hidden:
        repository.read(_context("content.read", mutation=False), CONTENT_ID, now=NOW)
    assert hidden.value.code is ContentErrorCode.NOT_FOUND
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT lifecycle_state FROM router.captured_content WHERE id = %s",
            (CONTENT_ID,),
        ).fetchone() == ("deleting",)
    repository.retry_lifecycle_job(
        lease, now=worker_now, retry_at=worker_now, safe_error="object delete failed"
    )
    retry = repository.claim_lifecycle_job(NODE_TWO, now=worker_now)
    assert retry is not None and retry.job_id == job
    repository.run_lifecycle_job(retry, now=worker_now)
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.captured_content"
        ).fetchone() == (0,)


def test_capture_commit_failure_removes_uploaded_object(
    database_url: str,
    repository: PostgresContentRepository,
    store: MemoryObjectStore,
) -> None:
    """Remove an uploaded object when a deferred database commit fails."""
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            CREATE FUNCTION router.fail_captured_content_commit()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'injected deferred failure' USING ERRCODE = '23514';
            END;
            $$
            """
        )
        connection.execute(
            """
            CREATE CONSTRAINT TRIGGER captured_content_commit_failure
            AFTER INSERT ON router.captured_content
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION router.fail_captured_content_commit()
            """
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        repository.capture(
            REQUEST_ROW_ID,
            "model.response",
            {"answer": "rollback"},
            content_id=CONTENT_ID,
            authenticated_control_values=(),
            now=NOW,
        )
    assert store.object_count_for_test() == 0


def test_export_commit_failure_removes_uploaded_object(
    database_url: str,
    repository: PostgresContentRepository,
    store: MemoryObjectStore,
) -> None:
    """Remove export bytes when the completed-state commit fails."""
    operation = repository.create_export(
        _context("export.create", mutation=True),
        ExportRequest(
            ExportDataClass.AUDIT,
            NOW - timedelta(minutes=1),
            NOW + timedelta(minutes=1),
            "jsonl",
        ),
        idempotency_key="export-commit-fail-01",
        administrator_session_id="session-one",
        now=NOW,
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            CREATE FUNCTION router.fail_export_commit()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.state = 'completed' THEN
                    RAISE EXCEPTION 'injected export failure' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        connection.execute(
            """
            CREATE CONSTRAINT TRIGGER export_commit_failure
            AFTER UPDATE ON router.protected_exports
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION router.fail_export_commit()
            """
        )
    lease = repository.claim_lifecycle_job(NODE_ONE, now=NOW)
    assert lease is not None and lease.scope_key == operation.operation_id
    with pytest.raises(psycopg.errors.CheckViolation):
        repository.run_lifecycle_job(lease, now=NOW)
    assert store.object_count_for_test() == 0


def test_retention_preview_counts_rows_and_worker_deletes_by_audit_class(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Count real effects and delete only the selected audit data class."""
    audit_ids = {audit_class: uuid.uuid4() for audit_class in ("agent_run", "security")}
    with psycopg.connect(database_url) as connection:
        for audit_class in ("agent_run", "security"):
            connection.execute(
                """
                INSERT INTO router.audit_events (
                    event_id, audit_class, actor_kind, actor_id, authority_class,
                    service_id, workspace_id, action, permission_result,
                    safe_details, occurred_at
                ) VALUES (%s, %s, 'system', 'retention-test', 'system',
                          %s, %s, 'retention.test', 'permitted', '{}'::jsonb, %s)
                """,
                (
                    audit_ids[audit_class],
                    audit_class,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    NOW - timedelta(days=20),
                ),
            )
        rows = connection.execute(
            """
            SELECT data_class, revision FROM router.retention_policies
            WHERE scope_kind = 'global' ORDER BY data_class, revision
            """
        ).fetchall()
        connection.execute(
            """
            INSERT INTO router.retention_policies (
                id, scope_kind, service_id, workspace_id, data_class,
                retention_days, revision, effective_at
            ) VALUES
                (%s, 'workspace', %s, %s, 'agent_tool_audit', 7, 1, %s),
                (%s, 'workspace', %s, %s, 'security_audit', 7, 1, %s)
            """,
            (
                uuid.uuid4(),
                SERVICE_ID,
                WORKSPACE_ID,
                NOW,
                uuid.uuid4(),
                SERVICE_ID,
                WORKSPACE_ID,
                NOW,
            ),
        )
    revision = hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode()
    ).hexdigest()
    preview = repository.preview_retention(
        _context("retention.manage", mutation=False),
        (RetentionSelection(RetentionDataClass.AGENT_TOOL_AUDIT, 7),),
        expected_revision=revision,
        now=NOW,
    )
    assert preview.effects[0].estimated_records == 1
    worker_now = _database_now(database_url)
    job = repository.enqueue_retention_execution(
        RetentionDataClass.AGENT_TOOL_AUDIT,
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        now=worker_now,
    )
    lease = repository.claim_lifecycle_job(NODE_ONE, now=worker_now)
    assert lease is not None and lease.job_id == job
    repository.run_lifecycle_job(lease, now=worker_now)
    later_job = repository.enqueue_retention_execution(
        RetentionDataClass.AGENT_TOOL_AUDIT,
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        now=worker_now + timedelta(days=1),
    )
    assert later_job != job
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT audit_class FROM router.audit_events WHERE action = 'retention.test'"
        ).fetchall() == [("security",)]
        connection.execute(
            """
            INSERT INTO router.configuration_audit_bindings (revision_id, event_id)
            VALUES (%s, %s)
            """,
            (CONFIGURATION_ID, audit_ids["security"]),
        )
        unreferenced_id = uuid.uuid4()
        connection.execute(
            """
            INSERT INTO router.audit_events (
                event_id, audit_class, actor_kind, actor_id, authority_class,
                service_id, workspace_id, action, permission_result,
                safe_details, occurred_at
            ) VALUES (%s, 'security', 'system', 'retention-test', 'system',
                      %s, %s, 'retention.unreferenced', 'permitted', '{}'::jsonb, %s)
            """,
            (
                unreferenced_id,
                SERVICE_ID,
                WORKSPACE_ID,
                NOW - timedelta(days=20),
            ),
        )
    security_job = repository.enqueue_retention_execution(
        RetentionDataClass.SECURITY_AUDIT,
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        now=worker_now,
    )
    security_lease = repository.claim_lifecycle_job(NODE_ONE, now=worker_now)
    assert security_lease is not None and security_lease.job_id == security_job
    repository.run_lifecycle_job(security_lease, now=worker_now)
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT event_id FROM router.audit_events
            WHERE event_id IN (%s, %s) ORDER BY event_id
            """,
            (audit_ids["security"], unreferenced_id),
        ).fetchall()
    assert rows == [(audit_ids["security"],)]


def test_configuration_revision_worker_keeps_count_age_and_references(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Delete only old excess revisions that have no active reference."""
    revision_ids = [uuid.uuid4() for _ in range(3)]
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "ALTER TABLE router.configuration_revisions DISABLE TRIGGER configuration_revisions_append_only"
        )
        connection.execute(
            "UPDATE router.configuration_revisions SET created_at = %s WHERE id = %s",
            (NOW - timedelta(days=30), "0198a080-0000-7000-8000-000000000004"),
        )
        connection.execute(
            "ALTER TABLE router.configuration_revisions ENABLE TRIGGER configuration_revisions_append_only"
        )
        for number, revision_id in enumerate(revision_ids, start=2):
            connection.execute(
                """
                INSERT INTO router.configuration_revisions (
                    id, scope_kind, service_id, workspace_id, revision_number,
                    content, content_sha256, created_at,
                    created_by_kind, created_by_id
                ) VALUES (%s, 'workspace', %s, %s, %s, '{}'::jsonb,
                          %s, %s, 'system', 'retention-test')
                """,
                (
                    revision_id,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    number,
                    bytes([number]) * 32,
                    NOW - timedelta(days=30 if number < 4 else 1),
                ),
            )
        connection.execute(
            """
            INSERT INTO router.retention_policies (
                id, scope_kind, service_id, workspace_id, data_class,
                retention_days, minimum_revision_count, revision, effective_at
            ) VALUES (%s, 'workspace', %s, %s, 'configuration_revisions',
                      7, 2, 1, %s)
            """,
            (uuid.uuid4(), SERVICE_ID, WORKSPACE_ID, NOW),
        )
    worker_now = _database_now(database_url)
    job = repository.enqueue_retention_execution(
        RetentionDataClass.CONFIGURATION_REVISIONS,
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        now=worker_now,
    )
    lease = repository.claim_lifecycle_job(NODE_ONE, now=worker_now)
    assert lease is not None and lease.job_id == job
    repository.run_lifecycle_job(lease, now=worker_now)
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT revision_number FROM router.configuration_revisions
            WHERE service_id = %s AND workspace_id = %s
            ORDER BY revision_number
            """,
            (SERVICE_ID, WORKSPACE_ID),
        ).fetchall()
    assert rows == [(1,), (3,), (4,)]


def test_direct_sql_rejects_content_identity_and_lifecycle_skips(
    database_url: str,
    repository: PostgresContentRepository,
) -> None:
    """Reject direct identity changes and invalid lifecycle state skips."""
    repository.capture(
        REQUEST_ROW_ID,
        "model.response",
        {"answer": "guard"},
        content_id=CONTENT_ID,
        authenticated_control_values=(),
        now=NOW,
    )
    job = repository.enqueue_lifecycle_job(
        "expiry", CONTENT_ID, {"content_id": CONTENT_ID}, now=NOW
    )
    operation = repository.create_export(
        _context("export.create", mutation=True),
        ExportRequest(
            ExportDataClass.AUDIT,
            NOW - timedelta(minutes=1),
            NOW + timedelta(minutes=1),
            "jsonl",
        ),
        idempotency_key="export-invariant-test-01",
        administrator_session_id="session-one",
        now=NOW,
    )
    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE router.captured_content SET request_id = %s WHERE id = %s",
                (uuid.uuid4(), CONTENT_ID),
            )
        connection.rollback()
        connection.execute("SET LOCAL llmrouter.lifecycle_cleanup = 'on'")
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "DELETE FROM router.captured_content WHERE id = %s", (CONTENT_ID,)
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE router.content_segments SET encrypted_data_key = %s
                WHERE manifest_id = (
                    SELECT manifest_id FROM router.captured_content WHERE id = %s
                )
                """,
                (bytes.fromhex("99" * 32), CONTENT_ID),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                UPDATE router.content_lifecycle_jobs SET
                    state = 'succeeded', lease_generation = lease_generation + 1,
                    updated_at = %s WHERE id = %s
                """,
                (NOW, job),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE router.protected_exports SET actor_id = 'changed' WHERE id = %s",
                (operation.operation_id,),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO router.export_redemptions (
                    export_id, token_digest, administrator_session_id,
                    issued_at, expires_at
                ) VALUES (%s, %s, 'session-one', %s, %s)
                """,
                (
                    operation.operation_id,
                    bytes.fromhex("88" * 32),
                    NOW,
                    NOW + timedelta(minutes=5),
                ),
            )


def test_capture_and_retention_limit_writers_serialize(
    repository: PostgresContentRepository,
) -> None:
    """Do not commit a policy outside a concurrent new global limit."""

    def service_capture() -> str:
        try:
            repository.put_capture_policy(
                _service_context("retention.write", mutation=True),
                CapturePolicy.COMPLETE,
                now=NOW,
            )
        except ContentError as error:
            return error.code.value
        return "written"

    def global_capture() -> str:
        try:
            repository.put_capture_policy(
                _context("retention.manage", mutation=True),
                CapturePolicy.DISABLED,
                now=NOW,
                minimum_policy=CapturePolicy.DISABLED,
                maximum_policy=CapturePolicy.DISABLED,
            )
        except ContentError as error:
            return error.code.value
        return "written"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        capture_results = list(
            executor.map(lambda call: call(), (service_capture, global_capture))
        )
    assert "written" in capture_results
    assert len(set(capture_results)) == 2

    service_context = _service_context("retention.preview", mutation=False)
    empty_revision = hashlib.sha256(b"[]").hexdigest()
    selection = RetentionSelection(RetentionDataClass.CAPTURED_CONTENT, 365)
    preview = repository.preview_retention(
        service_context,
        (selection,),
        expected_revision=empty_revision,
        now=NOW,
    )

    def service_retention() -> str:
        try:
            repository.put_retention(
                _service_context("retention.write", mutation=True),
                (selection,),
                expected_revision=empty_revision,
                confirmed_preview_id=preview.preview_id,
                now=NOW,
            )
        except ContentError as error:
            return error.code.value
        return "written"

    def global_retention() -> str:
        try:
            repository.put_retention_limits(
                _context("retention.manage", mutation=True),
                (
                    RetentionLimit(
                        RetentionDataClass.CAPTURED_CONTENT,
                        1,
                        10,
                    ),
                ),
                now=NOW,
            )
        except ContentError as error:
            return error.code.value
        return "written"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        retention_results = list(
            executor.map(lambda call: call(), (service_retention, global_retention))
        )
    assert "written" in retention_results
    assert len(set(retention_results)) == 2
