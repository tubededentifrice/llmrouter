"""Authenticated normal and urgent configuration distribution tests."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from llmrouter_backend.configuration import (
    AuthenticatedNormalRevision,
    AuthenticatedUrgentRevision,
    ConfigurationDistributionError,
    ConfigurationRevisionDistribution,
    CredentialGeneration,
    DistributionErrorCode,
    DistributionSafetyState,
    DistributionScope,
    NormalConfigurationRevision,
    RevisionAuthenticator,
    UrgentConfigurationRevision,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
SERVICE_ID = "0198b100-0000-7000-8000-000000000001"
PARENT_SERVICE_ID = "0198b100-0000-7000-8000-000000000002"
OTHER_SERVICE_ID = "0198b100-0000-7000-8000-000000000003"
WORKSPACE_ID = "0198b100-0000-7000-8000-000000000004"
OTHER_WORKSPACE_ID = "0198b100-0000-7000-8000-000000000005"
REVISION_1 = "0198b100-0000-7000-8000-000000000006"
REVISION_2 = "0198b100-0000-7000-8000-000000000007"
CREDENTIAL_ID = "0198b100-0000-7000-8000-000000000008"
AUTHENTICATION_KEY = b"test-only-revision-authentication-key-v1"
URGENT_SEQUENCE = 3


def _components() -> tuple[RevisionAuthenticator, ConfigurationRevisionDistribution]:
    authenticator = RevisionAuthenticator(AUTHENTICATION_KEY)
    return authenticator, ConfigurationRevisionDistribution(authenticator)


def _signed_normal(
    authenticator: RevisionAuthenticator,
    distribution: ConfigurationRevisionDistribution,
    revision: NormalConfigurationRevision,
) -> AuthenticatedNormalRevision:
    return authenticator.normal(
        revision, authentication_challenge=distribution.authentication_challenge
    )


def _signed_urgent(
    authenticator: RevisionAuthenticator,
    distribution: ConfigurationRevisionDistribution,
    revision: UrgentConfigurationRevision,
) -> AuthenticatedUrgentRevision:
    return authenticator.urgent(
        revision, authentication_challenge=distribution.authentication_challenge
    )


def _normal(
    *,
    scope: DistributionScope | None = None,
    revision_id: str = REVISION_1,
    revision_number: int = 1,
    required_urgent_sequence: int = 0,
    digest_byte: int = 1,
) -> NormalConfigurationRevision:
    return NormalConfigurationRevision(
        scope or DistributionScope(SERVICE_ID, WORKSPACE_ID),
        revision_id,
        revision_number,
        bytes([digest_byte]) * 32,
        NOW,
        required_urgent_sequence,
    )


def _urgent(
    sequence: int,
    *,
    disabled_service_ids: frozenset[str] = frozenset(),
    disabled_workspace_scopes: frozenset[DistributionScope] = frozenset(),
    revoked_credentials: frozenset[CredentialGeneration] = frozenset(),
    admission_allowed: bool = True,
) -> UrgentConfigurationRevision:
    return UrgentConfigurationRevision(
        sequence,
        disabled_service_ids,
        disabled_workspace_scopes,
        revoked_credentials,
        f"security-policy-{sequence}",
        admission_allowed,
        NOW,
    )


def test_authentication_and_revision_order_keep_last_valid_state() -> None:
    """Reject forged, changed, and older data without changing active state."""
    authenticator, distribution = _components()
    first = _normal()
    applied = distribution.apply_normal(
        _signed_normal(authenticator, distribution, first), received_at=NOW
    )
    assert applied.active_revision == REVISION_1
    assert "authentication_tag" not in repr(
        _signed_normal(authenticator, distribution, first)
    )

    forged = AuthenticatedNormalRevision(
        first, distribution.authentication_challenge, bytes(32)
    )
    with pytest.raises(ConfigurationDistributionError) as unauthenticated:
        distribution.apply_normal(forged, received_at=NOW + timedelta(seconds=1))
    assert unauthenticated.value.code is DistributionErrorCode.UNAUTHENTICATED

    restarted = ConfigurationRevisionDistribution(authenticator)
    with pytest.raises(ConfigurationDistributionError) as cross_restart:
        restarted.apply_normal(
            _signed_normal(authenticator, distribution, first),
            received_at=NOW + timedelta(seconds=1),
        )
    assert cross_restart.value.code is DistributionErrorCode.UNAUTHENTICATED
    assert restarted.status(first.scope, now=NOW).active_revision is None

    changed = replace(first, content_sha256=bytes([2]) * 32)
    with pytest.raises(ConfigurationDistributionError) as conflict:
        distribution.apply_normal(
            _signed_normal(authenticator, distribution, changed),
            received_at=NOW + timedelta(seconds=2),
        )
    assert conflict.value.code is DistributionErrorCode.INVALID_REVISION

    second = replace(first, revision_id=REVISION_2, revision_number=2)
    distribution.apply_normal(
        _signed_normal(authenticator, distribution, second),
        received_at=NOW + timedelta(seconds=3),
    )
    with pytest.raises(ConfigurationDistributionError) as rollback:
        distribution.apply_normal(
            _signed_normal(authenticator, distribution, first),
            received_at=NOW + timedelta(seconds=4),
        )
    assert rollback.value.code is DistributionErrorCode.REVISION_ROLLBACK
    assert (
        distribution.status(first.scope, now=NOW + timedelta(seconds=5)).active_revision
        == REVISION_2
    )


def test_normal_replay_does_not_extend_the_exact_24_hour_limit() -> None:
    """Fail new admission at 24 hours from the first validated receipt."""
    authenticator, distribution = _components()
    revision = _normal()
    signed = _signed_normal(authenticator, distribution, revision)
    distribution.apply_normal(signed, received_at=NOW)
    distribution.apply_normal(signed, received_at=NOW + timedelta(hours=23))
    scope = revision.scope
    with distribution.admission(
        scope,
        now=NOW + timedelta(hours=24) - timedelta(microseconds=1),
        ancestor_service_ids=(SERVICE_ID,),
    ) as snapshot:
        assert snapshot.configuration_revision_id == REVISION_1
    status = distribution.status(scope, now=NOW + timedelta(hours=24))
    assert status.stale
    assert status.safety_state is DistributionSafetyState.STALE
    assert status.received_at == NOW
    with (
        pytest.raises(ConfigurationDistributionError) as stale,
        distribution.admission(
            scope,
            now=NOW + timedelta(hours=24),
            ancestor_service_ids=(SERVICE_ID,),
        ),
    ):
        pass
    assert stale.value.code is DistributionErrorCode.CONFIGURATION_STALE
    rollback_status = distribution.status(scope, now=NOW - timedelta(microseconds=1))
    assert rollback_status.stale
    with (
        pytest.raises(ConfigurationDistributionError) as clock_rollback,
        distribution.admission(
            scope,
            now=NOW - timedelta(microseconds=1),
            ancestor_service_ids=(SERVICE_ID,),
        ),
    ):
        pass
    assert clock_rollback.value.code is DistributionErrorCode.CONFIGURATION_STALE


def test_urgent_watermark_and_replay_are_order_safe() -> None:
    """Apply urgent state before a normal revision that names its sequence."""
    authenticator, distribution = _components()
    waiting = _normal(required_urgent_sequence=URGENT_SEQUENCE)
    with pytest.raises(ConfigurationDistributionError) as pending:
        distribution.apply_normal(
            _signed_normal(authenticator, distribution, waiting), received_at=NOW
        )
    assert pending.value.code is DistributionErrorCode.URGENT_REVISION_PENDING

    urgent = _urgent(URGENT_SEQUENCE)
    signed = _signed_urgent(authenticator, distribution, urgent)
    distribution.apply_urgent(signed, received_at=NOW)
    with pytest.raises(ConfigurationDistributionError) as duplicate:
        distribution.apply_urgent(signed, received_at=NOW + timedelta(seconds=1))
    assert duplicate.value.code is DistributionErrorCode.REVISION_ROLLBACK
    with pytest.raises(ConfigurationDistributionError) as older:
        distribution.apply_urgent(
            _signed_urgent(authenticator, distribution, _urgent(2)),
            received_at=NOW + timedelta(seconds=2),
        )
    assert older.value.code is DistributionErrorCode.REVISION_ROLLBACK
    forged = AuthenticatedUrgentRevision(
        _urgent(4), distribution.authentication_challenge, bytes(32)
    )
    with pytest.raises(ConfigurationDistributionError) as unauthenticated:
        distribution.apply_urgent(
            forged,
            received_at=NOW + timedelta(seconds=3),
        )
    assert unauthenticated.value.code is DistributionErrorCode.UNAUTHENTICATED
    unchanged = distribution.status(waiting.scope, now=NOW)
    assert unchanged.urgent_sequence == URGENT_SEQUENCE
    assert unchanged.security_policy_revision == "security-policy-3"
    distribution.apply_normal(
        _signed_normal(authenticator, distribution, waiting),
        received_at=NOW + timedelta(seconds=4),
    )
    assert (
        distribution.status(waiting.scope, now=NOW).urgent_sequence == URGENT_SEQUENCE
    )


def test_urgent_scope_and_credential_generation_are_exact() -> None:
    """Block only the named scope and exact revoked credential generation."""
    authenticator, distribution = _components()
    scope = DistributionScope(SERVICE_ID, WORKSPACE_ID)
    other_workspace = DistributionScope(SERVICE_ID, OTHER_WORKSPACE_ID)
    distribution.apply_normal(
        _signed_normal(authenticator, distribution, _normal(scope=scope)),
        received_at=NOW,
    )
    distribution.apply_normal(
        _signed_normal(authenticator, distribution, _normal(scope=other_workspace)),
        received_at=NOW,
    )
    revoked = CredentialGeneration(CREDENTIAL_ID, 2, SERVICE_ID)
    urgent = _urgent(
        1,
        disabled_service_ids=frozenset({PARENT_SERVICE_ID}),
        disabled_workspace_scopes=frozenset({other_workspace}),
        revoked_credentials=frozenset({revoked}),
    )
    distribution.apply_urgent(
        _signed_urgent(authenticator, distribution, urgent), received_at=NOW
    )

    with (
        pytest.raises(ConfigurationDistributionError) as parent_disabled,
        distribution.admission(
            scope,
            now=NOW,
            ancestor_service_ids=(SERVICE_ID, PARENT_SERVICE_ID),
        ),
    ):
        pass
    assert parent_disabled.value.code is DistributionErrorCode.SERVICE_DISABLED
    with (
        pytest.raises(ConfigurationDistributionError) as workspace_disabled,
        distribution.admission(
            other_workspace,
            now=NOW,
            ancestor_service_ids=(SERVICE_ID,),
        ),
    ):
        pass
    assert workspace_disabled.value.code is DistributionErrorCode.WORKSPACE_DISABLED

    with distribution.admission(
        scope,
        now=NOW,
        ancestor_service_ids=(SERVICE_ID, OTHER_SERVICE_ID),
    ) as snapshot:
        snapshot.require_credentials(
            (
                CredentialGeneration(CREDENTIAL_ID, 1, SERVICE_ID),
                CredentialGeneration(CREDENTIAL_ID, 2, OTHER_SERVICE_ID),
            )
        )
        with pytest.raises(ConfigurationDistributionError) as credential:
            snapshot.require_credentials((revoked,))
        assert credential.value.code is DistributionErrorCode.CREDENTIAL_REVOKED

    distribution.apply_urgent(
        _signed_urgent(
            authenticator,
            distribution,
            _urgent(2, disabled_service_ids=frozenset({SERVICE_ID})),
        ),
        received_at=NOW + timedelta(seconds=1),
    )
    assert (
        distribution.status(scope, now=NOW + timedelta(seconds=1)).safety_state
        is DistributionSafetyState.BLOCKED
    )


def test_security_policy_and_urgent_apply_serialize_with_admission() -> None:
    """Do not change urgent state during one admission commit boundary."""
    authenticator, distribution = _components()
    revision = _normal()
    distribution.apply_normal(
        _signed_normal(authenticator, distribution, revision), received_at=NOW
    )
    apply_started = threading.Event()
    apply_finished = threading.Event()

    def apply_block() -> None:
        apply_started.set()
        distribution.apply_urgent(
            _signed_urgent(
                authenticator,
                distribution,
                _urgent(1, admission_allowed=False),
            ),
            received_at=NOW,
        )
        apply_finished.set()

    with distribution.admission(
        revision.scope, now=NOW, ancestor_service_ids=(SERVICE_ID,)
    ):
        thread = threading.Thread(target=apply_block)
        thread.start()
        assert apply_started.wait(timeout=1)
        assert not apply_finished.wait(timeout=0.05)
    thread.join(timeout=1)
    assert apply_finished.is_set()
    with (
        pytest.raises(ConfigurationDistributionError) as blocked,
        distribution.admission(
            revision.scope,
            now=NOW,
            ancestor_service_ids=(SERVICE_ID,),
        ),
    ):
        pass
    assert blocked.value.code is DistributionErrorCode.SECURITY_POLICY_BLOCKED
    assert (
        distribution.status(
            revision.scope,
            now=NOW,
            ancestor_service_ids=(SERVICE_ID,),
        ).safety_state
        is DistributionSafetyState.BLOCKED
    )
