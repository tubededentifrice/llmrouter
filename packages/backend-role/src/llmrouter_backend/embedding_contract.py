"""Shared closed bounds and local model identity for native embeddings."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

MAXIMUM_EMBEDDING_INPUTS = 32
MAXIMUM_EMBEDDING_INPUT_BYTES = 32_768
MAXIMUM_EMBEDDING_TOTAL_INPUT_BYTES = 262_144
LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
LOCAL_EMBEDDING_DIMENSION = 384


def validate_embedding_inputs(values: Sequence[str]) -> None:
    """Require the exact native count and UTF-8 byte bounds."""
    if not 1 <= len(values) <= MAXIMUM_EMBEDDING_INPUTS:
        message = "The embedding input count is invalid."
        raise ValueError(message)
    try:
        sizes = [len(value.encode("utf-8")) for value in values]
    except UnicodeEncodeError:
        message = "An embedding input is not valid UTF-8."
        raise ValueError(message) from None
    if (
        any(not 1 <= size <= MAXIMUM_EMBEDDING_INPUT_BYTES for size in sizes)
        or sum(sizes) > MAXIMUM_EMBEDDING_TOTAL_INPUT_BYTES
    ):
        message = "The embedding input byte size is invalid."
        raise ValueError(message)
