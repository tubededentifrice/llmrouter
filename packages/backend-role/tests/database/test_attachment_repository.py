"""PostgreSQL immutable attachment custody tests."""
# ruff: noqa: D103

from __future__ import annotations

import concurrent.futures
import hashlib
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from llmrouter_backend.attachments import (
    AttachmentError,
    AttachmentErrorCode,
    AttachmentState,
    CreateAttachment,
    PostgresAttachmentRepository,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.credential_store.crypto import EnvelopeCipher
from llmrouter_backend.database import migrate

from .helpers import (
    OTHER_SERVICE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_request,
    seed_scope,
)

NOW = datetime(2026, 8, 13, 20, tzinfo=UTC)
CONTENT = b"bounded attachment content"
DECLARATION = CreateAttachment(
    "text/plain", len(CONTENT), hashlib.sha256(CONTENT).hexdigest()
)
KEY = bytes(range(32))


def _context(
    *,
    operation: str = "attachment.create",
    mutation: bool = True,
    service_id: str = SERVICE_ID,
    workspace_id: str | None = WORKSPACE_ID,
) -> RequestContext:
    return RequestContext(
        request_id="transport-request",
        actor_kind=PrincipalKind.SERVICE,
        actor_id=service_id,
        authority_class=AuthorityClass.SERVICE,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=Audience.DATA_PLANE,
        operation=operation,
        scope=Scope(service_id, workspace_id),
        authorized_at=NOW,
        recent_authentication_at=None,
        mutation=mutation,
    )


def _cipher(key: bytes = KEY) -> EnvelopeCipher:
    return EnvelopeCipher(
        {"attachment-key": key},
        current_key_id="attachment-key",
        random_bytes=os.urandom,
    )


@pytest.fixture
def repository(database_url: str) -> PostgresAttachmentRepository:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
    return PostgresAttachmentRepository(database_url, cipher=_cipher())


def test_create_has_scope_equality_response_loss_recovery_and_opaque_id(
    repository: PostgresAttachmentRepository,
) -> None:
    """Serialize equal public creates without adding a caller identity field."""
    expires_at = NOW + timedelta(days=7)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: repository.create(
                    _context(), DECLARATION, expires_at=expires_at, now=NOW
                ),
                range(2),
            )
        )
    assert {item.value.attachment_id for item in results} == {
        results[0].value.attachment_id
    }
    assert sorted(item.replayed for item in results) == [False, True]
    assert results[0].value.state is AttachmentState.AWAITING_CONTENT
    later = repository.create(
        _context(),
        DECLARATION,
        expires_at=expires_at,
        now=NOW + timedelta(minutes=16),
    )
    assert not later.replayed
    assert later.value.attachment_id != results[0].value.attachment_id
    service_only = repository.create(
        _context(workspace_id=None),
        DECLARATION,
        expires_at=expires_at,
        now=NOW,
    )
    assert service_only.value.attachment_id != results[0].value.attachment_id


def test_upload_is_concurrent_one_time_verified_encrypted_and_scoped(
    database_url: str, repository: PostgresAttachmentRepository
) -> None:
    """Commit one exact encrypted body and hide it outside its scope."""
    created = repository.create(
        _context(), DECLARATION, expires_at=NOW + timedelta(days=7), now=NOW
    ).value
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                repository.upload,
                _context(),
                created.attachment_id,
                CONTENT,
                now=NOW,
            )
            for _ in range(2)
        ]
    outcomes: list[AttachmentState | AttachmentErrorCode] = []
    for future in futures:
        try:
            outcomes.append(future.result().state)
        except AttachmentError as error:
            outcomes.append(error.code)
    assert outcomes == [AttachmentState.READY, AttachmentState.READY]
    with psycopg.connect(database_url) as connection:
        stored = connection.execute(
            """SELECT content.ciphertext, content.encrypted_data_key
               FROM router.attachment_content AS content
               WHERE content.attachment_id = %s""",
            (created.attachment_id,),
        ).fetchone()
    assert stored is not None
    assert CONTENT not in bytes(stored[0])
    assert CONTENT not in bytes(stored[1])
    with pytest.raises(AttachmentError) as changed_replay:
        repository.upload(
            _context(), created.attachment_id, b"changed attachment bytes", now=NOW
        )
    assert changed_replay.value.code is AttachmentErrorCode.ALREADY_COMPLETE
    read = repository.content(
        _context(operation="attachment.read", mutation=False),
        created.attachment_id,
        now=NOW,
    )
    assert read.value == CONTENT
    with pytest.raises(AttachmentError) as hidden:
        repository.content(
            _context(
                operation="attachment.read",
                mutation=False,
                service_id=OTHER_SERVICE_ID,
                workspace_id=None,
            ),
            created.attachment_id,
            now=NOW,
        )
    assert hidden.value.code is AttachmentErrorCode.NOT_FOUND


def test_invalid_upload_does_not_consume_object_and_expiry_erases_ciphertext(
    database_url: str, repository: PostgresAttachmentRepository
) -> None:
    """Permit correction before completion and remove bytes at expiry."""
    expires_at = NOW + timedelta(hours=1)
    created = repository.create(
        _context(), DECLARATION, expires_at=expires_at, now=NOW
    ).value
    with pytest.raises(AttachmentError) as invalid:
        repository.upload(_context(), created.attachment_id, b"changed", now=NOW)
    assert invalid.value.code is AttachmentErrorCode.INVALID
    repository.upload(_context(), created.attachment_id, CONTENT, now=NOW)
    metadata = repository.metadata(
        _context(operation="attachment.read", mutation=False),
        created.attachment_id,
        now=expires_at,
    )
    assert metadata.state is AttachmentState.EXPIRED
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM router.attachment_content WHERE attachment_id = %s",
            (created.attachment_id,),
        ).fetchone()
    assert count == (0,)


def test_tamper_wrong_key_and_metadata_context_fail_closed(
    database_url: str, repository: PostgresAttachmentRepository
) -> None:
    """Reject a changed envelope, unavailable key, or changed authenticated scope."""
    created = repository.create(
        _context(), DECLARATION, expires_at=NOW + timedelta(days=7), now=NOW
    ).value
    repository.upload(_context(), created.attachment_id, CONTENT, now=NOW)
    wrong_key_repository = PostgresAttachmentRepository(
        database_url, cipher=_cipher(bytes(reversed(KEY)))
    )
    read_context = _context(operation="attachment.read", mutation=False)
    with pytest.raises(AttachmentError) as wrong_key:
        wrong_key_repository.content(read_context, created.attachment_id, now=NOW)
    assert wrong_key.value.code is AttachmentErrorCode.INTERNAL
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            """ALTER TABLE router.attachment_content
               DISABLE TRIGGER attachment_content_change_guard"""
        )
        connection.execute(
            """UPDATE router.attachment_content
               SET ciphertext = set_byte(ciphertext, 30, get_byte(ciphertext, 30) # 1)
               WHERE attachment_id = %s""",
            (created.attachment_id,),
        )
        connection.execute(
            """ALTER TABLE router.attachment_content
               ENABLE TRIGGER attachment_content_change_guard"""
        )
    with pytest.raises(AttachmentError) as tampered:
        repository.content(read_context, created.attachment_id, now=NOW)
    assert tampered.value.code is AttachmentErrorCode.INTERNAL


def test_ready_metadata_returns_closed_admission_reference(
    repository: PostgresAttachmentRepository,
) -> None:
    """Resolve one ready identity without request aggregate validation."""
    created = repository.create(
        _context(), DECLARATION, expires_at=NOW + timedelta(days=7), now=NOW
    ).value
    repository.upload(_context(), created.attachment_id, CONTENT, now=NOW)
    reference = repository.admission_reference(
        _context(operation="model.create", mutation=True),
        created.attachment_id,
        now=NOW,
    )
    assert reference.sha256 == DECLARATION.sha256
    assert reference.byte_length == len(CONTENT)


def test_database_rejects_ready_without_content_and_content_without_pending(
    database_url: str, repository: PostgresAttachmentRepository
) -> None:
    """Keep direct SQL from breaking the ready-content invariant."""
    created = repository.create(
        _context(), DECLARATION, expires_at=NOW + timedelta(days=7), now=NOW
    ).value
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
    ):
        connection.execute(
            """UPDATE router.attachment_status
               SET state = 'ready', verified_at = %s, revision = 2
               WHERE attachment_id = %s""",
            (NOW, created.attachment_id),
        )
    repository.upload(_context(), created.attachment_id, CONTENT, now=NOW)
    other = repository.create(
        _context(),
        CreateAttachment("text/plain", 1, hashlib.sha256(b"x").hexdigest()),
        expires_at=NOW + timedelta(days=7),
        now=NOW,
    ).value
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE router.attachment_status
               SET state = 'failed', revision = 2
               WHERE attachment_id = %s""",
            (other.attachment_id,),
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """INSERT INTO router.attachment_content (
                       attachment_id, ciphertext, encrypted_data_key,
                       wrapping_key_id
                   ) VALUES (%s, %s, %s, 'test-key')""",
                (other.attachment_id, bytes(41), bytes(72)),
            )


def test_database_rejects_content_without_ready_state_and_expiry_without_erasure(
    database_url: str, repository: PostgresAttachmentRepository
) -> None:
    """Keep attachment state and stored content equal at transaction commit."""
    created = repository.create(
        _context(), DECLARATION, expires_at=NOW + timedelta(days=7), now=NOW
    ).value
    with (
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """INSERT INTO router.attachment_content (
                   attachment_id, ciphertext, encrypted_data_key,
                   wrapping_key_id
               ) VALUES (%s, %s, %s, 'test-key')""",
            (
                created.attachment_id,
                bytes(len(CONTENT) + 40),
                bytes(72),
            ),
        )

    repository.upload(_context(), created.attachment_id, CONTENT, now=NOW)
    with (
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """UPDATE router.attachment_status
               SET state = 'expired', revision = revision + 1,
                   verified_at = NULL, updated_at = %s
               WHERE attachment_id = %s""",
            (NOW + timedelta(hours=1), created.attachment_id),
        )

    assert repository.content(
        _context(operation="attachment.read", mutation=False),
        created.attachment_id,
        now=NOW,
    ).value == CONTENT


def test_database_rejects_status_deletion_and_pending_request_reference(
    database_url: str, repository: PostgresAttachmentRepository
) -> None:
    """Keep status durable and incomplete content out of admitted requests."""
    created = repository.create(
        _context(), DECLARATION, expires_at=NOW + timedelta(days=7), now=NOW
    ).value
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
    ):
        connection.execute(
            "DELETE FROM router.attachment_status WHERE attachment_id = %s",
            (created.attachment_id,),
        )
    with psycopg.connect(database_url) as connection:
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.request_attachments (
                       request_row_id, service_id, workspace_id, attachment_id,
                       ordinal, content_sha256, byte_length
                   ) VALUES (%s, %s, %s, %s, 1, %s, %s)""",
                (
                    REQUEST_ROW_ID,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    created.attachment_id,
                    bytes.fromhex(DECLARATION.sha256),
                    DECLARATION.byte_length,
                ),
            )


@pytest.mark.parametrize(
    ("ciphertext_size", "encrypted_key_size"),
    [(40, 72), (42, 72), (41, 71), (41, 73)],
)
def test_database_rejects_malformed_envelope_sizes(
    database_url: str,
    repository: PostgresAttachmentRepository,
    ciphertext_size: int,
    encrypted_key_size: int,
) -> None:
    """Bound direct SQL envelopes to the declared plaintext and key sizes."""
    declaration = CreateAttachment("text/plain", 1, hashlib.sha256(b"x").hexdigest())
    created = repository.create(
        _context(), declaration, expires_at=NOW + timedelta(days=7), now=NOW
    ).value
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error),
    ):
        connection.execute(
            """INSERT INTO router.attachment_content (
                   attachment_id, ciphertext, encrypted_data_key,
                   wrapping_key_id
               ) VALUES (%s, %s, %s, 'test-key')""",
            (
                created.attachment_id,
                bytes(ciphertext_size),
                bytes(encrypted_key_size),
            ),
        )


def test_disabled_workspace_stops_all_access(
    database_url: str, repository: PostgresAttachmentRepository
) -> None:
    """Require the complete active service and workspace chain."""
    created = repository.create(
        _context(), DECLARATION, expires_at=NOW + timedelta(days=7), now=NOW
    ).value
    repository.upload(_context(), created.attachment_id, CONTENT, now=NOW)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE router.workspaces
               SET state = 'disabled', state_revision = state_revision + 1
               WHERE id = %s""",
            (WORKSPACE_ID,),
        )
    with pytest.raises(AttachmentError) as unavailable:
        repository.metadata(
            _context(operation="attachment.read", mutation=False),
            created.attachment_id,
            now=NOW,
        )
    assert unavailable.value.code is AttachmentErrorCode.WORKSPACE_UNAVAILABLE


def test_disabled_service_ancestor_stops_all_access(
    database_url: str, repository: PostgresAttachmentRepository
) -> None:
    """Require each service ancestor to stay active."""
    parent_id = "0198a080-0000-7000-8000-000000000154"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO router.services (id, stable_name) VALUES (%s, 'parent')",
            (parent_id,),
        )
        connection.execute(
            """UPDATE router.services
               SET parent_service_id = %s, state_revision = state_revision + 1
               WHERE id = %s""",
            (parent_id, SERVICE_ID),
        )
        connection.execute(
            """UPDATE router.services
               SET state = 'disabled', state_revision = state_revision + 1
               WHERE id = %s""",
            (parent_id,),
        )
    with pytest.raises(AttachmentError) as unavailable:
        repository.create(
            _context(),
            DECLARATION,
            expires_at=NOW + timedelta(days=7),
            now=NOW,
        )
    assert unavailable.value.code is AttachmentErrorCode.INSUFFICIENT_SCOPE
