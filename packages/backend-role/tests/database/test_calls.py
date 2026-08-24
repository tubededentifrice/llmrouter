"""Connection-lifetime routing, fallback, cooldown, and fact tests."""
# ruff: noqa: D107, PLR2004

from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pytest
from llmrouter_backend import accounting, catalog, diagnostics
from llmrouter_backend.calls import (
    CallExecutionError,
    CallExecutor,
    CallLimits,
    CallRequest,
    CallRequirements,
    CallResult,
    ProviderAdapter,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderCooldowns,
    ProviderEvent,
    ProviderFailureError,
    ProviderOutput,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.errors import ApiError
from llmrouter_backend.store import ServiceActor
from opendle import AssignmentSelector, CallFailurePhase, ExactModelSelector
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass(frozen=True, slots=True)
class WaitForRelease:
    """Pause one fake provider attempt until a test releases it."""

    started: asyncio.Event
    release: asyncio.Event


type ScriptItem = ProviderEvent | ProviderFailureError | Exception | WaitForRelease


class ScriptedAdapter:
    """Run one deterministic event script for each exact provider-model."""

    def __init__(
        self,
        scripts: dict[str, list[list[ScriptItem]]],
        *,
        usage_units: frozenset[str] = frozenset({"request", "input_token"}),
    ) -> None:
        self.scripts = scripts
        self.usage_units = usage_units
        self.calls: list[str] = []
        self.credentials: list[str | None] = []
        self.requests: list[ProviderAttemptRequest] = []

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one script without a hidden retry."""
        name = request.route.provider_model_api_name
        self.calls.append(name)
        self.credentials.append(request.credential)
        self.requests.append(request)
        script = self.scripts[name].pop(0)
        for item in script:
            if isinstance(item, WaitForRelease):
                item.started.set()
                await item.release.wait()
            elif isinstance(item, ProviderFailureError | Exception):
                raise item
            else:
                yield item


class CallContext:
    """One clean catalog with inherited services and priced fake routes."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.credential_keys = catalog.ProviderCredentialKeys(b"k" * 32)
        encrypted, fingerprint = self.credential_keys.encrypt(
            "provider-control", "provider-secret-control"
        )
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            migrate(connection)
            alpha = connection.execute(
                """INSERT INTO router.services (api_name, display_name)
                   VALUES ('alpha', 'Alpha') RETURNING id"""
            ).fetchone()
            assert alpha is not None
            child = connection.execute(
                """INSERT INTO router.services
                       (api_name, display_name, parent_service_id)
                   VALUES ('child', 'Child', %s) RETURNING id""",
                (alpha["id"],),
            ).fetchone()
            beta = connection.execute(
                """INSERT INTO router.services (api_name, display_name)
                   VALUES ('beta', 'Beta') RETURNING id"""
            ).fetchone()
            assert child is not None
            assert beta is not None
            self.service_ids = {
                "alpha": alpha["id"],
                "child": child["id"],
                "beta": beta["id"],
            }
            self.workspace_ids: dict[str, uuid.UUID] = {}
            for name, service_id in self.service_ids.items():
                workspace = connection.execute(
                    """INSERT INTO router.workspaces
                           (service_id, api_name, display_name)
                       VALUES (%s, 'main', 'Main') RETURNING id""",
                    (service_id,),
                ).fetchone()
                assert workspace is not None
                self.workspace_ids[name] = workspace["id"]
            connection.execute(
                """INSERT INTO router.provider_credentials
                       (api_name, encrypted_secret, fingerprint)
                   VALUES ('provider-control', %s, %s)""",
                (encrypted, fingerprint),
            )
            connection.execute(
                """INSERT INTO router.provider_connections
                       (api_name, display_name, adapter, credential_id, enabled)
                   SELECT 'fake-provider', 'Fake provider', 'fake', id, true
                   FROM router.provider_credentials
                   WHERE api_name = 'provider-control'"""
            )
            self._insert_models(connection)
            self._insert_assignments(connection)
        self.actors = {
            name: ServiceActor(service_id, name, uuid.uuid4())
            for name, service_id in self.service_ids.items()
        }

    def _insert_models(self, connection: psycopg.Connection[Any]) -> None:
        models: tuple[
            tuple[str, list[str], list[str], list[str], dict[str, object]], ...
        ] = (
            (
                "plain",
                ["text"],
                ["text"],
                [],
                {},
            ),
            (
                "text",
                ["text", "image"],
                ["text", "structured_json"],
                ["tool_calling", "streaming", "reasoning"],
                {"max_input_images": 8, "max_input_image_bytes": 20_971_520},
            ),
            ("embedding", ["text"], ["embedding"], [], {"embedding_dimensions": [3]}),
            (
                "media",
                ["text", "image"],
                ["image", "video", "audio"],
                [],
                {
                    "max_input_images": 8,
                    "max_input_image_bytes": 20_971_520,
                    "max_output_duration_seconds": 300,
                },
            ),
        )
        for name, inputs, outputs, capabilities, constraints in models:
            connection.execute(
                """INSERT INTO router.canonical_models
                       (api_name, display_name, input_modalities, output_modalities,
                        capabilities, constraints, manual_price)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)""",
                (
                    name,
                    name.title(),
                    inputs,
                    outputs,
                    capabilities,
                    json.dumps(constraints),
                    json.dumps(_price()),
                ),
            )
        mappings: tuple[tuple[str, str, list[str]], ...] = (
            ("plain", "plain", []),
            ("text-a", "text", ["tool_calling", "streaming", "reasoning"]),
            ("text-b", "text", ["tool_calling", "streaming", "reasoning"]),
            ("structured", "text", ["reasoning"]),
            ("embedding-a", "embedding", []),
            ("embedding-b", "embedding", []),
            ("media", "media", []),
        )
        reasoning = [
            {"level": level, "provider_value": level}
            for level in ("none", "low", "medium", "high")
        ]
        for name, model, capabilities in mappings:
            connection.execute(
                """INSERT INTO router.provider_models
                       (api_name, provider_id, model_id, provider_model_name,
                        enabled, input_modalities, output_modalities,
                        capabilities, constraints, reasoning_mappings)
                   SELECT %s, provider.id, model.id, %s, true,
                          model.input_modalities, model.output_modalities,
                          %s, model.constraints, %s::jsonb
                   FROM router.provider_connections AS provider,
                        router.canonical_models AS model
                   WHERE provider.api_name = 'fake-provider'
                     AND model.api_name = %s""",
                (
                    name,
                    name,
                    capabilities,
                    json.dumps(reasoning if "reasoning" in capabilities else []),
                    model,
                ),
            )
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name,
                    enabled, input_modalities, output_modalities,
                    capabilities, constraints, reasoning_mappings)
               SELECT 'broken', provider_id, model_id, 'broken', true,
                      input_modalities, output_modalities, capabilities,
                      '{"max_input_images":"invalid"}'::jsonb,
                      reasoning_mappings
               FROM router.provider_models WHERE api_name = 'text-a'"""
        )

    def _insert_assignments(self, connection: psycopg.Connection[Any]) -> None:
        self._direct_assignment(
            connection,
            "alpha",
            "workflow",
            ("plain", "text-a", "text-b"),
        )
        self._direct_assignment(connection, "alpha", "default", ("text-b",))
        self._direct_assignment(
            connection, "alpha", "embeddings", ("embedding-a", "embedding-b")
        )
        self._direct_assignment(connection, "alpha", "media", ("media",))
        self._direct_assignment(
            connection, "alpha", "excluded-broken", ("broken", "text-a")
        )
        connection.execute(
            """INSERT INTO router.assignment_definitions
                   (service_id, api_name, display_name,
                    inherits_assignment_api_name, reasoning_level)
               SELECT id, 'inherited', 'Inherited', 'workflow', 'high'
               FROM router.services WHERE api_name = 'child'"""
        )

    def _direct_assignment(
        self,
        connection: psycopg.Connection[Any],
        service: str,
        assignment: str,
        candidates: tuple[str, ...],
    ) -> None:
        row = connection.execute(
            """INSERT INTO router.assignment_definitions
                   (service_id, api_name, display_name)
               SELECT id, %s, %s FROM router.services WHERE api_name = %s
               RETURNING id""",
            (assignment, assignment.title(), service),
        ).fetchone()
        assert row is not None
        for position, candidate in enumerate(candidates):
            connection.execute(
                """INSERT INTO router.assignment_candidates
                       (assignment_id, position, provider_model_id)
                   SELECT %s, %s, id FROM router.provider_models WHERE api_name = %s""",
                (row["id"], position, candidate),
            )

    def executor(
        self,
        adapter: ScriptedAdapter,
        *,
        cooldowns: ProviderCooldowns | None = None,
        limits: CallLimits | None = None,
    ) -> CallExecutor:
        """Create one executor that uses this isolated database and credential."""
        return CallExecutor(
            database_url=self.database_url,
            adapters={"fake": cast("ProviderAdapter", adapter)},
            cooldowns=cooldowns,
            credential_keys=self.credential_keys,
            limits=limits,
        )


@pytest.fixture
def call_context(database_url: str) -> CallContext:
    """Apply one clean call-core schema and deterministic catalog."""
    return CallContext(database_url)


def _price() -> dict[str, object]:
    return {
        "currency": "USD",
        "unit_prices": [
            {"unit": "request", "amount": "0.25"},
            {"unit": "input_token", "amount": "0.01"},
        ],
        "source": "manual-test",
    }


def _usage() -> tuple[accounting.UsageAmount, ...]:
    return (accounting.UsageAmount("request", Decimal(1)),)


def _completed() -> ProviderCompleted:
    return ProviderCompleted(_usage())


def _standard(text: str = "ok") -> ProviderOutput:
    return ProviderOutput("standard", json.dumps([{"type": "text", "text": text}]))


def _text_request(
    selector: AssignmentSelector | ExactModelSelector,
    *,
    workspace: str = "main",
    streaming: bool = False,
    excluded: tuple[str, ...] = (),
    capabilities: frozenset[str] = frozenset(),
) -> CallRequest:
    return CallRequest(
        workspace_api_name=workspace,
        selector=selector,
        kind="model",
        requirements=CallRequirements(
            frozenset({"text"}),
            "text",
            capabilities | (frozenset({"streaming"}) if streaming else frozenset()),
        ),
        request_json=(
            '{"messages":[{"role":"user","content":"secret-like model text"}]}'
        ),
        tags=("zeta", "alpha", "zeta"),
        excluded_provider_model_api_names=excluded,
        streaming=streaming,
    )


def test_ordered_fallback_filters_exclusions_and_records_linked_facts(
    call_context: CallContext,
) -> None:
    """Try each eligible route once and keep all usage, prices, cost, and tags."""
    adapter = ScriptedAdapter(
        {
            "text-a": [[ProviderFailureError("rate_limited", usage=_usage())]],
            "text-b": [[_standard(), _completed()]],
        }
    )
    result = asyncio.run(
        call_context.executor(adapter).execute(
            call_context.actors["child"],
            _text_request(
                AssignmentSelector("inherited"),
                excluded=("plain", "not-in-chain"),
            ),
        )
    )
    assert adapter.calls == ["text-a", "text-b"]
    assert adapter.credentials == ["provider-secret-control"] * 2
    assert result.provider_model_api_name == "text-b"
    assert result.cost == Decimal("0.25")
    assert result.applied_price.currency == "USD"
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        call = connection.execute(
            "SELECT * FROM router.raw_accounting_calls WHERE id = %s", (result.call_id,)
        ).fetchone()
        assert call is not None
        assert call["assignment_api_name"] == "inherited"
        assert call["tags"] == ["alpha", "zeta"]
        attempts = connection.execute(
            """SELECT position, provider_model_api_name, outcome, usage,
                      applied_price, cost, failure_class
               FROM router.raw_accounting_attempts
               WHERE call_id = %s ORDER BY position""",
            (result.call_id,),
        ).fetchall()
        assert [item["provider_model_api_name"] for item in attempts] == [
            "text-a",
            "text-b",
        ]
        assert attempts[0]["failure_class"] == "rate_limited"
        assert attempts[0]["usage"] == [{"unit": "request", "quantity": "1"}]
        assert [item["cost"] for item in attempts] == [
            Decimal("0.25"),
            Decimal("0.25"),
        ]
        log = connection.execute(
            "SELECT * FROM router.request_logs WHERE id = %s", (result.call_id,)
        ).fetchone()
        assert log is not None
        assert len(log["attempts"]) == 2
        assert log["attempts"][0]["usage"]["units"] == [
            {"unit": "request", "quantity": "1"}
        ]
        assert log["tags"] == ["alpha", "zeta"]
        serialized = json.dumps(log, default=str)
        assert "provider-secret-control" not in serialized
        assert "secret-like model text" in log["request_json"]
        route = catalog.resolve_provider_route(
            connection,
            "text-a",
            required_inputs=frozenset({"text"}),
            required_output="text",
            required_capabilities=frozenset(),
            reasoning_level="high",
        )
        assert route.provider_reasoning_value == "high"


def test_exact_selection_has_no_fallback_and_keeps_service_isolation(
    call_context: CallContext,
) -> None:
    """Use one exact route and hide a foreign workspace before provider work."""
    adapter = ScriptedAdapter(
        {"text-a": [[ProviderFailureError("transport")]], "text-b": [[_standard()]]}
    )
    with pytest.raises(CallExecutionError) as exact:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
    assert exact.value.code == "upstream_failed"
    assert adapter.calls == ["text-a"]
    with pytest.raises(ApiError) as foreign:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["beta"],
                _text_request(ExactModelSelector("text-b"), workspace="missing"),
            )
        )
    assert foreign.value.code == "not_found"
    assert adapter.calls == ["text-a"]
    administrator = cast("Any", object())
    with pytest.raises(CallExecutionError) as denied:
        asyncio.run(
            call_context.executor(adapter).execute(
                administrator, _text_request(ExactModelSelector("text-b"))
            )
        )
    assert denied.value.code == "permission_denied"


def test_filtered_and_cooled_candidates_create_no_provider_attempt(
    call_context: CallContext,
) -> None:
    """Skip exclusions and active cooldowns before provider and attempt work."""
    cooldowns = ProviderCooldowns(clock=lambda: 10.0)
    for failure in ("transport", "timeout", "unavailable"):
        cooldowns.record_failure("text-a", cast("Any", failure))
    adapter = ScriptedAdapter({})
    with pytest.raises(CallExecutionError) as unavailable:
        asyncio.run(
            call_context.executor(adapter, cooldowns=cooldowns).execute(
                call_context.actors["alpha"],
                _text_request(
                    AssignmentSelector("workflow"), excluded=("plain", "text-b")
                ),
            )
        )
    assert unavailable.value.code == "provider_unavailable"
    assert not adapter.calls
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        call = connection.execute(
            "SELECT id, outcome FROM router.raw_accounting_calls"
        ).fetchone()
        assert call is not None
        assert call["outcome"] == "failed"
        assert connection.execute(
            "SELECT count(*) FROM router.raw_accounting_attempts"
        ).fetchone() == {"count": 0}
        assert connection.execute(
            "SELECT id FROM router.request_logs WHERE id = %s", (call["id"],)
        ).fetchone() == {"id": call["id"]}
        evidence = connection.execute(
            """SELECT observed_requirements FROM router.assignment_usage
               WHERE service_id = %s AND api_name = 'workflow'""",
            (call_context.service_ids["alpha"],),
        ).fetchone()
        assert evidence is not None
        assert "text_input" in evidence["observed_requirements"]
        assert "text_output" in evidence["observed_requirements"]


def test_streaming_call_requires_writer_before_admission(
    call_context: CallContext,
) -> None:
    """Return one corrective error before workspace or provider work."""
    adapter = ScriptedAdapter({"text-a": [[ProviderOutput("text_delta", '"x"')]]})
    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(
                    ExactModelSelector("text-a"),
                    workspace="missing",
                    streaming=True,
                ),
            )
        )
    assert failed.value.code == "invalid_request"
    assert failed.value.phase is CallFailurePhase.BEFORE_VISIBLE_OUTPUT
    assert failed.value.field == "streaming"
    assert adapter.calls == []
    with psycopg.connect(call_context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.raw_accounting_calls"
        ).fetchone() == (0,)


@pytest.mark.parametrize("visible_kind", ["text_delta", "tool_call"])
def test_visible_output_is_the_exact_no_fallback_boundary(
    call_context: CallContext, visible_kind: str
) -> None:
    """Fallback before output, then stop after the first text or tool event."""
    visible = (
        ProviderOutput("text_delta", '"part"')
        if visible_kind == "text_delta"
        else ProviderOutput(
            "tool_call",
            '{"type":"tool_call","id":"one","name":"lookup","arguments_json":"{}"}',
        )
    )
    adapter = ScriptedAdapter(
        {
            "text-a": [[ProviderFailureError("timeout")]],
            "text-b": [[visible, ProviderFailureError("transport")]],
        }
    )
    delivered: list[ProviderOutput] = []

    async def write(event: ProviderOutput) -> None:
        delivered.append(event)

    with pytest.raises(CallExecutionError) as interrupted:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["child"],
                _text_request(
                    AssignmentSelector("inherited"),
                    streaming=True,
                    excluded=("plain",),
                    capabilities=(
                        frozenset({"tool_calling"})
                        if visible_kind == "tool_call"
                        else frozenset()
                    ),
                ),
                write_visible_output=write,
            )
        )
    assert interrupted.value.phase is CallFailurePhase.AFTER_VISIBLE_OUTPUT
    assert adapter.calls == ["text-a", "text-b"]
    assert delivered == [visible]
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        log = connection.execute(
            "SELECT response_json, attempts FROM router.request_logs"
        ).fetchone()
        assert log is not None
        assert log["response_json"] is None
        assert "response_json" not in log["attempts"][0]
        assert json.loads(log["attempts"][1]["response_json"]) == [
            {"kind": visible.kind, "value": json.loads(visible.content_json)}
        ]


def test_writer_failure_after_possible_partial_send_stops_fallback(
    call_context: CallContext,
) -> None:
    """Treat a writer failure as visible because a partial send is uncertain."""
    visible = ProviderOutput("text_delta", '"partial"')
    adapter = ScriptedAdapter(
        {
            "text-a": [[visible, _completed()]],
            "text-b": [[ProviderOutput("text_delta", '"not-used"'), _completed()]],
        }
    )

    async def fail_writer(_event: ProviderOutput) -> None:
        message = "private writer state"
        raise RuntimeError(message)

    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(
                    AssignmentSelector("workflow"),
                    streaming=True,
                    excluded=("plain",),
                ),
                write_visible_output=fail_writer,
            )
        )
    assert failed.value.phase is CallFailurePhase.AFTER_VISIBLE_OUTPUT
    assert adapter.calls == ["text-a"]
    assert "private writer state" not in str(failed.value)


def test_detailed_log_keeps_each_buffered_fallback_response(
    call_context: CallContext,
) -> None:
    """Keep failed and successful provider content in its exact attempt."""
    first = _standard("failed-buffered")
    second = _standard("successful-fallback")
    adapter = ScriptedAdapter(
        {
            "text-a": [[first, ProviderFailureError("transport", usage=_usage())]],
            "text-b": [[second, _completed()]],
        }
    )
    result = asyncio.run(
        call_context.executor(adapter).execute(
            call_context.actors["alpha"],
            _text_request(AssignmentSelector("workflow"), excluded=("plain",)),
        )
    )
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        log = connection.execute(
            "SELECT response_json, attempts FROM router.request_logs WHERE id = %s",
            (result.call_id,),
        ).fetchone()
        assert log is not None
        assert json.loads(log["response_json"]) == [
            {"kind": second.kind, "value": json.loads(second.content_json)}
        ]
        assert second.content_json in log["response_json"]
        assert first.content_json in log["attempts"][0]["response_json"]
        assert [
            json.loads(attempt["response_json"])[0]["value"][0]["text"]
            for attempt in log["attempts"]
        ] == ["failed-buffered", "successful-fallback"]
        assert log["attempts"][0]["error"]["code"] == "upstream_failed"


def test_structured_embedding_and_media_validation_use_normal_fallback(
    call_context: CallContext,
) -> None:
    """Reject provider-caused invalid values before success for each output family."""
    structured_adapter = ScriptedAdapter(
        {
            "structured": [
                [ProviderOutput("structured_json", '{"ok":false}'), _completed()],
                [ProviderOutput("structured_json", '{"ok":true}'), _completed()],
            ]
        }
    )
    structured = CallRequest(
        "main",
        ExactModelSelector("structured"),
        "model",
        CallRequirements(frozenset({"text"}), "structured_json"),
        '{"output_format":{"type":"json_schema"}}',
        output_validator=lambda value: (
            isinstance(value, dict) and value.get("ok") is True
        ),
    )
    with pytest.raises(CallExecutionError):
        asyncio.run(
            call_context.executor(structured_adapter).execute(
                call_context.actors["alpha"], structured
            )
        )
    result = asyncio.run(
        call_context.executor(structured_adapter).execute(
            call_context.actors["alpha"], structured
        )
    )
    assert result.outputs[0].kind == "structured_json"

    embedding_adapter = ScriptedAdapter(
        {
            "embedding-a": [[ProviderOutput("embedding", "[[1,2]]"), _completed()]],
            "embedding-b": [
                [ProviderOutput("embedding", "[[1,2,3],[4,5,6]]"), _completed()]
            ],
        }
    )
    embedding = CallRequest(
        "main",
        AssignmentSelector("embeddings"),
        "embedding",
        CallRequirements(frozenset({"text"}), "embedding", embedding_dimension=3),
        '{"inputs":["one","two"]}',
        expected_embedding_count=2,
    )
    result = asyncio.run(
        call_context.executor(embedding_adapter).execute(
            call_context.actors["alpha"], embedding
        )
    )
    assert embedding_adapter.calls == ["embedding-a", "embedding-b"]
    assert result.provider_model_api_name == "embedding-b"

    for output in ("image", "video", "audio"):
        media_type = {
            "image": "image/png",
            "video": "video/mp4",
            "audio": "audio/mpeg",
        }[output]
        adapter = ScriptedAdapter(
            {
                "media": [
                    [
                        ProviderOutput(
                            "media",
                            json.dumps({"media_type": media_type, "size_bytes": 8}),
                        ),
                        _completed(),
                    ]
                ]
            }
        )
        result = asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                CallRequest(
                    "main",
                    AssignmentSelector("media"),
                    "media",
                    CallRequirements(
                        frozenset({"text"}),
                        output,
                        output_duration_seconds=10
                        if output in {"video", "audio"}
                        else None,
                    ),
                    json.dumps({"kind": output, "prompt": "safe"}),
                ),
            )
        )
        assert result.provider_model_api_name == "media"


def test_actual_tools_images_and_bounds_filter_before_provider_work(
    call_context: CallContext,
) -> None:
    """Use current call requirements and reject global bounds before an adapter call."""
    adapter = ScriptedAdapter({"text-a": [[_standard(), _completed()]]})
    request = CallRequest(
        "main",
        AssignmentSelector("workflow"),
        "model",
        CallRequirements(
            frozenset({"text", "image"}),
            "text",
            frozenset({"tool_calling"}),
            input_image_sizes=(8,),
        ),
        '{"messages":[{"role":"user","content":"image"}],"tools":[{"name":"x"}]}',
        excluded_provider_model_api_names=("text-b",),
        media=(diagnostics.CapturedMedia(b"12345678", "image/png", "input"),),
    )
    result = asyncio.run(
        call_context.executor(adapter).execute(call_context.actors["alpha"], request)
    )
    assert result.provider_model_api_name == "text-a"
    assert adapter.calls == ["text-a"]
    with pytest.raises(ValueError, match="image bounds"):
        CallRequirements(
            frozenset({"text", "image"}),
            "text",
            input_image_sizes=(20_971_521,),
        )
    with pytest.raises(ValueError, match="assignment call"):
        _text_request(ExactModelSelector("text-a"), excluded=("text-b",))
    with pytest.raises(ValueError, match="timeout"):
        CallLimits(attempt_timeout_seconds=601)
    with pytest.raises(ValueError, match="output JSON"):
        CallLimits(maximum_output_json_bytes=5_000_001)
    with pytest.raises(ValueError, match="valid JSON"):
        ProviderOutput("embedding", "[NaN]")


def test_exclusion_precedes_invalid_candidate_eligibility(
    call_context: CallContext,
) -> None:
    """Do not inspect an excluded invalid route or let it change the call."""
    adapter = ScriptedAdapter({"text-a": [[_standard(), _completed()]]})
    result = asyncio.run(
        call_context.executor(adapter).execute(
            call_context.actors["alpha"],
            _text_request(AssignmentSelector("excluded-broken"), excluded=("broken",)),
        )
    )
    assert result.provider_model_api_name == "text-a"
    assert adapter.calls == ["text-a"]


@pytest.mark.parametrize("output_mode", ["buffered", "streaming"])
def test_unexpected_tool_output_fails_before_visible_output(
    call_context: CallContext, output_mode: str
) -> None:
    """Reject each tool result when the call did not require tool calling."""
    streaming = output_mode == "streaming"
    tool = {
        "type": "tool_call",
        "id": "one",
        "name": "lookup",
        "arguments_json": "{}",
    }
    output = ProviderOutput(
        "tool_call" if streaming else "standard",
        json.dumps(tool if streaming else [tool]),
    )
    adapter = ScriptedAdapter({"text-a": [[output, _completed()]]})
    delivered: list[ProviderOutput] = []

    async def write(event: ProviderOutput) -> None:
        delivered.append(event)

    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a"), streaming=streaming),
                write_visible_output=write,
            )
        )
    assert failed.value.code == "upstream_failed"
    assert failed.value.phase is CallFailurePhase.BEFORE_VISIBLE_OUTPUT
    assert delivered == []


def test_stream_tool_call_id_is_unique_without_fallback(
    call_context: CallContext,
) -> None:
    """Stop after a provider repeats one visible tool-call identity."""
    tool = ProviderOutput(
        "tool_call",
        '{"type":"tool_call","id":"one","name":"lookup","arguments_json":"{}"}',
    )
    adapter = ScriptedAdapter(
        {
            "text-a": [[tool, tool, _completed()]],
            "text-b": [[tool, _completed()]],
        }
    )
    delivered: list[ProviderOutput] = []

    async def write(event: ProviderOutput) -> None:
        delivered.append(event)

    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(
                    AssignmentSelector("workflow"),
                    streaming=True,
                    excluded=("plain",),
                    capabilities=frozenset({"tool_calling"}),
                ),
                write_visible_output=write,
            )
        )
    assert failed.value.phase is CallFailurePhase.AFTER_VISIBLE_OUTPUT
    assert adapter.calls == ["text-a"]
    assert delivered == [tool]


def test_incomplete_declared_price_skips_provider_work(
    call_context: CallContext,
) -> None:
    """Do not call an adapter when its possible billable units have no price."""
    adapter = ScriptedAdapter(
        {"text-a": [[_standard(), _completed()]]},
        usage_units=frozenset({"request", "image"}),
    )
    with pytest.raises(CallExecutionError) as unavailable:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
    assert unavailable.value.code == "provider_unavailable"
    assert adapter.calls == []


def test_undeclared_failure_usage_has_safe_incomplete_cost_posture(
    call_context: CallContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Do not store a false full cost when an adapter breaks its declaration."""
    adapter = ScriptedAdapter(
        {
            "text-a": [
                [
                    ProviderFailureError(
                        "rate_limited",
                        usage=(accounting.UsageAmount("input_token", Decimal(10)),),
                    )
                ]
            ]
        },
        usage_units=frozenset({"request"}),
    )
    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
    assert failed.value.code == "internal_error"
    assert failed.value.phase is CallFailurePhase.UNCERTAIN
    assert "input_token" not in str(failed.value)
    messages = [record.getMessage() for record in caplog.records]
    assert any("Provider adapter usage declaration breach" in item for item in messages)
    assert any("text-a" in item and "input_token" in item for item in messages)
    assert all("10" not in item for item in messages)
    with psycopg.connect(call_context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.raw_accounting_calls"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.request_logs"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("selector", "excluded"),
    [
        (AssignmentSelector("workflow"), ("plain", "text-b")),
        (ExactModelSelector("text-a"), ()),
    ],
    ids=["assignment", "exact"],
)
def test_admission_atomically_freezes_route_price_and_credential(
    call_context: CallContext,
    selector: AssignmentSelector | ExactModelSelector,
    excluded: tuple[str, ...],
) -> None:
    """Use one immutable admission snapshot while a catalog writer commits."""
    started = asyncio.Event()
    release = asyncio.Event()
    adapter = ScriptedAdapter(
        {"text-a": [[WaitForRelease(started, release), _standard(), _completed()]]}
    )

    def replace_catalog() -> None:
        encrypted, fingerprint = call_context.credential_keys.encrypt(
            "provider-control", "provider-secret-replaced"
        )
        replacement_price = _price()
        cast("list[dict[str, str]]", replacement_price["unit_prices"])[0]["amount"] = (
            "9.00"
        )
        with psycopg.connect(call_context.database_url) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (4_993_044_345_823,))
            connection.execute(
                """UPDATE router.provider_models
                   SET provider_model_name = 'text-a-replaced'
                   WHERE api_name = 'text-a'"""
            )
            connection.execute(
                """UPDATE router.canonical_models
                   SET manual_price = %s::jsonb WHERE api_name = 'text'""",
                (json.dumps(replacement_price),),
            )
            connection.execute(
                """UPDATE router.provider_credentials
                   SET encrypted_secret = %s, fingerprint = %s
                   WHERE api_name = 'provider-control'""",
                (encrypted, fingerprint),
            )

    async def run_case() -> CallResult:
        task = asyncio.create_task(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(selector, excluded=excluded),
            )
        )
        await started.wait()
        adapter.usage_units = frozenset({"input_token"})
        await asyncio.to_thread(replace_catalog)
        release.set()
        return await task

    result = asyncio.run(run_case())
    assert result.provider_model_api_name == "text-a"
    assert result.cost == Decimal("0.25")
    assert adapter.requests[0].route.provider_model_name == "text-a"
    assert adapter.requests[0].credential == "provider-secret-control"
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        current = connection.execute(
            """SELECT mapping.provider_model_name, model.manual_price
               FROM router.provider_models AS mapping
               JOIN router.canonical_models AS model ON model.id = mapping.model_id
               WHERE mapping.api_name = 'text-a'"""
        ).fetchone()
        assert current is not None
        assert current["provider_model_name"] == "text-a-replaced"
        assert current["manual_price"]["unit_prices"][0]["amount"] == "9.00"
        assert (
            catalog.resolve_credential(
                connection, "provider-control", call_context.credential_keys
            )
            == "provider-secret-replaced"
        )


def test_cooldown_uses_exact_three_failure_rolling_window_and_restart_reset() -> None:
    """Start at three failures, expire at 60 seconds, and stay best-effort."""
    now = [0.0]
    cooldowns = ProviderCooldowns(clock=lambda: now[0])
    assert not cooldowns.record_failure("text-a", "refusal")
    assert not cooldowns.record_failure("text-a", "transport")
    now[0] = 30.0
    assert not cooldowns.record_failure("text-a", "timeout")
    now[0] = 60.0
    assert cooldowns.record_failure("text-a", "invalid_response")
    assert cooldowns.is_active("text-a")
    assert cooldowns.snapshots()[0].last_failure_class == "invalid_response"
    now[0] = 61.0
    assert cooldowns.record_failure("text-a", "transport")
    assert cooldowns.snapshots()[0].remaining_seconds == 59.0
    now[0] = 119.999
    assert cooldowns.is_active("text-a")
    now[0] = 120.0
    assert not cooldowns.is_active("text-a")
    assert not cooldowns.snapshots()
    assert not ProviderCooldowns(clock=lambda: now[0]).is_active("text-a")

    concurrent = ProviderCooldowns(clock=lambda: 10.0)
    failures: tuple[Any, ...] = ("authentication", "rate_limited", "unavailable")
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda failure: concurrent.record_failure("text-b", failure),
                failures,
            )
        )
    assert sum(results) == 1
    assert concurrent.is_active("text-b")


@pytest.mark.parametrize(
    ("failure_class", "counts"),
    [
        ("authentication", True),
        ("rate_limited", True),
        ("timeout", True),
        ("transport", True),
        ("unavailable", True),
        ("invalid_response", True),
        ("refusal", False),
        ("incompatible", False),
        ("interrupted", False),
        ("upstream_failed", False),
    ],
)
def test_cooldown_counting_property_for_each_failure_class(
    failure_class: str, *, counts: bool
) -> None:
    """Count exactly the required safe failure classes for a cooldown."""
    cooldowns = ProviderCooldowns(clock=lambda: 10.0)
    results = [
        cooldowns.record_failure("text-a", cast("Any", failure_class)) for _ in range(3)
    ]
    assert results == ([False, False, True] if counts else [False] * 3)
    assert cooldowns.is_active("text-a") is counts


def test_uncertain_media_write_stops_fallback_and_records_one_attempt(
    call_context: CallContext,
) -> None:
    """Do not create replacement media after an uncertain provider write."""
    with psycopg.connect(call_context.database_url) as connection:
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name,
                    enabled, input_modalities, output_modalities,
                    capabilities, constraints, reasoning_mappings)
               SELECT 'media-b', provider_id, model_id, 'media-b', enabled,
                      input_modalities, output_modalities, capabilities,
                      constraints, reasoning_mappings
               FROM router.provider_models WHERE api_name = 'media'"""
        )
        connection.execute(
            """INSERT INTO router.assignment_candidates
                   (assignment_id, position, provider_model_id)
               SELECT assignment.id, 1, mapping.id
               FROM router.assignment_definitions AS assignment
               JOIN router.services AS service ON service.id = assignment.service_id
               JOIN router.provider_models AS mapping ON mapping.api_name = 'media-b'
               WHERE service.api_name = 'alpha'
                 AND assignment.api_name = 'media'"""
        )
    adapter = ScriptedAdapter(
        {
            "media": [[RuntimeError("private uncertain provider state")]],
            "media-b": [
                [
                    ProviderOutput(
                        "media",
                        '{"media_type":"image/png","size_bytes":8}',
                    ),
                    _completed(),
                ]
            ],
        }
    )
    request = CallRequest(
        "main",
        AssignmentSelector("media"),
        "media",
        CallRequirements(frozenset({"text"}), "image"),
        '{"kind":"image","prompt":"safe"}',
    )
    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"], request
            )
        )
    assert failed.value.code == "upstream_failed"
    assert failed.value.phase is CallFailurePhase.UNCERTAIN
    assert adapter.calls == ["media"]
    assert "private uncertain provider state" not in str(failed.value)
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        attempts = connection.execute(
            """SELECT provider_model_api_name, outcome, failure_class
               FROM router.raw_accounting_attempts"""
        ).fetchall()
        assert attempts == [
            {
                "provider_model_api_name": "media",
                "outcome": "failed",
                "failure_class": "transport",
            }
        ]


def test_explicit_uncertainty_wins_after_reported_usage(
    call_context: CallContext,
) -> None:
    """Keep an explicit uncertain phase after an invalid completion sequence."""
    adapter = ScriptedAdapter(
        {
            "text-a": [
                [
                    ProviderCompleted(_usage()),
                    ProviderFailureError("transport", phase=CallFailurePhase.UNCERTAIN),
                ]
            ],
            "text-b": [[_standard(), _completed()]],
        }
    )
    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(AssignmentSelector("workflow"), excluded=("plain",)),
            )
        )
    assert failed.value.phase is CallFailurePhase.UNCERTAIN
    assert adapter.calls == ["text-a"]
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        attempt = connection.execute(
            """SELECT usage, cost, failure_class
               FROM router.raw_accounting_attempts"""
        ).fetchone()
        assert attempt == {
            "usage": [{"unit": "request", "quantity": "1"}],
            "cost": Decimal("0.25"),
            "failure_class": "invalid_response",
        }


def test_response_log_accepts_the_complete_contract_bound(
    call_context: CallContext,
) -> None:
    """Keep a valid response that is larger than the former database bound."""
    response_json = json.dumps("x" * 9_999_998)
    assert len(response_json) == 10_000_000
    log_id = diagnostics.write_detailed_log_best_effort(
        call_context.database_url,
        None,
        diagnostics.DetailedLogWrite(
            service_id=call_context.service_ids["alpha"],
            workspace_id=call_context.workspace_ids["alpha"],
            kind="model",
            outcome="succeeded",
            request_json="{}",
            response_json=response_json,
            attempts=(),
            started_at=datetime.now(tz=UTC),
        ),
    )
    assert log_id is not None
    with psycopg.connect(call_context.database_url) as connection:
        assert connection.execute(
            "SELECT char_length(response_json) FROM router.request_logs WHERE id = %s",
            (log_id,),
        ).fetchone() == (len(response_json),)


def test_attempt_cost_keeps_all_supported_decimal_precision(
    call_context: CallContext,
) -> None:
    """Keep one exact immutable cost without Decimal rounding."""
    quantity = Decimal("12345678901234567890.123456789012345678")
    expected = Decimal("12.345678901234567890123456789012345678")
    with psycopg.connect(call_context.database_url) as connection:
        price = _price()
        cast("list[dict[str, str]]", price["unit_prices"])[0]["amount"] = (
            "0.000000000000000001"
        )
        connection.execute(
            """UPDATE router.canonical_models
               SET manual_price = %s::jsonb WHERE api_name = 'text'""",
            (json.dumps(price),),
        )
    adapter = ScriptedAdapter(
        {
            "text-a": [
                [
                    _standard(),
                    ProviderCompleted((accounting.UsageAmount("request", quantity),)),
                ]
            ]
        },
        usage_units=frozenset({"request"}),
    )
    result = asyncio.run(
        call_context.executor(adapter).execute(
            call_context.actors["alpha"],
            _text_request(ExactModelSelector("text-a")),
        )
    )
    assert result.cost == expected
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        attempt = connection.execute(
            """SELECT cost FROM router.raw_accounting_attempts
               WHERE call_id = %s""",
            (result.call_id,),
        ).fetchone()
        log = connection.execute(
            "SELECT attempts FROM router.request_logs WHERE id = %s",
            (result.call_id,),
        ).fetchone()
        assert attempt == {"cost": expected}
        assert log is not None
        assert log["attempts"][0]["usage"]["cost"] == format(expected, "f")


def test_dependency_timeout_concurrency_and_accounting_rollback(
    call_context: CallContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound active calls and do not publish output after durable-fact failure."""
    started = asyncio.Event()
    release = asyncio.Event()
    adapter = ScriptedAdapter(
        {"text-a": [[WaitForRelease(started, release), _standard(), _completed()]]}
    )
    executor = call_context.executor(adapter, limits=CallLimits(concurrency=1))

    async def concurrent_case() -> None:
        first = asyncio.create_task(
            executor.execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
        await started.wait()
        with pytest.raises(CallExecutionError) as full:
            await executor.execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        assert full.value.code == "rate_limited"
        release.set()
        await first

    asyncio.run(concurrent_case())

    dependency = ScriptedAdapter(
        {
            "text-a": [[RuntimeError("private dependency detail")]],
            "text-b": [[_standard(), _completed()]],
        }
    )
    result = asyncio.run(
        call_context.executor(dependency).execute(
            call_context.actors["child"],
            _text_request(AssignmentSelector("inherited"), excluded=("plain",)),
        )
    )
    assert result.provider_model_api_name == "text-b"

    rollback = ScriptedAdapter({"text-a": [[_standard(), _completed()]]})

    def fail_accounting(*_args: object, **_kwargs: object) -> None:
        raise psycopg.DatabaseError

    monkeypatch.setattr(accounting, "record_call_accounting", fail_accounting)
    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            call_context.executor(rollback).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
    assert failed.value.code == "internal_error"
    assert failed.value.phase is CallFailurePhase.UNCERTAIN


def test_database_lock_timeout_is_bounded_and_does_not_stall_event_loop(
    call_context: CallContext,
) -> None:
    """Return one safe failure while a catalog transaction holds the lock."""
    adapter = ScriptedAdapter({})
    holder = psycopg.connect(call_context.database_url)
    holder.execute("SELECT pg_advisory_xact_lock(%s)", (4_993_044_345_823,))

    async def run_case() -> CallExecutionError:
        task = asyncio.create_task(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        try:
            await task
        except CallExecutionError as error:
            return error
        pytest.fail("The locked call unexpectedly succeeded.")

    async def run_cancelled_case() -> None:
        task = asyncio.create_task(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started_at = monotonic()
    try:
        error = asyncio.run(run_case())
        asyncio.run(run_cancelled_case())
    finally:
        holder.rollback()
        holder.close()
    assert monotonic() - started_at < 2
    assert error.code == "internal_error"
    assert error.phase is CallFailurePhase.BEFORE_VISIBLE_OUTPUT
    assert adapter.calls == []


def test_database_connection_failure_is_safe_and_bounded(
    call_context: CallContext,
) -> None:
    """Hide connection details and apply the short database connect timeout."""
    adapter = ScriptedAdapter({})
    executor = CallExecutor(
        database_url="postgresql://router@127.0.0.1:1/router",
        adapters={"fake": cast("ProviderAdapter", adapter)},
        credential_keys=call_context.credential_keys,
    )
    started_at = monotonic()
    with pytest.raises(CallExecutionError) as failed:
        asyncio.run(
            executor.execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
    assert monotonic() - started_at < 3
    assert failed.value.code == "internal_error"
    assert failed.value.phase is CallFailurePhase.BEFORE_VISIBLE_OUTPUT
    assert "127.0.0.1" not in str(failed.value)


def test_provider_cancellation_keeps_already_reported_usage(
    call_context: CallContext,
) -> None:
    """Record available failed-attempt usage before a cancelled call returns."""
    started = asyncio.Event()
    release = asyncio.Event()
    adapter = ScriptedAdapter(
        {"text-a": [[ProviderCompleted(_usage()), WaitForRelease(started, release)]]}
    )

    async def run_case() -> None:
        task = asyncio.create_task(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_case())
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        attempt = connection.execute(
            """SELECT usage, cost, outcome FROM router.raw_accounting_attempts"""
        ).fetchone()
        assert attempt is not None
        assert attempt["usage"] == [{"unit": "request", "quantity": "1"}]
        assert attempt["cost"] == Decimal("0.25")
        assert attempt["outcome"] == "failed"
        log = connection.execute("SELECT attempts FROM router.request_logs").fetchone()
        assert log is not None
        assert log["attempts"][0]["usage"]["units"] == [
            {"unit": "request", "quantity": "1"}
        ]


def test_workspace_delete_waits_for_provider_and_accounting(
    call_context: CallContext,
) -> None:
    """Keep the workspace until one admitted provider call records its facts."""
    started = asyncio.Event()
    release = asyncio.Event()
    adapter = ScriptedAdapter(
        {"text-a": [[WaitForRelease(started, release), _standard(), _completed()]]}
    )

    def delete_workspace() -> None:
        with psycopg.connect(call_context.database_url) as connection:
            connection.execute(
                """DELETE FROM router.workspaces
                   WHERE service_id = %s AND api_name = 'main'""",
                (call_context.service_ids["alpha"],),
            )

    async def run_case() -> CallResult:
        call_task = asyncio.create_task(
            call_context.executor(adapter).execute(
                call_context.actors["alpha"],
                _text_request(ExactModelSelector("text-a")),
            )
        )
        await started.wait()
        delete_task = asyncio.create_task(asyncio.to_thread(delete_workspace))
        await asyncio.sleep(0.05)
        assert not delete_task.done()
        release.set()
        result = await call_task
        await delete_task
        return result

    result = asyncio.run(run_case())
    assert result.cost == Decimal("0.25")
    with psycopg.connect(call_context.database_url) as connection:
        assert connection.execute(
            """SELECT count(*) FROM router.workspaces
               WHERE service_id = %s AND api_name = 'main'""",
            (call_context.service_ids["alpha"],),
        ).fetchone() == (0,)


def test_provider_attempt_timeout_is_safe_and_falls_back(
    call_context: CallContext,
) -> None:
    """Stop one slow provider at the attempt deadline and use the next candidate."""

    class TimeoutThenSuccess(ScriptedAdapter):
        async def attempt(
            self, request: ProviderAttemptRequest, /
        ) -> AsyncIterator[ProviderEvent]:
            self.calls.append(request.route.provider_model_api_name)
            self.credentials.append(request.credential)
            if request.route.provider_model_api_name == "text-a":
                yield ProviderCompleted(_usage())
                await asyncio.sleep(2)
                return
            yield _standard()
            yield _completed()

    adapter = TimeoutThenSuccess({})
    result = asyncio.run(
        call_context.executor(
            adapter,
            limits=CallLimits(attempt_timeout_seconds=1, connection_timeout_seconds=3),
        ).execute(
            call_context.actors["child"],
            _text_request(AssignmentSelector("inherited"), excluded=("plain",)),
        )
    )
    assert result.provider_model_api_name == "text-b"
    assert adapter.calls == ["text-a", "text-b"]
    with psycopg.connect(call_context.database_url, row_factory=dict_row) as connection:
        attempts = connection.execute(
            """SELECT provider_model_api_name, usage, cost, failure_class
               FROM router.raw_accounting_attempts ORDER BY position"""
        ).fetchall()
        assert attempts[0] == {
            "provider_model_api_name": "text-a",
            "usage": [{"unit": "request", "quantity": "1"}],
            "cost": Decimal("0.25"),
            "failure_class": "timeout",
        }


def test_detailed_log_rows_reject_mutation(call_context: CallContext) -> None:
    """Keep a completed detailed request log immutable in PostgreSQL."""
    adapter = ScriptedAdapter({"text-a": [[_standard(), _completed()]]})
    result = asyncio.run(
        call_context.executor(adapter).execute(
            call_context.actors["alpha"], _text_request(ExactModelSelector("text-a"))
        )
    )
    with psycopg.connect(call_context.database_url) as connection:
        with pytest.raises(psycopg.errors.CheckViolation) as immutable:
            connection.execute(
                "UPDATE router.request_logs SET outcome = 'failed' WHERE id = %s",
                (result.call_id,),
            )
        assert immutable.value.diag.constraint_name == "request_log_immutable"
