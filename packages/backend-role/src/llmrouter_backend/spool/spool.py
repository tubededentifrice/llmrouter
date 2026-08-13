"""Durable reservations and pressure control for one local spool."""
# ruff: noqa: C901, EM101, PLR0912, PLR0913, PLR0915, TRY003

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .model import (
    AdmissionResult,
    CanonicalEvent,
    CanonicalLoadBounds,
    EventClass,
    PressureState,
    SpoolHealth,
    SpoolLimits,
    WorkClass,
)
from .storage import EncryptedFrameJournal, SpoolStorageError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .ledger import CanonicalLedger, ResponsibilityReceiver

_SHED_CLASSES = (
    WorkClass.BACKGROUND,
    WorkClass.BATCH,
    WorkClass.PLAYGROUND,
    WorkClass.AGENT,
)
_PROTECTED_CLASSES = (
    WorkClass.CANCELLATION,
    WorkClass.RECONCILIATION,
    WorkClass.SECURITY,
)
_NORMAL_WORK_CLASSES = (WorkClass.FOREGROUND, *_SHED_CLASSES)
_OPAQUE_TEXT = re.compile(r"[A-Za-z0-9._:/-]+\Z")


class SpoolCapacityError(RuntimeError):
    """The retryable safe admission failure for local spool pressure."""

    code = "spool_capacity_exhausted"
    retryable = True


class SpoolConflictError(RuntimeError):
    """An immutable local identity has different content."""


@dataclass(slots=True)
class _Reservation:
    maximum_bytes: int
    remaining_bytes: int
    work_class: WorkClass
    capture_enabled: bool
    capture_reason: str
    diagnostic_logs_enabled: bool
    capture_requested: bool
    diagnostic_logs_requested: bool
    request_id: str
    created_at: datetime
    maximum_event_bytes: int
    remaining_event_count: int
    closed: bool = False


@dataclass(slots=True)
class _PendingEvent:
    event: CanonicalEvent
    reservation_id: str
    payload_sha256: bytes
    released: bool = False
    release_kind: str | None = None
    release_receipt: str | None = None


class LocalCanonicalSpool:
    """Keep local responsibility until central confirmation or safe transfer."""

    def __init__(
        self,
        primary: EncryptedFrameJournal,
        limits: SpoolLimits,
        *,
        source_node_id: str,
        load_bounds: CanonicalLoadBounds,
        spill: EncryptedFrameJournal | None = None,
    ) -> None:
        """Recover durable reservations and events from configured journals."""
        if not source_node_id:
            msg = "The source node identity must not be empty."
            raise ValueError(msg)
        try:
            parsed_source_node_id = UUID(source_node_id)
        except ValueError as error:
            raise ValueError("The source node identity must be a UUID.") from error
        if str(parsed_source_node_id) != source_node_id:
            raise ValueError("The source node identity must use canonical UUID text.")
        if spill is not None and primary.storage_identity == spill.storage_identity:
            raise ValueError("The primary and spill journals must be different files.")
        self._primary = primary
        self._spill = spill
        self._limits = limits
        self._load_bounds = load_bounds
        self._source_node_id = source_node_id
        self._lock = threading.RLock()
        self._reservations: dict[str, _Reservation] = {}
        self._events: dict[str, _PendingEvent] = {}
        self._sequences: dict[int, str] = {}
        self._generation = 0
        self._record_ordinal = 0
        self._event_sequence = 0
        self._state = PressureState.NORMAL
        self._last_delivery_error: str | None = None
        self._closed = False
        self._primary.acquire_owner()
        try:
            if self._spill is not None:
                self._spill.acquire_owner()
            self._recover()
        except Exception:
            if self._spill is not None and self._spill.holds_owner():
                self._spill.close_owner()
            self._primary.close_owner()
            raise

    def close(self) -> None:
        """Release this process owner lease after a controlled shutdown."""
        if self._closed:
            return
        self._closed = True
        if self._spill is not None:
            self._spill.close()
        self._primary.close()

    @property
    def pending_events(self) -> tuple[CanonicalEvent, ...]:
        """Return events that this node still owns."""
        with self._lock:
            self._ensure_open()
            return tuple(
                pending.event
                for pending in sorted(
                    self._events.values(), key=lambda item: item.event.source_sequence
                )
                if not pending.released
            )

    def reserve(
        self,
        reservation_id: str,
        work_class: WorkClass,
        *,
        capture_requested: bool,
        request_id: str,
        now: datetime,
        diagnostic_logs_requested: bool = True,
    ) -> AdmissionResult:
        """Store an admission reservation and its required pressure audit."""
        maximum_bytes = self._load_bounds.maximum_reserved_load_bytes
        if not reservation_id or not request_id:
            msg = "A spool reservation needs a request identity."
            raise ValueError(msg)
        self._bounded_text(reservation_id, "reservation identity")
        self._bounded_text(request_id, "request identity")
        if not isinstance(work_class, WorkClass):
            raise TypeError("The spool work class is invalid.")
        self._aware(now)
        with self._lock:
            self._ensure_open()
            existing = self._reservations.get(reservation_id)
            if existing is not None:
                if (
                    existing.maximum_bytes != maximum_bytes
                    or existing.work_class is not work_class
                    or existing.request_id != request_id
                    or existing.capture_requested is not capture_requested
                    or existing.diagnostic_logs_requested
                    is not diagnostic_logs_requested
                    or existing.created_at != now
                    or existing.closed
                ):
                    raise SpoolConflictError(
                        "The spool reservation identity conflicts."
                    )
                return self._result(reservation_id, existing)

            current = self._update_pressure(self._used_bytes())
            protected = work_class in _PROTECTED_CLASSES
            projected = self._used_bytes() + maximum_bytes
            if projected > self._limits.capacity_bytes:
                raise SpoolCapacityError("The local spool cannot reserve this work.")
            if not protected and (
                current in (PressureState.STOPPED, PressureState.EMERGENCY)
                or projected >= self._limits.stop_bytes
            ):
                raise SpoolCapacityError("The local spool stopped new admissions.")
            if (
                not protected
                and work_class in _SHED_CLASSES
                and (
                    current is PressureState.SHEDDING
                    or projected >= self._limits.shedding_bytes
                )
            ):
                raise SpoolCapacityError("The local spool shed this work class.")

            projected_state = self._classify(projected)
            shedding = current in (
                PressureState.SHEDDING,
                PressureState.STOPPED,
                PressureState.EMERGENCY,
            ) or projected_state in (
                PressureState.SHEDDING,
                PressureState.STOPPED,
                PressureState.EMERGENCY,
            )
            capture_enabled = capture_requested and not shedding
            capture_reason = "spool_pressure" if shedding else "configured"
            diagnostic_logs_enabled = diagnostic_logs_requested and not shedding
            audit_event = None
            if shedding:
                audit_event = self._new_pressure_audit(
                    request_id,
                    now,
                    capture_requested=capture_requested,
                    diagnostic_logs_requested=diagnostic_logs_requested,
                )
                if len(audit_event.payload) > self._load_bounds.maximum_event_bytes:
                    raise SpoolCapacityError(
                        "The pressure audit event exceeds its configured bound."
                    )
            record: dict[str, Any] = {
                "kind": "reserve",
                "reservation_id": reservation_id,
                "maximum_bytes": maximum_bytes,
                "maximum_event_bytes": self._load_bounds.maximum_event_bytes,
                "remaining_event_count": (
                    self._load_bounds.maximum_canonical_event_count
                ),
                "work_class": work_class.value,
                "capture_enabled": capture_enabled,
                "capture_reason": capture_reason,
                "diagnostic_logs_enabled": diagnostic_logs_enabled,
                "capture_requested": capture_requested,
                "diagnostic_logs_requested": diagnostic_logs_requested,
                "request_id": request_id,
                "created_at": now.isoformat(),
                "audit_event": self._event_record(audit_event) if audit_event else None,
            }
            charged = self._append_record(record, maximum_charge=maximum_bytes)
            reservation = _Reservation(
                maximum_bytes=maximum_bytes,
                remaining_bytes=maximum_bytes - charged,
                work_class=work_class,
                capture_enabled=capture_enabled,
                capture_reason=capture_reason,
                diagnostic_logs_enabled=diagnostic_logs_enabled,
                capture_requested=capture_requested,
                diagnostic_logs_requested=diagnostic_logs_requested,
                request_id=request_id,
                created_at=now,
                maximum_event_bytes=self._load_bounds.maximum_event_bytes,
                remaining_event_count=self._load_bounds.maximum_canonical_event_count,
            )
            self._reservations[reservation_id] = reservation
            if audit_event is not None:
                self._add_event(reservation_id, audit_event)
                reservation.remaining_event_count -= 1
            self._state = self._classify(projected)
            return self._result(reservation_id, reservation)

    def append_event(self, reservation_id: str, event: CanonicalEvent) -> None:
        """Durably append an immutable event before its related success."""
        with self._lock:
            self._ensure_open()
            reservation = self._open_reservation(reservation_id)
            if len(event.payload) > reservation.maximum_event_bytes:
                raise SpoolCapacityError(
                    "The canonical event payload exceeds its bound."
                )
            if self._event_replayed(reservation_id, event):
                return
            if reservation.remaining_event_count <= 0:
                raise SpoolCapacityError("The canonical event count exceeds its bound.")
            self._validate_new_event(event)
            record = {
                "kind": "event",
                "reservation_id": reservation_id,
                **self._event_record(event),
            }
            charged = self._append_record(
                record, maximum_charge=reservation.remaining_bytes
            )
            reservation.remaining_bytes -= charged
            reservation.remaining_event_count -= 1
            self._add_event(reservation_id, event)

    def confirm_ingest(self, event_id: str, durable_replay_position: str) -> None:
        """Release event responsibility only after recoverable central ingest."""
        if not durable_replay_position:
            msg = "Central confirmation needs a durable replay position."
            raise ValueError(msg)
        self._bounded_receipt(durable_replay_position)
        with self._lock:
            self._ensure_open()
            pending = self._events.get(event_id)
            if pending is None:
                raise KeyError(event_id)
            if pending.released:
                if (
                    pending.release_kind != "confirm"
                    or pending.release_receipt != durable_replay_position
                ):
                    raise SpoolConflictError("The event release receipt conflicts.")
                self._compact()
                return
            reservation = self._open_reservation(pending.reservation_id)
            charged = self._append_record(
                {
                    "kind": "confirm",
                    "event_id": event_id,
                    "durable_replay_position": durable_replay_position,
                },
                maximum_charge=reservation.remaining_bytes,
            )
            reservation.remaining_bytes -= charged
            pending.released = True
            pending.release_kind = "confirm"
            pending.release_receipt = durable_replay_position
            self._compact()
            self._update_pressure(self._used_bytes())

    def transfer_responsibility(
        self, event_id: str, receiver: ResponsibilityReceiver
    ) -> str:
        """Release only after a destination gives a durable transfer receipt."""
        with self._lock:
            self._ensure_open()
            pending = self._events.get(event_id)
            if pending is None:
                raise KeyError(event_id)
            if pending.released:
                if pending.release_kind != "transfer" or not pending.release_receipt:
                    raise SpoolConflictError("The event release method conflicts.")
                self._compact()
                return pending.release_receipt
            receipt = receiver.accept(pending.event, pending.payload_sha256)
            if not receipt:
                raise SpoolStorageError("The responsibility transfer was not durable.")
            self._bounded_receipt(receipt)
            reservation = self._open_reservation(pending.reservation_id)
            charged = self._append_record(
                {"kind": "transfer", "event_id": event_id, "receipt": receipt},
                maximum_charge=reservation.remaining_bytes,
            )
            reservation.remaining_bytes -= charged
            pending.released = True
            pending.release_kind = "transfer"
            pending.release_receipt = receipt
            self._compact()
            self._update_pressure(self._used_bytes())
            return receipt

    def close_reservation(self, reservation_id: str) -> None:
        """Release a reservation only after all required event responsibility."""
        with self._lock:
            self._ensure_open()
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                raise KeyError(reservation_id)
            if reservation.closed:
                self._compact()
                return
            owned = [
                item
                for item in self._events.values()
                if item.reservation_id == reservation_id
            ]
            if not owned or any(not item.released for item in owned):
                raise SpoolConflictError(
                    "The reservation needs confirmed or transferred events."
                )
            self._append_record(
                {"kind": "close", "reservation_id": reservation_id},
                maximum_charge=reservation.remaining_bytes,
            )
            reservation.closed = True
            reservation.remaining_bytes = 0
            self._compact()
            self._update_pressure(self._used_bytes())

    def deliver_pending(self, ledger: CanonicalLedger) -> int:
        """Asynchronously deliver all events and keep failed events local."""
        delivered = 0
        failure: str | None = None
        for event in self.pending_events:
            try:
                receipt = ledger.ingest(event)
                if not receipt.replay_protected:
                    failure = "central_ingest_not_replay_protected"
                    continue
                self.confirm_ingest(event.event_id, receipt.durable_replay_position)
                delivered += 1
            except Exception as error:  # noqa: BLE001
                if failure is None:
                    failure = type(error).__name__
        with self._lock:
            self._last_delivery_error = failure
        return delivered

    def health(self, *, now: datetime) -> SpoolHealth:
        """Return safe health without payload, key, or storage path data."""
        self._aware(now)
        with self._lock:
            self._ensure_open()
            used = self._used_bytes()
            state = self._update_pressure(used)
            pending_times = [
                item.event.occurred_at
                for item in self._events.values()
                if not item.released
            ]
            oldest = None
            if pending_times:
                oldest = max(0, int((now - min(pending_times)).total_seconds()))
            return SpoolHealth(
                state=state,
                used_bytes=used,
                capacity_bytes=self._limits.capacity_bytes,
                oldest_event_age_seconds=oldest,
                shed_classes=(
                    _NORMAL_WORK_CLASSES
                    if state in (PressureState.STOPPED, PressureState.EMERGENCY)
                    else _SHED_CLASSES
                    if state is PressureState.SHEDDING
                    else ()
                ),
                last_delivery_error=self._last_delivery_error,
                estimated_remaining_bytes=max(0, self._limits.capacity_bytes - used),
                delivery_urgent=state is not PressureState.NORMAL,
                operator_alert=state is not PressureState.NORMAL,
                external_effects_allowed=state is not PressureState.EMERGENCY,
                optional_work_allowed=state is not PressureState.EMERGENCY,
            )

    def _recover(self) -> None:
        records = self._primary.read_all()
        if self._spill is not None:
            records.extend(self._spill.read_all())
        try:
            generations = [
                self._integer(item, "generation", minimum=0) for item in records
            ]
            self._generation = max(generations, default=0)
            selected = [
                item
                for item in records
                if self._integer(item, "generation", minimum=0) == self._generation
            ]
            unique: dict[int, Mapping[str, Any]] = {}
            for record in selected:
                ordinal = self._integer(record, "ordinal", minimum=1)
                prior = unique.get(ordinal)
                if prior is not None and prior != record:
                    raise SpoolStorageError("The spool journals contain a conflict.")
                unique[ordinal] = record
            if unique and sorted(unique) != list(range(1, max(unique) + 1)):
                raise SpoolStorageError("The local spool record order has a gap.")
            ordered = [unique[index] for index in sorted(unique)]
            if self._generation > 0 and (
                not ordered or ordered[0].get("kind") != "snapshot"
            ):
                raise SpoolStorageError("The local spool snapshot is incomplete.")
            for recovered_record in ordered:
                self._record_ordinal = self._integer(
                    recovered_record, "ordinal", minimum=1
                )
                self._apply_record(recovered_record)
        except (KeyError, TypeError, ValueError) as error:
            raise SpoolStorageError(
                "The local spool record schema is invalid."
            ) from error
        self._state = self._classify(self._used_bytes())

    def _apply_record(self, record: Mapping[str, Any]) -> None:
        kind = str(record["kind"])
        if kind == "snapshot":
            if self._record_ordinal != 1:
                raise SpoolStorageError("The local spool snapshot is out of order.")
            return
        if kind == "reserve":
            reservation_id = str(record["reservation_id"])
            if reservation_id in self._reservations:
                raise SpoolStorageError("A recovered reservation identity conflicts.")
            maximum = self._integer(record, "maximum_bytes", minimum=1)
            remaining = (
                self._integer(record, "remaining_bytes", minimum=0)
                if "remaining_bytes" in record
                else maximum - self._integer(record, "charged_bytes", minimum=1)
            )
            if remaining < 0 or remaining > maximum:
                raise SpoolStorageError("A recovered reservation bound is invalid.")
            reservation = _Reservation(
                maximum_bytes=maximum,
                remaining_bytes=remaining,
                work_class=WorkClass(str(record["work_class"])),
                capture_enabled=self._boolean(record, "capture_enabled"),
                capture_reason=str(record["capture_reason"]),
                diagnostic_logs_enabled=self._boolean(
                    record, "diagnostic_logs_enabled"
                ),
                capture_requested=self._boolean(record, "capture_requested"),
                diagnostic_logs_requested=self._boolean(
                    record, "diagnostic_logs_requested"
                ),
                request_id=str(record["request_id"]),
                created_at=self._time(record, "created_at"),
                maximum_event_bytes=self._integer(
                    record, "maximum_event_bytes", minimum=1
                ),
                remaining_event_count=self._integer(
                    record, "remaining_event_count", minimum=0
                ),
            )
            if not reservation_id or not reservation.request_id:
                raise SpoolStorageError("A recovered reservation identity is empty.")
            self._reservations[reservation_id] = reservation
            audit = record.get("audit_event")
            if audit is not None:
                event = self._decode_event(audit)
                if (
                    reservation.capture_reason != "spool_pressure"
                    or event.event_class is not EventClass.AUDIT
                ):
                    raise SpoolStorageError("A pressure audit record is invalid.")
                if (
                    len(event.payload) > reservation.maximum_event_bytes
                    or reservation.remaining_event_count <= 0
                ):
                    raise SpoolStorageError("A pressure audit exceeds its bound.")
                self._add_event(reservation_id, event)
                if not record.get("snapshot_state", False):
                    reservation.remaining_event_count -= 1
            elif reservation.capture_reason == "spool_pressure" and not record.get(
                "snapshot_state", False
            ):
                raise SpoolStorageError("A pressure-disabled admission lacks audit.")
            return
        if kind == "event":
            event = self._decode_event(record)
            reservation_id = str(record["reservation_id"])
            reservation = self._open_reservation(reservation_id)
            self._validate_new_event(event)
            if not record.get("snapshot_state", False):
                charge = self._integer(record, "charged_bytes", minimum=1)
                if charge > reservation.remaining_bytes:
                    raise SpoolStorageError(
                        "A recovered event exceeds its reservation."
                    )
                reservation.remaining_bytes -= charge
                if reservation.remaining_event_count <= 0:
                    raise SpoolStorageError(
                        "A recovered event exceeds its count bound."
                    )
                reservation.remaining_event_count -= 1
            self._add_event(reservation_id, event)
            if record.get("snapshot_state", False):
                self._events[event.event_id].released = self._boolean(
                    record, "released"
                )
                if self._events[event.event_id].released:
                    release_kind = str(record["release_kind"])
                    release_receipt = str(record["release_receipt"])
                    if (
                        release_kind not in ("confirm", "transfer")
                        or not release_receipt
                    ):
                        raise SpoolStorageError("A recovered release is incomplete.")
                    self._events[event.event_id].release_kind = release_kind
                    self._events[event.event_id].release_receipt = release_receipt
            return
        if kind in ("confirm", "transfer"):
            event_id = str(record["event_id"])
            pending = self._events.get(event_id)
            if pending is None or pending.released:
                raise SpoolStorageError("The spool release record is out of order.")
            reservation = self._open_reservation(pending.reservation_id)
            charge = self._integer(record, "charged_bytes", minimum=1)
            if charge > reservation.remaining_bytes:
                raise SpoolStorageError("A release exceeds its reservation.")
            if kind == "confirm" and not str(record["durable_replay_position"]):
                raise SpoolStorageError("A central confirmation is incomplete.")
            if kind == "transfer" and not str(record["receipt"]):
                raise SpoolStorageError("A transfer receipt is incomplete.")
            reservation.remaining_bytes -= charge
            pending.released = True
            pending.release_kind = kind
            pending.release_receipt = str(
                record["durable_replay_position" if kind == "confirm" else "receipt"]
            )
            return
        if kind == "close":
            reservation_id = str(record["reservation_id"])
            reservation = self._open_reservation(reservation_id)
            owned = [
                item
                for item in self._events.values()
                if item.reservation_id == reservation_id
            ]
            if not owned or any(not item.released for item in owned):
                raise SpoolStorageError("The spool close record is out of order.")
            charge = self._integer(record, "charged_bytes", minimum=1)
            if charge > reservation.remaining_bytes:
                raise SpoolStorageError("A close exceeds its reservation.")
            reservation.closed = True
            reservation.remaining_bytes = 0
            return
        raise SpoolStorageError("The local spool record kind is invalid.")

    def _append_record(self, record: Mapping[str, Any], *, maximum_charge: int) -> int:
        ordinal = self._record_ordinal + 1
        framed = dict(record)
        framed.update(generation=self._generation, ordinal=ordinal, charged_bytes=0)
        charge = 0
        for _ in range(4):
            charge = self._maximum_encoded_size(framed)
            if framed["charged_bytes"] == charge:
                break
            framed["charged_bytes"] = charge
        if self._maximum_encoded_size(framed) != charge:
            raise SpoolStorageError("The local spool frame size is unstable.")
        if charge > maximum_charge:
            raise SpoolCapacityError("The durable record exceeds its reservation.")
        try:
            self._primary.append(framed)
        except SpoolStorageError as primary_error:
            if self._spill is None:
                raise
            try:
                self._spill.append(framed)
            except SpoolStorageError as spill_error:
                raise SpoolStorageError(
                    "The primary and spill spool writes failed."
                ) from spill_error
            self._last_delivery_error = type(primary_error).__name__
        self._record_ordinal = ordinal
        return charge

    def _used_bytes(self) -> int:
        physical = self._primary.size_bytes
        if self._spill is not None:
            physical += self._spill.size_bytes
        promised = sum(
            item.remaining_bytes
            for item in self._reservations.values()
            if not item.closed
        )
        return physical + promised

    def _compact(self) -> None:
        generation = self._generation + 1
        records: list[Mapping[str, Any]] = [
            {"generation": generation, "ordinal": 1, "kind": "snapshot"}
        ]
        for reservation_id, reservation in self._reservations.items():
            if reservation.closed:
                continue
            records.append(
                {
                    "generation": generation,
                    "ordinal": len(records) + 1,
                    "kind": "reserve",
                    "reservation_id": reservation_id,
                    "maximum_bytes": reservation.maximum_bytes,
                    "remaining_bytes": reservation.remaining_bytes,
                    "work_class": reservation.work_class.value,
                    "capture_enabled": reservation.capture_enabled,
                    "capture_reason": reservation.capture_reason,
                    "diagnostic_logs_enabled": reservation.diagnostic_logs_enabled,
                    "capture_requested": reservation.capture_requested,
                    "diagnostic_logs_requested": reservation.diagnostic_logs_requested,
                    "request_id": reservation.request_id,
                    "created_at": reservation.created_at.isoformat(),
                    "maximum_event_bytes": reservation.maximum_event_bytes,
                    "remaining_event_count": reservation.remaining_event_count,
                    "audit_event": None,
                    "snapshot_state": True,
                }
            )
        for pending in sorted(
            self._events.values(), key=lambda item: item.event.source_sequence
        ):
            reservation = self._reservations[pending.reservation_id]
            if reservation.closed:
                continue
            records.append(
                {
                    "generation": generation,
                    "ordinal": len(records) + 1,
                    "kind": "event",
                    "reservation_id": pending.reservation_id,
                    "snapshot_state": True,
                    "released": pending.released,
                    "release_kind": pending.release_kind,
                    "release_receipt": pending.release_receipt,
                    **self._event_record(pending.event),
                }
            )
        try:
            snapshot_bytes = sum(self._maximum_encoded_size(item) for item in records)
            promised_bytes = sum(
                item.remaining_bytes
                for item in self._reservations.values()
                if not item.closed
            )
            if snapshot_bytes + promised_bytes > self._limits.capacity_bytes:
                raise SpoolCapacityError(
                    "The local spool has no safe compaction capacity."
                )
            try:
                self._primary.compact(records)
                stale = self._spill
            except SpoolStorageError as primary_error:
                if self._spill is None:
                    raise
                self._spill.compact(records)
                stale = self._primary
                self._last_delivery_error = type(primary_error).__name__
            self._generation = generation
            self._record_ordinal = len(records)
            if stale is not None:
                try:
                    stale.compact([])
                except SpoolStorageError as error:
                    self._last_delivery_error = type(error).__name__
        except SpoolStorageError:
            raise
        except OSError as error:
            raise SpoolStorageError("The local spool compaction failed.") from error

    def _new_pressure_audit(
        self,
        request_id: str,
        now: datetime,
        *,
        capture_requested: bool,
        diagnostic_logs_requested: bool,
    ) -> CanonicalEvent:
        payload = json.dumps(
            {
                "action": "spool.capture_policy",
                "capture_enabled": False,
                "capture_requested": capture_requested,
                "diagnostic_logs_enabled": False,
                "diagnostic_logs_requested": diagnostic_logs_requested,
                "pressure_reason": "spool_pressure",
                "request_id": request_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            source_node_id=self._source_node_id,
            source_sequence=self._event_sequence + 1,
            event_class=EventClass.AUDIT,
            payload=payload,
            occurred_at=now,
        )

    @staticmethod
    def _event_record(event: CanonicalEvent) -> dict[str, Any]:
        digest = hashlib.sha256(event.payload).hexdigest()
        return {
            "event_id": event.event_id,
            "source_node_id": event.source_node_id,
            "source_sequence": event.source_sequence,
            "event_class": event.event_class.value,
            "payload": base64.b64encode(event.payload).decode(),
            "payload_sha256": digest,
            "occurred_at": event.occurred_at.isoformat(),
        }

    def _decode_event(self, record: Mapping[str, Any]) -> CanonicalEvent:
        payload = base64.b64decode(str(record["payload"]), validate=True)
        event = CanonicalEvent(
            event_id=str(record["event_id"]),
            source_node_id=str(record["source_node_id"]),
            source_sequence=self._integer(record, "source_sequence", minimum=1),
            event_class=EventClass(str(record["event_class"])),
            payload=payload,
            occurred_at=self._time(record, "occurred_at"),
        )
        if hashlib.sha256(payload).hexdigest() != str(record["payload_sha256"]):
            raise SpoolStorageError("A recovered event digest does not match.")
        return event

    def _validate_new_event(self, event: CanonicalEvent) -> None:
        if event.source_node_id != self._source_node_id:
            raise SpoolConflictError("The canonical event has a wrong source node.")
        existing = self._events.get(event.event_id)
        if existing is not None:
            raise SpoolConflictError("The canonical event identity conflicts.")
        if event.source_sequence in self._sequences:
            raise SpoolConflictError("The canonical source sequence conflicts.")
        if event.source_sequence <= self._event_sequence:
            raise SpoolConflictError("The canonical source sequence is out of order.")

    def _event_replayed(self, reservation_id: str, event: CanonicalEvent) -> bool:
        existing = self._events.get(event.event_id)
        if existing is None:
            return False
        if existing.event != event or existing.reservation_id != reservation_id:
            raise SpoolConflictError("The canonical event identity conflicts.")
        return True

    def _add_event(self, reservation_id: str, event: CanonicalEvent) -> None:
        self._validate_new_event(event)
        digest = hashlib.sha256(event.payload).digest()
        self._events[event.event_id] = _PendingEvent(
            event=event, reservation_id=reservation_id, payload_sha256=digest
        )
        self._sequences[event.source_sequence] = event.event_id
        self._event_sequence = event.source_sequence

    def _classify(self, used: int) -> PressureState:
        if used >= self._limits.capacity_bytes - self._limits.emergency_reserve_bytes:
            return PressureState.EMERGENCY
        if used >= self._limits.stop_bytes:
            return PressureState.STOPPED
        if used >= self._limits.shedding_bytes:
            return PressureState.SHEDDING
        if used >= self._limits.warning_bytes:
            return PressureState.WARNING
        return PressureState.NORMAL

    def _update_pressure(self, used: int) -> PressureState:
        classified = self._classify(used)
        order = {
            PressureState.NORMAL: 0,
            PressureState.WARNING: 1,
            PressureState.SHEDDING: 2,
            PressureState.STOPPED: 3,
            PressureState.EMERGENCY: 4,
        }
        if order[classified] > order[self._state]:
            self._state = classified
            return self._state
        threshold = {
            PressureState.EMERGENCY: self._limits.capacity_bytes
            - self._limits.emergency_reserve_bytes,
            PressureState.STOPPED: self._limits.stop_bytes,
            PressureState.SHEDDING: self._limits.shedding_bytes,
            PressureState.WARNING: self._limits.warning_bytes,
            PressureState.NORMAL: 0,
        }[self._state]
        if self._state is not PressureState.NORMAL and used >= (
            threshold - self._limits.recovery_hysteresis_bytes
        ):
            return self._state
        self._state = classified
        return self._state

    def _open_reservation(self, reservation_id: str) -> _Reservation:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise KeyError(reservation_id)
        if reservation.closed:
            raise SpoolConflictError("The spool reservation is closed.")
        return reservation

    def _result(
        self, reservation_id: str, reservation: _Reservation
    ) -> AdmissionResult:
        state = self._update_pressure(self._used_bytes())
        return AdmissionResult(
            reservation_id=reservation_id,
            reserved_bytes=reservation.maximum_bytes,
            capture_enabled=reservation.capture_enabled,
            capture_reason=reservation.capture_reason,
            diagnostic_logs_enabled=reservation.diagnostic_logs_enabled,
            pressure_state=state,
            delivery_urgent=state is not PressureState.NORMAL,
            operator_alert=state is not PressureState.NORMAL,
        )

    def _maximum_encoded_size(self, record: Mapping[str, Any]) -> int:
        sizes = [self._primary.encoded_size(record)]
        if self._spill is not None:
            sizes.append(self._spill.encoded_size(record))
        return max(sizes)

    def _ensure_open(self) -> None:
        if self._closed:
            raise SpoolStorageError("The local spool is closed.")

    def _bounded_text(self, value: str, name: str) -> None:
        if (
            not _OPAQUE_TEXT.fullmatch(value)
            or len(value.encode("ascii")) > self._load_bounds.maximum_identity_bytes
        ):
            msg = f"The {name} exceeds its configured bound."
            raise ValueError(msg)

    def _bounded_receipt(self, value: str) -> None:
        if (
            not _OPAQUE_TEXT.fullmatch(value)
            or len(value.encode("ascii")) > self._load_bounds.maximum_receipt_bytes
        ):
            raise ValueError("The durable receipt exceeds its configured bound.")

    @staticmethod
    def _integer(record: Mapping[str, Any], name: str, *, minimum: int) -> int:
        value = record[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(name)
        return value

    @staticmethod
    def _boolean(record: Mapping[str, Any], name: str) -> bool:
        value = record[name]
        if not isinstance(value, bool):
            raise TypeError(name)
        return value

    @staticmethod
    def _time(record: Mapping[str, Any], name: str) -> datetime:
        value = datetime.fromisoformat(str(record[name]))
        LocalCanonicalSpool._aware(value)
        return value

    @staticmethod
    def _aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "Spool times must include a time zone."
            raise ValueError(msg)


def utc_now() -> datetime:
    """Return an aware time for process integrations."""
    return datetime.now(tz=UTC)
