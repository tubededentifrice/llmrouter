"""Idempotent PostgreSQL ingest for immutable canonical events."""
# ruff: noqa: EM101, TRY003

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from psycopg.pq import TransactionStatus

if TYPE_CHECKING:
    from psycopg import Connection

    from .model import CanonicalEvent


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    """Evidence that central ingest has recoverable replay protection."""

    event_id: str
    durable_replay_position: str
    replay_protected: bool
    replayed: bool


class ReplayProtector(Protocol):
    """Write an event to a standby or independent durable replay journal."""

    def protect(self, event: CanonicalEvent, payload_sha256: bytes) -> str:
        """Idempotently protect this exact identity and return its position."""
        ...


class ResponsibilityReceiver(Protocol):
    """Accept durable event responsibility during an approved repair."""

    def accept(self, event: CanonicalEvent, payload_sha256: bytes) -> str:
        """Return a durable, idempotent handoff receipt."""
        ...


class CanonicalLedgerConflictError(RuntimeError):
    """A central immutable event identity or sequence conflicts."""


class CanonicalLedgerTransactionError(RuntimeError):
    """The caller supplied a connection with an active transaction."""


class CanonicalLedger:
    """Insert one canonical envelope after independent replay protection."""

    def __init__(self, connection: Connection[Any], protector: ReplayProtector) -> None:
        """Use one PostgreSQL authority and one durability protector."""
        self._connection = connection
        self._protector = protector

    def ingest(self, event: CanonicalEvent) -> IngestReceipt:
        """Accept an equal replay and reject changed identity or sequence data."""
        if self._connection.info.transaction_status is not TransactionStatus.IDLE:
            raise CanonicalLedgerTransactionError(
                "Canonical ingest needs an idle database connection."
            )
        digest = hashlib.sha256(event.payload).digest()
        receipt: IngestReceipt
        lock_names = sorted(
            (event.event_id, f"{event.source_node_id}:{event.source_sequence}")
        )
        with self._connection.transaction() as transaction:
            for lock_name in lock_names:
                self._connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (lock_name,),
                )
            existing_rows = self._connection.execute(
                """
            SELECT event_id::text, source_node_id::text, source_sequence, event_class,
                   payload_sha256, durable_replay_position, occurred_at
            FROM router.canonical_events
            WHERE event_id = %s OR (
                source_node_id = %s AND source_sequence = %s
            )
            FOR UPDATE
            """,
                (event.event_id, event.source_node_id, event.source_sequence),
            ).fetchall()
            if existing_rows:
                if len(existing_rows) != 1:
                    raise CanonicalLedgerConflictError(
                        "The canonical identity and source sequence are split."
                    )
                existing = existing_rows[0]
                expected = (
                    event.event_id,
                    event.source_node_id,
                    event.source_sequence,
                    event.event_class.value,
                    digest,
                    event.occurred_at,
                )
                actual = (
                    existing[0],
                    existing[1],
                    existing[2],
                    existing[3],
                    existing[4],
                    existing[6],
                )
                if actual != expected:
                    raise CanonicalLedgerConflictError(
                        "The canonical event identity or source sequence conflicts."
                    )
                receipt = IngestReceipt(
                    event_id=event.event_id,
                    durable_replay_position=str(existing[5]),
                    replay_protected=True,
                    replayed=True,
                )
            else:
                replay_position = self._protector.protect(event, digest)
                if not replay_position:
                    raise RuntimeError("Canonical ingest is not replay protected.")
                self._connection.execute(
                    """
                    INSERT INTO router.canonical_events (
                        event_id, source_node_id, source_sequence, event_class,
                        payload_sha256, durable_replay_position, occurred_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event.event_id,
                        event.source_node_id,
                        event.source_sequence,
                        event.event_class.value,
                        digest,
                        replay_position,
                        event.occurred_at,
                    ),
                )
                receipt = IngestReceipt(
                    event_id=event.event_id,
                    durable_replay_position=replay_position,
                    replay_protected=True,
                    replayed=False,
                )
        if transaction.status.name != "COMMITTED":
            raise CanonicalLedgerTransactionError(
                "Canonical ingest did not commit its transaction."
            )
        if self._connection.info.transaction_status is not TransactionStatus.IDLE:
            raise CanonicalLedgerTransactionError(
                "Canonical ingest did not commit its transaction."
            )
        return receipt
