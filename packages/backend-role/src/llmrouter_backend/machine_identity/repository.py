"""PostgreSQL authority for service bootstrap and opaque access tokens."""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from llmrouter_backend.authority import (
    MACHINE_OPERATIONS_BY_AUDIENCE,
    SERVICE_TOKEN_LIFETIME,
    TOKEN_CLOCK_SKEW,
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    ScopeKind,
    ServicePrincipal,
)
from llmrouter_backend.machine_identity.errors import MachineIdentityError
from llmrouter_backend.machine_identity.model import (
    DEFAULT_ROTATION_OVERLAP,
    MAXIMUM_ROTATION_OVERLAP,
    MAXIMUM_WORKSPACE_IDS,
    BootstrapCreated,
    BootstrapScope,
    DigestKeyCustodyState,
    DigestKeyCustodyStatus,
    SecretValue,
    TLSClientIdentity,
    TokenExchange,
    WorkspaceLimit,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from psycopg import Connection

_ARGON2 = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
_RECENT_ADMIN_AUTH_LIMIT = timedelta(minutes=5)
_MINIMUM_DIGEST_KEY_BYTES = 32


class MachineCredentialRepository:
    """Issue and authenticate machine credentials without storing plaintext."""

    def __init__(
        self,
        database_url: str,
        *,
        issuer: str,
        digest_keys: Mapping[str, bytes],
        current_digest_key_id: str,
    ) -> None:
        """Use deployment-held keyed-digest material and one exact issuer."""
        if not database_url or not issuer:
            msg = "The database URL and machine issuer must not be empty."
            raise ValueError(msg)
        if current_digest_key_id not in digest_keys:
            msg = "The current token-digest key is not available."
            raise ValueError(msg)
        if any(
            not key_id or len(key) < _MINIMUM_DIGEST_KEY_BYTES
            for key_id, key in digest_keys.items()
        ):
            msg = "Each token-digest key must have an identity and 256 bits."
            raise ValueError(msg)
        self._database_url = database_url
        self._issuer = issuer
        self._digest_keys = dict(digest_keys)
        self._current_digest_key_id = current_digest_key_id

    def create_initial_bootstrap(
        self,
        context: RequestContext,
        service_id: str,
        scope: BootstrapScope,
        *,
        now: datetime,
    ) -> BootstrapCreated:
        """Create generation one and return its secret one time."""
        _require_aware_now(now)
        _require_credential_administrator(context, now=now)
        parsed_service_id = _parse_uuid(service_id, context.request_id)
        secret = _new_secret()
        verifier = _ARGON2.hash(secret.value)
        generation_id = uuid.uuid4()
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                _lock_service_credentials(connection, parsed_service_id)
                row = connection.execute(
                    "SELECT state FROM router.services WHERE id = %s FOR UPDATE",
                    (parsed_service_id,),
                ).fetchone()
                if row is None:
                    msg = "not_found"
                    raise MachineIdentityError(msg, context.request_id)
                if row[0] != "active":
                    msg = "invalid_token"
                    raise MachineIdentityError(msg, context.request_id)
                if (
                    connection.execute(
                        """
                    SELECT 1 FROM router.service_bootstrap_generations
                    WHERE service_id = %s
                    """,
                        (parsed_service_id,),
                    ).fetchone()
                    is not None
                ):
                    msg = "insufficient_scope"
                    raise MachineIdentityError(msg, context.request_id)
                _insert_generation(
                    connection,
                    generation_id=generation_id,
                    service_id=parsed_service_id,
                    generation=1,
                    issuer=self._issuer,
                    verifier=verifier,
                    scope=scope,
                    now=now,
                )
                _insert_audit(
                    connection,
                    context,
                    event_id=generation_id,
                    service_id=parsed_service_id,
                    action="credential.create",
                    now=now,
                )
        return BootstrapCreated(str(parsed_service_id), 1, secret)

    def rotate_bootstrap(
        self,
        context: RequestContext,
        service_id: str,
        scope: BootstrapScope,
        *,
        now: datetime,
        overlap: timedelta = DEFAULT_ROTATION_OVERLAP,
    ) -> BootstrapCreated:
        """Create one newer generation and bound the previous overlap."""
        _require_aware_now(now)
        _require_credential_administrator(context, now=now)
        if not timedelta(0) <= overlap <= MAXIMUM_ROTATION_OVERLAP:
            msg = "The bootstrap overlap must be from zero to 24 hours."
            raise ValueError(msg)
        parsed_service_id = _parse_uuid(service_id, context.request_id)
        secret = _new_secret()
        verifier = _ARGON2.hash(secret.value)
        generation_id = uuid.uuid4()
        overlap_end = now + overlap
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                _lock_service_credentials(connection, parsed_service_id)
                service = connection.execute(
                    """
                    SELECT state FROM router.services
                    WHERE id = %s FOR UPDATE
                    """,
                    (parsed_service_id,),
                ).fetchone()
                if service is None or service[0] != "active":
                    msg_0 = "not_found"
                    raise MachineIdentityError(msg_0, context.request_id)
                newest = connection.execute(
                    """
                    SELECT generation, revoked_at, valid_until, created_at
                    FROM router.service_bootstrap_generations
                    WHERE service_id = %s
                    ORDER BY generation DESC
                    LIMIT 1
                    """,
                    (parsed_service_id,),
                ).fetchone()
                if newest is None:
                    msg_1 = "not_found"
                    raise MachineIdentityError(msg_1, context.request_id)
                _reject_backdated_credential_change(
                    connection,
                    parsed_service_id,
                    now=now,
                    request_id=context.request_id,
                )
                if newest[3] > now:
                    msg_2 = "not_found"
                    raise MachineIdentityError(msg_2, context.request_id)
                previous_generation = int(newest[0])
                prior_is_live = newest[1] is None and (
                    newest[2] is None or newest[2] > now
                )
                generation = previous_generation + 1
                connection.execute(
                    """
                    UPDATE router.service_bootstrap_generations
                    SET revoked_at = COALESCE(revoked_at, %s),
                        valid_until = LEAST(COALESCE(valid_until, %s), %s)
                    WHERE service_id = %s AND generation < %s
                    """,
                    (now, now, now, parsed_service_id, previous_generation),
                )
                connection.execute(
                    """
                    UPDATE router.service_access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE service_id = %s
                      AND bootstrap_generation < %s
                    """,
                    (now, parsed_service_id, previous_generation),
                )
                connection.execute(
                    """
                    UPDATE router.service_bootstrap_generations
                    SET valid_until = %s
                    WHERE service_id = %s AND generation = %s
                      AND revoked_at IS NULL
                      AND (valid_until IS NULL OR valid_until > %s)
                    """,
                    (overlap_end, parsed_service_id, previous_generation, now),
                )
                if overlap == timedelta(0):
                    connection.execute(
                        """
                        UPDATE router.service_access_tokens
                        SET revoked_at = %s
                        WHERE service_id = %s
                          AND bootstrap_generation = %s
                          AND revoked_at IS NULL
                        """,
                        (now, parsed_service_id, previous_generation),
                    )
                _insert_generation(
                    connection,
                    generation_id=generation_id,
                    service_id=parsed_service_id,
                    generation=generation,
                    issuer=self._issuer,
                    verifier=verifier,
                    scope=scope,
                    now=now,
                )
                _insert_audit(
                    connection,
                    context,
                    event_id=generation_id,
                    service_id=parsed_service_id,
                    action="credential.rotate",
                    now=now,
                )
        return BootstrapCreated(
            str(parsed_service_id),
            generation,
            secret,
            overlap_end if prior_is_live else None,
        )

    def revoke_generation(
        self,
        context: RequestContext,
        service_id: str,
        generation: int,
        *,
        now: datetime,
    ) -> None:
        """Stop exchange and all access tokens for one generation immediately."""
        _require_aware_now(now)
        _require_credential_administrator(context, now=now)
        parsed_service_id = _parse_uuid(service_id, context.request_id)
        event_id = uuid.uuid4()
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                _lock_service_credentials(connection, parsed_service_id)
                _reject_backdated_credential_change(
                    connection,
                    parsed_service_id,
                    now=now,
                    request_id=context.request_id,
                )
                changed = connection.execute(
                    """
                    UPDATE router.service_bootstrap_generations
                    SET revoked_at = %s, valid_until = %s
                    WHERE service_id = %s AND generation = %s
                      AND revoked_at IS NULL
                      AND created_at <= %s
                    RETURNING id
                    """,
                    (now, now, parsed_service_id, generation, now),
                ).fetchone()
                if changed is None:
                    msg = "not_found"
                    raise MachineIdentityError(msg, context.request_id)
                connection.execute(
                    """
                    UPDATE router.service_access_tokens
                    SET revoked_at = %s
                    WHERE service_id = %s AND bootstrap_generation = %s
                      AND revoked_at IS NULL
                    """,
                    (now, parsed_service_id, generation),
                )
                _insert_audit(
                    connection,
                    context,
                    event_id=event_id,
                    service_id=parsed_service_id,
                    action="credential.revoke",
                    now=now,
                )

    def end_overlap(
        self,
        context: RequestContext,
        service_id: str,
        generation: int,
        *,
        now: datetime,
    ) -> None:
        """End an earlier generation overlap and invalidate its tokens."""
        self.revoke_generation(context, service_id, generation, now=now)

    def exchange(  # noqa: PLR0913
        self,
        *,
        request_id: str,
        service_id: str,
        bootstrap_secret: str,
        audience: Audience,
        operations: frozenset[str],
        now: datetime,
        workspace_ids: frozenset[str] | None = None,
        tls_identity: TLSClientIdentity | None = None,
    ) -> TokenExchange:
        """Verify one bootstrap secret and issue one opaque five-minute token."""
        _require_aware_now(now)
        parsed_service_id = _parse_uuid(service_id, request_id, invalid_token=True)
        parsed_workspaces = _parse_workspace_ids(workspace_ids, request_id)
        with psycopg.connect(self._database_url) as connection:
            _lock_service_credentials_shared(connection, parsed_service_id)
            generations = connection.execute(
                """
                SELECT generation.generation, generation.argon2id_verifier,
                       generation.issuer, generation.allowed_audiences,
                       generation.allowed_operations,
                       generation.workspace_limit,
                       generation.valid_until, generation.created_at
                FROM router.service_bootstrap_generations AS generation
                JOIN router.services AS service ON service.id = generation.service_id
                WHERE generation.service_id = %s
                  AND service.state = 'active'
                  AND generation.revoked_at IS NULL
                  AND generation.created_at <= %s
                  AND (generation.valid_until IS NULL OR generation.valid_until > %s)
                ORDER BY generation.generation DESC
                FOR SHARE OF generation, service
                """,
                (parsed_service_id, now, now),
            ).fetchall()
            generation = _matching_generation(generations, bootstrap_secret, request_id)
            generation_number = int(generation[0])
            if generation[2] != self._issuer:
                msg = "invalid_token"
                raise MachineIdentityError(msg, request_id)
            _check_tls(
                connection,
                service_id=parsed_service_id,
                generation=generation_number,
                identity=tls_identity,
                now=now,
                request_id=request_id,
            )
            _check_requested_scope(
                audience=audience,
                operations=operations,
                workspace_ids=parsed_workspaces,
                generation=generation,
                request_id=request_id,
            )
            _check_workspace_ownership(
                connection,
                service_id=parsed_service_id,
                workspace_ids=parsed_workspaces,
                request_id=request_id,
            )
            valid_until = generation[6]
            expires_at = now + SERVICE_TOKEN_LIFETIME
            if valid_until is not None and expires_at > valid_until:
                msg = "invalid_token"
                raise MachineIdentityError(msg, request_id)
            token = _new_secret()
            token_id = uuid.uuid4()
            digest_key_id = self._current_digest_key_id
            digest = _digest(self._digest_keys[digest_key_id], token.value)
            connection.execute(
                """
                INSERT INTO router.service_access_tokens (
                    token_id, token_digest, service_id, bootstrap_generation,
                    audience, operations, workspace_ids, issued_at, expires_at,
                    issuer, digest_key_id, workspace_restricted
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    token_id,
                    digest,
                    parsed_service_id,
                    generation_number,
                    audience.value,
                    sorted(operations),
                    list(parsed_workspaces or ()),
                    now,
                    expires_at,
                    self._issuer,
                    digest_key_id,
                    parsed_workspaces is not None,
                ),
            )
        return TokenExchange(
            access_token=token,
            service_id=str(parsed_service_id),
            audience=audience,
            operations=operations,
            credential_generation=generation_number,
            workspace_ids=(
                None
                if parsed_workspaces is None
                else frozenset(str(item) for item in parsed_workspaces)
            ),
        )

    def authenticate(
        self,
        token: str,
        *,
        request_id: str,
        now: datetime,
        tls_identity: TLSClientIdentity | None = None,
    ) -> ServicePrincipal:
        """Resolve an opaque token by keyed digest and validate all stored claims."""
        _require_aware_now(now)
        try:
            SecretValue(token)
        except ValueError as error:
            msg = "invalid_token"
            raise MachineIdentityError(msg, request_id) from error
        rows: list[tuple[Any, ...]] = []
        with psycopg.connect(self._database_url) as connection:
            for key_id, key in self._digest_keys.items():
                digest = _digest(key, token)
                candidate = connection.execute(
                    """
                    SELECT service_id FROM router.service_access_tokens
                    WHERE digest_key_id = %s AND token_digest = %s
                    """,
                    (key_id, digest),
                ).fetchone()
                if candidate is None:
                    continue
                candidate_service_id = candidate[0]
                _lock_service_credentials_shared(connection, candidate_service_id)
                row = connection.execute(
                    """
                    SELECT token.token_id::text, token.service_id::text,
                           token.bootstrap_generation, token.audience,
                           token.operations, token.workspace_ids,
                           token.issued_at, token.expires_at, token.revoked_at,
                           token.issuer, generation.valid_until,
                           generation.revoked_at, service.state,
                           token.workspace_restricted, generation.issuer
                    FROM router.service_access_tokens AS token
                    JOIN router.service_bootstrap_generations AS generation
                      ON generation.service_id = token.service_id
                     AND generation.generation = token.bootstrap_generation
                    JOIN router.services AS service ON service.id = token.service_id
                    WHERE token.digest_key_id = %s AND token.token_digest = %s
                    FOR SHARE OF token, generation, service
                    """,
                    (key_id, digest),
                ).fetchone()
                if row is not None:
                    rows.append(row)
            if len(rows) != 1:
                msg = "invalid_token"
                raise MachineIdentityError(msg, request_id)
            row = rows[0]
            service_id = uuid.UUID(row[1])
            generation = int(row[2])
            _check_tls(
                connection,
                service_id=service_id,
                generation=generation,
                identity=tls_identity,
                now=now,
                request_id=request_id,
            )
            if (
                row[8] is not None
                or row[11] is not None
                or row[12] != "active"
                or row[9] != self._issuer
                or row[14] != self._issuer
                or row[6] > now + TOKEN_CLOCK_SKEW
                or now > row[7] + TOKEN_CLOCK_SKEW
                or row[7] - row[6] != SERVICE_TOKEN_LIFETIME
                or (row[10] is not None and now >= row[10])
            ):
                msg = "invalid_token"
                raise MachineIdentityError(msg, request_id)
            return ServicePrincipal(
                issuer=row[9],
                token_id=row[0],
                audience=Audience(row[3]),
                service_id=row[1],
                operations=frozenset(row[4]),
                issued_at=row[6],
                expires_at=row[7],
                credential_generation=generation,
                allowed_workspace_ids=(
                    None if not row[13] else frozenset(str(item) for item in row[5])
                ),
            )

    def digest_key_custody_status(self, *, now: datetime) -> DigestKeyCustodyStatus:
        """Report active token rows that need an unavailable deployment key."""
        _require_aware_now(now)
        with psycopg.connect(self._database_url) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT token.digest_key_id
                FROM router.service_access_tokens AS token
                JOIN router.service_bootstrap_generations AS generation
                  ON generation.service_id = token.service_id
                 AND generation.generation = token.bootstrap_generation
                JOIN router.services AS service ON service.id = token.service_id
                WHERE token.revoked_at IS NULL
                  AND token.expires_at + interval '30 seconds' >= %s
                  AND generation.revoked_at IS NULL
                  AND (generation.valid_until IS NULL OR generation.valid_until > %s)
                  AND service.state = 'active'
                ORDER BY token.digest_key_id
                """,
                (now, now),
            ).fetchall()
        missing = tuple(row[0] for row in rows if row[0] not in self._digest_keys)
        return DigestKeyCustodyStatus(
            state=(
                DigestKeyCustodyState.DEGRADED
                if missing
                else DigestKeyCustodyState.NORMAL
            ),
            missing_key_ids=missing,
        )

    def set_tls_policy(
        self,
        context: RequestContext,
        service_id: str,
        *,
        required: bool,
        now: datetime,
    ) -> None:
        """Enable or disable the additional TLS control for one service."""
        _require_aware_now(now)
        _require_credential_administrator(context, now=now)
        parsed_service_id = _parse_uuid(service_id, context.request_id)
        event_id = uuid.uuid4()
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                service = connection.execute(
                    "SELECT state FROM router.services WHERE id = %s FOR UPDATE",
                    (parsed_service_id,),
                ).fetchone()
                if service is None or service[0] != "active":
                    msg = "not_found"
                    raise MachineIdentityError(msg, context.request_id)
                _reject_backdated_credential_change(
                    connection,
                    parsed_service_id,
                    now=now,
                    request_id=context.request_id,
                )
                current = connection.execute(
                    """
                    SELECT updated_at FROM router.service_machine_tls_policies
                    WHERE service_id = %s FOR UPDATE
                    """,
                    (parsed_service_id,),
                ).fetchone()
                if current is not None and current[0] > now:
                    msg = "not_found"
                    raise MachineIdentityError(msg, context.request_id)
                connection.execute(
                    """
                    INSERT INTO router.service_machine_tls_policies (
                        service_id, required, revision, updated_at
                    ) VALUES (%s, %s, 1, %s)
                    ON CONFLICT (service_id) DO UPDATE
                    SET required = EXCLUDED.required,
                        revision = router.service_machine_tls_policies.revision + 1,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (parsed_service_id, required, now),
                )
                _insert_audit(
                    connection,
                    context,
                    event_id=event_id,
                    service_id=parsed_service_id,
                    action="credential.tls_policy",
                    now=now,
                )

    def register_tls_identity(
        self,
        context: RequestContext,
        identity: TLSClientIdentity,
        *,
        now: datetime,
    ) -> None:
        """Register one validated short-lived certificate identity."""
        _require_aware_now(now)
        _require_credential_administrator(context, now=now)
        service_id = _parse_uuid(identity.service_id, context.request_id)
        if identity.revoked or identity.issued_at > now or identity.expires_at <= now:
            msg = "not_found"
            raise MachineIdentityError(msg, context.request_id)
        event_id = uuid.uuid4()
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"machine-tls:{identity.certificate_identity}",),
                )
                generation = connection.execute(
                    """
                    SELECT service.state, generation.valid_until,
                           generation.revoked_at, generation.created_at
                    FROM router.services AS service
                    JOIN router.service_bootstrap_generations AS generation
                      ON generation.service_id = service.id
                    WHERE service.id = %s AND generation.generation = %s
                    FOR UPDATE OF service, generation
                    """,
                    (service_id, identity.credential_generation),
                ).fetchone()
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM router.service_machine_tls_identities
                    WHERE certificate_identity = %s
                    """,
                    (identity.certificate_identity,),
                ).fetchone()
                if (
                    generation is None
                    or generation[0] != "active"
                    or generation[2] is not None
                    or generation[3] > now
                    or (
                        generation[1] is not None
                        and identity.expires_at > generation[1]
                    )
                    or duplicate is not None
                ):
                    msg = "not_found"
                    raise MachineIdentityError(msg, context.request_id)
                connection.execute(
                    """
                    INSERT INTO router.service_machine_tls_identities (
                        certificate_identity, service_id, bootstrap_generation,
                        issued_at, expires_at, revoked_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        identity.certificate_identity,
                        service_id,
                        identity.credential_generation,
                        identity.issued_at,
                        identity.expires_at,
                        None,
                    ),
                )
                _insert_audit(
                    connection,
                    context,
                    event_id=event_id,
                    service_id=service_id,
                    action="credential.tls_identity",
                    now=now,
                )

    def revoke_tls_identity(
        self,
        context: RequestContext,
        certificate_identity: str,
        *,
        now: datetime,
    ) -> None:
        """Revoke one registered machine certificate immediately."""
        _require_aware_now(now)
        _require_credential_administrator(context, now=now)
        if not certificate_identity:
            msg = "not_found"
            raise MachineIdentityError(msg, context.request_id)
        event_id = uuid.uuid4()
        with psycopg.connect(self._database_url) as connection:  # noqa: SIM117
            with connection.transaction():
                changed = connection.execute(
                    """
                    UPDATE router.service_machine_tls_identities
                    SET revoked_at = %s
                    WHERE certificate_identity = %s AND revoked_at IS NULL
                      AND issued_at <= %s
                    RETURNING service_id
                    """,
                    (now, certificate_identity, now),
                ).fetchone()
                if changed is None:
                    msg = "not_found"
                    raise MachineIdentityError(msg, context.request_id)
                _insert_audit(
                    connection,
                    context,
                    event_id=event_id,
                    service_id=changed[0],
                    action="credential.tls_revoke",
                    now=now,
                )


def _new_secret() -> SecretValue:
    return SecretValue(secrets.token_urlsafe(32))


def _digest(key: bytes, value: str) -> bytes:
    return hmac.digest(key, value.encode(), "sha256")


def _parse_uuid(
    value: str, request_id: str, *, invalid_token: bool = False
) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise MachineIdentityError(
            "invalid_token" if invalid_token else "not_found", request_id
        ) from error


def _require_aware_now(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "The current time must include a time zone."
        raise ValueError(msg)


def _parse_workspace_ids(
    values: frozenset[str] | None, request_id: str
) -> frozenset[uuid.UUID] | None:
    if values is None:
        return None
    if len(values) > MAXIMUM_WORKSPACE_IDS:
        msg = "insufficient_scope"
        raise MachineIdentityError(msg, request_id)
    return frozenset(
        _parse_uuid(value, request_id, invalid_token=True) for value in values
    )


def _require_credential_administrator(
    context: RequestContext, *, now: datetime
) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.scope.kind is ScopeKind.GLOBAL
        and context.operation == "credential.manage"
        and context.mutation
        and context.recent_authentication_at is not None
        and context.recent_authentication_at <= context.authorized_at
        and context.authorized_at <= now
        and now - context.recent_authentication_at <= _RECENT_ADMIN_AUTH_LIMIT
    ):
        msg = "insufficient_scope"
        raise MachineIdentityError(msg, context.request_id)


def _insert_generation(  # noqa: PLR0913
    connection: Connection[Any],
    *,
    generation_id: uuid.UUID,
    service_id: uuid.UUID,
    generation: int,
    issuer: str,
    verifier: str,
    scope: BootstrapScope,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO router.service_bootstrap_generations (
            id, service_id, generation, argon2id_verifier,
            allowed_operations, created_at, issuer, allowed_audiences,
            workspace_limit
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            generation_id,
            service_id,
            generation,
            verifier,
            sorted(scope.operations),
            now,
            issuer,
            sorted(audience.value for audience in scope.audiences),
            scope.workspace_limit.value,
        ),
    )


def _matching_generation(
    rows: list[tuple[Any, ...]], secret: str, request_id: str
) -> tuple[Any, ...]:
    try:
        SecretValue(secret)
    except ValueError as error:
        msg = "invalid_token"
        raise MachineIdentityError(msg, request_id) from error
    matches: list[tuple[Any, ...]] = []
    for row in rows:
        try:
            if _ARGON2.verify(row[1], secret):
                matches.append(row)
        except (InvalidHashError, VerificationError):
            continue
    if len(matches) != 1:
        msg = "invalid_token"
        raise MachineIdentityError(msg, request_id)
    return matches[0]


def _check_requested_scope(
    *,
    audience: Audience,
    operations: frozenset[str],
    workspace_ids: frozenset[uuid.UUID] | None,
    generation: tuple[Any, ...],
    request_id: str,
) -> None:
    if (
        not operations
        or audience.value not in generation[3]
        or not operations <= frozenset(generation[4])
        or not operations <= MACHINE_OPERATIONS_BY_AUDIENCE[audience]
    ):
        msg = "insufficient_scope"
        raise MachineIdentityError(msg, request_id)
    limit = WorkspaceLimit(generation[5])
    if limit is WorkspaceLimit.EXPLICIT_ONLY and (
        workspace_ids is None or "workspace.create" in operations
    ):
        msg = "insufficient_scope"
        raise MachineIdentityError(msg, request_id)


def _check_workspace_ownership(
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    workspace_ids: frozenset[uuid.UUID] | None,
    request_id: str,
) -> None:
    if not workspace_ids:
        return
    row = connection.execute(
        """
        SELECT count(*)
        FROM router.workspaces
        WHERE service_id = %s AND id = ANY(%s)
        """,
        (service_id, list(workspace_ids)),
    ).fetchone()
    if row is None or int(row[0]) != len(workspace_ids):
        msg = "insufficient_scope"
        raise MachineIdentityError(msg, request_id)


def _lock_service_credentials(
    connection: Connection[Any], service_id: uuid.UUID
) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"machine-credential:{service_id}",),
    )


def _lock_service_credentials_shared(
    connection: Connection[Any], service_id: uuid.UUID
) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
        (f"machine-credential:{service_id}",),
    )


def _reject_backdated_credential_change(
    connection: Connection[Any],
    service_id: uuid.UUID,
    *,
    now: datetime,
    request_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM router.service_bootstrap_generations
            WHERE service_id = %s AND created_at > %s
        ) OR EXISTS (
            SELECT 1 FROM router.service_access_tokens
            WHERE service_id = %s AND issued_at > %s
        ) OR EXISTS (
            SELECT 1 FROM router.service_machine_tls_identities
            WHERE service_id = %s AND issued_at > %s
        )
        """,
        (service_id, now, service_id, now, service_id, now),
    ).fetchone()
    if row is not None and row[0]:
        msg = "not_found"
        raise MachineIdentityError(msg, request_id)


def _check_tls(  # noqa: PLR0913
    connection: Connection[Any],
    *,
    service_id: uuid.UUID,
    generation: int,
    identity: TLSClientIdentity | None,
    now: datetime,
    request_id: str,
) -> None:
    policy = connection.execute(
        """
        SELECT required FROM router.service_machine_tls_policies
        WHERE service_id = %s
        FOR SHARE
        """,
        (service_id,),
    ).fetchone()
    required = policy is not None and bool(policy[0])
    if identity is None:
        if required:
            msg = "invalid_token"
            raise MachineIdentityError(msg, request_id)
        return
    try:
        identity_service_id = uuid.UUID(identity.service_id)
    except ValueError as error:
        msg = "invalid_token"
        raise MachineIdentityError(msg, request_id) from error
    if (
        identity_service_id != service_id
        or identity.credential_generation != generation
        or identity.revoked
        or identity.issued_at > now
        or now >= identity.expires_at
    ):
        msg = "invalid_token"
        raise MachineIdentityError(msg, request_id)
    registered = connection.execute(
        """
        SELECT 1 FROM router.service_machine_tls_identities
        WHERE certificate_identity = %s AND service_id = %s
          AND bootstrap_generation = %s AND revoked_at IS NULL
          AND issued_at <= %s AND expires_at > %s
        FOR SHARE
        """,
        (
            identity.certificate_identity,
            service_id,
            generation,
            now,
            now,
        ),
    ).fetchone()
    if registered is None:
        msg = "invalid_token"
        raise MachineIdentityError(msg, request_id)


def _insert_audit(  # noqa: PLR0913
    connection: Connection[Any],
    context: RequestContext,
    *,
    event_id: uuid.UUID,
    service_id: uuid.UUID,
    action: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            service_id, action, permission_result, occurred_at
        ) VALUES (
            %s, 'global_administration', %s, %s, %s,
            %s, %s, 'permitted', %s
        )
        """,
        (
            event_id,
            context.actor_kind.value,
            context.actor_id,
            context.authority_class.value,
            service_id,
            action,
            now,
        ),
    )
