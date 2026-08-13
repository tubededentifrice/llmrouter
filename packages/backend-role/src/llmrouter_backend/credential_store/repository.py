"""PostgreSQL encrypted credential custody and route-scoped delivery."""
# ruff: noqa: PLR0913

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import timedelta
from threading import RLock
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from llmrouter_backend.authority import (
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    ScopeKind,
)
from llmrouter_backend.credential_store.cache import BoundedCredentialCache
from llmrouter_backend.credential_store.crypto import (
    EncryptedEnvelope,
    EnvelopeCipher,
    EnvelopeDecryptionError,
)
from llmrouter_backend.credential_store.errors import (
    CredentialStoreError,
    CredentialStoreErrorCode,
)
from llmrouter_backend.credential_store.model import (
    MAXIMUM_REASON_CHARACTERS,
    MAXIMUM_SAFE_LABEL_CHARACTERS,
    CredentialAction,
    CredentialMetadata,
    CredentialOwner,
    CredentialResult,
    CredentialState,
    SecretInput,
    SecretLease,
    UrgentInvalidation,
    WrappingKeyCustodyState,
    WrappingKeyCustodyStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from psycopg import Connection

_RECENT_AUTHENTICATION_LIMIT = timedelta(minutes=5)
_MINIMUM_KEY_BYTES = 32
_MINIMUM_IDEMPOTENCY_LENGTH = 16
_MAXIMUM_IDEMPOTENCY_LENGTH = 200
_MAXIMUM_OPAQUE_ID_LENGTH = 200
_MAXIMUM_LIST_ITEMS = 1_000
_INVALIDATION_BATCH_SIZE = 1_000
_FINGERPRINT_HEX_CHARACTERS = 16


class EncryptedCredentialRepository:
    """Store write-only values and expose only bounded safe metadata."""

    def __init__(
        self,
        database_url: str,
        *,
        wrapping_keys: Mapping[str, bytes],
        current_wrapping_key_id: str,
        idempotency_digest_key: bytes,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        invalidation_sink: Callable[[UrgentInvalidation], None] | None = None,
    ) -> None:
        """Use separate deployment-held wrapping and idempotency key material."""
        if not database_url:
            msg = "The database URL must not be empty."
            raise ValueError(msg)
        if len(idempotency_digest_key) < _MINIMUM_KEY_BYTES:
            msg = "The idempotency digest key must contain at least 256 bits."
            raise ValueError(msg)
        if any(
            hmac.compare_digest(idempotency_digest_key, wrapping_key)
            for wrapping_key in wrapping_keys.values()
        ):
            msg = "The wrapping and idempotency digest keys must be separate."
            raise ValueError(msg)
        self._database_url = database_url
        self._cipher = EnvelopeCipher(
            wrapping_keys,
            current_key_id=current_wrapping_key_id,
            random_bytes=random_bytes,
        )
        self._idempotency_digest_key = hashlib.blake2b(
            idempotency_digest_key,
            digest_size=32,
            person=b"llmr-idem-v1",
        ).digest()
        self._identity_factory = identity_factory
        self._invalidation_sink = invalidation_sink

    def create(
        self,
        context: RequestContext,
        *,
        idempotency_key: str,
        owner: CredentialOwner,
        provider_catalog_id: str,
        secret: SecretInput,
        now: datetime,
        safe_label: str | None = None,
    ) -> CredentialResult:
        """Create one encrypted value or return its identical durable replay."""
        _require_global_credential_change(context, now=now)
        _require_idempotency_key(idempotency_key)
        owner_service_id = _parse_owner(owner, context.request_id)
        _require_opaque_id(provider_catalog_id, "provider catalog identity")
        _require_optional_label(safe_label)
        fingerprint = self._request_fingerprint(
            owner=owner,
            provider_catalog_id=provider_catalog_id,
            safe_label=safe_label,
            secret=secret,
        )
        with self._connect() as connection, connection.transaction():
            _lock(connection, f"credential-create:{context.actor_id}:{idempotency_key}")
            replay = connection.execute(
                """
                SELECT request_fingerprint, credential_id
                FROM router.credential_idempotency_bindings
                WHERE actor_id = %s AND idempotency_key = %s
                """,
                (context.actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(
                    bytes(replay["request_fingerprint"]), fingerprint
                ):
                    raise CredentialStoreError(
                        CredentialStoreErrorCode.IDEMPOTENCY_CONFLICT,
                        context.request_id,
                    )
                row = _credential_row(connection, replay["credential_id"])
                if row is None:
                    msg = "A credential idempotency binding has no result."
                    raise RuntimeError(msg)
                return CredentialResult(_metadata(row), replayed=True)
            if owner_service_id is not None and not _active_service_exists(
                connection, owner_service_id
            ):
                raise CredentialStoreError(
                    CredentialStoreErrorCode.NOT_FOUND, context.request_id
                )
            credential_id = self._identity_factory()
            revision = self._identity_factory()
            envelope = self._cipher.encrypt(
                secret.value.encode(),
                context=_encryption_context(
                    credential_id,
                    owner_service_id=owner_service_id,
                    provider_catalog_id=provider_catalog_id,
                ),
            )
            safe_fingerprint = self._safe_fingerprint(secret)
            connection.execute(
                """
                INSERT INTO router.encrypted_credentials (
                    id, owner_kind, owner_service_id, credential_kind,
                    ciphertext, encrypted_data_key, wrapping_key_id,
                    safe_fingerprint, generation, state, created_at,
                    current_revision, safe_label, last_changed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 1, 'active',
                    %s, %s, %s, %s
                )
                """,
                (
                    credential_id,
                    "global" if owner_service_id is None else "service",
                    owner_service_id,
                    provider_catalog_id,
                    envelope.ciphertext,
                    envelope.encrypted_data_key,
                    envelope.wrapping_key_id,
                    safe_fingerprint,
                    now,
                    revision,
                    safe_label,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO router.credential_idempotency_bindings (
                    actor_id, idempotency_key, request_fingerprint,
                    credential_id, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (context.actor_id, idempotency_key, fingerprint, credential_id, now),
            )
            _insert_audit(
                connection,
                context,
                event_id=self._identity_factory(),
                service_id=owner_service_id,
                action="credential.create",
                now=now,
                credential_id=credential_id,
            )
            row = _credential_row(connection, credential_id)
            if row is None:
                msg = "The created credential has no metadata."
                raise RuntimeError(msg)
        return CredentialResult(_metadata(row), replayed=False)

    def list_metadata(
        self,
        context: RequestContext,
        *,
        owner: CredentialOwner | None = None,
    ) -> tuple[CredentialMetadata, ...]:
        """List safe metadata through global credential authority."""
        _require_global_credential_read(context)
        owner_service_id = (
            None if owner is None else _parse_owner(owner, context.request_id)
        )
        with self._connect() as connection:
            if owner is None:
                rows = connection.execute(
                    _METADATA_SELECT + " ORDER BY created_at, id LIMIT %s",
                    (_MAXIMUM_LIST_ITEMS,),
                ).fetchall()
            elif owner_service_id is None:
                rows = connection.execute(
                    _METADATA_SELECT
                    + " WHERE owner_kind = 'global' ORDER BY created_at, id LIMIT %s",
                    (_MAXIMUM_LIST_ITEMS,),
                ).fetchall()
            else:
                rows = connection.execute(
                    _METADATA_SELECT
                    + " WHERE owner_kind = 'service' AND owner_service_id = %s"
                    " ORDER BY created_at, id LIMIT %s",
                    (owner_service_id, _MAXIMUM_LIST_ITEMS),
                ).fetchall()
        return tuple(_metadata(row) for row in rows)

    def change(
        self,
        context: RequestContext,
        credential_id: str,
        action: CredentialAction,
        *,
        expected_revision: str,
        reason: str,
        now: datetime,
        replacement_secret: SecretInput | None = None,
    ) -> CredentialMetadata:
        """Rotate, disable, or retire one value with exact revision control."""
        _require_global_credential_change(context, now=now)
        parsed_id = _parse_uuid(credential_id, context.request_id)
        parsed_revision = _parse_uuid(expected_revision, context.request_id)
        _require_reason(reason)
        if (action is CredentialAction.ROTATE) != (replacement_secret is not None):
            raise CredentialStoreError(
                CredentialStoreErrorCode.INVALID_REQUEST, context.request_id
            )
        invalidation: UrgentInvalidation | None = None
        with self._connect() as connection, connection.transaction():
            _lock(connection, f"encrypted-credential:{parsed_id}")
            row = connection.execute(
                _METADATA_SELECT + " WHERE id = %s FOR UPDATE",
                (parsed_id,),
            ).fetchone()
            if row is None:
                raise CredentialStoreError(
                    CredentialStoreErrorCode.NOT_FOUND, context.request_id
                )
            if CredentialState(row["state"]) is CredentialState.RETIRED:
                raise CredentialStoreError(
                    CredentialStoreErrorCode.TERMINAL_STATE, context.request_id
                )
            if row["current_revision"] != parsed_revision:
                raise CredentialStoreError(
                    CredentialStoreErrorCode.STATE_REVISION_CONFLICT,
                    context.request_id,
                    current_revision=str(row["current_revision"]),
                )
            current_state = CredentialState(row["state"])
            changed = (
                action is CredentialAction.ROTATE
                or (
                    action is CredentialAction.DISABLE
                    and current_state is CredentialState.ACTIVE
                )
                or action is CredentialAction.RETIRE
            )
            if not changed:
                return _metadata(row)
            next_generation = int(row["generation"]) + 1
            next_revision = self._identity_factory()
            next_state = {
                CredentialAction.ROTATE: current_state,
                CredentialAction.DISABLE: CredentialState.DISABLED,
                CredentialAction.RETIRE: CredentialState.RETIRED,
            }[action]
            if replacement_secret is not None:
                envelope = self._cipher.encrypt(
                    replacement_secret.value.encode(),
                    context=_encryption_context(
                        parsed_id,
                        owner_service_id=row["owner_service_id"],
                        provider_catalog_id=row["credential_kind"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE router.encrypted_credentials
                    SET ciphertext = %s, encrypted_data_key = %s,
                        wrapping_key_id = %s, safe_fingerprint = %s,
                        generation = %s, current_revision = %s,
                        last_changed_at = %s
                    WHERE id = %s
                    """,
                    (
                        envelope.ciphertext,
                        envelope.encrypted_data_key,
                        envelope.wrapping_key_id,
                        self._safe_fingerprint(replacement_secret),
                        next_generation,
                        next_revision,
                        now,
                        parsed_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE router.encrypted_credentials
                    SET state = %s, generation = %s, current_revision = %s,
                        last_changed_at = %s,
                        retired_at = CASE WHEN %s = 'retired' THEN %s ELSE NULL END
                    WHERE id = %s
                    """,
                    (
                        next_state.value,
                        next_generation,
                        next_revision,
                        now,
                        next_state.value,
                        now,
                        parsed_id,
                    ),
                )
            invalidation = _insert_invalidation(
                connection,
                identity_factory=self._identity_factory,
                credential_id=parsed_id,
                generation=next_generation,
                action=action,
                now=now,
            )
            _insert_audit(
                connection,
                context,
                event_id=self._identity_factory(),
                service_id=row["owner_service_id"],
                action=f"credential.{action.value}",
                now=now,
                credential_id=parsed_id,
                reason=reason,
            )
            changed_row = _credential_row(connection, parsed_id)
            if changed_row is None:
                msg = "The changed credential has no metadata."
                raise RuntimeError(msg)
        if invalidation is not None and self._invalidation_sink is not None:
            self._invalidation_sink(invalidation)
        return _metadata(changed_row)

    def replace(
        self,
        context: RequestContext,
        credential_id: str,
        *,
        expected_revision: str,
        reason: str,
        replacement_secret: SecretInput,
        now: datetime,
    ) -> CredentialMetadata:
        """Replace secret material through the public rotate transition."""
        return self.change(
            context,
            credential_id,
            CredentialAction.ROTATE,
            expected_revision=expected_revision,
            reason=reason,
            replacement_secret=replacement_secret,
            now=now,
        )

    def disable(
        self,
        context: RequestContext,
        credential_id: str,
        *,
        expected_revision: str,
        reason: str,
        now: datetime,
    ) -> CredentialMetadata:
        """Disable one credential and publish an urgent invalidation."""
        return self.change(
            context,
            credential_id,
            CredentialAction.DISABLE,
            expected_revision=expected_revision,
            reason=reason,
            now=now,
        )

    def retire(
        self,
        context: RequestContext,
        credential_id: str,
        *,
        expected_revision: str,
        reason: str,
        now: datetime,
    ) -> CredentialMetadata:
        """Retire one credential and publish an urgent invalidation."""
        return self.change(
            context,
            credential_id,
            CredentialAction.RETIRE,
            expected_revision=expected_revision,
            reason=reason,
            now=now,
        )

    def reference_is_eligible(
        self,
        context: RequestContext,
        credential_id: str,
        *,
        service_id: str,
        provider_catalog_id: str,
    ) -> bool:
        """Let one service select an eligible reference without secret access."""
        parsed_credential = _parse_uuid(credential_id, context.request_id)
        parsed_service = _parse_uuid(service_id, context.request_id)
        _require_opaque_id(provider_catalog_id, "provider catalog identity")
        if not (
            context.actor_kind is PrincipalKind.ADMINISTRATOR
            and context.operation == "provider_instance.manage"
            and context.scope.kind is ScopeKind.SERVICE
            and context.scope.service_id == str(parsed_service)
            and context.authority_class
            in {AuthorityClass.GLOBAL_ADMINISTRATOR, AuthorityClass.SERVICE}
            and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        ):
            raise CredentialStoreError(
                CredentialStoreErrorCode.INSUFFICIENT_SCOPE, context.request_id
            )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM router.encrypted_credentials
                WHERE id = %s AND state = 'active'
                  AND credential_kind = %s
                  AND (owner_kind = 'global' OR owner_service_id = %s)
                """,
                (parsed_credential, provider_catalog_id, parsed_service),
            ).fetchone()
        return row is not None

    def custody_status(self) -> WrappingKeyCustodyStatus:
        """Report unavailable wrapping-key identities without secret detail."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT wrapping_key_id
                FROM router.encrypted_credentials
                WHERE state <> 'retired'
                ORDER BY wrapping_key_id
                """
            ).fetchall()
        missing = tuple(
            row["wrapping_key_id"]
            for row in rows
            if row["wrapping_key_id"] not in self._cipher.available_key_ids
        )
        return WrappingKeyCustodyStatus(
            (
                WrappingKeyCustodyState.DEGRADED
                if missing
                else WrappingKeyCustodyState.NORMAL
            ),
            missing,
        )

    def rotate_wrapping_key(
        self,
        context: RequestContext,
        *,
        now: datetime,
    ) -> int:
        """Rewrap all available data keys under the staged current key."""
        _require_global_credential_change(context, now=now)
        with self._connect() as connection, connection.transaction():
            _lock(connection, "credential-wrapping-key-rotation")
            rows = connection.execute(
                """
                SELECT id, owner_service_id, credential_kind,
                       encrypted_data_key, wrapping_key_id
                FROM router.encrypted_credentials
                WHERE state <> 'retired' AND wrapping_key_id <> %s
                ORDER BY id
                FOR UPDATE
                """,
                (self._cipher.current_key_id,),
            ).fetchall()
            try:
                replacements = [
                    (
                        self._cipher.rewrap(
                            bytes(row["encrypted_data_key"]),
                            old_key_id=row["wrapping_key_id"],
                            context=_encryption_context(
                                row["id"],
                                owner_service_id=row["owner_service_id"],
                                provider_catalog_id=row["credential_kind"],
                            ),
                        ),
                        row["id"],
                    )
                    for row in rows
                ]
            except EnvelopeDecryptionError as error:
                raise CredentialStoreError(
                    CredentialStoreErrorCode.CREDENTIAL_UNAVAILABLE,
                    context.request_id,
                ) from error
            for encrypted_data_key, credential_id in replacements:
                connection.execute(
                    """
                    UPDATE router.encrypted_credentials
                    SET encrypted_data_key = %s, wrapping_key_id = %s
                    WHERE id = %s
                    """,
                    (
                        encrypted_data_key,
                        self._cipher.current_key_id,
                        credential_id,
                    ),
                )
            _insert_audit(
                connection,
                context,
                event_id=self._identity_factory(),
                service_id=None,
                action="credential.wrapping_key.rotate",
                now=now,
                safe_details={"rewrapped_count": len(replacements)},
            )
        return len(replacements)

    def _request_fingerprint(
        self,
        *,
        owner: CredentialOwner,
        provider_catalog_id: str,
        safe_label: str | None,
        secret: SecretInput,
    ) -> bytes:
        canonical = json.dumps(
            {
                "owner_scope": owner.public_scope,
                "provider_catalog_id": provider_catalog_id,
                "safe_label": safe_label,
                "secret": secret.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.digest(self._idempotency_digest_key, canonical, "sha256")

    def _safe_fingerprint(self, secret: SecretInput) -> str:
        """Return a short keyed marker that cannot validate offline guesses."""
        return hmac.digest(
            self._idempotency_digest_key,
            b"safe-fingerprint-v1\0" + secret.value.encode(),
            "sha256",
        ).hex()[:_FINGERPRINT_HEX_CHARACTERS]

    def _connect(self) -> Connection[dict[str, Any]]:
        return psycopg.connect(self._database_url, row_factory=dict_row)


class DataPlaneCredentialDistributor:
    """Deliver credentials only for routes that are active on one node."""

    def __init__(
        self,
        database_url: str,
        *,
        wrapping_keys: Mapping[str, bytes],
        current_wrapping_key_id: str,
        active_route_ids: frozenset[str],
        maximum_cache_entries: int = 256,
        cache_lifetime: timedelta = timedelta(minutes=5),
    ) -> None:
        """Use one bounded cache and one exact node-local route set."""
        if not database_url:
            msg = "The database URL must not be empty."
            raise ValueError(msg)
        self._database_url = database_url
        self._cipher = EnvelopeCipher(
            wrapping_keys,
            current_key_id=current_wrapping_key_id,
            random_bytes=secrets.token_bytes,
        )
        self._active_route_ids = _parse_route_set(active_route_ids)
        self._cache = BoundedCredentialCache(
            maximum_entries=maximum_cache_entries,
            lifetime=cache_lifetime,
        )
        self._invalidation_cursor = 0
        self._lock = RLock()

    @property
    def cache_entry_count(self) -> int:
        """Return the current safe cache entry count."""
        return self._cache.entry_count

    @property
    def invalidation_cursor(self) -> int:
        """Return the last applied urgent sequence."""
        with self._lock:
            return self._invalidation_cursor

    def secret_for_route(
        self,
        route_id: str,
        *,
        request_id: str,
        now: datetime,
    ) -> SecretLease:
        """Apply urgent changes, then deliver one active route credential."""
        _require_aware(now)
        parsed_route = _parse_uuid(route_id, request_id)

        def load() -> tuple[str, int, bytearray]:
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                row = connection.execute(
                    """
                    SELECT credential.id, credential.owner_service_id,
                           credential.credential_kind, credential.ciphertext,
                           credential.encrypted_data_key,
                           credential.wrapping_key_id, credential.generation
                    FROM router.provider_model_routes AS route
                    JOIN router.provider_instances AS instance
                      ON instance.id = route.provider_instance_id
                    JOIN router.encrypted_credentials AS credential
                      ON credential.id = instance.credential_id
                    LEFT JOIN router.services AS route_service
                      ON route_service.id = route.owner_service_id
                    LEFT JOIN router.services AS instance_service
                      ON instance_service.id = instance.owner_service_id
                    WHERE route.id = %s AND route.state = 'active'
                      AND instance.state = 'active'
                      AND credential.state = 'active'
                      AND credential.credential_kind = instance.adapter_type_id
                      AND (route.owner_kind = 'global'
                           OR route_service.state = 'active')
                      AND (instance.owner_kind = 'global'
                           OR instance_service.state = 'active')
                    FOR SHARE OF credential
                    """,
                    (parsed_route,),
                ).fetchone()
                if row is None:
                    raise CredentialStoreError(
                        CredentialStoreErrorCode.NOT_FOUND, request_id
                    )
                try:
                    value = self._cipher.decrypt(
                        EncryptedEnvelope(
                            bytes(row["ciphertext"]),
                            bytes(row["encrypted_data_key"]),
                            row["wrapping_key_id"],
                        ),
                        context=_encryption_context(
                            row["id"],
                            owner_service_id=row["owner_service_id"],
                            provider_catalog_id=row["credential_kind"],
                        ),
                    )
                except EnvelopeDecryptionError as error:
                    raise CredentialStoreError(
                        CredentialStoreErrorCode.CREDENTIAL_UNAVAILABLE,
                        request_id,
                    ) from error
            return str(row["id"]), int(row["generation"]), value

        with self._lock:
            if parsed_route not in self._active_route_ids:
                raise CredentialStoreError(
                    CredentialStoreErrorCode.INSUFFICIENT_SCOPE, request_id
                )
            self.apply_urgent_invalidations()
            return self._cache.acquire(str(parsed_route), now=now, loader=load)

    def apply_urgent_invalidations(self) -> tuple[UrgentInvalidation, ...]:
        """Apply every durable urgent event before later normal work."""
        with self._lock:
            applied: list[UrgentInvalidation] = []
            while True:
                with psycopg.connect(
                    self._database_url, row_factory=dict_row
                ) as connection:
                    rows = connection.execute(
                        """
                        SELECT sequence, credential_id, generation, action, occurred_at
                        FROM router.credential_urgent_invalidations
                        WHERE sequence > %s
                        ORDER BY sequence
                        LIMIT %s
                        """,
                        (self._invalidation_cursor, _INVALIDATION_BATCH_SIZE),
                    ).fetchall()
                for row in rows:
                    invalidation = UrgentInvalidation(
                        int(row["sequence"]),
                        str(row["credential_id"]),
                        int(row["generation"]),
                        CredentialAction(row["action"]),
                        row["occurred_at"],
                    )
                    self._apply_durable_invalidation(invalidation)
                    applied.append(invalidation)
                if len(rows) < _INVALIDATION_BATCH_SIZE:
                    return tuple(applied)

    def apply_urgent_invalidation(self, invalidation: UrgentInvalidation) -> None:
        """Apply one pushed urgent event and erase outstanding leases."""
        with self._lock:
            if invalidation.sequence <= self._invalidation_cursor:
                return
            if invalidation.sequence != self._invalidation_cursor + 1:
                self.apply_urgent_invalidations()
                if invalidation.sequence <= self._invalidation_cursor:
                    return
                msg = "The urgent credential sequence has a durable gap."
                raise RuntimeError(msg)
            self._apply_durable_invalidation(invalidation)

    def _apply_durable_invalidation(self, invalidation: UrgentInvalidation) -> None:
        """Apply one database event while allowing rolled-back identity gaps."""
        if invalidation.sequence <= self._invalidation_cursor:
            return
        self._cache.invalidate(invalidation.credential_id)
        self._invalidation_cursor = invalidation.sequence

    def replace_active_routes(self, route_ids: frozenset[str]) -> None:
        """Apply a new node-local route set and erase removed route values."""
        parsed = _parse_route_set(route_ids)
        with self._lock:
            self._active_route_ids = parsed
            self._cache.retain_routes(frozenset(str(item) for item in parsed))

    def close(self) -> None:
        """Erase all node-local cached material."""
        with self._lock:
            self._cache.close()


_METADATA_SELECT = """
SELECT id, owner_kind, owner_service_id, credential_kind, state,
       generation, current_revision, created_at, safe_fingerprint, safe_label,
       ciphertext, encrypted_data_key, wrapping_key_id
FROM router.encrypted_credentials
"""


def _credential_row(
    connection: Connection[dict[str, Any]], credential_id: uuid.UUID
) -> dict[str, Any] | None:
    return connection.execute(
        _METADATA_SELECT + " WHERE id = %s", (credential_id,)
    ).fetchone()


def _metadata(row: dict[str, Any]) -> CredentialMetadata:
    return CredentialMetadata(
        credential_id=str(row["id"]),
        owner_scope=(
            "global" if row["owner_kind"] == "global" else str(row["owner_service_id"])
        ),
        provider_catalog_id=row["credential_kind"],
        state=CredentialState(row["state"]),
        revision=str(row["current_revision"]),
        created_at=row["created_at"],
        fingerprint=row["safe_fingerprint"],
    )


def _encryption_context(
    credential_id: uuid.UUID,
    *,
    owner_service_id: uuid.UUID | None,
    provider_catalog_id: str,
) -> dict[str, str]:
    return {
        "credential_id": str(credential_id),
        "owner_scope": "global" if owner_service_id is None else str(owner_service_id),
        "provider_catalog_id": provider_catalog_id,
    }


def _insert_invalidation(
    connection: Connection[dict[str, Any]],
    *,
    identity_factory: Callable[[], uuid.UUID],
    credential_id: uuid.UUID,
    generation: int,
    action: CredentialAction,
    now: datetime,
) -> UrgentInvalidation:
    _lock(connection, "credential-urgent-invalidation-sequence")
    row = connection.execute(
        """
        INSERT INTO router.credential_urgent_invalidations (
            event_id, credential_id, generation, action, occurred_at
        ) VALUES (%s, %s, %s, %s, %s)
        RETURNING sequence
        """,
        (identity_factory(), credential_id, generation, action.value, now),
    ).fetchone()
    if row is None:
        msg = "The urgent invalidation has no sequence."
        raise RuntimeError(msg)
    return UrgentInvalidation(
        int(row["sequence"]), str(credential_id), generation, action, now
    )


def _insert_audit(
    connection: Connection[dict[str, Any]],
    context: RequestContext,
    *,
    event_id: uuid.UUID,
    service_id: uuid.UUID | None,
    action: str,
    now: datetime,
    credential_id: uuid.UUID | None = None,
    reason: str | None = None,
    safe_details: dict[str, object] | None = None,
) -> None:
    details = {} if safe_details is None else dict(safe_details)
    if credential_id is not None:
        details["credential_id"] = str(credential_id)
    if reason is not None:
        details["reason"] = reason
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            service_id, workspace_id, action, permission_result,
            safe_details, occurred_at
        ) VALUES (
            %s, 'global_administration', 'administrator', %s,
            'global_administrator', %s, NULL, %s, 'permitted', %s, %s
        )
        """,
        (event_id, context.actor_id, service_id, action, Jsonb(details), now),
    )


def _require_global_credential_read(context: RequestContext) -> None:
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.scope.kind is ScopeKind.GLOBAL
        and context.operation == "credential.manage"
        and not context.mutation
    ):
        raise CredentialStoreError(
            CredentialStoreErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )


def _require_global_credential_change(
    context: RequestContext, *, now: datetime
) -> None:
    _require_aware(now)
    recent = context.recent_authentication_at
    if not (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.scope.kind is ScopeKind.GLOBAL
        and context.operation == "credential.manage"
        and context.mutation
        and recent is not None
        and recent <= context.authorized_at <= now
        and now - recent <= _RECENT_AUTHENTICATION_LIMIT
    ):
        raise CredentialStoreError(
            CredentialStoreErrorCode.INSUFFICIENT_SCOPE, context.request_id
        )


def _parse_owner(owner: CredentialOwner, request_id: str) -> uuid.UUID | None:
    if owner.service_id is None:
        return None
    return _parse_uuid(owner.service_id, request_id)


def _parse_uuid(value: str, request_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise CredentialStoreError(
            CredentialStoreErrorCode.NOT_FOUND, request_id
        ) from error


def _parse_route_set(values: frozenset[str]) -> frozenset[uuid.UUID]:
    try:
        return frozenset(uuid.UUID(value) for value in values)
    except (AttributeError, TypeError, ValueError) as error:
        msg = "Each active route identity must be a UUID."
        raise ValueError(msg) from error


def _require_idempotency_key(value: str) -> None:
    if not _MINIMUM_IDEMPOTENCY_LENGTH <= len(value) <= _MAXIMUM_IDEMPOTENCY_LENGTH:
        msg = "The idempotency key must contain from 16 to 200 characters."
        raise ValueError(msg)


def _require_opaque_id(value: str, label: str) -> None:
    if not 1 <= len(value) <= _MAXIMUM_OPAQUE_ID_LENGTH:
        msg = f"The {label} must contain from 1 to 200 characters."
        raise ValueError(msg)


def _require_optional_label(value: str | None) -> None:
    if value is not None and len(value) > MAXIMUM_SAFE_LABEL_CHARACTERS:
        msg = "The safe label must contain no more than 200 characters."
        raise ValueError(msg)


def _require_reason(value: str) -> None:
    if not 1 <= len(value) <= MAXIMUM_REASON_CHARACTERS:
        msg = "The reason must contain from 1 to 500 characters."
        raise ValueError(msg)


def _active_service_exists(
    connection: Connection[dict[str, Any]], service_id: uuid.UUID
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM router.services WHERE id = %s AND state = 'active'",
            (service_id,),
        ).fetchone()
        is not None
    )


def _lock(connection: Connection[dict[str, Any]], name: str) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "The current time must include a time zone."
        raise ValueError(msg)
