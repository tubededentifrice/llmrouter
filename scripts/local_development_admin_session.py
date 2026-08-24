"""Create one short-lived administrator session for localhost browser tests."""
# ruff: noqa: EM101, INP001, TRY003

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import psycopg
from llmrouter_backend.config import Settings
from llmrouter_backend.control_files import ControlFileError, read_control_file
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import (
    create_administrator_session,
    delete_administrator_session,
)
from opendle import validate_canonical_token

if TYPE_CHECKING:
    from psycopg import Connection

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STATE_DIRECTORY = REPOSITORY_ROOT / ".local-development"
SESSION_FILE_NAME = "test-administrator-session.json"
ADMIN_ORIGIN = "http://127.0.0.1:5174"
SESSION_MINUTES = 15
MAXIMUM_SESSION_FILE_BYTES = 4_096
MAXIMUM_PASSWORD_BYTES = 500
MINIMUM_TOKEN_LENGTH = 40
MAXIMUM_TOKEN_LENGTH = 500
SESSION_FILE_MODE = 0o600
EXPECTED_ARGUMENT_COUNT = 2
_SESSION_FIELDS = {
    "cookie_name",
    "cookie_value",
    "csrf_token",
    "expires_at",
    "origin",
}


@dataclass(frozen=True, slots=True)
class DevelopmentAdministratorSession:
    """Local browser controls that never enter Git or command output."""

    cookie_value: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


def read_development_administrator_session() -> DevelopmentAdministratorSession:
    """Read and validate the current unexpired localhost test session."""
    document, _identity = _read_session_document()
    if set(document) != _SESSION_FIELDS:
        raise ValueError("The development administrator session file is invalid.")
    if (
        document.get("cookie_name") != "llmrouter_admin_session"
        or document.get("origin") != ADMIN_ORIGIN
    ):
        raise ValueError("The development administrator session file is invalid.")
    cookie_value = document.get("cookie_value")
    csrf_token = document.get("csrf_token")
    raw_expiry = document.get("expires_at")
    if (
        not isinstance(cookie_value, str)
        or not isinstance(csrf_token, str)
        or not isinstance(raw_expiry, str)
    ):
        raise ValueError(  # noqa: TRY004 - One safe validation error for callers.
            "The development administrator session file is invalid."
        )
    try:
        expires_at = datetime.fromisoformat(raw_expiry)
    except ValueError:
        raise ValueError(
            "The development administrator session file is invalid."
        ) from None
    if expires_at.tzinfo is None or expires_at <= datetime.now(tz=UTC):
        raise ValueError("The development administrator session file is invalid.")
    _validate_token(cookie_value)
    _validate_token(csrf_token)
    return DevelopmentAdministratorSession(
        cookie_value=cookie_value,
        csrf_token=csrf_token,
        expires_at=expires_at,
    )


def create() -> None:
    """Replace the localhost test session without printing its controls."""
    controls = _control_keys()
    existing: DevelopmentAdministratorSession | None = None
    existing_identity: _FileIdentity | None = None
    try:
        existing_document, existing_identity = _read_session_document()
        existing = _session_from_document(existing_document)
    except FileNotFoundError:
        pass

    session_token = new_token()
    csrf_token = new_token()
    expires_at = datetime.now(tz=UTC) + timedelta(minutes=SESSION_MINUTES)
    document = {
        "cookie_name": "llmrouter_admin_session",
        "cookie_value": session_token,
        "csrf_token": csrf_token,
        "expires_at": expires_at.isoformat(),
        "origin": ADMIN_ORIGIN,
    }
    temporary_name = _write_temporary_document(document)
    committed = False
    try:
        with _connect() as connection:
            _set_transaction_limits(connection)
            if existing is not None:
                delete_administrator_session(
                    connection, controls.verifier(existing.cookie_value)
                )
            create_administrator_session(
                connection,
                session_verifier=controls.verifier(session_token),
                csrf_verifier=controls.verifier(csrf_token),
                encrypted_csrf_token=controls.encrypt({"csrf_token": csrf_token}),
                issuer="https://local-development.invalid",
                subject="local-development-test-administrator",
                display_name="Local development test administrator",
                expires_at=expires_at,
            )
            connection.commit()
            committed = True
        _install_temporary_document(temporary_name, existing_identity)
    except Exception:
        if committed:
            _revoke_token(controls, session_token)
        with suppress(Exception):
            _remove_temporary_document(temporary_name)
        raise
    print(
        "Created a 15-minute localhost administrator session in "
        ".local-development/test-administrator-session.json."
    )


def clear() -> None:
    """Revoke and remove the current localhost test session."""
    try:
        document, identity = _read_session_document()
    except FileNotFoundError:
        print("No localhost administrator test session is active.")
        return
    session = _session_from_document(document)
    controls = _control_keys()
    with _connect() as connection:
        _set_transaction_limits(connection)
        delete_administrator_session(
            connection, controls.verifier(session.cookie_value)
        )
        connection.commit()
    _remove_matching_session_file(identity)
    print("Revoked the localhost administrator test session.")


def _session_from_document(
    document: dict[str, object],
) -> DevelopmentAdministratorSession:
    """Validate a document without requiring that it is still current."""
    if set(document) != _SESSION_FIELDS:
        raise ValueError("The development administrator session file is invalid.")
    cookie_value = document.get("cookie_value")
    csrf_token = document.get("csrf_token")
    raw_expiry = document.get("expires_at")
    if (
        document.get("cookie_name") != "llmrouter_admin_session"
        or document.get("origin") != ADMIN_ORIGIN
        or not isinstance(cookie_value, str)
        or not isinstance(csrf_token, str)
        or not isinstance(raw_expiry, str)
    ):
        raise ValueError("The development administrator session file is invalid.")
    _validate_token(cookie_value)
    _validate_token(csrf_token)
    try:
        expires_at = datetime.fromisoformat(raw_expiry)
    except ValueError:
        raise ValueError(
            "The development administrator session file is invalid."
        ) from None
    if expires_at.tzinfo is None:
        raise ValueError("The development administrator session file is invalid.")
    return DevelopmentAdministratorSession(cookie_value, csrf_token, expires_at)


def _control_keys() -> ControlKeys:
    """Load only the two production session control keys."""
    return ControlKeys.load(
        Settings(
            administrator_digest_key_file=STATE_DIRECTORY / "administrator-digest-key",
            administrator_encryption_key_file=STATE_DIRECTORY
            / "administrator-encryption-key",
            allowed_origins=(ADMIN_ORIGIN,),
        )
    )


def _validate_token(value: str) -> None:
    """Require the exact high-entropy token grammar used by production."""
    if not MINIMUM_TOKEN_LENGTH <= len(value) <= MAXIMUM_TOKEN_LENGTH:
        raise ValueError("The development administrator session file is invalid.")
    try:
        validate_canonical_token(value)
    except ValueError:
        raise ValueError(
            "The development administrator session file is invalid."
        ) from None


def _connect() -> Connection[tuple[object, ...]]:
    """Open one bounded loopback database connection."""
    password = read_control_file(
        STATE_DIRECTORY / "postgres-password", maximum=MAXIMUM_PASSWORD_BYTES
    ).decode("utf-8")
    password = password.strip()
    if not password or any(character.isspace() for character in password):
        raise ValueError("The local PostgreSQL control file is invalid.")
    database_url = (
        f"postgresql://llmrouter:{quote(password, safe='')}@127.0.0.1:5434/llmrouter"
    )
    return psycopg.connect(database_url, connect_timeout=2)


def _set_transaction_limits(connection: Connection[object]) -> None:
    """Bound all session fixture database work."""
    connection.execute("SET LOCAL statement_timeout = '2s'")
    connection.execute("SET LOCAL lock_timeout = '500ms'")


def _revoke_token(controls: ControlKeys, session_token: str) -> None:
    """Best-effort cleanup after a file installation failure."""
    try:
        with _connect() as connection:
            _set_transaction_limits(connection)
            delete_administrator_session(connection, controls.verifier(session_token))
            connection.commit()
    except ControlFileError, OSError, UnicodeError, ValueError, psycopg.Error:
        pass


def _open_state_directory() -> int:
    """Open the private ignored state directory without following a link."""
    descriptor = os.open(
        STATE_DIRECTORY,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(descriptor)
        raise ValueError("The local development state directory is unsafe.")
    return descriptor


def _read_session_document() -> tuple[dict[str, object], _FileIdentity]:
    """Read one bounded regular session file from the private directory."""
    directory = _open_state_directory()
    descriptor = -1
    try:
        descriptor = os.open(
            SESSION_FILE_NAME,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != SESSION_FILE_MODE
        ):
            raise ValueError("The development administrator session file is unsafe.")
        raw = os.read(descriptor, MAXIMUM_SESSION_FILE_BYTES + 1)
        if not raw or len(raw) > MAXIMUM_SESSION_FILE_BYTES:
            raise ValueError("The development administrator session file is invalid.")
        if os.read(descriptor, 1):
            raise ValueError("The development administrator session file is invalid.")
        current = os.stat(SESSION_FILE_NAME, dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("The development administrator session file changed.")
        document = json.loads(raw, object_pairs_hook=_closed_document)
        if not isinstance(document, dict):
            raise TypeError("The development administrator session file is invalid.")
        return document, _FileIdentity(metadata.st_dev, metadata.st_ino)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def _write_temporary_document(document: dict[str, str]) -> str:
    """Write one bounded private document without replacing the active file."""
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(raw) > MAXIMUM_SESSION_FILE_BYTES:
        raise ValueError("The development administrator session file is too large.")
    directory = _open_state_directory()
    name = f".{SESSION_FILE_NAME}.{secrets.token_hex(16)}"
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            SESSION_FILE_MODE,
            dir_fd=directory,
        )
        _write_all(descriptor, raw)
        os.fsync(descriptor)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory)
        raise
    else:
        return name
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def _install_temporary_document(
    temporary_name: str, existing_identity: _FileIdentity | None
) -> None:
    """Install the committed session only if the prior file did not change."""
    directory = _open_state_directory()
    try:
        try:
            current = os.stat(
                SESSION_FILE_NAME, dir_fd=directory, follow_symlinks=False
            )
        except FileNotFoundError:
            if existing_identity is not None:
                raise ValueError(
                    "The development administrator session file changed."
                ) from None
        else:
            if (
                existing_identity is None
                or (current.st_dev, current.st_ino)
                != (existing_identity.device, existing_identity.inode)
                or not _safe_session_metadata(current)
            ):
                raise ValueError("The development administrator session file changed.")
        os.replace(
            temporary_name,
            SESSION_FILE_NAME,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_all(descriptor: int, raw: bytes) -> None:
    """Write all private control bytes or fail without an incomplete success."""
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("The development administrator session write stopped.")
        view = view[written:]


def _remove_temporary_document(temporary_name: str) -> None:
    """Remove a private temporary file after a failed create operation."""
    directory = _open_state_directory()
    try:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory)
    finally:
        os.close(directory)


def _remove_matching_session_file(identity: _FileIdentity) -> None:
    """Remove only the exact file that supplied the revoked session."""
    directory = _open_state_directory()
    try:
        current = os.stat(SESSION_FILE_NAME, dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
            identity.device,
            identity.inode,
        ) or not _safe_session_metadata(current):
            raise ValueError("The development administrator session file changed.")
        os.unlink(SESSION_FILE_NAME, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def _closed_document(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON controls before the file becomes trusted."""
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("The development administrator session file is invalid.")
        document[key] = value
    return document


def _safe_session_metadata(metadata: os.stat_result) -> bool:
    """Return true only for the exact private regular-file boundary."""
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == SESSION_FILE_MODE
    )


def main() -> None:
    """Run the fixed create or clear operation with safe failures."""
    if len(sys.argv) != EXPECTED_ARGUMENT_COUNT or sys.argv[1] not in {
        "create",
        "clear",
    }:
        raise SystemExit(
            "Usage: uv run python scripts/local_development_admin_session.py "
            "{create|clear}"
        )
    try:
        if sys.argv[1] == "create":
            create()
        else:
            clear()
    except (
        ControlFileError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        psycopg.Error,
    ):
        raise SystemExit(
            "The localhost administrator test session operation failed safely."
        ) from None


if __name__ == "__main__":
    main()
