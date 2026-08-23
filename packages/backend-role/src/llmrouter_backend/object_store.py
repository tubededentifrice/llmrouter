"""Router-controlled S3-compatible object storage."""
# ruff: noqa: ANN401

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from llmrouter_backend.control_files import ControlFileError, read_control_file

if TYPE_CHECKING:
    from pathlib import Path

    from llmrouter_backend.config import Settings

_MAX_OBJECT_BYTES = 1024 * 1024 * 1024
_MAX_OBJECT_KEY_BYTES = 1024
_MAX_CONTENT_TYPE_CHARACTERS = 200
_OBJECT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/()'-]*$")
_ASCII_SPACE = 0x20
_ASCII_DELETE = 0x7F


class ObjectStoreError(Exception):
    """Hide deployment and credential details from callers."""


class ObjectNotFoundError(ObjectStoreError):
    """Report an absent or early-evicted object."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """One bounded object-store read result."""

    body: bytes
    content_type: str


class ObjectStore:
    """Use one private bucket without exposing its identifiers."""

    def __init__(self, client: Any, bucket: str) -> None:
        """Keep the SDK client and private bucket internal."""
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls, settings: Settings) -> ObjectStore | None:
        """Create storage only from complete structured deployment controls."""
        if settings.object_store_endpoint is None:
            return None
        if (
            settings.object_store_bucket is None
            or settings.object_store_access_key_file is None
            or settings.object_store_secret_key_file is None
        ):
            raise ObjectStoreError
        access_key = _read_control(settings.object_store_access_key_file, maximum=500)
        secret_key = _read_control(
            settings.object_store_secret_key_file, maximum=10_000
        )
        if settings.object_store_ca_file is not None:
            _validate_control_file(settings.object_store_ca_file, maximum=1_000_000)
        verify: bool | str = (
            str(settings.object_store_ca_file)
            if settings.object_store_ca_file is not None
            else True
        )
        try:
            client = boto3.client(
                "s3",
                endpoint_url=settings.object_store_endpoint,
                region_name=settings.object_store_region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                verify=verify,
                config=Config(
                    connect_timeout=settings.object_store_connect_timeout_seconds,
                    read_timeout=settings.object_store_read_timeout_seconds,
                    retries={"max_attempts": 0},
                    s3={"addressing_style": "path"},
                ),
            )
        except Exception as error:
            raise ObjectStoreError from error
        return cls(client, settings.object_store_bucket)

    def put(self, key: str, body: bytes, content_type: str) -> None:
        """Write one private object without caller-visible storage metadata."""
        _require_object_key(key)
        if (
            not 1 <= len(body) <= _MAX_OBJECT_BYTES
            or not 1 <= len(content_type) <= _MAX_CONTENT_TYPE_CHARACTERS
            or not _valid_content_type(content_type)
        ):
            raise ObjectStoreError
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentLength=len(body),
                ContentType=content_type,
            )
        except Exception as error:
            raise ObjectStoreError from error

    def get(self, key: str, maximum_bytes: int = _MAX_OBJECT_BYTES) -> StoredObject:
        """Read one private object and close the SDK stream."""
        _require_object_key(key)
        if not 1 <= maximum_bytes <= _MAX_OBJECT_BYTES:
            raise ObjectStoreError
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            stream = response["Body"]
            try:
                body = stream.read(maximum_bytes + 1)
            finally:
                stream.close()
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "NoSuchBucket", "404", "NotFound"}:
                raise ObjectNotFoundError from error
            raise ObjectStoreError from error
        except Exception as error:
            raise ObjectStoreError from error
        content_type = response.get("ContentType")
        if (
            not isinstance(body, bytes)
            or len(body) > maximum_bytes
            or not isinstance(content_type, str)
            or not _valid_content_type(content_type)
        ):
            raise ObjectStoreError
        return StoredObject(body=body, content_type=content_type)

    def delete(self, key: str) -> None:
        """Delete one object. S3 deletion is idempotent for an absent key."""
        _require_object_key(key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as error:
            raise ObjectStoreError from error

    def healthy(self) -> bool:
        """Check bucket access without returning deployment details."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:  # noqa: BLE001 - Health never exposes SDK failure details.
            return False
        return True


def _read_control(path: Path, *, maximum: int) -> str:
    raw = _read_control_file(path, maximum=maximum)
    if b"\x00" in raw:
        raise ObjectStoreError
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ObjectStoreError from error
    if not value or any(
        ord(character) <= _ASCII_SPACE or ord(character) == _ASCII_DELETE
        for character in value
    ):
        raise ObjectStoreError
    return value


def _validate_control_file(path: Path, *, maximum: int) -> None:
    _read_control_file(path, maximum=maximum)


def _read_control_file(path: Path, *, maximum: int) -> bytes:
    try:
        return read_control_file(path, maximum=maximum)
    except ControlFileError as error:
        raise ObjectStoreError from error


def _require_object_key(key: str) -> None:
    try:
        encoded = key.encode("ascii")
    except UnicodeEncodeError as error:
        raise ObjectStoreError from error
    segments = key.split("/")
    if (
        not 1 <= len(encoded) <= _MAX_OBJECT_KEY_BYTES
        or _OBJECT_KEY.fullmatch(key) is None
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise ObjectStoreError


def _valid_content_type(value: str) -> bool:
    return bool(
        1 <= len(value) <= _MAX_CONTENT_TYPE_CHARACTERS
        and "/" in value
        and all(_ASCII_SPACE < ord(character) < _ASCII_DELETE for character in value)
    )
