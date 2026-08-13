"""Deterministic encrypted local spool tests."""
# ruff: noqa: ARG005, PLR2004, TC003

from __future__ import annotations

import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from llmrouter_backend.spool import (
    CanonicalEvent,
    CanonicalLoadBounds,
    EncryptedFrameJournal,
    EventClass,
    LocalCanonicalSpool,
    PressureState,
    SpoolCapacityError,
    SpoolConflictError,
    SpoolLimits,
    SpoolStorageError,
    WorkClass,
)

NOW = datetime(2026, 8, 13, tzinfo=UTC)
NODE = "0198a080-0000-7000-8000-000000000101"


def _journal(path: Path, *, key: bytes = b"a" * 32) -> EncryptedFrameJournal:
    return EncryptedFrameJournal(
        path, {"key-1": key}, "key-1", trusted_root=path.parent
    )


def _limits() -> SpoolLimits:
    return SpoolLimits(100_000, 20_000, 40_000, 70_000, 20_000, 5_000, 100_000)


def _bounds(
    *, maximum_event_bytes: int = 128, fixed_event_count: int = 3
) -> CanonicalLoadBounds:
    return CanonicalLoadBounds(
        maximum_event_bytes=maximum_event_bytes,
        encrypted_frame_overhead_bytes=256,
        reservation_state_overhead_bytes=512,
        fixed_event_count=fixed_event_count,
        maximum_provider_attempts=0,
        events_per_provider_attempt=0,
        maximum_tool_steps=0,
        events_per_tool_step=0,
        maximum_identity_bytes=128,
        maximum_receipt_bytes=512,
    )


def _early_warning_limits() -> SpoolLimits:
    return SpoolLimits(100_000, 10_000, 40_000, 70_000, 20_000, 5_000, 100_000)


def _event(sequence: int, payload: bytes = b"canonical-safe-data") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=f"0198a080-0000-7000-8000-{sequence:012d}",
        source_node_id=NODE,
        source_sequence=sequence,
        event_class=EventClass.ACCOUNTING,
        payload=payload,
        occurred_at=NOW,
    )


def test_bounds_include_encrypted_and_state_overhead() -> None:
    """Calculate a deterministic maximum load and concurrency capacity."""
    bounds = CanonicalLoadBounds(100, 256, 512, 2, 8, 3, 100, 2, 128, 512)
    assert bounds.maximum_event_count == 226
    assert bounds.maximum_load_bytes == 512 + 226 * (136 + 256)
    assert bounds.required_node_capacity(8) == bounds.maximum_reserved_load_bytes * 8


def test_all_canonical_load_bounds_are_explicit() -> None:
    """Do not add an unaccepted product default to a capacity input."""
    assert all(field.default is MISSING for field in fields(CanonicalLoadBounds))
    assert all(
        field.default_factory is MISSING for field in fields(CanonicalLoadBounds)
    )


def test_worst_case_bound_releases_and_recovers(tmp_path: Path) -> None:
    """Complete all maximum-size records inside one admitted reservation."""
    bounds = _bounds(maximum_event_bytes=1024, fixed_event_count=2)
    reservation_bytes = bounds.maximum_reserved_load_bytes
    limits = SpoolLimits(
        capacity_bytes=reservation_bytes * 3,
        warning_bytes=reservation_bytes // 4,
        shedding_bytes=reservation_bytes // 2,
        stop_bytes=reservation_bytes * 2,
        emergency_reserve_bytes=reservation_bytes // 2,
        recovery_hysteresis_bytes=reservation_bytes // 8,
        operational_headroom_bytes=reservation_bytes * 3,
    )
    path = tmp_path / "spool.bin"
    reservation_id = "i" * bounds.maximum_identity_bytes
    request_id = "q" * bounds.maximum_identity_bytes
    receipt = "r" * bounds.maximum_receipt_bytes
    spool = LocalCanonicalSpool(
        _journal(path), limits, source_node_id=NODE, load_bounds=bounds
    )
    result = spool.reserve(
        reservation_id,
        WorkClass.FOREGROUND,
        capture_requested=True,
        request_id=request_id,
        now=NOW,
    )
    assert result.pressure_state is PressureState.SHEDDING
    for sequence in range(2, bounds.maximum_event_count + 2):
        spool.append_event(
            reservation_id,
            _event(sequence, payload=b"p" * bounds.maximum_event_bytes),
        )
    assert len(spool.pending_events) == bounds.maximum_canonical_event_count
    for event in spool.pending_events:
        spool.confirm_ingest(event.event_id, receipt)
    spool.close_reservation(reservation_id)
    assert spool.health(now=NOW).used_bytes <= limits.capacity_bytes
    spool.close()
    recovered = LocalCanonicalSpool(
        _journal(path), limits, source_node_id=NODE, load_bounds=bounds
    )
    assert recovered.pending_events == ()
    assert recovered.health(now=NOW).used_bytes <= limits.capacity_bytes


def test_limits_keep_stop_strictly_below_emergency_reserve() -> None:
    """Reject a stop threshold at the emergency-capacity boundary."""
    with pytest.raises(ValueError, match="below"):
        SpoolLimits(10_000, 2_000, 4_000, 8_000, 2_000, 500, 10_000)
    with pytest.raises(ValueError, match="compaction"):
        SpoolLimits(10_000, 2_000, 4_000, 7_000, 2_000, 500, 9_999)


def test_canonical_events_require_canonical_uuid_text() -> None:
    """Reject UUID text that PostgreSQL would normalize on storage."""
    with pytest.raises(ValueError, match="canonical UUID"):
        CanonicalEvent(
            event_id=_event(1).event_id.upper(),
            source_node_id=NODE,
            source_sequence=1,
            event_class=EventClass.ACCOUNTING,
            payload=b"event",
            occurred_at=NOW,
        )


def test_frames_encrypt_rotate_tamper_and_repair_tail(tmp_path: Path) -> None:
    """Encrypt plaintext, retain old keys, and fail closed on complete tamper."""
    path = tmp_path / "spool.bin"
    journal = _journal(path)
    journal.acquire_owner()
    journal.append({"secret": "not-plain"})
    assert b"not-plain" not in path.read_bytes()
    journal.close_owner()
    rotated = EncryptedFrameJournal(
        path,
        {"key-1": b"a" * 32, "key-2": b"b" * 32},
        "key-2",
        trusted_root=tmp_path,
    )
    rotated.acquire_owner()
    assert rotated.read_all() == [{"secret": "not-plain"}]
    rotated.append({"second": True})
    assert len(rotated.read_all()) == 2
    assert b'"key_id":"key-2"' in path.with_name("spool.bin.state").read_bytes()
    with path.open("ab") as stream:
        stream.write(b"partial")
    assert len(rotated.read_all()) == 2
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    with pytest.raises(SpoolStorageError, match="integrity"):
        rotated.read_all()


@pytest.mark.parametrize("length_offset", [5, 7])
def test_committed_header_length_tamper_never_drops_a_frame(
    tmp_path: Path, length_offset: int
) -> None:
    """Fail closed when a complete committed frame has a changed length."""
    path = tmp_path / "spool.bin"
    journal = _journal(path)
    journal.acquire_owner()
    journal.append({"canonical": "event"})
    data = bytearray(path.read_bytes())
    if length_offset == 5:
        data[length_offset : length_offset + 2] = struct.pack(">H", 65535)
    else:
        data[length_offset : length_offset + 4] = struct.pack(">I", 2**32 - 1)
    path.write_bytes(data)
    with pytest.raises(SpoolStorageError, match="integrity"):
        journal.read_all()


def test_commit_state_tamper_and_combined_frame_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    """Do not trust a changed header or unauthenticated commit metadata."""
    path = tmp_path / "spool.bin"
    journal = _journal(path)
    journal.acquire_owner()
    journal.append({"canonical": "event"})
    data = bytearray(path.read_bytes())
    data[7:11] = struct.pack(">I", 2**32 - 1)
    path.write_bytes(data)
    state = path.with_name(f"{path.name}.state")
    state_data = bytearray(state.read_bytes())
    state_data[-2] ^= 1
    state.write_bytes(state_data)
    with pytest.raises(SpoolStorageError, match=r"integrity|invalid"):
        journal.read_all()


def test_authenticated_intent_repairs_only_an_incomplete_append(tmp_path: Path) -> None:
    """Use authenticated old state to remove bytes from an interrupted append."""
    path = tmp_path / "spool.bin"
    journal = _journal(path)
    journal.acquire_owner()
    journal.append({"first": True})
    previous = path.read_bytes()
    complete = previous + journal._frame({"second": True})  # noqa: SLF001
    journal._write_intent_state("append", previous, complete)  # noqa: SLF001
    with path.open("ab") as stream:
        stream.write(complete[len(previous) : -3])
        stream.flush()
    assert journal.read_all() == [{"first": True}]
    assert path.read_bytes() == previous


def test_journal_concurrent_append_is_frame_atomic(tmp_path: Path) -> None:
    """Serialize concurrent writers so frames cannot interleave."""
    path = tmp_path / "spool.bin"
    journal = _journal(path)
    journal.acquire_owner()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda value: journal.append({"n": value}), range(40)))
    assert sorted(record["n"] for record in journal.read_all()) == list(range(40))


def test_append_uses_cached_digest_and_does_not_reread_large_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep normal append work proportional to the new encrypted frame."""
    journal = _journal(tmp_path / "spool.bin")
    journal.acquire_owner()
    journal.append({"large": "x" * 1_000_000})
    monkeypatch.setattr(
        journal,
        "_read_file",
        lambda path: (_ for _ in ()).throw(AssertionError("full reread")),
    )
    journal.append({"small": True})


def test_second_journal_object_cannot_mutate_owned_files(tmp_path: Path) -> None:
    """Require the process owner lock for every journal mutation."""
    path = tmp_path / "spool.bin"
    owner = _journal(path)
    owner.acquire_owner()
    other = _journal(path)
    with pytest.raises(SpoolStorageError, match="logical owner"):
        other.append({"bypass": True})
    with pytest.raises(SpoolStorageError, match="logical owner"):
        other.compact([])
    assert owner.read_all() == []


def test_reservations_survive_restart_and_release_only_after_transfer(
    tmp_path: Path,
) -> None:
    """Recover responsibility and compact only after durable transfer."""
    path = tmp_path / "spool.bin"
    spool = LocalCanonicalSpool(
        _journal(path), _limits(), source_node_id=NODE, load_bounds=_bounds()
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=True,
        request_id="request-1",
        now=NOW,
    )
    spool.append_event("r1", _event(1))
    spool.close()
    recovered = LocalCanonicalSpool(
        _journal(path), _limits(), source_node_id=NODE, load_bounds=_bounds()
    )
    assert recovered.pending_events == (_event(1),)

    class Receiver:
        def accept(self, event: CanonicalEvent, payload_sha256: bytes) -> str:
            assert event == _event(1)
            assert len(payload_sha256) == 32
            return "repair:1"

    recovered.transfer_responsibility(_event(1).event_id, Receiver())
    recovered.close_reservation("r1")
    assert recovered.health(now=NOW).used_bytes < 1000
    recovered.close()
    assert (
        LocalCanonicalSpool(
            _journal(path), _limits(), source_node_id=NODE, load_bounds=_bounds()
        ).pending_events
        == ()
    )


def test_pressure_order_capture_hysteresis_and_emergency(tmp_path: Path) -> None:
    """Shed optional work before foreground and keep stopped state hysteresis."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _limits(),
        source_node_id=NODE,
        load_bounds=_bounds(maximum_event_bytes=7_000),
    )
    first = spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=True,
        request_id="request-1",
        now=NOW,
    )
    assert first.pressure_state is PressureState.SHEDDING
    assert first.operator_alert
    assert first.capture_reason == "spool_pressure"
    assert not first.diagnostic_logs_enabled
    assert len(spool.pending_events) == 1
    assert spool.pending_events[0].event_class is EventClass.AUDIT
    assert b'"request_id":"request-1"' in spool.pending_events[0].payload
    with pytest.raises(SpoolCapacityError):
        spool.reserve(
            "batch",
            WorkClass.BATCH,
            capture_requested=True,
            request_id="request-batch",
            now=NOW,
        )
    with pytest.raises(SpoolCapacityError):
        spool.reserve(
            "stop",
            WorkClass.FOREGROUND,
            capture_requested=True,
            request_id="request-stop",
            now=NOW,
        )
    emergency = spool.reserve(
        "security",
        WorkClass.SECURITY,
        capture_requested=False,
        request_id="request-security",
        now=NOW,
    )
    assert emergency.pressure_state is PressureState.EMERGENCY
    assert not spool.health(now=NOW + timedelta(seconds=5)).external_effects_allowed
    assert WorkClass.FOREGROUND in spool.health(now=NOW).shed_classes


def test_warning_sets_operator_alert_and_delivery_urgency(tmp_path: Path) -> None:
    """Expose the required warning alert separately from urgent delivery."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _early_warning_limits(),
        source_node_id=NODE,
        load_bounds=_bounds(maximum_event_bytes=1_400),
    )
    result = spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=True,
        request_id="request-1",
        now=NOW,
    )
    assert result.pressure_state is PressureState.WARNING
    assert result.operator_alert
    assert result.delivery_urgent
    health = spool.health(now=NOW)
    assert health.operator_alert
    assert health.delivery_urgent


def test_failed_pressure_audit_validation_leaves_no_reservation(tmp_path: Path) -> None:
    """Validate the mandatory audit before the atomic reservation write."""
    bounds = _bounds(maximum_event_bytes=32)
    limits = SpoolLimits(100_000, 1_000, 2_000, 70_000, 20_000, 500, 100_000)
    path = tmp_path / "spool.bin"
    spool = LocalCanonicalSpool(
        _journal(path), limits, source_node_id=NODE, load_bounds=bounds
    )
    with pytest.raises(SpoolCapacityError, match="audit"):
        spool.reserve(
            "r1",
            WorkClass.FOREGROUND,
            capture_requested=True,
            request_id="request-1",
            now=NOW,
        )
    assert spool.pending_events == ()
    assert _journal(path).read_all() == []


def test_spill_is_encrypted_and_recovers_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the configured encrypted spill after a primary write failure."""
    primary = _journal(tmp_path / "primary.bin")
    spill = _journal(tmp_path / "spill.bin", key=b"s" * 32)
    spool = LocalCanonicalSpool(
        primary, _limits(), source_node_id=NODE, load_bounds=_bounds(), spill=spill
    )
    monkeypatch.setattr(
        primary,
        "append",
        lambda record: (_ for _ in ()).throw(SpoolStorageError("full")),
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=True,
        request_id="request-1",
        now=NOW,
    )
    assert b"reservation_id" not in (tmp_path / "spill.bin").read_bytes()
    spool.close()
    assert (
        LocalCanonicalSpool(
            _journal(tmp_path / "primary.bin"),
            _limits(),
            source_node_id=NODE,
            load_bounds=_bounds(),
            spill=_journal(tmp_path / "spill.bin", key=b"s" * 32),
        )
        .health(now=NOW)
        .used_bytes
        >= 1000
    )


def test_symlink_target_is_rejected(tmp_path: Path) -> None:
    """Refuse a spool target that follows a symbolic link."""
    target = tmp_path / "target"
    target.write_bytes(b"")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SpoolStorageError):
        _journal(link)


def test_a_second_process_owner_is_rejected(tmp_path: Path) -> None:
    """Reject two local owners before they can reuse an ordinal or over-admit."""
    path = tmp_path / "spool.bin"
    first = LocalCanonicalSpool(
        _journal(path), _limits(), source_node_id=NODE, load_bounds=_bounds()
    )
    with pytest.raises(SpoolStorageError, match="owns"):
        LocalCanonicalSpool(
            _journal(path), _limits(), source_node_id=NODE, load_bounds=_bounds()
        )
    first.close()


def test_one_journal_object_cannot_have_two_logical_spool_owners(
    tmp_path: Path,
) -> None:
    """Do not let a shared object bypass the owner lock."""
    journal = _journal(tmp_path / "spool.bin")
    first = LocalCanonicalSpool(
        journal, _limits(), source_node_id=NODE, load_bounds=_bounds()
    )
    with pytest.raises(SpoolStorageError, match="owns"):
        LocalCanonicalSpool(
            journal, _limits(), source_node_id=NODE, load_bounds=_bounds()
        )
    first.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-1",
        now=NOW,
    )
    first.close()


def test_primary_and_spill_must_be_distinct(tmp_path: Path) -> None:
    """Reject two journal handles that point to the same file."""
    path = tmp_path / "spool.bin"
    with pytest.raises(ValueError, match="different"):
        LocalCanonicalSpool(
            _journal(path),
            _limits(),
            source_node_id=NODE,
            load_bounds=_bounds(),
            spill=_journal(path),
        )


def test_reservation_replay_checks_all_admission_inputs(tmp_path: Path) -> None:
    """Reject a repeat reservation that changes an immutable admission input."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _early_warning_limits(),
        source_node_id=NODE,
        load_bounds=_bounds(),
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=True,
        diagnostic_logs_requested=True,
        request_id="request-1",
        now=NOW,
    )
    for capture, diagnostics, admitted_at in (
        (False, True, NOW),
        (True, False, NOW),
        (True, True, NOW + timedelta(seconds=1)),
    ):
        with pytest.raises(SpoolConflictError, match="conflicts"):
            spool.reserve(
                "r1",
                WorkClass.FOREGROUND,
                request_id="request-1",
                capture_requested=capture,
                diagnostic_logs_requested=diagnostics,
                now=admitted_at,
            )


def test_event_replay_is_not_charged_and_release_receipt_is_immutable(
    tmp_path: Path,
) -> None:
    """Make exact event replay free and keep one immutable release receipt."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _early_warning_limits(),
        source_node_id=NODE,
        load_bounds=_bounds(maximum_event_bytes=1_400),
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=True,
        request_id="request-1",
        now=NOW,
    )
    spool.append_event("r1", _event(1))
    used = spool.health(now=NOW).used_bytes
    spool.append_event("r1", _event(1))
    assert spool.health(now=NOW).used_bytes == used
    spool.confirm_ingest(_event(1).event_id, "replay:1")
    spool.confirm_ingest(_event(1).event_id, "replay:1")
    with pytest.raises(SpoolConflictError, match="conflicts"):
        spool.confirm_ingest(_event(1).event_id, "replay:2")


def test_empty_reservation_cannot_release(tmp_path: Path) -> None:
    """Keep an admission reservation until at least one event has a receipt."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _limits(),
        source_node_id=NODE,
        load_bounds=_bounds(),
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=True,
        request_id="request-1",
        now=NOW,
    )
    with pytest.raises(SpoolConflictError, match="confirmed or transferred"):
        spool.close_reservation("r1")


def test_both_journal_writes_fail_without_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject admission and keep memory empty after two durable write failures."""
    primary = _journal(tmp_path / "primary.bin")
    spill = _journal(tmp_path / "spill.bin", key=b"b" * 32)
    spool = LocalCanonicalSpool(
        primary, _limits(), source_node_id=NODE, load_bounds=_bounds(), spill=spill
    )

    def failure(_record: object) -> None:
        msg = "full"
        raise SpoolStorageError(msg)

    monkeypatch.setattr(primary, "append", failure)
    monkeypatch.setattr(spill, "append", failure)
    with pytest.raises(SpoolStorageError, match="primary and spill"):
        spool.reserve(
            "r1",
            WorkClass.FOREGROUND,
            capture_requested=True,
            request_id="request-1",
            now=NOW,
        )
    assert spool.pending_events == ()
    assert spool.health(now=NOW).state is PressureState.NORMAL


def test_spill_key_identity_overhead_is_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Charge the larger spill frame before a primary write failure."""
    primary = _journal(tmp_path / "primary.bin")
    spill = EncryptedFrameJournal(
        tmp_path / "spill.bin",
        {"spill-key-with-long-identity": b"s" * 32},
        "spill-key-with-long-identity",
        trusted_root=tmp_path,
    )
    spool = LocalCanonicalSpool(
        primary, _limits(), source_node_id=NODE, load_bounds=_bounds(), spill=spill
    )
    monkeypatch.setattr(
        primary,
        "append",
        lambda record: (_ for _ in ()).throw(SpoolStorageError("full")),
    )
    result = spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-1",
        now=NOW,
    )
    records = spill.read_all()
    assert records[0]["charged_bytes"] == spill.encoded_size(records[0])
    assert result.reserved_bytes == _bounds().maximum_reserved_load_bytes


def test_delivery_error_stays_visible_when_a_later_event_succeeds(
    tmp_path: Path,
) -> None:
    """Keep a failed event visible through a partially successful delivery pass."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _limits(),
        source_node_id=NODE,
        load_bounds=_bounds(),
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-1",
        now=NOW,
    )
    spool.append_event("r1", _event(1))
    spool.append_event("r1", _event(2))

    class Ledger:
        def ingest(self, event: CanonicalEvent) -> object:
            if event.source_sequence == 1:
                msg = "down"
                raise RuntimeError(msg)

            class Receipt:
                replay_protected = True
                durable_replay_position = "replay:2"

            return Receipt()

    assert spool.deliver_pending(Ledger()) == 1  # type: ignore[arg-type]
    assert spool.health(now=NOW).last_delivery_error == "RuntimeError"


def test_emergency_blocks_optional_work_and_external_effects(tmp_path: Path) -> None:
    """Show both emergency controls when protected work uses the reserve."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _limits(),
        source_node_id=NODE,
        load_bounds=_bounds(maximum_event_bytes=14_000),
    )
    result = spool.reserve(
        "security",
        WorkClass.SECURITY,
        capture_requested=False,
        request_id="security-1",
        now=NOW,
    )
    assert result.pressure_state is PressureState.EMERGENCY
    health = spool.health(now=NOW)
    assert not health.external_effects_allowed
    assert not health.optional_work_allowed


def test_close_releases_pressure_only_after_hysteresis(tmp_path: Path) -> None:
    """Keep the warning latch until usage falls below its recovery margin."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _early_warning_limits(),
        source_node_id=NODE,
        load_bounds=_bounds(),
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-1",
        now=NOW,
    )
    spool.append_event("r1", _event(1))
    spool.reserve(
        "r2",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-2",
        now=NOW,
    )
    spool.append_event("r2", _event(2))
    assert spool.health(now=NOW).state is PressureState.WARNING
    spool.confirm_ingest(_event(1).event_id, "replay:1")
    spool.close_reservation("r1")
    assert spool.health(now=NOW).state is PressureState.WARNING
    spool.confirm_ingest(_event(2).event_id, "replay:2")
    spool.close_reservation("r2")
    assert spool.health(now=NOW).state is PressureState.NORMAL


def test_recovery_rejects_conflicting_ordinals_and_gaps(tmp_path: Path) -> None:
    """Fail closed for authenticated conflicts and incomplete record order."""
    path = tmp_path / "spool.bin"
    journal = _journal(path)
    journal.acquire_owner()
    journal.append({"generation": 0, "ordinal": 1, "kind": "bad"})
    journal.append({"generation": 0, "ordinal": 1, "kind": "changed"})
    journal.close_owner()
    with pytest.raises(SpoolStorageError, match="conflict"):
        LocalCanonicalSpool(
            _journal(path), _limits(), source_node_id=NODE, load_bounds=_bounds()
        )
    gap = tmp_path / "gap.bin"
    gap_journal = _journal(gap)
    gap_journal.acquire_owner()
    gap_journal.append({"generation": 0, "ordinal": 2, "kind": "bad"})
    gap_journal.close_owner()
    with pytest.raises(SpoolStorageError, match="gap"):
        LocalCanonicalSpool(
            _journal(gap), _limits(), source_node_id=NODE, load_bounds=_bounds()
        )


def test_crash_after_release_record_keeps_responsibility_until_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover a durable release even when snapshot replacement fails."""
    path = tmp_path / "spool.bin"
    primary = _journal(path)
    spool = LocalCanonicalSpool(
        primary, _limits(), source_node_id=NODE, load_bounds=_bounds()
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-1",
        now=NOW,
    )
    spool.append_event("r1", _event(1))
    monkeypatch.setattr(
        primary,
        "compact",
        lambda records: (_ for _ in ()).throw(SpoolStorageError("crash")),
    )
    with pytest.raises(SpoolStorageError, match="crash"):
        spool.confirm_ingest(_event(1).event_id, "replay:1")
    spool.close()
    recovered = LocalCanonicalSpool(
        _journal(path), _limits(), source_node_id=NODE, load_bounds=_bounds()
    )
    assert recovered.pending_events == ()
    recovered.confirm_ingest(_event(1).event_id, "replay:1")
    recovered.close_reservation("r1")


def test_spill_compaction_reclaims_responsibility_while_primary_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a complete newer spill generation when primary compaction fails."""
    primary_path = tmp_path / "primary.bin"
    spill_path = tmp_path / "spill.bin"
    primary = _journal(primary_path)
    spill = _journal(spill_path, key=b"s" * 32)
    spool = LocalCanonicalSpool(
        primary, _limits(), source_node_id=NODE, load_bounds=_bounds(), spill=spill
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-1",
        now=NOW,
    )
    spool.append_event("r1", _event(1))
    monkeypatch.setattr(
        primary,
        "compact",
        lambda records: (_ for _ in ()).throw(SpoolStorageError("primary down")),
    )
    spool.confirm_ingest(_event(1).event_id, "replay:1")
    spool.close_reservation("r1")
    spool.close()
    recovered = LocalCanonicalSpool(
        _journal(primary_path),
        _limits(),
        source_node_id=NODE,
        load_bounds=_bounds(),
        spill=_journal(spill_path, key=b"s" * 32),
    )
    assert recovered.pending_events == ()


def test_pressure_state_escalates_while_recovery_hysteresis_is_latched(
    tmp_path: Path,
) -> None:
    """Do not let lower-state hysteresis hide an emergency escalation."""
    spool = LocalCanonicalSpool(
        _journal(tmp_path / "spool.bin"),
        _early_warning_limits(),
        source_node_id=NODE,
        load_bounds=_bounds(maximum_event_bytes=1_400),
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-1",
        now=NOW,
    )
    assert spool.health(now=NOW).state is PressureState.WARNING
    spool.reserve(
        "security",
        WorkClass.SECURITY,
        capture_requested=False,
        request_id="security-1",
        now=NOW,
    )
    spool.reserve(
        "security-2",
        WorkClass.SECURITY,
        capture_requested=False,
        request_id="security-2",
        now=NOW,
    )
    spool.reserve(
        "security-3",
        WorkClass.SECURITY,
        capture_requested=False,
        request_id="security-3",
        now=NOW,
    )
    spool.reserve(
        "security-4",
        WorkClass.SECURITY,
        capture_requested=False,
        request_id="security-4",
        now=NOW,
    )
    spool.reserve(
        "security-5",
        WorkClass.SECURITY,
        capture_requested=False,
        request_id="security-5",
        now=NOW,
    )
    assert spool.health(now=NOW).state is PressureState.EMERGENCY


def test_spill_stale_generation_does_not_replay_after_primary_compaction(
    tmp_path: Path,
) -> None:
    """Select the complete primary generation and ignore an older spill."""
    primary_path = tmp_path / "primary.bin"
    spill_path = tmp_path / "spill.bin"
    primary = _journal(primary_path)
    spill = _journal(spill_path, key=b"s" * 32)
    spool = LocalCanonicalSpool(
        primary, _limits(), source_node_id=NODE, load_bounds=_bounds(), spill=spill
    )
    spool.reserve(
        "r1",
        WorkClass.FOREGROUND,
        capture_requested=False,
        request_id="request-1",
        now=NOW,
    )
    spool.append_event("r1", _event(1))
    stale = spill.read_all()
    spool.confirm_ingest(_event(1).event_id, "replay:1")
    spill.compact(stale)
    spool.close()
    recovered = LocalCanonicalSpool(
        _journal(primary_path),
        _limits(),
        source_node_id=NODE,
        load_bounds=_bounds(),
        spill=_journal(spill_path, key=b"s" * 32),
    )
    assert recovered.pending_events == ()


def test_private_parent_directory_is_required(tmp_path: Path) -> None:
    """Reject a journal below a group-writable or world-writable directory."""
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(SpoolStorageError, match="private"):
        EncryptedFrameJournal(
            unsafe / "spool.bin",
            {"key-1": b"a" * 32},
            "key-1",
            trusted_root=unsafe,
        )


def test_trusted_root_rejects_an_unsafe_ancestor_and_symlink(tmp_path: Path) -> None:
    """Validate each configured directory component before file creation."""
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    nested = unsafe / "nested" / "spool.bin"
    with pytest.raises(SpoolStorageError, match="private"):
        EncryptedFrameJournal(
            nested, {"key-1": b"a" * 32}, "key-1", trusted_root=unsafe
        )
    safe = tmp_path / "safe"
    safe.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (safe / "link").symlink_to(target, target_is_directory=True)
    with pytest.raises(SpoolStorageError, match="safe"):
        EncryptedFrameJournal(
            safe / "link" / "spool.bin",
            {"key-1": b"a" * 32},
            "key-1",
            trusted_root=safe,
        )
