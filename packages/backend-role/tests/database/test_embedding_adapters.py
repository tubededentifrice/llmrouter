"""Deterministic conformance tests for remote and local embedding adapters."""
# ruff: noqa: D102, D107

from __future__ import annotations

import asyncio
import gzip
import importlib
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from llmrouter_backend.adapters import (
    OllamaEmbeddingAdapter,
    OpenAIEmbeddingAdapter,
    local_embedding,
)
from llmrouter_backend.adapters.local_embedding import (
    FastEmbedEngine,
    LocalArtifactError,
    LocalEmbeddingAdapter,
    LocalEmbeddingConfiguration,
    local_artifact_sha256,
)
from llmrouter_backend.calls import (
    CallRequirements,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderOutput,
)
from llmrouter_backend.catalog import ProviderRoute
from llmrouter_backend.config import Settings
from llmrouter_backend.models import ModelConstraints

from .provider_adapter_conformance import (
    FailureCase,
    SuccessCase,
    assert_failure,
    assert_success,
    capture_attempt,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_SECRET = "embedding-control-placeholder"  # noqa: S105  # nosec B105
_REMOTE_UNITS = frozenset({"input_token", "request"})
_LOCAL_UNITS = frozenset({"provider_unit", "request"})
_EMBEDDING_CASE = SuccessCase("embedding", ("embedding",), _REMOTE_UNITS)


def _request(
    adapter: str,
    *,
    endpoint: str | None = "https://provider.example/v1",
    credential: str | None = _SECRET,
    dimension: int = 3,
    model_name: str = "wire-embedding-model",
) -> ProviderAttemptRequest:
    if adapter in {"openai", "local_embeddings"}:
        endpoint = None
    if adapter in {"ollama", "local_embeddings"}:
        credential = None
    return ProviderAttemptRequest(
        route=ProviderRoute(
            provider_model_api_name="embedding-model",
            provider_connection_api_name="embedding-provider",
            adapter=adapter,
            endpoint=endpoint,
            provider_model_name=model_name,
            credential_api_name="credential" if credential is not None else None,
            constraints=ModelConstraints(embedding_dimensions=[dimension]),
            reasoning_level=None,
            provider_reasoning_value=None,
        ),
        request_json=json.dumps(
            {
                "workspace_api_name": "main",
                "selector": {"assignment_api_name": "embeddings"},
                "inputs": ["first text", "second text"],
                "tags": ["test"],
            },
            separators=(",", ":"),
        ),
        credential=credential,
        kind="embedding",
        requirements=CallRequirements(frozenset({"text"}), "embedding"),
        streaming=False,
        expected_embedding_count=2,
        input_media=(),
    )


def test_openai_embedding_maps_one_complete_ordered_batch_and_usage() -> None:
    """Send all texts once and restore provider-index order in the neutral result."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
        )

    adapter = OpenAIEmbeddingAdapter("openai", httpx.MockTransport(handler))
    request = _request("openai")
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_success(
        adapter,
        request,
        capture,
        _EMBEDDING_CASE,
        priced_usage_units=_REMOTE_UNITS,
    )
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.openai.com/v1/embeddings"
    assert seen[0].headers["authorization"] == f"Bearer {_SECRET}"
    assert json.loads(seen[0].content) == {
        "model": "wire-embedding-model",
        "input": ["first text", "second text"],
        "encoding_format": "float",
    }
    output = cast("ProviderOutput", capture.events[0])
    assert json.loads(output.content_json) == [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ]
    completion = cast("ProviderCompleted", capture.events[-1])
    assert {item.unit: item.quantity for item in completion.usage} == {
        "request": 1,
        "input_token": 7,
    }


def test_custom_embedding_uses_exact_safe_endpoint_without_required_auth() -> None:
    """Permit one trusted custom endpoint and omit an absent credential header."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [1, 0, 0]},
                    {"index": 1, "embedding": [0, 1, 0]},
                ]
            },
        )

    adapter = OpenAIEmbeddingAdapter("custom", httpx.MockTransport(handler))
    request = _request("custom", credential=None)
    capture = asyncio.run(capture_attempt(adapter, request))

    assert capture.failure is None
    assert str(seen[0].url) == "https://provider.example/v1/embeddings"
    assert "authorization" not in seen[0].headers
    assert _SECRET not in repr(capture.events)


def test_ollama_embedding_uses_native_atomic_batch_and_no_truncation() -> None:
    """Use one loopback Ollama request and require the complete input batch."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "embeddings": [[1, 0, 0], [0, 1, 0]],
                "prompt_eval_count": 9,
            },
        )

    adapter = OllamaEmbeddingAdapter(httpx.MockTransport(handler))
    request = _request("ollama", endpoint="http://127.0.0.1:11434")
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_success(
        adapter,
        request,
        capture,
        _EMBEDDING_CASE,
        priced_usage_units=_REMOTE_UNITS,
    )
    assert str(seen[0].url) == "http://127.0.0.1:11434/api/embed"
    assert json.loads(seen[0].content) == {
        "model": "wire-embedding-model",
        "input": ["first text", "second text"],
        "truncate": False,
    }


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (httpx.Response(401, text=f"private {_SECRET}"), "authentication"),
        (httpx.Response(429, text="private"), "rate_limited"),
        (
            httpx.Response(
                200,
                content=b'{"data":[{"index":0,"embedding":[NaN]}]}',
                headers={"Content-Type": "application/json"},
            ),
            "invalid_response",
        ),
        (
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [1, 0, 0]},
                        {"index": 0, "embedding": [0, 1, 0]},
                    ]
                },
            ),
            "invalid_response",
        ),
    ],
)
def test_remote_embedding_failures_are_safe_and_do_not_expose_controls(
    response: httpx.Response, failure: str
) -> None:
    """Normalize HTTP and malformed response failures without provider detail."""
    adapter = OpenAIEmbeddingAdapter(
        "openai", httpx.MockTransport(lambda _request: response)
    )
    request = _request("openai")
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        capture,
        FailureCase(failure, visible_before_failure=False),
        priced_usage_units=_REMOTE_UNITS,
    )
    assert _SECRET not in str(capture.failure)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://provider.example/v1",
        "https://10.0.0.1/v1",
        "https://provider.example/v1?control=value",
        "https://user:pass@provider.example/v1",
    ],
)
def test_remote_embedding_revalidates_endpoint_trust_before_transport(
    endpoint: str,
) -> None:
    """Reject an unsafe route snapshot before it can send a batch or credential."""
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    adapter = OpenAIEmbeddingAdapter("custom", httpx.MockTransport(handler))
    request = _request("custom", endpoint=endpoint)
    capture = asyncio.run(capture_attempt(adapter, request))

    assert capture.failure is not None
    assert capture.failure.failure_class == "incompatible"
    assert not called


def test_remote_embedding_rejects_unsafe_credentials_and_oversized_results() -> None:
    """Stop control injection before transport and bound provider response bytes."""
    called = False

    def should_not_run(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    adapter = OpenAIEmbeddingAdapter("openai", httpx.MockTransport(should_not_run))
    unsafe = replace(_request("openai"), credential="secret\r\nX-Control: value")
    rejected = asyncio.run(capture_attempt(adapter, unsafe))
    assert rejected.failure is not None
    assert rejected.failure.failure_class == "incompatible"
    assert not called

    oversized = OpenAIEmbeddingAdapter(
        "openai",
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "5000001",
                },
            )
        ),
    )
    bounded = asyncio.run(capture_attempt(oversized, _request("openai")))
    assert bounded.failure is not None
    assert bounded.failure.failure_class == "invalid_response"


def test_remote_embedding_accepts_the_maximum_escaped_native_batch() -> None:
    """Keep the text-byte limit valid when JSON must escape each input byte."""
    inputs = ["\x00" * 32_768] * 8
    seen_request_bytes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request_bytes
        seen_request_bytes = len(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [1, 0, 0]}
                    for index in range(len(inputs))
                ],
                "usage": {"prompt_tokens": 1},
            },
        )

    request = replace(
        _request("openai"),
        request_json=json.dumps(
            {
                "workspace_api_name": "main",
                "selector": {"assignment_api_name": "embeddings"},
                "inputs": inputs,
            },
            separators=(",", ":"),
        ),
        expected_embedding_count=len(inputs),
    )
    capture = asyncio.run(
        capture_attempt(
            OpenAIEmbeddingAdapter("openai", httpx.MockTransport(handler)), request
        )
    )

    assert capture.failure is None
    assert seen_request_bytes > 512 * 1024


def test_remote_embedding_keeps_reported_usage_for_an_invalid_result() -> None:
    """Keep billable facts when a reported non-finite vector stops the attempt."""
    response = httpx.Response(
        200,
        content=(
            b'{"data":[{"index":0,"embedding":[1e9999]},'
            b'{"index":1,"embedding":[0]}],"usage":{"prompt_tokens":7}}'
        ),
        headers={"Content-Type": "application/json"},
    )
    capture = asyncio.run(
        capture_attempt(
            OpenAIEmbeddingAdapter(
                "openai", httpx.MockTransport(lambda _request: response)
            ),
            _request("openai"),
        )
    )

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1,
        "input_token": 7,
    }


def test_remote_embedding_rejects_encoded_provider_bodies() -> None:
    """Do not decompress an encoded body before the response-byte bound applies."""
    response = httpx.Response(
        200,
        content=gzip.compress(b"{}"),
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    capture = asyncio.run(
        capture_attempt(
            OpenAIEmbeddingAdapter(
                "openai", httpx.MockTransport(lambda _request: response)
            ),
            _request("openai"),
        )
    )

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert {item.unit for item in capture.failure.usage} == {"request"}


class FakeLocalEngine:
    """Return deterministic vectors and record exact batch input."""

    def __init__(self) -> None:
        self.inputs: list[tuple[str, ...]] = []

    def embed(self, inputs: Sequence[str]) -> Iterable[object]:
        self.inputs.append(tuple(inputs))
        first = [0.0] * 384
        second = [0.0] * 384
        first[0] = 3.0
        first[1] = 4.0
        second[2] = 2.0
        return [first, second]


def test_local_embedding_is_offline_digest_checked_ordered_and_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify once, load eagerly, and reuse one engine for complete batches."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    model = artifact / "model.onnx"
    model.write_bytes(b"fixed-public-test-artifact")
    configuration = LocalEmbeddingConfiguration(
        artifact, local_artifact_sha256(artifact), 2
    )
    engine = FakeLocalEngine()
    builds: list[tuple[Path, int]] = []

    def build(cache_dir: Path, threads: int) -> FakeLocalEngine:
        builds.append((cache_dir, threads))
        return engine

    real_verify = local_embedding._verify_artifact  # noqa: SLF001
    verifications = 0
    expected_verifications = 2

    def verify(value: LocalEmbeddingConfiguration) -> None:
        nonlocal verifications
        verifications += 1
        real_verify(value)

    monkeypatch.setattr(local_embedding, "_verify_artifact", verify)

    adapter = LocalEmbeddingAdapter(configuration, engine_factory=build)
    request = _request(
        "local_embeddings",
        dimension=384,
        model_name="BAAI/bge-small-en-v1.5",
    )
    first = asyncio.run(capture_attempt(adapter, request))
    model.unlink()
    second = asyncio.run(capture_attempt(adapter, request))
    local_case = replace(_EMBEDDING_CASE, expected_usage_units=_LOCAL_UNITS)

    assert_success(
        adapter,
        request,
        first,
        local_case,
        priced_usage_units=_LOCAL_UNITS,
    )
    assert_success(
        adapter,
        request,
        second,
        local_case,
        priced_usage_units=_LOCAL_UNITS,
    )
    assert builds == [(artifact, 2)]
    assert verifications == expected_verifications
    assert engine.inputs == [
        ("first text", "second text"),
        ("first text", "second text"),
    ]
    values = json.loads(cast("ProviderOutput", first.events[0]).content_json)
    assert values[0][:3] == [0.6, 0.8, 0.0]
    assert values[1][:3] == [0.0, 0.0, 1.0]


def test_local_embedding_stops_on_changed_artifact_and_invalid_engine_result(
    tmp_path: Path,
) -> None:
    """Fail safely before changed input and reject a zero-norm engine vector."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    model = artifact / "model.onnx"
    model.write_bytes(b"first")
    request = _request(
        "local_embeddings",
        dimension=384,
        model_name="BAAI/bge-small-en-v1.5",
    )
    engine = FakeLocalEngine()
    adapter = LocalEmbeddingAdapter(
        LocalEmbeddingConfiguration(artifact, local_artifact_sha256(artifact), 1),
        engine_factory=lambda _path, _threads: engine,
    )
    model.write_bytes(b"changed")
    changed = asyncio.run(capture_attempt(adapter, request))
    assert changed.failure is not None
    assert changed.failure.failure_class == "unavailable"
    assert not engine.inputs

    model.write_bytes(b"first")

    class InvalidEngine:
        def embed(self, _inputs: Sequence[str]) -> Iterable[object]:
            return [[0.0] * 384, [0.0] * 384]

    invalid_adapter = LocalEmbeddingAdapter(
        LocalEmbeddingConfiguration(artifact, local_artifact_sha256(artifact), 1),
        engine_factory=lambda _path, _threads: InvalidEngine(),
    )
    invalid = asyncio.run(capture_attempt(invalid_adapter, request))
    assert invalid.failure is not None
    assert invalid.failure.failure_class == "invalid_response"
    assert str(invalid.failure) == "The provider attempt failed."


def test_local_embedding_bounds_and_rejects_invalid_engine_iterators(
    tmp_path: Path,
) -> None:
    """Stop after one extra result or value and reject non-numeric vector values."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"fixed")
    configuration = LocalEmbeddingConfiguration(
        artifact, local_artifact_sha256(artifact), 1
    )
    request = _request(
        "local_embeddings",
        dimension=384,
        model_name="BAAI/bge-small-en-v1.5",
    )
    expected_bounded_vectors = 3
    expected_bounded_values = 385
    yielded_vectors = 0

    class TooManyVectors:
        def embed(self, _inputs: Sequence[str]) -> Iterable[object]:
            nonlocal yielded_vectors
            while True:
                yielded_vectors += 1
                yield [1.0] * 384

    too_many = asyncio.run(
        capture_attempt(
            LocalEmbeddingAdapter(
                configuration,
                engine_factory=lambda _path, _threads: TooManyVectors(),
            ),
            request,
        )
    )
    assert too_many.failure is not None
    assert too_many.failure.failure_class == "invalid_response"
    assert yielded_vectors == expected_bounded_vectors

    yielded_values = 0

    class TooManyValues:
        def embed(self, _inputs: Sequence[str]) -> Iterable[object]:
            def vector() -> Iterable[float]:
                nonlocal yielded_values
                while True:
                    yielded_values += 1
                    yield 1.0

            return [vector(), [1.0] * 384]

    too_long = asyncio.run(
        capture_attempt(
            LocalEmbeddingAdapter(
                configuration,
                engine_factory=lambda _path, _threads: TooManyValues(),
            ),
            request,
        )
    )
    assert too_long.failure is not None
    assert too_long.failure.failure_class == "invalid_response"
    assert yielded_values == expected_bounded_values

    class NonNumericValues:
        def embed(self, _inputs: Sequence[str]) -> Iterable[object]:
            return [["1"] * 384, [1.0] * 384]

    nonnumeric = asyncio.run(
        capture_attempt(
            LocalEmbeddingAdapter(
                configuration,
                engine_factory=lambda _path, _threads: NonNumericValues(),
            ),
            request,
        )
    )
    assert nonnumeric.failure is not None
    assert nonnumeric.failure.failure_class == "invalid_response"

    class BrokenValues:
        def embed(self, _inputs: Sequence[str]) -> Iterable[object]:
            def vector() -> Iterable[float]:
                yield 1.0
                message = "private engine failure"
                raise RuntimeError(message)

            return [vector(), [1.0] * 384]

    broken = asyncio.run(
        capture_attempt(
            LocalEmbeddingAdapter(
                configuration,
                engine_factory=lambda _path, _threads: BrokenValues(),
            ),
            request,
        )
    )
    assert broken.failure is not None
    assert broken.failure.failure_class == "invalid_response"
    assert "private engine failure" not in str(broken.failure)


def test_local_embedding_rechecks_artifact_after_eager_engine_load(
    tmp_path: Path,
) -> None:
    """Reject an artifact changed while the exact eager engine is constructed."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    model = artifact / "model.onnx"
    model.write_bytes(b"fixed")
    engine = FakeLocalEngine()

    def changing_build(_cache_dir: Path, _threads: int) -> FakeLocalEngine:
        model.write_bytes(b"other")
        return engine

    adapter = LocalEmbeddingAdapter(
        LocalEmbeddingConfiguration(artifact, local_artifact_sha256(artifact), 1),
        engine_factory=changing_build,
    )
    request = _request(
        "local_embeddings",
        dimension=384,
        model_name="BAAI/bge-small-en-v1.5",
    )
    capture = asyncio.run(capture_attempt(adapter, request))
    assert capture.failure is not None
    assert capture.failure.failure_class == "unavailable"
    assert not engine.inputs


def test_local_artifact_rejects_links_and_noncanonical_paths(tmp_path: Path) -> None:
    """Keep the verified artifact inside one canonical regular-file tree."""
    source = tmp_path / "source.onnx"
    source.write_bytes(b"fixed")
    hardlinked = tmp_path / "hardlinked"
    hardlinked.mkdir()
    (hardlinked / "model.onnx").hardlink_to(source)
    with pytest.raises(RuntimeError):
        local_artifact_sha256(hardlinked)

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"fixed")
    linked = tmp_path / "linked"
    linked.symlink_to(artifact, target_is_directory=True)
    with pytest.raises(RuntimeError):
        local_artifact_sha256(linked)

    maximum_depth = 32
    deep = tmp_path / "deep"
    deep.mkdir()
    current = deep
    for index in range(maximum_depth + 1):
        current = current / f"level-{index}"
        current.mkdir()
    (current / "model.onnx").write_bytes(b"fixed")
    with pytest.raises(RuntimeError):
        local_artifact_sha256(deep)


@pytest.mark.parametrize("replacement_kind", ["symlink", "fifo", "regular"])
def test_local_artifact_rejects_entry_replacement_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    """Reject a link, FIFO, or different inode swapped after entry inspection."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"fixed")
    replacement = tmp_path / "replacement.onnx"
    replacement.write_bytes(b"other")
    real_open = os.open
    raced = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if path == "model.onnx" and dir_fd is not None and not raced:
            raced = True
            os.unlink("model.onnx", dir_fd=dir_fd)
            if replacement_kind == "symlink":
                os.symlink("/dev/null", "model.onnx", dir_fd=dir_fd)
            elif replacement_kind == "fifo":
                os.mkfifo("model.onnx", dir_fd=dir_fd)
            else:
                os.rename(replacement, "model.onnx", dst_dir_fd=dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(LocalArtifactError):
        local_artifact_sha256(artifact)
    assert raced


def test_local_artifact_rejects_same_inode_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject content that changes after its descriptor is validated."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    model = artifact / "model.onnx"
    original = b"fixed-public-artifact"
    model.write_bytes(original)
    real_read = os.read
    raced = False

    def racing_read(file_descriptor: int, count: int) -> bytes:
        nonlocal raced
        value = real_read(file_descriptor, count)
        if value and not raced:
            raced = True
            model.write_bytes(b"x" * len(original))
        return value

    monkeypatch.setattr(os, "read", racing_read)
    with pytest.raises(LocalArtifactError):
        local_artifact_sha256(artifact)
    assert raced


def test_local_artifact_rejects_root_replacement_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm that the configured root still names the verified directory."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"fixed")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "model.onnx").write_bytes(b"other")
    detached = tmp_path / "detached"
    real_lstat = Path.lstat
    root_stats = 0
    replacement_stat = 2

    def racing_lstat(path: Path) -> os.stat_result:
        nonlocal root_stats
        if path == artifact:
            root_stats += 1
            if root_stats == replacement_stat:
                artifact.rename(detached)
                replacement.rename(artifact)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    with pytest.raises(LocalArtifactError):
        local_artifact_sha256(artifact)
    assert root_stats == replacement_stat


def test_local_artifact_reads_are_size_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read a stable artifact through fixed small descriptor reads."""
    digest_characters = 64
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"x" * (1024 * 1024 + 17))
    real_read = os.read
    requested: list[int] = []

    def bounded_read(file_descriptor: int, count: int) -> bytes:
        requested.append(count)
        return real_read(file_descriptor, count)

    monkeypatch.setattr(os, "read", bounded_read)
    assert len(local_artifact_sha256(artifact)) == digest_characters
    assert requested
    assert max(requested) <= 1024 * 1024
    assert requested[-1] == 1


def test_fastembed_engine_sets_exact_offline_cpu_thread_and_cache_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass only the approved model and closed offline execution controls."""
    created: dict[str, object] = {}

    class Engine:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)

        def embed(self, inputs: Sequence[str], **kwargs: object) -> Iterable[object]:
            created["inputs"] = tuple(inputs)
            created["embed_options"] = kwargs
            return [[1.0] * 384]

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(TextEmbedding=Engine)
            if name == "fastembed"
            else pytest.fail("An unexpected module was imported.")
        ),
    )
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "true")
    engine = FastEmbedEngine(tmp_path, 3)
    assert list(engine.embed(["public input"])) == [[1.0] * 384]
    assert os.environ["TOKENIZERS_PARALLELISM"] == "false"
    assert created == {
        "model_name": "BAAI/bge-small-en-v1.5",
        "cache_dir": str(tmp_path),
        "specific_model_path": str(tmp_path),
        "threads": 3,
        "providers": ["CPUExecutionProvider"],
        "cuda": False,
        "lazy_load": False,
        "local_files_only": True,
        "inputs": ("public input",),
        "embed_options": {"parallel": None},
    }


def test_local_embedding_serializes_inference_for_bounded_native_threads(
    tmp_path: Path,
) -> None:
    """Do not multiply the configured native thread count across concurrent calls."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"fixed")
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    class BlockingEngine(FakeLocalEngine):
        def embed(self, inputs: Sequence[str]) -> Iterable[object]:
            nonlocal calls
            with calls_lock:
                calls += 1
                current = calls
            if current == 1:
                entered.set()
                assert release.wait(timeout=5)
            else:
                second_entered.set()
            return super().embed(inputs)

    adapter = LocalEmbeddingAdapter(
        LocalEmbeddingConfiguration(
            artifact, local_artifact_sha256(artifact), threads=4
        ),
        engine_factory=lambda _path, _threads: BlockingEngine(),
    )
    request = _request(
        "local_embeddings",
        dimension=384,
        model_name="BAAI/bge-small-en-v1.5",
    )

    async def run() -> None:
        first = asyncio.create_task(capture_attempt(adapter, request))
        assert await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(capture_attempt(adapter, request))
        await asyncio.sleep(0.05)
        assert not second_entered.is_set()
        release.set()
        results = await asyncio.gather(first, second)
        assert all(result.failure is None for result in results)

    asyncio.run(run())
    assert second_entered.is_set()


def test_cancelled_local_calls_do_not_queue_worker_threads(
    tmp_path: Path,
) -> None:
    """Keep cancelled waiters out of the native artifact and inference worker."""
    expected_calls = 2
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"fixed")
    entered = threading.Event()
    finished = threading.Event()
    release = threading.Event()
    later_entered = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    class BlockingEngine(FakeLocalEngine):
        def embed(self, inputs: Sequence[str]) -> Iterable[object]:
            nonlocal calls
            with calls_lock:
                calls += 1
                current = calls
            if current == 1:
                entered.set()
                try:
                    assert release.wait(timeout=5)
                finally:
                    finished.set()
            else:
                later_entered.set()
            return super().embed(inputs)

    adapter = LocalEmbeddingAdapter(
        LocalEmbeddingConfiguration(
            artifact, local_artifact_sha256(artifact), threads=1
        ),
        engine_factory=lambda _path, _threads: BlockingEngine(),
    )
    request = _request(
        "local_embeddings",
        dimension=384,
        model_name="BAAI/bge-small-en-v1.5",
    )

    async def run() -> None:
        active = asyncio.create_task(capture_attempt(adapter, request))
        assert await asyncio.to_thread(entered.wait, 5)
        waiting = [
            asyncio.create_task(capture_attempt(adapter, request)) for _ in range(20)
        ]
        await asyncio.sleep(0.05)
        assert calls == 1
        for task in (active, *waiting):
            task.cancel()
        await asyncio.gather(active, *waiting, return_exceptions=True)
        assert calls == 1
        assert not later_entered.is_set()
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        await asyncio.sleep(0.05)
        final = await capture_attempt(adapter, request)
        assert final.failure is None

    asyncio.run(run())
    assert calls == expected_calls
    assert later_entered.is_set()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"local_embedding_cache_dir": Path("relative")}, "complete"),
        (
            {
                "local_embedding_cache_dir": Path("relative"),
                "local_embedding_artifact_sha256": "0" * 64,
            },
            "absolute",
        ),
        (
            {
                "local_embedding_cache_dir": Path("/verified/model"),
                "local_embedding_artifact_sha256": "invalid",
            },
            "SHA-256",
        ),
        ({"local_embedding_threads": 0}, "1 through 32"),
        ({"local_embedding_threads": 33}, "1 through 32"),
    ],
)
def test_local_embedding_deployment_controls_are_closed_and_bounded(
    values: dict[str, object], message: str
) -> None:
    """Reject incomplete, relative, malformed, and unbounded local controls."""
    with pytest.raises(ValueError, match=message):
        Settings(**cast("Any", values))


def test_local_embedding_controls_load_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Load the exact local artifact controls without a secret or endpoint."""
    threads = 4
    monkeypatch.setenv("LLMROUTER_LOCAL_EMBEDDING_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("LLMROUTER_LOCAL_EMBEDDING_ARTIFACT_SHA256", "a" * 64)
    monkeypatch.setenv("LLMROUTER_LOCAL_EMBEDDING_THREADS", str(threads))
    settings = Settings.from_environment()
    assert settings.local_embedding_cache_dir == tmp_path
    assert settings.local_embedding_artifact_sha256 == "a" * 64
    assert settings.local_embedding_threads == threads
