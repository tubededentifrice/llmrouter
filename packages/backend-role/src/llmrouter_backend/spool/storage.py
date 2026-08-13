"""Authenticated append-only storage for local spool records."""
# ruff: noqa: EM101, PLR2004, SIM105, TC003, TRY003, TRY300, TRY301

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import stat
import struct
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

_MAGIC = b"LRSP"
_VERSION = 1
_HEADER = struct.Struct(">4sBHI24s")
_STATE_VERSION = 1
_STATE_DOMAIN = b"llmrouter-spool-state-v1\x00"
_STATE_KEY_DOMAIN = b"llmrouter-spool-state-mac-key-v1\x00"


class SpoolStorageError(RuntimeError):
    """A safe local spool storage failure."""


class EncryptedFrameJournal:
    """Append JSON records as independently authenticated encrypted frames."""

    def __init__(
        self,
        path: Path,
        keys: Mapping[str, bytes],
        current_key_id: str,
        *,
        trusted_root: Path,
    ) -> None:
        """Use an explicit key ring and process-owned trusted root."""
        if current_key_id not in keys:
            raise ValueError("The current spool key is not in the key ring.")
        if not keys or any(len(key) != 32 for key in keys.values()):
            raise ValueError("Each spool encryption key must contain 32 bytes.")
        if any(not key_id or len(key_id.encode()) > 65535 for key_id in keys):
            raise ValueError("A spool key identity has an invalid length.")
        path = path.absolute()
        self._directory_descriptor = self._prepare_directory(
            path.parent, trusted_root.absolute()
        )
        self._path = path
        self._lock_path = path.with_name(f"{path.name}.lock")
        self._owner_path = path.with_name(f"{path.name}.owner")
        self._state_path = path.with_name(f"{path.name}.state")
        self._thread_lock = threading.RLock()
        self._keys = dict(keys)
        self._current_key_id = current_key_id
        self._committed_length = 0
        self._committed_digest = hashlib.sha256().hexdigest()
        self._committed_hasher = hashlib.sha256()
        created = not self._entry_exists(path.name)
        for entry in (self._path, self._lock_path, self._owner_path):
            descriptor = self._open(entry, os.O_RDWR | os.O_CREAT)
            os.close(descriptor)
        if created:
            if self._entry_exists(self._state_path.name):
                raise SpoolStorageError("The new local spool has stale commit state.")
            self._write_committed_state(b"")
            os.fsync(self._directory_descriptor)
        elif not self._entry_exists(self._state_path.name):
            raise SpoolStorageError("The local spool commit state is missing.")
        with self._locked():
            self._set_cache(self._recover_storage())

    @property
    def storage_identity(self) -> tuple[int, int]:
        """Return the device and inode that identify this journal."""
        descriptor = self._open(self._path, os.O_RDONLY)
        try:
            status = os.fstat(descriptor)
            return status.st_dev, status.st_ino
        finally:
            os.close(descriptor)

    @property
    def size_bytes(self) -> int:
        """Return the current committed journal byte size."""
        with self._locked():
            self._verify_cached_size()
            return self._committed_length

    def append(self, record: Mapping[str, Any]) -> None:
        """Append and sync one complete encrypted frame."""
        self._append_frames([self._frame(record)])

    def append_many(self, records: list[Mapping[str, Any]]) -> int:
        """Append and sync a group of independently recoverable frames."""
        frames = [self._frame(record) for record in records]
        self._append_frames(frames)
        return sum(len(frame) for frame in frames)

    def encoded_size(self, record: Mapping[str, Any]) -> int:
        """Return the exact encoded frame size without exposing plaintext."""
        return len(self._frame(record))

    def acquire_owner(self) -> None:
        """Hold exclusive logical and process ownership for the journal lifetime."""
        if hasattr(self, "_owner_descriptor"):
            raise SpoolStorageError("Another local spool already owns this journal.")
        descriptor = self._open(self._owner_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            raise SpoolStorageError(
                "Another process already owns the local spool."
            ) from error
        self._owner_descriptor = descriptor

    def close_owner(self) -> None:
        """Release exclusive process ownership of this journal."""
        descriptor = getattr(self, "_owner_descriptor", None)
        if descriptor is None:
            return
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        del self._owner_descriptor

    def holds_owner(self) -> bool:
        """Return true while this object holds the process owner lock."""
        return hasattr(self, "_owner_descriptor")

    def close(self) -> None:
        """Close owner and trusted-directory descriptors."""
        self.close_owner()
        descriptor = getattr(self, "_directory_descriptor", None)
        if descriptor is not None:
            os.close(descriptor)
            del self._directory_descriptor

    def __del__(self) -> None:
        """Best-effort release of local descriptors."""
        with suppress(Exception):
            self.close()

    def compact(self, records: Sequence[Mapping[str, Any]]) -> None:
        """Atomically replace released frames with the live responsibility set."""
        self._require_owner()
        replacement = b"".join(self._frame(record) for record in records)
        temporary = self._path.with_name(
            f".{self._path.name}.{os.urandom(8).hex()}.tmp"
        )
        with self._locked():
            self._verify_cached_size()
            replacement_digest = self._digest(replacement)
            self._write_intent_metadata("replace", len(replacement), replacement_digest)
            try:
                descriptor = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    self._write_complete(descriptor, replacement)
                    os.fdatasync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(
                    temporary.name,
                    self._path.name,
                    src_dir_fd=self._directory_descriptor,
                    dst_dir_fd=self._directory_descriptor,
                )
                os.fsync(self._directory_descriptor)
                self._write_committed_state(replacement)
                self._set_cache(replacement)
            except (OSError, SpoolStorageError) as error:
                with suppress(OSError):
                    os.unlink(temporary.name, dir_fd=self._directory_descriptor)
                raise SpoolStorageError("The local spool compaction failed.") from error

    def read_all(self, *, repair_partial_tail: bool = True) -> list[dict[str, Any]]:
        """Read committed frames and repair only authenticated uncommitted bytes."""
        del repair_partial_tail
        with self._locked():
            return self._decode(self._recover_storage())

    def _frame(self, record: Mapping[str, Any]) -> bytes:
        plaintext = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        key_id = self._current_key_id.encode()
        nonce = os.urandom(24)
        header = _HEADER.pack(_MAGIC, _VERSION, len(key_id), len(plaintext) + 16, nonce)
        aad = header + key_id
        ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(
            plaintext, aad, nonce, self._keys[self._current_key_id]
        )
        return aad + ciphertext

    def _append_frames(self, frames: Sequence[bytes]) -> None:
        self._require_owner()
        addition = b"".join(frames)
        with self._locked():
            self._verify_cached_size()
            complete_length = self._committed_length + len(addition)
            complete_hasher = self._committed_hasher.copy()
            complete_hasher.update(addition)
            complete_digest = complete_hasher.hexdigest()
            self._write_intent_metadata("append", complete_length, complete_digest)
            descriptor = self._open(self._path, os.O_WRONLY | os.O_APPEND)
            synced = False
            try:
                self._write_complete(descriptor, addition)
                os.fdatasync(descriptor)
                synced = True
                self._write_committed_metadata(complete_length, complete_digest)
                self._committed_length = complete_length
                self._committed_digest = complete_digest
                self._committed_hasher = complete_hasher
            except (OSError, SpoolStorageError) as error:
                if not synced:
                    self._truncate(self._committed_length)
                    self._write_committed_metadata(
                        self._committed_length, self._committed_digest
                    )
                else:
                    recovered = self._recover_storage()
                    if (
                        len(recovered) == complete_length
                        and self._digest(recovered) == complete_digest
                    ):
                        self._set_cache(recovered)
                        return
                raise SpoolStorageError("The local spool write failed.") from error
            finally:
                os.close(descriptor)

    @staticmethod
    def _write_complete(descriptor: int, value: bytes) -> None:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("The spool write did not make progress.")
            view = view[written:]

    def _decode(self, data: bytes) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        while offset < len(data):
            frame_start = offset
            if len(data) - offset < _HEADER.size:
                raise SpoolStorageError(
                    "The committed local spool frame is incomplete."
                )
            magic, version, key_length, cipher_length, nonce = _HEADER.unpack_from(
                data, offset
            )
            if (
                magic != _MAGIC
                or version != _VERSION
                or key_length == 0
                or cipher_length < 16
            ):
                raise SpoolStorageError("The local spool frame is invalid.")
            offset += _HEADER.size
            frame_end = offset + key_length + cipher_length
            if frame_end > len(data):
                raise SpoolStorageError(
                    "The committed local spool frame is incomplete."
                )
            key_id_bytes = data[offset : offset + key_length]
            offset += key_length
            ciphertext = data[offset:frame_end]
            offset = frame_end
            try:
                key_id = key_id_bytes.decode()
            except UnicodeDecodeError as error:
                raise SpoolStorageError(
                    "The local spool key identity is invalid."
                ) from error
            key = self._keys.get(key_id)
            if key is None:
                raise SpoolStorageError("A required local spool key is not available.")
            header = data[frame_start : frame_start + _HEADER.size]
            try:
                plaintext = crypto_aead_xchacha20poly1305_ietf_decrypt(
                    ciphertext, header + key_id_bytes, nonce, key
                )
                value = json.loads(plaintext)
            except (CryptoError, json.JSONDecodeError, UnicodeDecodeError) as error:
                raise SpoolStorageError(
                    "The local spool frame failed integrity checks."
                ) from error
            if not isinstance(value, dict):
                raise SpoolStorageError("The local spool record is invalid.")
            records.append(value)
        return records

    def _recover_storage(self) -> bytes:
        data = self._read_file(self._path)
        state = self._read_state()
        status = state.get("status")
        old_length = self._state_integer(state, "old_length")
        old_digest = str(state.get("old_sha256", ""))
        if status == "committed":
            return self._recover_committed(data, old_length, old_digest)
        if status != "intent" or state.get("operation") not in ("append", "replace"):
            raise SpoolStorageError("The local spool commit state is invalid.")
        new_length = self._state_integer(state, "new_length")
        new_digest = str(state.get("new_sha256", ""))
        if len(data) == new_length and self._digest(data) == new_digest:
            self._write_committed_state(data)
            return data
        if state["operation"] == "append" and len(data) >= old_length:
            old = data[:old_length]
            if self._digest(old) == old_digest and len(data) < new_length:
                self._truncate(old_length)
                self._write_committed_state(old)
                return old
        if len(data) == old_length and self._digest(data) == old_digest:
            self._write_committed_state(data)
            return data
        raise SpoolStorageError("The local spool commit state failed integrity checks.")

    def _recover_committed(self, data: bytes, length: int, digest: str) -> bytes:
        if len(data) < length or self._digest(data[:length]) != digest:
            raise SpoolStorageError(
                "The local spool commit state failed integrity checks."
            )
        if len(data) > length:
            self._truncate(length)
        return data[:length]

    def _truncate(self, length: int) -> None:
        descriptor = self._open(self._path, os.O_WRONLY)
        try:
            os.ftruncate(descriptor, length)
            os.fdatasync(descriptor)
        finally:
            os.close(descriptor)

    def _write_committed_state(self, data: bytes) -> None:
        self._write_committed_metadata(len(data), self._digest(data))

    def _write_committed_metadata(self, length: int, digest: str) -> None:
        self._write_state(
            {
                "version": _STATE_VERSION,
                "status": "committed",
                "operation": "none",
                "old_length": length,
                "old_sha256": digest,
                "new_length": length,
                "new_sha256": digest,
            }
        )

    def _write_intent_state(
        self, operation: str, previous: bytes, complete: bytes
    ) -> None:
        self._write_intent_metadata(
            operation,
            len(complete),
            self._digest(complete),
            old_length=len(previous),
            old_digest=self._digest(previous),
        )

    def _write_intent_metadata(
        self,
        operation: str,
        new_length: int,
        new_digest: str,
        *,
        old_length: int | None = None,
        old_digest: str | None = None,
    ) -> None:
        self._write_state(
            {
                "version": _STATE_VERSION,
                "status": "intent",
                "operation": operation,
                "old_length": self._committed_length
                if old_length is None
                else old_length,
                "old_sha256": self._committed_digest
                if old_digest is None
                else old_digest,
                "new_length": new_length,
                "new_sha256": new_digest,
            }
        )

    def _write_state(self, body: Mapping[str, Any]) -> None:
        signed = dict(body)
        signed["key_id"] = self._current_key_id
        encoded = self._canonical(signed)
        envelope = dict(signed)
        envelope["mac"] = hmac.new(
            self._state_key(self._keys[self._current_key_id]),
            _STATE_DOMAIN + encoded,
            hashlib.sha256,
        ).hexdigest()
        value = self._canonical(envelope)
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{os.urandom(8).hex()}.tmp"
        )
        try:
            descriptor = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                self._write_complete(descriptor, value)
                os.fdatasync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary.name,
                self._state_path.name,
                src_dir_fd=self._directory_descriptor,
                dst_dir_fd=self._directory_descriptor,
            )
            os.fsync(self._directory_descriptor)
        except OSError as error:
            with suppress(OSError):
                os.unlink(temporary.name, dir_fd=self._directory_descriptor)
            raise SpoolStorageError(
                "The local spool commit state write failed."
            ) from error

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._read_file(self._state_path))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SpoolStorageError(
                "The local spool commit state is invalid."
            ) from error
        if not isinstance(value, dict):
            raise SpoolStorageError("The local spool commit state is invalid.")
        mac = value.pop("mac", None)
        key_id = value.get("key_id")
        key = self._keys.get(key_id) if isinstance(key_id, str) else None
        if key is None or not isinstance(mac, str):
            raise SpoolStorageError("The local spool commit key is not available.")
        expected = hmac.new(
            self._state_key(key),
            _STATE_DOMAIN + self._canonical(value),
            hashlib.sha256,
        ).hexdigest()
        if (
            not hmac.compare_digest(mac, expected)
            or value.get("version") != _STATE_VERSION
        ):
            raise SpoolStorageError(
                "The local spool commit state failed integrity checks."
            )
        return value

    def _read_file(self, path: Path) -> bytes:
        descriptor = self._open(path, os.O_RDONLY)
        try:
            size = os.fstat(descriptor).st_size
            chunks: list[bytes] = []
            offset = 0
            while offset < size:
                chunk = os.pread(descriptor, size - offset, offset)
                if not chunk:
                    raise SpoolStorageError("The local spool read was incomplete.")
                chunks.append(chunk)
                offset += len(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _digest(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def _set_cache(self, data: bytes) -> None:
        self._committed_length = len(data)
        self._committed_hasher = hashlib.sha256(data)
        self._committed_digest = self._committed_hasher.hexdigest()

    def _verify_cached_size(self) -> None:
        descriptor = self._open(self._path, os.O_RDONLY)
        try:
            if os.fstat(descriptor).st_size != self._committed_length:
                raise SpoolStorageError(
                    "The local spool changed outside its logical owner."
                )
        finally:
            os.close(descriptor)

    def _require_owner(self) -> None:
        if not self.holds_owner():
            raise SpoolStorageError("Local spool mutation requires its logical owner.")

    @staticmethod
    def _state_key(encryption_key: bytes) -> bytes:
        return hmac.new(encryption_key, _STATE_KEY_DOMAIN, hashlib.sha256).digest()

    @staticmethod
    def _canonical(value: Mapping[str, Any]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _state_integer(state: Mapping[str, Any], name: str) -> int:
        value = state.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SpoolStorageError("The local spool commit state is invalid.")
        return value

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            descriptor = self._open(self._lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _open(self, path: Path, flags: int) -> int:
        directory_descriptor = getattr(self, "_directory_descriptor", None)
        if directory_descriptor is None:
            raise SpoolStorageError("The local spool journal is closed.")
        try:
            descriptor = os.open(
                path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory_descriptor
            )
        except OSError as error:
            raise SpoolStorageError("The local spool path is not safe.") from error
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_mode & 0o022
            or status.st_nlink != 1
        ):
            os.close(descriptor)
            raise SpoolStorageError("The local spool file is not private.")
        return descriptor

    def _entry_exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _prepare_directory(directory: Path, trusted_root: Path) -> int:
        try:
            relative = directory.relative_to(trusted_root)
        except ValueError as error:
            raise SpoolStorageError(
                "The local spool path is outside its trusted root."
            ) from error
        try:
            descriptor = os.open(
                trusted_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as error:
            raise SpoolStorageError(
                "The local spool trusted root is not safe."
            ) from error
        try:
            EncryptedFrameJournal._validate_directory(descriptor)
            for component in relative.parts:
                if component in ("", ".", ".."):
                    raise SpoolStorageError("The local spool directory is not safe.")
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                EncryptedFrameJournal._validate_directory(child)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except (OSError, SpoolStorageError) as error:
            os.close(descriptor)
            if isinstance(error, SpoolStorageError):
                raise
            raise SpoolStorageError("The local spool directory is not safe.") from error

    @staticmethod
    def _validate_directory(descriptor: int) -> None:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_mode & 0o022
        ):
            raise SpoolStorageError("The local spool directory is not private.")
