"""Regression tests for the localhost development deployment."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import httpx
import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = REPOSITORY_ROOT / "scripts/check-local-development.py"
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.dev.yml"
BOOTSTRAP_PATH = REPOSITORY_ROOT / "scripts/local-development-bootstrap.py"
LIVE_PATH = REPOSITORY_ROOT / "scripts/local-development-live-openrouter.py"


def _check_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_local_development", CHECK_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _bootstrap_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "local_development_bootstrap", BOOTSTRAP_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _live_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "local_development_live_openrouter", LIVE_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_local_development_contract_accepts_repository_compose() -> None:
    """Accept the complete immutable loopback deployment."""
    _check_module().main()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("127.0.0.1:8010:8000", "8010:8000", "public port binding"),
        (
            '"127.0.0.1:8010:8000"',
            "0.0.0.0:8010:8000",
            "public port binding",
        ),
        (
            "node:24.17.0-alpine@sha256:156b55f92e98ccd5ef49578a8cea0df4679826564bad1c9d4ef04462b9f0ded6",
            "node:24.17.0-alpine",
            "not immutable",
        ),
    ],
)
def test_local_development_contract_rejects_unsafe_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    """Reject a public port or floating image before local startup."""
    unsafe = tmp_path / "docker-compose.dev.yml"
    source = COMPOSE_PATH.read_text(encoding="utf-8")
    assert old in source
    unsafe.write_text(source.replace(old, new, 1), encoding="utf-8")
    module = _check_module()
    monkeypatch.setattr(module, "COMPOSE_PATH", unsafe)
    with pytest.raises(SystemExit, match=message):
        module.main()


def test_local_start_script_rejects_a_public_address() -> None:
    """Keep the wrapper public-binding failure explicit and early."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    validation = script.index("validate_environment")
    startup = script.index("compose up")
    assert validation < startup
    assert "LLMROUTER_BIND_ADDRESS:-127.0.0.1" in script
    assert "can bind only to 127.0.0.1" in script


def test_local_start_serializes_operations_and_installs_secrets_exclusively() -> None:
    """Prevent concurrent startup and unsafe secret replacement."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    assert "flock --nonblock" in script
    assert 'ln "${temporary}" "${target}"' in script
    assert "stat -c %h" in script
    assert ">${target}" not in script
    assert "cleanup_failed_start" in script


def test_bootstrap_writer_rejects_a_secret_symlink(tmp_path: Path) -> None:
    """Do not follow a replaced local secret path."""
    victim = tmp_path / "victim"
    victim.write_text("keep\n", encoding="ascii")
    target = tmp_path / "token"
    target.symlink_to(victim)
    with pytest.raises(SystemExit, match="unsafe"):
        _bootstrap_module()._write_secret(target, "replacement")  # noqa: SLF001
    assert victim.read_text(encoding="ascii") == "keep\n"


def test_bootstrap_writer_rejects_a_secret_hard_link(tmp_path: Path) -> None:
    """Do not truncate a multiply linked secret file."""
    victim = tmp_path / "victim"
    victim.write_text("keep\n", encoding="ascii")
    target = tmp_path / "token"
    target.hardlink_to(victim)
    with pytest.raises(SystemExit, match="unsafe"):
        _bootstrap_module()._write_secret(target, "replacement")  # noqa: SLF001
    assert victim.read_text(encoding="ascii") == "keep\n"


def test_local_secret_paths_are_ignored_and_not_printed() -> None:
    """Keep generated secret material outside tracked and displayed output."""
    ignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    assert ".local-development/" in ignore
    assert "set -x" not in script
    assert "cat /run/secrets" not in script
    assert "LLMROUTER_OPENROUTER_API_KEY" not in script
    assert "env -u OPENROUTER_API_KEY docker compose" in script
    for name in (
        "credential-wrapping-key",
        "idempotency-digest-key",
        "distribution-key",
        "canonical-replay-key",
        "administrator-session",
        "administrator-csrf",
        "data-plane-token",
    ):
        assert f'install_secret "${{state_directory}}/{name}"' in script
    assert "local-development-e2e.py prepare" in script
    assert "local-development-e2e.py resume" in script
    assert "compose kill --signal KILL backend" in script
    assert "compose up --detach backend" in script


def test_local_proof_resets_and_stops_the_deployment() -> None:
    """Keep the clean deterministic proof as one safe command."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    proof = script[script.index("    prove)") : script.index("    *)")]
    assert 'local-development.sh" reset' in proof
    assert 'local-development.sh" start' in proof
    assert 'local-development.sh" e2e' in proof
    assert 'local-development.sh" stop' in proof
    assert "trap" in proof


def test_live_openrouter_proof_is_guarded_and_always_resets() -> None:
    """Keep the provider key outside Compose and remove live database state."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    live = script[
        script.index(
            "    live-openrouter|live-openrouter-mimo|live-openrouter-granite|"
            "live-openrouter-granite-stream)"
        ) : script.index("    *)")
    ]
    assert "! -v OPENROUTER_API_KEY" in live
    assert live.count("env -u OPENROUTER_API_KEY") >= 3  # noqa: PLR2004
    assert "LLMROUTER_LOCAL_OPENROUTER_LIVE=1" in live
    assert "local-development-live-openrouter.py" in live
    assert "model_arguments=(--model mimo)" in live
    assert "model_arguments=(--model granite)" in live
    assert "model_arguments=(--model granite --stream-only)" in live
    assert (
        'scripts/local-development-live-openrouter.py "${model_arguments[@]}"' in live
    )
    assert "env -u OPENROUTER_API_KEY LLMROUTER_LOCAL_OPENROUTER_LIVE=1" in live
    prove = live.index('local-development.sh" prove')
    reset = live.index('local-development.sh" reset', prove)
    live_start = live.index("LLMROUTER_LOCAL_OPENROUTER_LIVE=1", reset)
    assert prove < reset < live_start
    assert "model_arguments" not in live[reset:live_start]
    assert live.count('local-development.sh" reset') >= 3  # noqa: PLR2004
    assert "trap" in live


@pytest.mark.parametrize(
    ("selector", "wire_model", "input_price", "output_price"),
    [
        (
            "deepseek",
            "deepseek/deepseek-v4-flash",
            "0.00000009",
            "0.00000018",
        ),
        ("mimo", "xiaomi/mimo-v2.5", "0.0000002", "0.0000006"),
        (
            "granite",
            "ibm-granite/granite-4.1-8b",
            "0.00000005",
            "0.0000001",
        ),
    ],
)
def test_live_openrouter_preflight_accepts_only_the_selected_bounded_model(
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
    wire_model: str,
    input_price: str,
    output_price: str,
) -> None:
    """Validate current model and cost metadata without an inference call."""
    module = _live_module()

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, path: str, **kwargs: object) -> httpx.Response:
            if path == "/key":
                return httpx.Response(200, json={"data": {"limit_remaining": 1}})
            assert path == "/models/user"
            assert kwargs["params"] == {"q": module.LIVE_MODELS[selector].display_name}
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": wire_model,
                            "supported_parameters": ["max_tokens"],
                            "pricing": {
                                "prompt": input_price,
                                "completion": output_price,
                                "request": "0",
                            },
                        }
                    ]
                },
            )

    monkeypatch.setattr(module.httpx, "Client", FakeClient)
    prices = module._provider_preflight(  # noqa: SLF001
        "test-provider-secret-placeholder", module.LIVE_MODELS[selector]
    )
    assert prices == {
        "input_token": Decimal(input_price),
        "output_token": Decimal(output_price),
        "request": Decimal(0),
    }
    estimated_request_cost = (
        Decimal(128) * prices["input_token"]
        + Decimal(module.MAXIMUM_OUTPUT_UNITS) * prices["output_token"]
        + prices["request"]
    )
    assert estimated_request_cost <= module.MAXIMUM_REQUEST_COST


def test_live_openrouter_preflight_stops_on_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make no inference call after one failed no-cost authentication check."""
    module = _live_module()

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, path: str, **_kwargs: object) -> httpx.Response:
            assert path == "/key"
            return httpx.Response(401, json={"error": {"code": "authentication"}})

    monkeypatch.setattr(module.httpx, "Client", FakeClient)
    with pytest.raises(module.LiveProofError, match="authentication preflight"):
        module._provider_preflight(  # noqa: SLF001
            "test-provider-secret-placeholder", module.LIVE_MODELS["deepseek"]
        )


def test_live_openrouter_model_selector_and_catalog_identities_are_closed() -> None:
    """Keep MiMo separate while DeepSeek stays the default model."""
    module = _live_module()
    bootstrap = _bootstrap_module()

    default = module._selected_plan(())  # noqa: SLF001
    mimo = module._selected_plan(("--model", "mimo"))  # noqa: SLF001
    granite = module._selected_plan(("--model", "granite"))  # noqa: SLF001
    diagnostic = module._selected_plan(  # noqa: SLF001
        ("--model", "granite", "--stream-only")
    )
    assert default.model.selector == "deepseek"
    assert default.operations == ("non-stream", "stream")
    assert mimo.model.wire_model == (
        "xiaomi/mimo-v2.5"
    )
    assert granite.model.wire_model == (
        "ibm-granite/granite-4.1-8b"
    )
    assert diagnostic.model is module.LIVE_MODELS["granite"]
    assert diagnostic.operations == ("stream",)
    assert (
        module.LIVE_MODELS["deepseek"].canonical_model_id
        == bootstrap.DEEPSEEK_CANONICAL_MODEL_ID
    )
    assert (
        module.LIVE_MODELS["mimo"].canonical_model_id
        == bootstrap.MIMO_CANONICAL_MODEL_ID
    )
    assert (
        module.LIVE_MODELS["granite"].canonical_model_id
        == bootstrap.GRANITE_CANONICAL_MODEL_ID
    )
    assert (
        len(
            {
                bootstrap.DEEPSEEK_CANONICAL_MODEL_ID,
                bootstrap.MIMO_CANONICAL_MODEL_ID,
                bootstrap.GRANITE_CANONICAL_MODEL_ID,
            }
        )
        == len(module.LIVE_MODELS)
    )
    with pytest.raises(module.LiveProofError, match="selector is invalid"):
        module._selected_plan(("--model", "private-model"))  # noqa: SLF001
    with pytest.raises(module.LiveProofError, match="selector is invalid"):
        module._selected_plan(("--model", "mimo", "extra"))  # noqa: SLF001
    with pytest.raises(module.LiveProofError, match="selector is invalid"):
        module._selected_plan(("--model", "mimo", "--stream-only"))  # noqa: SLF001


def test_live_openrouter_stream_diagnostic_reads_status_before_sse() -> None:
    """Keep native evidence available before compatible stream validation."""
    source = LIVE_PATH.read_text(encoding="utf-8")
    runner = source[
        source.index("def _run_provider_operations(") : source.index(
            "def _compatible_chat("
        )
    ]
    stream_branch = runner[runner.index('if operation == "stream":') :]

    assert stream_branch.index("_read_status(") < stream_branch.index(
        "_compatible_stream_content("
    )
    main = source[source.index("def main(") : source.index("def _selected_plan(")]
    isolation = main.index("_assert_isolation(")
    paid = main.index("_run_provider_operations(")
    accounting = main.index("_accounting_cost(")
    embed = main.index("_load_real_embed(")
    scan = main.index("_assert_no_sensitive_artifact(")
    assert isolation < paid < accounting < embed < scan


def test_browser_proof_keeps_live_embed_separate_from_deterministic_admin() -> None:
    """Keep live embed scope exact and retain deterministic admin assertions."""
    e2e = (REPOSITORY_ROOT / "scripts/local-development-e2e.py").read_text(
        encoding="utf-8"
    )
    main = e2e[e2e.index("def main(") : e2e.index("def _prove_service_scoped_embed(")]
    embed = e2e[
        e2e.index("def _prove_service_scoped_embed(") : e2e.index(
            "def _prove_global_administration("
        )
    ]
    admin = e2e[
        e2e.index("def _prove_global_administration(") : e2e.index(
            "class _CdpBrowser:"
        )
    ]

    assert "_prove_service_scoped_embed()" in main
    assert "_prove_global_administration()" in main
    assert 'headers={"Origin": "http://127.0.0.1:5999"}' in embed
    assert 'browser.click_button("Switch user")' in embed
    assert "excluded_id=old_frame" in embed
    assert "deepseek/deepseek-v4-flash" not in embed
    assert "Primary" not in embed
    assert "Fallback 1" not in embed
    assert "deepseek/deepseek-v4-flash" in admin
    assert "Primary" in admin
    assert "Fallback 1" in admin


def test_live_openrouter_loads_only_required_service_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoke the embed proof and do not invoke deterministic global admin."""
    module = _live_module()
    calls: list[str] = []
    monkeypatch.setattr(
        module.runpy,
        "run_path",
        lambda *_args: {
            "_prove_service_scoped_embed": lambda: calls.append("embed"),
            "_prove_global_administration": lambda: calls.append("admin"),
        },
    )

    module._load_real_embed()  # noqa: SLF001

    assert calls == ["embed"]


def test_live_openrouter_does_not_bypass_failed_service_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop the live proof when the required embed proof fails."""
    module = _live_module()

    def fail() -> None:
        message = "private browser detail"
        raise AssertionError(message)

    monkeypatch.setattr(
        module.runpy,
        "run_path",
        lambda *_args: {"_prove_service_scoped_embed": fail},
    )

    with pytest.raises(module.LiveProofError, match="embed did not load"):
        module._load_real_embed()  # noqa: SLF001


def test_live_openrouter_stream_diagnostic_makes_one_successful_attempt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run one provider-native stream and verify native status and accounting."""
    module = _live_module()
    request_ids: list[str] = []
    calls: list[bool] = []
    content = b"test answer"
    stream = (
        b'data: {"choices":[{"delta":{"content":"test answer"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    status = {
        "state": "succeeded",
        "result": {"outputs": [{"text": content.decode()}]},
        "accounting": {"cost": "0.00001"},
    }

    def compatible_chat(*_args: object, stream: bool) -> httpx.Response:
        calls.append(stream)
        return httpx.Response(200, content=stream_bytes)

    def attempt_count(ids: tuple[str, ...]) -> int:
        return len(ids)

    monkeypatch.setattr(module, "_uuidv7", lambda: "test-request")
    stream_bytes = stream
    monkeypatch.setattr(module, "_compatible_chat", compatible_chat)
    monkeypatch.setattr(module, "_read_status", lambda *_args: status)
    monkeypatch.setattr(module, "_attempt_count", attempt_count)

    attempts, returned = module._run_provider_operations(  # noqa: SLF001
        "test-token", "test prompt", request_ids, ("stream",)
    )

    assert attempts == 1
    assert calls == [True]
    assert request_ids == ["test-request"]
    assert returned == [content, content]
    assert capsys.readouterr().out == "Live OpenRouter stream phase passed.\n"


def test_live_openrouter_stream_diagnostic_failure_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stop after one failed stream and print only closed native evidence."""
    module = _live_module()
    calls: list[bool] = []
    response = httpx.Response(
        200,
        content=(
            b'data: {"error":{"code":502},"choices":'
            b'[{"delta":{"content":""},"finish_reason":"error"}]}\n\n'
        ),
    )
    status = {
        "state": "failed",
        "error": {
            "class": "provider_unavailable",
            "affected_scope": "provider_model_route",
            "message": "private provider detail",
        },
    }
    monkeypatch.setattr(module, "_uuidv7", lambda: "test-request")
    monkeypatch.setattr(
        module,
        "_compatible_chat",
        lambda *_args, stream: calls.append(stream) or response,
    )
    monkeypatch.setattr(module, "_read_status", lambda *_args: status)
    monkeypatch.setattr(
        module,
        "_redacted_stream_parser_evidence",
        lambda *_args: (
            "Live stream parser evidence: closed branch=stream_missing_content."
        ),
    )

    with pytest.raises(module.LiveProofError, match="terminal completion"):
        module._run_provider_operations(  # noqa: SLF001
            "test-token", "test prompt", [], ("stream",)
        )

    evidence = capsys.readouterr().out
    assert calls == [True]
    assert "native request ended as failed" in evidence
    assert "provider_unavailable" in evidence
    assert "closed branch=stream_missing_content" in evidence
    assert "private" not in evidence


def test_live_openrouter_parser_evidence_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose only fixed parser branches without provider data."""
    module = _live_module()

    class Result:
        def __init__(self, value: str | None) -> None:
            self.value = value

        def fetchone(self) -> tuple[str | None]:
            return (self.value,)

    class Connection:
        value: str | None

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> Result:
            return Result(self.value)

    connection = Connection()
    monkeypatch.setattr(module, "_database", lambda: connection)
    assert {
        "stream_missing_done",
        "stream_missing_finish",
        "stream_missing_content",
        "stream_missing_usage",
    } < module._STREAM_PARSER_DETAIL_CODES  # noqa: SLF001
    for detail in module._STREAM_PARSER_DETAIL_CODES:  # noqa: SLF001
        connection.value = detail
        assert module._redacted_stream_parser_evidence("request") == (  # noqa: SLF001
            f"Live stream parser evidence: closed branch={detail}."
        )
    connection.value = "private provider detail"
    assert module._redacted_stream_parser_evidence("request") is None  # noqa: SLF001


def test_live_openrouter_parser_evidence_failure_does_not_mask_safe_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the fixed native failure when the optional evidence read fails."""
    module = _live_module()
    status = {
        "state": "failed",
        "error": {
            "class": "invalid_provider_response",
            "affected_scope": "provider_model_route",
        },
    }
    monkeypatch.setattr(module, "_uuidv7", lambda: "request")
    monkeypatch.setattr(
        module,
        "_compatible_chat",
        lambda *_args, **_kwargs: httpx.Response(200, content=b""),
    )
    monkeypatch.setattr(module, "_read_status", lambda *_args: status)
    monkeypatch.setattr(
        module,
        "_redacted_stream_parser_evidence",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("private database detail")),
    )

    with pytest.raises(module.LiveProofError, match="terminal completion"):
        module._run_provider_operations(  # noqa: SLF001
            "token", "prompt", [], ("stream",)
        )


def test_live_openrouter_stream_diagnostic_requires_one_accounting_attempt() -> None:
    """Accept only one request and attempt in stream-only accounting."""
    module = _live_module()

    class Admin:
        def get(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(
                200,
                json={"logical_requests": 1, "attempts": 1, "cost": "0.00001"},
            )

    assert module._accounting_cost(  # noqa: SLF001
        Admin(), datetime.now(UTC), expected_requests=1
    ) == Decimal("0.00001")
    with pytest.raises(module.LiveProofError, match="counts are not exact"):
        module._accounting_cost(  # noqa: SLF001
            Admin(), datetime.now(UTC), expected_requests=2
        )


def test_local_bootstrap_seeds_distinct_live_proof_catalog_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publish each live wire model under its separate canonical identity."""
    bootstrap = _bootstrap_module()
    published: list[Any] = []

    class FakeConnection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _statement: str) -> Self:
            return self

        def fetchone(self) -> tuple[bool]:
            return (False,)

    class FakeRepository:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def publish(
            self,
            _context: object,
            _scope: object,
            content: object,
            **_kwargs: object,
        ) -> None:
            published.append(content)

    monkeypatch.setattr(bootstrap.psycopg, "connect", lambda _url: FakeConnection())
    monkeypatch.setattr(bootstrap, "PostgresConfigurationRepository", FakeRepository)

    bootstrap._seed_catalog("test-database", bootstrap.datetime.now(bootstrap.UTC))  # noqa: SLF001

    assert len(published) == 1
    catalog = published[0].catalog
    models = {
        entry.stable_id: entry.display_name
        for entry in catalog
        if entry.kind is bootstrap.CatalogKind.MODEL
    }
    assert models == {
        bootstrap.DEEPSEEK_CANONICAL_MODEL_ID: "DeepSeek V4 Flash",
        bootstrap.MIMO_CANONICAL_MODEL_ID: "MiMo 2.5",
        bootstrap.GRANITE_CANONICAL_MODEL_ID: "Granite 4.1 8B",
    }


def test_live_openrouter_configuration_uses_the_resource_revision_chain() -> None:
    """Use the create revision contract for the full protected setup sequence."""
    module = _live_module()
    model = module.LIVE_MODELS["mimo"]

    class FakeAdmin:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        def request(
            self,
            method: str,
            path: str,
            *,
            headers: dict[str, str],
            json: dict[str, object] | None,
        ) -> httpx.Response:
            del headers
            assert json is not None
            self.calls.append((method, path, json))
            if path.endswith("/provider-instances"):
                return httpx.Response(
                    200,
                    json={"resource_id": "provider-1", "active_revision": "rev-1"},
                )
            if path.endswith("/provider-model-routes"):
                return httpx.Response(
                    200,
                    json={"resource_id": "route-1", "active_revision": "rev-2"},
                )
            assert path.endswith("/assignments/live-model-proof")
            return httpx.Response(
                200,
                json={"distribution_state": "current"},
            )

    admin = FakeAdmin()
    route_id = module._configure_route(  # noqa: SLF001
        admin,
        "credential-1",
        {
            "input_token": Decimal("0.00000009"),
            "output_token": Decimal("0.00000018"),
            "request": Decimal(0),
        },
        model,
    )

    assert route_id == "route-1"
    assert [call[0] for call in admin.calls] == ["POST", "POST", "PUT"]
    assert admin.calls[0][2]["expected_revision"] is None
    assert admin.calls[1][2]["expected_revision"] == "rev-1"
    assert admin.calls[1][2]["canonical_model_id"] == model.canonical_model_id
    assert admin.calls[1][2]["wire_model"] == model.wire_model
    assert admin.calls[2][2]["expected_revision"] == "rev-2"


def test_live_openrouter_administration_failure_reports_only_label_and_status() -> None:
    """Keep protected failure diagnostics safe and useful."""
    module = _live_module()

    class FailedAdmin:
        def request(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(422, text="private administration response")

    with pytest.raises(module.LiveProofError) as captured:
        module._request(  # noqa: SLF001
            FailedAdmin(),
            "POST",
            "/v1/admin/test",
            operation="route create",
            expected={201},
        )

    assert str(captured.value) == "The protected route create failed with HTTP 422."
    assert "private administration response" not in str(captured.value)


@pytest.mark.parametrize("status_code", [403, 404])
def test_live_openrouter_isolation_uses_an_ungranted_workspace(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    """Probe one valid workspace identity outside every bootstrap grant."""
    module = _live_module()
    bootstrap = _bootstrap_module()
    captured_body: dict[str, object] = {}

    def rejected_create(
        _token: str, _request_id: str, body: dict[str, object]
    ) -> httpx.Response:
        captured_body.update(body)
        return httpx.Response(status_code)

    monkeypatch.setattr(module, "_raw_create", rejected_create)
    module._assert_isolation("test-token", "private prompt")  # noqa: SLF001

    assert uuid.UUID(module.OTHER_WORKSPACE_ID).version == 7  # noqa: PLR2004
    assert module.OTHER_WORKSPACE_ID not in bootstrap.WORKSPACE_IDS
    assert captured_body["workspace_id"] == module.OTHER_WORKSPACE_ID


def test_live_openrouter_isolation_rejects_an_authorized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop before paid calls unless the isolation probe is denied."""
    module = _live_module()
    monkeypatch.setattr(
        module,
        "_raw_create",
        lambda *_args, **_kwargs: httpx.Response(200),
    )

    with pytest.raises(module.LiveProofError, match="workspace isolation"):
        module._assert_isolation("test-token", "private prompt")  # noqa: SLF001


def test_live_openrouter_accepts_bounded_content_without_challenge_echo() -> None:
    """Accept useful provider content without requiring exact prompt echo."""
    module = _live_module()
    content = module._assert_compatible_content(  # noqa: SLF001
        {"choices": [{"message": {"content": "A short independent answer."}}]}
    )
    stream = module._compatible_stream_content(  # noqa: SLF001
        b'data: {"choices":[{"delta":{"content":"A short "}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"answer."}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    assert content == b"A short independent answer."
    assert stream == b"A short answer."


@pytest.mark.parametrize("value", [None, "", "   "])
def test_live_openrouter_rejects_empty_compatible_output(value: object) -> None:
    """Stop before the next paid call when compatible output is empty."""
    module = _live_module()
    with pytest.raises(module.LiveProofError, match="has no output"):
        module._assert_compatible_content(  # noqa: SLF001
            {"choices": [{"message": {"content": value}}]}
        )


@pytest.mark.parametrize(
    "stream",
    [
        b"data: [DONE]\n\n",
        b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
    ],
)
def test_live_openrouter_rejects_empty_or_unterminated_stream(stream: bytes) -> None:
    """Require content and terminal completion from compatible SSE."""
    module = _live_module()
    with pytest.raises(module.LiveProofError, match="terminal completion"):
        module._compatible_stream_content(stream)  # noqa: SLF001


def test_live_openrouter_network_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Convert an HTTP timeout to one fixed message without a traceback."""
    module = _live_module()
    inherited_value = "test-only-inherited-provider-value"
    provider_content = "test-only-private-provider-content"
    request = httpx.Request(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {inherited_value}"},
        content=provider_content,
    )

    def timed_out(_key: str, _model: object) -> dict[str, Decimal]:
        raise httpx.ReadTimeout(provider_content, request=request)

    monkeypatch.setenv("OPENROUTER_API_KEY", inherited_value)
    monkeypatch.setattr(module, "_provider_preflight", timed_out)
    with pytest.raises(SystemExit) as captured:
        module.main()

    evidence = capsys.readouterr()
    report = evidence.out + evidence.err + str(captured.value)
    assert "Paid provider calls: 0." in report
    assert "The live no-cost preflight network request failed safely." in report
    assert "Traceback" not in report
    assert "Authorization" not in report
    assert inherited_value not in report
    assert provider_content not in report


def test_live_openrouter_unexpected_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Convert an unexpected dependency failure to one fixed safe message."""
    module = _live_module()
    inherited_value = "test-only-inherited-provider-value"
    private_detail = "test-only-private-database-detail"

    def failed(_key: str, _model: object) -> dict[str, Decimal]:
        raise RuntimeError(private_detail)

    monkeypatch.setenv("OPENROUTER_API_KEY", inherited_value)
    monkeypatch.setattr(module, "_provider_preflight", failed)
    with pytest.raises(SystemExit) as captured:
        module.main()

    evidence = capsys.readouterr()
    report = evidence.out + evidence.err + str(captured.value)
    assert "Paid provider calls: 0." in report
    assert "The live no-cost preflight operation failed safely." in report
    assert "Traceback" not in report
    assert inherited_value not in report
    assert private_detail not in report


def test_live_openrouter_timeout_evidence_is_closed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report only one admitted request state and its bounded attempt outcomes."""
    module = _live_module()
    request_id = "0198a080-0000-7000-8000-000000000141"

    class FakeDatabase:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _statement: str, parameters: tuple[str]) -> Self:
            assert parameters == (request_id,)
            return self

        def fetchone(self) -> tuple[str, int, list[str]]:
            return "failed", 1, ["failed"]

    monkeypatch.setattr(module, "_database", FakeDatabase)

    assert module._redacted_timeout_evidence(request_id) == (  # noqa: SLF001
        "Live timeout evidence: request state=failed; provider attempts=1; "
        "attempt outcomes=failed."
    )


@pytest.mark.parametrize(
    "row",
    [
        ("private-state", 1, ["failed"]),
        ("failed", 9, ["failed"] * 9),
        ("failed", 1, ["private-outcome"]),
    ],
)
def test_live_openrouter_timeout_evidence_rejects_unclosed_values(
    monkeypatch: pytest.MonkeyPatch, row: tuple[str, int, list[str]]
) -> None:
    """Do not print unexpected database values after a host timeout."""
    module = _live_module()

    class FakeDatabase:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _statement: str, _parameters: tuple[str]) -> Self:
            return self

        def fetchone(self) -> tuple[str, int, list[str]]:
            return row

    monkeypatch.setattr(module, "_database", FakeDatabase)

    assert module._redacted_timeout_evidence("test-request") is None  # noqa: SLF001


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            {"state": "succeeded", "result": {"outputs": [{"text": "private"}]}},
            (
                "native request succeeded; compatible SSE is missing valid output "
                "or terminal completion"
            ),
        ),
        (
            {
                "state": "failed",
                "error": {
                    "class": "invalid_provider_response",
                    "affected_scope": "provider_model_route",
                    "message": "private provider detail",
                },
            },
            (
                "native request ended as failed; safe error "
                "class=invalid_provider_response; affected scope=provider_model_route"
            ),
        ),
        (
            {"state": "running", "result": {"outputs": [{"text": "private"}]}},
            "native request is nonterminal",
        ),
    ],
)
def test_live_openrouter_stream_evidence_is_closed_and_redacted(
    status: dict[str, object], expected: str
) -> None:
    """Distinguish each stream failure branch without private values."""
    module = _live_module()

    evidence = module._redacted_stream_evidence(status)  # noqa: SLF001

    assert evidence is not None
    assert expected in evidence
    assert "private" not in evidence


@pytest.mark.parametrize(
    "status",
    [
        {"state": "private"},
        {
            "state": "failed",
            "error": {
                "class": "private",
                "affected_scope": "provider_model_route",
            },
        },
    ],
)
def test_live_openrouter_stream_evidence_rejects_unclosed_values(
    status: dict[str, object],
) -> None:
    """Do not report status values outside the closed contract."""
    module = _live_module()

    assert module._redacted_stream_evidence(status) is None  # noqa: SLF001


def test_live_openrouter_provider_timeout_is_explicit_and_has_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give provider startup a bounded read window and issue one request."""
    module = _live_module()
    calls: list[dict[str, object]] = []

    def post(_url: str, **kwargs: object) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, json={"choices": []})

    monkeypatch.setattr(module.httpx, "post", post)
    module._compatible_chat(  # noqa: SLF001
        "test-data-token",
        "0198a080-0000-7000-8000-000000000140",
        "test prompt",
        stream=True,
    )

    assert len(calls) == 1
    timeout = calls[0]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (
        10.0,
        module.PROVIDER_READ_TIMEOUT_SECONDS,
        10.0,
        5.0,
    )


def test_live_openrouter_phase_marker_follows_all_prerequisites(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print redacted phase evidence after content, status, accounting, and attempts."""
    module = _live_module()
    content = b"test answer"
    status = {
        "state": "succeeded",
        "result": {"outputs": [{"text": "test answer"}]},
        "accounting": {"cost": "0.00001"},
    }

    verified = module._verify_provider_phase(  # noqa: SLF001
        content,
        status,
        attempts=1,
        expected_attempts=1,
        phase="non-stream",
    )

    assert verified == content
    assert capsys.readouterr().out == "Live OpenRouter non-stream phase passed.\n"


@pytest.mark.parametrize(
    ("content", "status", "attempts"),
    [
        (
            b"test answer",
            {"state": "failed", "accounting": {"cost": "0"}},
            1,
        ),
        (
            b"different answer",
            {
                "state": "succeeded",
                "result": {"outputs": [{"text": "test answer"}]},
                "accounting": {"cost": "0.00001"},
            },
            1,
        ),
        (
            b"test answer",
            {
                "state": "succeeded",
                "result": {"outputs": [{"text": "test answer"}]},
            },
            1,
        ),
        (
            b"test answer",
            {
                "state": "succeeded",
                "result": {"outputs": [{"text": "test answer"}]},
                "accounting": {"cost": "0.00001"},
            },
            2,
        ),
    ],
)
def test_live_openrouter_phase_marker_is_silent_on_failed_prerequisite(
    content: bytes,
    status: dict[str, object],
    attempts: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Do not report a provider phase before each required check passes."""
    module = _live_module()
    with pytest.raises(module.LiveProofError):
        module._verify_provider_phase(  # noqa: SLF001
            content,
            status,
            attempts=attempts,
            expected_attempts=1,
            phase="non-stream",
        )

    assert "phase passed" not in capsys.readouterr().out
