"""Product-neutral S3-compatible object storage boundary and local adapter."""
# ruff: noqa: EM101, PLR2004, TRY003

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from threading import RLock
from typing import Protocol

from .errors import ContentError, ContentErrorCode


class ObjectStore(Protocol):
    """The minimum immutable object operations used by content lifecycle work."""

    def put(self, key: str, value: bytes, *, sha256: str) -> None:
        """Put equal bytes idempotently and reject an identity conflict."""

    def get(self, key: str, *, sha256: str) -> bytes:
        """Get bytes only when their expected checksum matches."""

    def delete(self, key: str, *, sha256: str) -> None:
        """Delete equal bytes idempotently."""


@dataclass
class MemoryObjectStore:
    """A deterministic local adapter for tests and offline conformance."""

    _objects: dict[str, bytes] = field(default_factory=dict, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def put(self, key: str, value: bytes, *, sha256: str) -> None:
        """Store one immutable checksummed object."""
        _require_key_digest(key, sha256)
        actual = hashlib.sha256(value).hexdigest()
        if not hmac.compare_digest(actual, sha256):
            raise ContentError(ContentErrorCode.INTEGRITY, "object-store")
        with self._lock:
            current = self._objects.get(key)
            if current is not None and current != value:
                raise ContentError(ContentErrorCode.CONFLICT, "object-store")
            self._objects[key] = bytes(value)

    def get(self, key: str, *, sha256: str) -> bytes:
        """Return one object after an integrity check."""
        _require_key_digest(key, sha256)
        with self._lock:
            value = self._objects.get(key)
            if value is None:
                raise ContentError(ContentErrorCode.NOT_FOUND, "object-store")
            if not hmac.compare_digest(hashlib.sha256(value).hexdigest(), sha256):
                raise ContentError(ContentErrorCode.INTEGRITY, "object-store")
            return bytes(value)

    def delete(self, key: str, *, sha256: str) -> None:
        """Delete one equal object and accept a repeated delete."""
        _require_key_digest(key, sha256)
        with self._lock:
            value = self._objects.get(key)
            if value is None:
                return
            if not hmac.compare_digest(hashlib.sha256(value).hexdigest(), sha256):
                raise ContentError(ContentErrorCode.INTEGRITY, "object-store")
            del self._objects[key]

    def corrupt_for_test(self, key: str, value: bytes) -> None:
        """Replace bytes so a test can verify integrity failure."""
        with self._lock:
            self._objects[key] = bytes(value)

    def object_count_for_test(self) -> int:
        """Return the object count for deterministic cleanup checks."""
        with self._lock:
            return len(self._objects)


def _require_key_digest(key: str, sha256: str) -> None:
    if not key or len(key) > 1000:
        raise ValueError("An object key is invalid.")
    if len(sha256) != 64 or any(value not in "0123456789abcdef" for value in sha256):
        raise ValueError("An object checksum must be lowercase SHA-256.")
