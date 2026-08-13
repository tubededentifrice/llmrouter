"""PostgreSQL canonical ledger ingest tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psycopg
import pytest
from llmrouter_backend.database import migrate
from llmrouter_backend.spool import (
    CanonicalEvent,
    CanonicalLedger,
    CanonicalLedgerConflictError,
    CanonicalLedgerTransactionError,
    EventClass,
)

if TYPE_CHECKING:
    from types import TracebackType

NODE = "0198a080-0000-7000-8000-000000000191"
NOW = datetime(2026, 8, 13, tzinfo=UTC)
DIGEST_BYTES = 32


class _Protector:
    def __init__(self, position: str = "replay:1") -> None:
        self.position = position
        self.calls = 0

    def protect(self, event: CanonicalEvent, payload_sha256: bytes) -> str:
        self.calls += 1
        assert event.source_node_id == NODE
        assert len(payload_sha256) == DIGEST_BYTES
        return self.position


def _event(
    *,
    event_id: str = "0198a080-0000-7000-8000-000000000192",
    sequence: int = 1,
    payload: bytes = b"canonical-ledger-event",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        source_node_id=NODE,
        source_sequence=sequence,
        event_class=EventClass.AUDIT,
        payload=payload,
        occurred_at=NOW,
    )


def _migrated(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)


def _ingest(database_url: str, event: CanonicalEvent) -> bool:
    with psycopg.connect(database_url) as connection:
        return CanonicalLedger(connection, _Protector()).ingest(event).replayed


def test_ledger_returns_only_after_commit_and_duplicate_is_idempotent(
    database_url: str,
) -> None:
    """Commit before receipt and return the stored replay position on retry."""
    _migrated(database_url)
    protector = _Protector()
    with psycopg.connect(database_url) as connection:
        first = CanonicalLedger(connection, protector).ingest(_event())
        assert not first.replayed
        assert connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with psycopg.connect(database_url) as connection:
        second = CanonicalLedger(connection, _Protector("wrong")).ingest(_event())
        assert second.replayed
        assert second.durable_replay_position == "replay:1"
    assert protector.calls == 1


def test_ledger_does_not_return_receipt_when_commit_fails(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Propagate a lost commit receipt so the node keeps its local event."""
    _migrated(database_url)
    with psycopg.connect(database_url) as connection:
        original_transaction = connection.transaction

        def fail_transaction(
            savepoint_name: str | None = None, *, force_rollback: bool = False
        ) -> object:
            context = original_transaction(savepoint_name, force_rollback)

            class LostReceipt:
                def __enter__(self) -> object:
                    return context.__enter__()

                def __exit__(
                    self,
                    exc_type: type[BaseException] | None,
                    exc_value: BaseException | None,
                    traceback: TracebackType | None,
                ) -> bool:
                    context.__exit__(exc_type, exc_value, traceback)
                    msg = "Simulated lost commit receipt."
                    raise psycopg.OperationalError(msg)

            return LostReceipt()

        monkeypatch.setattr(connection, "transaction", fail_transaction)
        with pytest.raises(psycopg.OperationalError):
            CanonicalLedger(connection, _Protector()).ingest(_event())
    with psycopg.connect(database_url) as observer:
        count = observer.execute(
            "SELECT count(*) FROM router.canonical_events WHERE event_id = %s",
            (_event().event_id,),
        ).fetchone()
        assert count is not None
        assert count[0] == 1


def test_ledger_rejects_connection_with_caller_transaction(
    database_url: str,
) -> None:
    """Do not give a receipt while a caller can still roll back the ingest."""
    _migrated(database_url)
    with psycopg.connect(database_url) as connection:
        connection.execute("SELECT 1")
        with pytest.raises(CanonicalLedgerTransactionError):
            CanonicalLedger(connection, _Protector()).ingest(_event())


def test_concurrent_duplicate_delivery_commits_one_row(database_url: str) -> None:
    """Serialize two missing-row deliveries without a unique race."""
    _migrated(database_url)
    with ThreadPoolExecutor(max_workers=2) as executor:
        replayed = list(
            executor.map(lambda _: _ingest(database_url, _event()), range(2))
        )
    assert sorted(replayed) == [False, True]
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM router.canonical_events WHERE event_id = %s",
            (_event().event_id,),
        ).fetchone()
        assert count is not None
        assert count[0] == 1


def test_split_identity_and_sequence_conflicts_are_rejected(
    database_url: str,
) -> None:
    """Reject changed identity, changed sequence, and split central rows."""
    _migrated(database_url)
    with psycopg.connect(database_url) as connection:
        ledger = CanonicalLedger(connection, _Protector())
        ledger.ingest(_event())
        with pytest.raises(CanonicalLedgerConflictError):
            ledger.ingest(_event(payload=b"changed"))
        second = _event(
            event_id="0198a080-0000-7000-8000-000000000193",
            sequence=2,
        )
        ledger.ingest(second)
        with pytest.raises(CanonicalLedgerConflictError):
            ledger.ingest(_event(sequence=2))


def test_same_sequence_and_content_with_changed_event_id_is_rejected(
    database_url: str,
) -> None:
    """Require both immutable identity coordinates on an equal replay."""
    _migrated(database_url)
    with psycopg.connect(database_url) as connection:
        ledger = CanonicalLedger(connection, _Protector())
        ledger.ingest(_event())
        with pytest.raises(CanonicalLedgerConflictError):
            ledger.ingest(_event(event_id="0198a080-0000-7000-8000-000000000194"))


def test_protector_failure_rolls_back_central_insert(database_url: str) -> None:
    """Do not insert a row if independent replay protection does not complete."""
    _migrated(database_url)
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(RuntimeError, match="not replay protected"),
    ):
        CanonicalLedger(connection, _Protector("")).ingest(_event())
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM router.canonical_events WHERE event_id = %s",
            (_event().event_id,),
        ).fetchone()
        assert count is not None
        assert count[0] == 0
