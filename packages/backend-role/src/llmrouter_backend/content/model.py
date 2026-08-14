"""Closed values for content capture, retention, and protected exports."""
# ruff: noqa: D105, EM101, PLR2004, TRY003, UP037

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | Mapping[str, "JsonValue"] | Sequence["JsonValue"]


class CapturePolicy(StrEnum):
    """Accepted capture values in increasing content detail order."""

    DISABLED = "disabled"
    METADATA_ONLY = "metadata_only"
    COMPLETE = "complete"


class CaptureReason(StrEnum):
    """The admission-time source of one effective capture value."""

    CONFIGURED = "configured"
    SPOOL_PRESSURE = "spool_pressure"


class RetentionDataClass(StrEnum):
    """Exact public retention data-class names."""

    DIAGNOSTIC_LOGS = "diagnostic_logs"
    CAPTURED_CONTENT = "captured_content"
    RAW_ACCOUNTING = "raw_accounting"
    AGENT_TOOL_AUDIT = "agent_tool_audit"
    DAILY_ACCOUNTING = "daily_accounting"
    SECURITY_AUDIT = "security_audit"
    CONFIGURATION_REVISIONS = "configuration_revisions"


MAXIMUM_PREVIEW_EFFECTS = 7
MAXIMUM_DISCOVERY_ITEMS = 1000
MAXIMUM_EXPORT_AGE = timedelta(days=1)
MAXIMUM_REDEMPTION_AGE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class EffectiveCapture:
    """One immutable admission-time capture decision."""

    policy: CapturePolicy
    reason: CaptureReason
    source_layer: str
    expires_at: datetime | None

    def __post_init__(self) -> None:
        if not self.source_layer:
            raise ValueError("The capture source layer must not be empty.")
        if self.reason is CaptureReason.SPOOL_PRESSURE and (
            self.policy is not CapturePolicy.DISABLED
        ):
            raise ValueError("Spool pressure can only disable capture.")
        if (self.policy is CapturePolicy.DISABLED) != (self.expires_at is None):
            raise ValueError("The capture policy and expiry do not match.")


@dataclass(frozen=True, slots=True)
class RetentionSelection:
    """One configured retention value."""

    data_class: RetentionDataClass
    days: int
    minimum_count: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.days <= 36500:
            raise ValueError("Retention days are outside the public limits.")
        is_revision = self.data_class is RetentionDataClass.CONFIGURATION_REVISIONS
        if is_revision != (self.minimum_count is not None):
            raise ValueError("Revision retention needs both count and days.")
        if self.minimum_count is not None and not 1 <= self.minimum_count <= 1000000:
            raise ValueError("The revision count is outside the public limits.")
        if self.data_class is RetentionDataClass.AGENT_TOOL_AUDIT and not (
            7 <= self.days <= 365
        ):
            raise ValueError("Agent and tool audit retention must be 7 to 365 days.")


@dataclass(frozen=True, slots=True)
class RetentionLimit:
    """One global allowed retention range."""

    data_class: RetentionDataClass
    minimum_days: int
    maximum_days: int
    allowed_minimum_count: int | None = None
    allowed_maximum_count: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.minimum_days <= self.maximum_days <= 36500:
            raise ValueError("The retention day limits are invalid.")
        is_revision = self.data_class is RetentionDataClass.CONFIGURATION_REVISIONS
        has_counts = (
            self.allowed_minimum_count is not None
            and self.allowed_maximum_count is not None
        )
        if is_revision != has_counts:
            raise ValueError("Only revision retention has count limits.")
        if has_counts:
            minimum_count = cast("int", self.allowed_minimum_count)
            maximum_count = cast("int", self.allowed_maximum_count)
            if not 1 <= minimum_count <= maximum_count <= 1000000:
                raise ValueError("The revision count limits are invalid.")
        if self.data_class is RetentionDataClass.AGENT_TOOL_AUDIT and (
            self.minimum_days < 7 or self.maximum_days > 365
        ):
            raise ValueError("Agent and tool audit limits must stay in 7 to 365 days.")

    def permits(self, selection: RetentionSelection) -> bool:
        """Return true when one selection is in all effective limits."""
        if selection.data_class is not self.data_class:
            return False
        if not self.minimum_days <= selection.days <= self.maximum_days:
            return False
        if self.data_class is not RetentionDataClass.CONFIGURATION_REVISIONS:
            return selection.minimum_count is None
        count = selection.minimum_count
        return bool(
            count is not None
            and self.allowed_minimum_count is not None
            and self.allowed_maximum_count is not None
            and self.allowed_minimum_count <= count <= self.allowed_maximum_count
        )


@dataclass(frozen=True, slots=True)
class RetentionEffect:
    """One bounded retention preview effect."""

    data_class: RetentionDataClass
    direction: str
    estimated_records: int
    estimated_bytes: int
    evidence: str = "stored_rows"


@dataclass(frozen=True, slots=True)
class RetentionPreview:
    """One short-lived, bounded retention preview."""

    preview_id: str
    revision: str
    expires_at: datetime
    effects: tuple[RetentionEffect, ...]

    def __post_init__(self) -> None:
        if not self.preview_id or not self.revision:
            raise ValueError("A retention preview needs identities.")
        if len(self.effects) > MAXIMUM_PREVIEW_EFFECTS:
            raise ValueError("A retention preview has too many effects.")


@dataclass(frozen=True, slots=True)
class CapturedContentMetadata:
    """Metadata that does not contain a captured value."""

    content_id: str
    service_id: str
    workspace_id: str | None
    request_id: str
    capture_policy: CapturePolicy
    expires_at: datetime
    content_type: str


@dataclass(frozen=True, slots=True)
class CapturedContent:
    """One protected decrypted captured-content record."""

    metadata: CapturedContentMetadata
    value: JsonValue = field(repr=False)


@dataclass(frozen=True, slots=True)
class ObjectSegment:
    """One checksummed encrypted object segment."""

    ordinal: int
    object_key: str
    ciphertext_bytes: int
    ciphertext_sha256: str
    encrypted_data_key: bytes = field(repr=False)
    wrapping_key_id: str


@dataclass(frozen=True, slots=True)
class ObjectManifest:
    """One deterministic manifest for ordered encrypted object segments."""

    manifest_id: str
    segments: tuple[ObjectSegment, ...]
    sha256: str

    @classmethod
    def build(
        cls, manifest_id: str, segments: tuple[ObjectSegment, ...]
    ) -> "ObjectManifest":
        """Build one manifest digest without encryption key bytes."""
        document = {
            "manifest_id": manifest_id,
            "manifest_version": 1,
            "segments": [
                {
                    "ciphertext_bytes": item.ciphertext_bytes,
                    "ciphertext_sha256": item.ciphertext_sha256,
                    "encrypted_data_key_sha256": hashlib.sha256(
                        item.encrypted_data_key
                    ).hexdigest(),
                    "object_key": item.object_key,
                    "ordinal": item.ordinal,
                    "wrapping_key_id": item.wrapping_key_id,
                }
                for item in segments
            ],
        }
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(manifest_id, segments, digest)

    def __post_init__(self) -> None:
        if not self.manifest_id or not self.segments:
            raise ValueError("An object manifest needs an identity and segments.")
        if tuple(item.ordinal for item in self.segments) != tuple(
            range(1, len(self.segments) + 1)
        ):
            raise ValueError("Object manifest segment ordinals must be contiguous.")
        if len(self.sha256) != 64:
            raise ValueError("An object manifest needs a SHA-256 digest.")


class ExportDataClass(StrEnum):
    """Accepted export classes from the public contract."""

    ACCOUNTING = "accounting"
    AUDIT = "audit"
    CONFIGURATION = "configuration"
    CAPTURED_CONTENT = "captured_content"


class ExportState(StrEnum):
    """Protected export lifecycle states."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ExportRequest:
    """One bounded export request."""

    data_class: ExportDataClass
    range_start: datetime
    range_end: datetime
    export_format: str
    service_id: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if self.range_start.tzinfo is None or self.range_end.tzinfo is None:
            raise ValueError("Export times must include a time zone.")
        if not self.range_start < self.range_end:
            raise ValueError("The export time range is not ordered.")
        if self.range_end - self.range_start > MAXIMUM_EXPORT_AGE:
            raise ValueError("The export time range exceeds one day.")
        if self.export_format not in {"jsonl", "csv"}:
            raise ValueError("The export format is not supported.")
        if self.workspace_id is not None and self.service_id is None:
            raise ValueError("A workspace export needs a service identity.")

    def fingerprint(self) -> bytes:
        """Return one stable idempotency fingerprint."""
        document = {
            "data_class": self.data_class.value,
            "export_format": self.export_format,
            "range_end": self.range_end.isoformat(),
            "range_start": self.range_start.isoformat(),
            "service_id": self.service_id,
            "workspace_id": self.workspace_id,
        }
        return hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).digest()


@dataclass(frozen=True, slots=True)
class ExportOperation:
    """One public protected-export operation state."""

    operation_id: str
    state: ExportState
    created_at: datetime
    expires_at: datetime
    redemption_path: str | None = None
    redemption_token: str | None = field(default=None, repr=False)
    redemption_expires_at: datetime | None = None
    sha256: str | None = None
    safe_error: str | None = None


@dataclass(frozen=True, slots=True)
class RedeemedExport:
    """Protected bytes and mandatory response controls."""

    value: bytes = field(repr=False)
    cache_control: str = "no-store"
    referrer_policy: str = "no-referrer"


@dataclass(frozen=True, slots=True)
class LifecycleLease:
    """One fenced content lifecycle job lease."""

    job_id: str
    job_kind: str
    scope_key: str
    payload: dict[str, object]
    owner_node_id: str
    generation: int
    expires_at: datetime
