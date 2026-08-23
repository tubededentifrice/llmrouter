"""Verify private local S3 storage without printing controls or identifiers."""
# ruff: noqa: EM101, INP001, TRY003

from __future__ import annotations

import argparse

from llmrouter_backend.config import Settings
from llmrouter_backend.object_store import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)

_PROOF_KEY = "verification/restart-proof"
_PROOF_BODY = b"LLM Router private object-storage restart proof."
_PROOF_TYPE = "application/octet-stream"


def main() -> None:
    """Run one bounded proof step against the configured private bucket."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("put", "get", "delete", "failure"))
    mode = parser.parse_args().mode
    storage = ObjectStore.from_settings(Settings.from_environment())
    if storage is None:
        raise SystemExit("Object storage is not configured.")
    operations = {
        "put": _put,
        "get": _get,
        "delete": _delete,
        "failure": _failure,
    }
    operations[mode](storage)
    print(f"Object-storage {mode} proof passed.")


def _put(storage: ObjectStore) -> None:
    storage.put(_PROOF_KEY, _PROOF_BODY, _PROOF_TYPE)


def _get(storage: ObjectStore) -> None:
    value = storage.get(_PROOF_KEY)
    if value.body != _PROOF_BODY or value.content_type != _PROOF_TYPE:
        raise SystemExit("The retained object does not match.")


def _delete(storage: ObjectStore) -> None:
    storage.delete(_PROOF_KEY)
    try:
        storage.get(_PROOF_KEY)
    except ObjectNotFoundError:
        pass
    else:
        raise SystemExit("The deleted object is still available.")


def _failure(storage: ObjectStore) -> None:
    if storage.healthy():
        raise SystemExit("Object storage unexpectedly remained available.")
    try:
        storage.get(_PROOF_KEY)
    except ObjectStoreError:
        pass
    else:
        raise SystemExit("The unavailable object store accepted a read.")


if __name__ == "__main__":
    main()
