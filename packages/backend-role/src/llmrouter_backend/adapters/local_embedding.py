"""Offline digest-checked local embedding adapter and FastEmbed engine."""
# ruff: noqa: BLE001, D107, EM101, PLR2004

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import os
import stat
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from llmrouter_backend.accounting import UsageAmount
from llmrouter_backend.calls import (
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderFailureError,
    ProviderOperation,
    ProviderOutput,
)
from llmrouter_backend.embedding_contract import (
    LOCAL_EMBEDDING_DIMENSION,
    LOCAL_EMBEDDING_MODEL,
    validate_embedding_inputs,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Sequence

    from llmrouter_backend.config import Settings

_MAXIMUM_CACHE_FILES = 4096
_MAXIMUM_CACHE_ENTRIES = 8192
_MAXIMUM_CACHE_DEPTH = 32
_MAXIMUM_CACHE_BYTES = 2 * 1024 * 1024 * 1024
_MAXIMUM_REQUEST_BYTES = 512 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
)
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_USAGE_UNITS = frozenset({"request", "provider_unit"})


class LocalEmbeddingEngine(Protocol):
    """Produce one complete local batch without network access."""

    def embed(self, inputs: Sequence[str]) -> Iterable[object]:
        """Return one vector for each input in original order."""
        ...


class _Digest(Protocol):
    """Accept the hash operations used by artifact verification."""

    def update(self, value: bytes) -> None:
        """Add bytes to the artifact digest."""
        ...

    def hexdigest(self) -> str:
        """Return the lower-case artifact digest."""
        ...


type LocalEngineFactory = Callable[[Path, int], LocalEmbeddingEngine]


@dataclass(frozen=True, slots=True)
class LocalEmbeddingConfiguration:
    """Bind one offline artifact tree and bounded inference thread count."""

    cache_dir: Path
    artifact_sha256: str
    threads: int

    @classmethod
    def from_settings(cls, settings: Settings) -> LocalEmbeddingConfiguration | None:
        """Create a complete local configuration or keep the adapter unavailable."""
        cache_dir = settings.local_embedding_cache_dir
        digest = settings.local_embedding_artifact_sha256
        if cache_dir is None or digest is None:
            return None
        return cls(cache_dir, digest, settings.local_embedding_threads)


class FastEmbedEngine:
    """Use exact FastEmbed with CPU-only, offline, bounded-thread controls."""

    def __init__(self, cache_dir: Path, threads: int) -> None:
        module = importlib.import_module("fastembed")
        engine_type = module.TextEmbedding
        self._engine = engine_type(
            model_name=LOCAL_EMBEDDING_MODEL,
            cache_dir=str(cache_dir),
            specific_model_path=str(cache_dir),
            threads=threads,
            providers=["CPUExecutionProvider"],
            cuda=False,
            lazy_load=False,
            local_files_only=True,
        )

    def embed(self, inputs: Sequence[str]) -> Iterable[object]:
        """Use one atomic batch with no FastEmbed worker process."""
        return cast("Iterable[object]", self._engine.embed(inputs, parallel=None))


class LocalEmbeddingAdapter:
    """Run one approved local embedding model with no network transport."""

    usage_units = _USAGE_UNITS

    def __init__(
        self,
        configuration: LocalEmbeddingConfiguration | None,
        *,
        engine_factory: LocalEngineFactory = FastEmbedEngine,
    ) -> None:
        self._configuration = configuration
        self._engine_factory = engine_factory
        self._engine: LocalEmbeddingEngine | None = None
        self._engine_lock = threading.Lock()
        self._work_slot = asyncio.Semaphore(1)

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Price one request and one provider unit for each input text."""
        return self.usage_units if operation.kind == "embedding" else frozenset()

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        """Verify the immutable cache and run one bounded local batch."""
        if (
            request.route.adapter != "local_embeddings"
            or request.kind != "embedding"
            or request.streaming
            or request.input_media
            or request.credential is not None
            or request.route.endpoint is not None
            or request.route.provider_model_name != LOCAL_EMBEDDING_MODEL
            or request.route.constraints.embedding_dimensions
            != [LOCAL_EMBEDDING_DIMENSION]
        ):
            raise ProviderFailureError("incompatible")
        try:
            inputs = _native_inputs(request)
        except TypeError, UnicodeError, ValueError, RecursionError:
            raise ProviderFailureError("incompatible") from None
        if self._configuration is None:
            raise ProviderFailureError("unavailable")
        try:
            vectors = await self._bounded_embed(inputs)
        except LocalArtifactError:
            raise ProviderFailureError("unavailable") from None
        except LocalEngineError:
            raise ProviderFailureError(
                "invalid_response", usage=_request_usage()
            ) from None
        except Exception:
            raise ProviderFailureError("unavailable", usage=_request_usage()) from None
        yield ProviderOutput(
            "embedding",
            json.dumps(vectors, allow_nan=False, separators=(",", ":")),
        )
        yield ProviderCompleted(
            (
                UsageAmount("request", Decimal(1)),
                UsageAmount("provider_unit", Decimal(len(inputs))),
            )
        )

    async def _bounded_embed(self, inputs: Sequence[str]) -> list[list[float]]:
        """Start at most one artifact or inference worker at one time."""
        await self._work_slot.acquire()
        try:
            work = asyncio.create_task(asyncio.to_thread(self._embed, inputs))
        except BaseException:
            self._work_slot.release()
            raise
        work.add_done_callback(self._complete_work)
        return await asyncio.shield(work)

    def _complete_work(self, work: asyncio.Task[list[list[float]]]) -> None:
        """Release admission only after the native worker has stopped."""
        self._work_slot.release()
        if not work.cancelled():
            work.exception()

    def _embed(self, inputs: Sequence[str]) -> list[list[float]]:
        configuration = self._configuration
        if configuration is None:
            raise LocalArtifactError
        engine = self._local_engine(configuration)
        try:
            raw = list(engine.embed(inputs))
        except Exception as error:
            raise LocalEngineError from error
        if len(raw) != len(inputs):
            raise LocalEngineError
        return [_normalized_vector(value) for value in raw]

    def _local_engine(
        self, configuration: LocalEmbeddingConfiguration
    ) -> LocalEmbeddingEngine:
        with self._engine_lock:
            if self._engine is None:
                _verify_artifact(configuration)
                try:
                    engine = self._engine_factory(
                        configuration.cache_dir, configuration.threads
                    )
                except Exception as error:
                    raise LocalArtifactError from error
                _verify_artifact(configuration)
                self._engine = engine
            return self._engine


class LocalArtifactError(RuntimeError):
    """Report a missing, changed, unsafe, or unloadable local artifact."""


class LocalEngineError(RuntimeError):
    """Report an invalid result from an active local inference engine."""


@dataclass(slots=True)
class _ArtifactDigestState:
    digest: _Digest
    file_count: int = 0
    entry_count: int = 0
    total_bytes: int = 0


def local_artifact_sha256(cache_dir: Path) -> str:
    """Return one deterministic digest for a bounded regular-file artifact tree."""
    try:
        root_stat = cache_dir.lstat()
        root = cache_dir.resolve(strict=True)
    except OSError as error:
        raise LocalArtifactError from error
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or cache_dir.is_symlink()
        or root != cache_dir
        or not root.is_dir()
    ):
        raise LocalArtifactError
    state = _ArtifactDigestState(hashlib.sha256())
    root_fd = -1
    try:
        root_fd = os.open(cache_dir, _DIRECTORY_OPEN_FLAGS)
        opened_root = os.fstat(root_fd)
        _require_same_entry(root_stat, opened_root, directory=True)
        _digest_directory(root_fd, (), opened_root, state)
        _require_same_entry(opened_root, cache_dir.lstat(), directory=True)
    except (OSError, UnicodeError, ValueError) as error:
        raise LocalArtifactError from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    if state.file_count == 0:
        raise LocalArtifactError
    return state.digest.hexdigest()


def _digest_directory(
    directory_fd: int,
    parents: tuple[str, ...],
    opened_stat: os.stat_result,
    state: _ArtifactDigestState,
) -> None:
    names = os.listdir(directory_fd)
    if state.entry_count + len(names) > _MAXIMUM_CACHE_ENTRIES:
        raise LocalArtifactError
    for name in sorted(names):
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        state.entry_count += 1
        if stat.S_ISDIR(entry_stat.st_mode):
            if len(parents) >= _MAXIMUM_CACHE_DEPTH:
                raise LocalArtifactError
            child_fd = -1
            try:
                child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
                opened_child = os.fstat(child_fd)
                _require_same_entry(entry_stat, opened_child, directory=True)
                _digest_directory(
                    child_fd,
                    (*parents, name),
                    opened_child,
                    state,
                )
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
            continue
        if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_nlink != 1:
            raise LocalArtifactError
        _digest_file(directory_fd, name, parents, entry_stat, state)
    _require_stable_entry(opened_stat, os.fstat(directory_fd))


def _digest_file(
    directory_fd: int,
    name: str,
    parents: tuple[str, ...],
    entry_stat: os.stat_result,
    state: _ArtifactDigestState,
) -> None:
    source_fd = -1
    try:
        source_fd = os.open(name, _FILE_OPEN_FLAGS, dir_fd=directory_fd)
        opened_stat = os.fstat(source_fd)
        _require_same_entry(entry_stat, opened_stat, directory=False)
        state.file_count += 1
        state.total_bytes += opened_stat.st_size
        if (
            state.file_count > _MAXIMUM_CACHE_FILES
            or state.total_bytes > _MAXIMUM_CACHE_BYTES
        ):
            raise LocalArtifactError
        relative = "/".join((*parents, name)).encode("utf-8")
        state.digest.update(len(relative).to_bytes(4, "big"))
        state.digest.update(relative)
        state.digest.update(opened_stat.st_size.to_bytes(8, "big"))
        remaining = opened_stat.st_size
        while remaining:
            chunk = os.read(source_fd, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                raise LocalArtifactError
            state.digest.update(chunk)
            remaining -= len(chunk)
        if os.read(source_fd, 1):
            raise LocalArtifactError
        _require_stable_entry(opened_stat, os.fstat(source_fd))
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def _require_same_entry(
    expected: os.stat_result, opened: os.stat_result, *, directory: bool
) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_type(expected.st_mode)
        or not expected_type(opened.st_mode)
        or expected.st_dev != opened.st_dev
        or expected.st_ino != opened.st_ino
        or (not directory and (expected.st_nlink != 1 or opened.st_nlink != 1))
    ):
        raise LocalArtifactError
    _require_stable_entry(expected, opened)


def _require_stable_entry(before: os.stat_result, after: os.stat_result) -> None:
    if any(
        getattr(before, field) != getattr(after, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    ):
        raise LocalArtifactError


def _verify_artifact(configuration: LocalEmbeddingConfiguration) -> None:
    if (
        configuration.artifact_sha256 != local_artifact_sha256(configuration.cache_dir)
        or not 1 <= configuration.threads <= 32
    ):
        raise LocalArtifactError


def _native_inputs(request: ProviderAttemptRequest) -> list[str]:
    if len(request.request_json.encode("utf-8")) > _MAXIMUM_REQUEST_BYTES:
        raise ValueError
    native = json.loads(request.request_json, parse_constant=_reject_constant)
    if not isinstance(native, dict) or not set(native) <= {
        "workspace_api_name",
        "selector",
        "inputs",
        "tags",
    }:
        raise ValueError
    inputs = native.get("inputs")
    if (
        not isinstance(inputs, list)
        or len(inputs) != request.expected_embedding_count
        or any(not isinstance(value, str) or not value for value in inputs)
    ):
        raise ValueError
    typed = cast("list[str]", inputs)
    validate_embedding_inputs(typed)
    return typed


def _normalized_vector(value: object) -> list[float]:
    if isinstance(value, str | bytes | bytearray):
        raise LocalEngineError
    try:
        result = [float(cast("Any", item)) for item in cast("Iterable[object]", value)]
    except (OverflowError, TypeError, ValueError) as error:
        raise LocalEngineError from error
    if len(result) != LOCAL_EMBEDDING_DIMENSION or any(
        not math.isfinite(item) for item in result
    ):
        raise LocalEngineError
    norm = math.sqrt(sum(item * item for item in result))
    if not math.isfinite(norm) or norm == 0:
        raise LocalEngineError
    normalized = [item / norm for item in result]
    if any(not math.isfinite(item) for item in normalized):
        raise LocalEngineError
    return normalized


def _request_usage() -> tuple[UsageAmount, ...]:
    return (UsageAmount("request", Decimal(1)),)


def _reject_constant(_value: str) -> None:
    raise ValueError
