"""PostgreSQL tests for one-use administration embed sessions."""
# ruff: noqa: PLR2004, S105

from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    OperationPolicy,
    PrincipalKind,
    RequestContext,
    Scope,
    ScopeKind,
    authorize,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.embed_sessions import (
    EmbedSessionError,
    EmbedSessionRepository,
    EmbedSessionRequest,
    EmbedTheme,
)

from .helpers import OTHER_WORKSPACE_ID, SERVICE_ID, WORKSPACE_ID, seed_scope

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
FRAME_ORIGIN = "https://router.example"
HOST_ORIGIN = "https://host.example"


@pytest.fixture
def repository(database_url: str) -> EmbedSessionRepository:
    """Create the current schema and its embed-session repository."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
    return EmbedSessionRepository(
        database_url,
        frame_origin=FRAME_ORIGIN,
        allowed_host_origins={SERVICE_ID: frozenset({HOST_ORIGIN})},
    )


def _context(*, workspace_id: str | None = WORKSPACE_ID) -> RequestContext:
    return RequestContext(
        request_id="request-create",
        actor_kind=PrincipalKind.SERVICE,
        actor_id=SERVICE_ID,
        authority_class=AuthorityClass.SERVICE,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=Audience.HOST_BACKEND,
        operation="admin_embed.create",
        scope=Scope(SERVICE_ID, workspace_id),
        authorized_at=NOW,
        recent_authentication_at=None,
        mutation=True,
    )


def _document(
    *, sensitive: bool = False, workspace_id: str | None = WORKSPACE_ID
) -> EmbedSessionRequest:
    return EmbedSessionRequest(
        host_user_subject="host-user",
        workspace_id=workspace_id,
        allowed_origin=HOST_ORIGIN,
        permissions=(
            ["configuration.write"] if sensitive else ["configuration.read"]
        ),
        recent_auth_at=NOW - timedelta(minutes=1) if sensitive else None,
        theme=EmbedTheme(mode="dark", density="compact", corner_style="rounded"),
    )


def test_create_redeem_authenticate_and_audit_without_plaintext(
    database_url: str, repository: EmbedSessionRepository
) -> None:
    """A valid flow stores digests and returns exact bounded authority."""
    created = repository.create(_context(), _document(), now=NOW)
    assert created.bootstrap_token not in created.frame_url
    assert created.expires_at == NOW + timedelta(minutes=5)
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT bootstrap_token_digest, recent_auth_at, theme_mode,
                   theme_density, theme_corner_style
            FROM router.embed_sessions WHERE id = %s
            """,
            (created.session_id,),
        ).fetchone()
        assert row is not None
        assert row[0] != created.bootstrap_token.encode()
        assert len(row[0]) == 32
        assert row[1:] == (None, "dark", "compact", "rounded")
    redeemed = repository.redeem(
        created.session_id,
        created.bootstrap_token,
        "nonce-0123456789",
        HOST_ORIGIN,
        request_origin=FRAME_ORIGIN,
        request_id="request-redeem",
        now=NOW + timedelta(seconds=1),
    )
    assert redeemed.principal.service_id == SERVICE_ID
    assert redeemed.principal.allowed_workspace_ids == frozenset({WORKSPACE_ID})
    assert redeemed.principal.operations == frozenset({"configuration.read"})
    authenticated = repository.authenticate_session(
        redeemed.session_token,
        request_origin=FRAME_ORIGIN,
        request_id="request-authenticate",
        now=NOW + timedelta(seconds=2),
    )
    assert authenticated == redeemed.principal
    context = authorize(
        authenticated,
        OperationPolicy(
            operation="configuration.read",
            authority_path=AuthorityPath.EMBED,
            principal_kinds=frozenset({PrincipalKind.EMBED}),
            scope_kind=ScopeKind.WORKSPACE,
        ),
        Scope(SERVICE_ID, WORKSPACE_ID),
        request_id="request-configuration-read",
        now=NOW + timedelta(seconds=2),
    )
    assert context.actor_kind is PrincipalKind.EMBED
    assert context.scope == Scope(SERVICE_ID, WORKSPACE_ID)
    with pytest.raises(EmbedSessionError) as wrong_origin:
        repository.authenticate_session(
            redeemed.session_token,
            request_origin="https://other.example",
            request_id="request-wrong-origin",
            now=NOW + timedelta(seconds=2),
        )
    assert wrong_origin.value.code == "invalid_token"
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """
            SELECT action, permission_result
            FROM router.audit_events
            WHERE action LIKE 'embed_session.%'
            ORDER BY occurred_at, action
            """
        ).fetchall() == [
            ("embed_session.create", "permitted"),
            ("embed_session.bootstrap", "permitted"),
        ]
        stored = connection.execute(
            "SELECT session_token_digest FROM router.embed_sessions WHERE id = %s",
            (created.session_id,),
        ).fetchone()
        assert stored is not None
        assert stored[0] != redeemed.session_token.encode()


def test_sensitive_session_uses_host_authentication_window(
    repository: EmbedSessionRepository,
) -> None:
    """A sensitive session expires at the asserted five-minute boundary."""
    created = repository.create(_context(), _document(sensitive=True), now=NOW)
    assert created.expires_at == NOW + timedelta(minutes=4)
    missing = _document(sensitive=True).model_copy(update={"recent_auth_at": None})
    with pytest.raises(EmbedSessionError) as missing_error:
        repository.create(_context(), missing, now=NOW)
    assert missing_error.value.code == "recent_auth_required"
    stale = _document(sensitive=True).model_copy(
        update={"recent_auth_at": NOW - timedelta(minutes=5)}
    )
    with pytest.raises(EmbedSessionError, match="Recent authentication"):
        repository.create(_context(), stale, now=NOW)
    future = _document(sensitive=True).model_copy(
        update={"recent_auth_at": NOW + timedelta(seconds=1)}
    )
    with pytest.raises(EmbedSessionError, match="Recent authentication"):
        repository.create(_context(), future, now=NOW)


def test_host_origin_must_match_the_service_allow_list(
    repository: EmbedSessionRepository,
) -> None:
    """A trusted host token cannot select an unconfigured web origin."""
    document = _document().model_copy(
        update={"allowed_origin": "https://other.example"}
    )
    with pytest.raises(EmbedSessionError) as captured:
        repository.create(_context(), document, now=NOW)
    assert captured.value.code == "insufficient_scope"


@pytest.mark.parametrize(
    ("session_id", "token", "host_origin", "frame_origin"),
    [
        ("opaque", "x" * 43, HOST_ORIGIN, FRAME_ORIGIN),
        (None, "x" * 43, HOST_ORIGIN, FRAME_ORIGIN),
        ("created", "x" * 43, HOST_ORIGIN, FRAME_ORIGIN),
        ("created", "token", "https://other.example", FRAME_ORIGIN),
        ("created", "token", HOST_ORIGIN, "https://other.example"),
    ],
)
def test_redemption_failures_are_safe(
    repository: EmbedSessionRepository,
    session_id: str | None,
    token: str,
    host_origin: str,
    frame_origin: str,
) -> None:
    """Malformed identity, wrong token, or wrong origins return one safe result."""
    created = repository.create(_context(), _document(), now=NOW)
    selected_id = created.session_id if session_id == "created" else session_id
    selected_token = created.bootstrap_token if token == "token" else token
    with pytest.raises(EmbedSessionError) as captured:
        repository.redeem(
            selected_id,  # type: ignore[arg-type]
            selected_token,
            "nonce-0123456789",
            host_origin,
            request_origin=frame_origin,
            request_id="request-redemption",
            now=NOW + timedelta(seconds=1),
        )
    assert captured.value.code == "not_found"
    assert created.bootstrap_token not in str(captured.value)


def test_replay_expiry_and_revocation_fail_closed(
    database_url: str,
    repository: EmbedSessionRepository,
) -> None:
    """Only an unexpired, unrevoked bootstrap can be consumed once."""
    replay = repository.create(_context(), _document(), now=NOW)
    repository.redeem(
        replay.session_id,
        replay.bootstrap_token,
        "nonce-0123456789",
        HOST_ORIGIN,
        request_origin=FRAME_ORIGIN,
        request_id="first",
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(EmbedSessionError):
        repository.redeem(
            replay.session_id,
            replay.bootstrap_token,
            "nonce-0123456789",
            HOST_ORIGIN,
            request_origin=FRAME_ORIGIN,
            request_id="replay",
            now=NOW + timedelta(seconds=2),
        )
    with psycopg.connect(database_url) as connection:
        denied = connection.execute(
            """
            SELECT count(*) FROM router.audit_events
            WHERE action = 'embed_session.bootstrap'
              AND permission_result = 'denied'
            """
        ).fetchone()
        assert denied == (1,)
    expired = repository.create(_context(), _document(), now=NOW)
    with pytest.raises(EmbedSessionError):
        repository.redeem(
            expired.session_id,
            expired.bootstrap_token,
            "nonce-0123456789",
            HOST_ORIGIN,
            request_origin=FRAME_ORIGIN,
            request_id="expired",
            now=expired.expires_at,
        )
    revoked = repository.create(_context(), _document(), now=NOW)
    repository.revoke(_context(), revoked.session_id, now=NOW + timedelta(seconds=1))
    with pytest.raises(EmbedSessionError):
        repository.redeem(
            revoked.session_id,
            revoked.bootstrap_token,
            "nonce-0123456789",
            HOST_ORIGIN,
            request_origin=FRAME_ORIGIN,
            request_id="revoked",
            now=NOW + timedelta(seconds=2),
        )


def test_workspace_limited_revocation_cannot_cross_scope(
    repository: EmbedSessionRepository,
) -> None:
    """A workspace-limited host token cannot revoke a service-wide session."""
    service_wide = repository.create(
        _context(workspace_id=None), _document(workspace_id=None), now=NOW
    )
    with pytest.raises(EmbedSessionError) as captured:
        repository.revoke(
            _context(workspace_id=None),
            service_wide.session_id,
            now=NOW + timedelta(seconds=1),
            allowed_workspace_ids=frozenset({WORKSPACE_ID}),
        )
    assert captured.value.code == "not_found"


def test_wrong_workspace_and_service_scope_are_hidden(
    repository: EmbedSessionRepository,
) -> None:
    """A session cannot cross a service or workspace boundary."""
    with pytest.raises(EmbedSessionError) as workspace:
        repository.create(
            _context(workspace_id=OTHER_WORKSPACE_ID),
            _document(workspace_id=OTHER_WORKSPACE_ID),
            now=NOW,
        )
    assert workspace.value.code == "not_found"
    with pytest.raises(EmbedSessionError) as service:
        repository.create(
            replace(
                _context(),
                scope=Scope(
                    "0198a080-0000-7000-8000-000000000002", WORKSPACE_ID
                ),
            ),
            _document(),
            now=NOW,
        )
    assert service.value.code == "not_found"
    wrong_audience = replace(
        _context(), machine_audience=Audience.CONFIGURATION
    )
    with pytest.raises(EmbedSessionError) as audience:
        repository.create(wrong_audience, _document(), now=NOW)
    assert audience.value.code == "insufficient_scope"
    mismatched_document = _document().model_copy(
        update={"workspace_id": "0198a080-0000-7000-8000-000000000099"}
    )
    with pytest.raises(EmbedSessionError) as document_scope:
        repository.create(_context(), mismatched_document, now=NOW)
    assert document_scope.value.code == "not_found"


def test_concurrent_redemption_has_one_winner(
    repository: EmbedSessionRepository,
) -> None:
    """The conditional update makes two transaction races consume at most once."""
    created = repository.create(_context(), _document(), now=NOW)

    def redeem(index: int) -> str:
        try:
            repository.redeem(
                created.session_id,
                created.bootstrap_token,
                f"nonce-012345678{index}",
                HOST_ORIGIN,
                request_origin=FRAME_ORIGIN,
                request_id=f"race-{index}",
                now=NOW + timedelta(seconds=1),
            )
        except EmbedSessionError:
            return "denied"
        return "permitted"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(redeem, range(2)))
    assert sorted(results) == ["denied", "permitted"]
