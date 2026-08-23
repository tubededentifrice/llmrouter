"""Router-controlled S3-compatible object storage."""
# ruff: noqa: ANN401

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from pathlib import Path

    from llmrouter_backend.config import Settings

_MAX_CONTROL_FILE_BYTES = 10_000
_MAX_OBJECT_BYTES = 1024 * 1024 * 1024


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
        access_key = _read_control(settings.object_store_access_key_file)
        secret_key = _read_control(settings.object_store_secret_key_file)
        verify: bool | str = (
            str(settings.object_store_ca_file)
            if settings.object_store_ca_file is not None
            else True
        )
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
        return cls(client, settings.object_store_bucket)

    def put(self, key: str, body: bytes, content_type: str) -> None:
        """Write one private object without caller-visible storage metadata."""
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentLength=len(body),
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError, OSError) as error:
            raise ObjectStoreError from error

    def get(self, key: str, maximum_bytes: int = _MAX_OBJECT_BYTES) -> StoredObject:
        """Read one private object and close the SDK stream."""
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
        except (BotoCoreError, OSError, KeyError, TypeError) as error:
            raise ObjectStoreError from error
        content_type = response.get("ContentType")
        if (
            not isinstance(body, bytes)
            or len(body) > maximum_bytes
            or not isinstance(content_type, str)
        ):
            raise ObjectStoreError
        return StoredObject(body=body, content_type=content_type)

    def delete(self, key: str) -> None:
        """Delete one object. S3 deletion is idempotent for an absent key."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError, OSError) as error:
            raise ObjectStoreError from error

    def healthy(self) -> bool:
        """Check bucket access without returning deployment details."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except BotoCoreError, ClientError, OSError:
            return False
        return True


def _read_control(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise ObjectStoreError
        raw = path.read_bytes()
    except OSError as error:
        raise ObjectStoreError from error
    if not 1 <= len(raw) <= _MAX_CONTROL_FILE_BYTES or b"\x00" in raw:
        raise ObjectStoreError
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ObjectStoreError from error
    if not value:
        raise ObjectStoreError
    return value
