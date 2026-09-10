"""Executable safety checks for the bounded localhost browser proof."""
# ruff: noqa: SLF001, S108 - Test private helpers and rejected /tmp paths.

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://127.0.0.1:5174"
EXPECTED_AXE_SCANS = 2
UNSAFE_PATHS = (
    "",
    "overview",
    "https://example.invalid/overview",
    "//example.invalid/overview",
    "///example.invalid/overview",
    "/\\example.invalid/overview",
    "/%2fexample.invalid/overview",
    "/%2F%2Fexample.invalid/overview",
    "/%5cexample.invalid/overview",
    "/%252fexample.invalid/overview",
    "/\n/overview",
    "/%0d%0a/overview",
    "/overview\x00",
    "//user:synthetic-control@example.invalid/overview",
    "//[synthetic-control]/overview",
)
UNSAFE_URLS = (
    "https://127.0.0.1:5174/overview",
    "http://localhost:5174/overview",
    "http://127.0.0.1:5175/overview",
    "http://127.0.0.1:5174.example.invalid/overview",
    "http://127.0.0.1:5174@example.invalid/overview",
    "http://user:synthetic-control@127.0.0.1:5174/overview",
    "http://example.invalid/overview",
    "//127.0.0.1:5174/overview",
    "/overview",
    "data:text/plain,synthetic-control",
    "file:///tmp/overview",
    ORIGIN + "/%2fexample.invalid/overview",
    ORIGIN + "/%5cexample.invalid/overview",
    ORIGIN + "/%0d%0a/overview",
)


@pytest.fixture
def proof(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import helpers without calling the deployment proof entry point."""
    monkeypatch.syspath_prepend(str(REPOSITORY_ROOT / "scripts"))
    specification = importlib.util.spec_from_file_location(
        "browser_proof_safety_test", REPOSITORY_ROOT / "scripts/prove-localhost.py"
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/overview",
        "/configuration?service=alpha",
        "/services/alpha",
        "/logs?proof_mode=normal",
        "/logs?administrator=Fixture%20Administrator",
        "/logs?from=2026-09-09T00%3A00%3A00Z",
        "/v1/admin/media-jobs/fixture/content",
    ],
)
def test_local_application_url_keeps_the_fixed_origin(
    proof: ModuleType, path: str
) -> None:
    """Keep valid application and resource paths on the fixed origin."""
    assert proof._local_application_url(path) == ORIGIN + path


@pytest.mark.parametrize("path", UNSAFE_PATHS)
def test_navigation_rejects_unsafe_paths_before_browser_use(
    proof: ModuleType, path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject unsafe navigation input before any browser command."""
    browser = Mock()
    with pytest.raises((ValueError, AssertionError)) as failure:
        proof._navigate(browser, path, "Overview")
    browser.command.assert_not_called()
    browser.evaluate.assert_not_called()
    captured = capsys.readouterr()
    assert "synthetic-control" not in str(failure.value) + captured.out + captured.err


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
)
def test_read_only_request_rejects_every_non_get_method(
    proof: ModuleType, method: str
) -> None:
    """Reject product writes and every method outside the GET-only grant."""
    assert (
        proof._read_only_request_allowed(method, ORIGIN + "/v1/admin/services") is False
    )


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_read_only_request_rejects_foreign_or_unsafe_urls(
    proof: ModuleType, url: str
) -> None:
    """Reject credentials, foreign origins, and encoded unsafe paths."""
    assert proof._read_only_request_allowed("GET", url) is False


@pytest.mark.parametrize(
    "path",
    [
        "/overview",
        "/assets/application.js",
        "/v1/admin/session",
        "/v1/admin/request-logs?administrator=Fixture%20Administrator",
    ],
)
def test_read_only_request_permits_local_get(proof: ModuleType, path: str) -> None:
    """Permit the document, its local assets, and authenticated reads."""
    assert proof._read_only_request_allowed("GET", ORIGIN + path) is True


@pytest.mark.parametrize("outer_response_first", [False, True])
def test_cdp_applies_read_only_policy_to_paused_network_requests(
    proof: ModuleType, monkeypatch: pytest.MonkeyPatch, *, outer_response_first: bool
) -> None:
    """Block writes and foreign redirects without losing the pending command."""
    browser = proof._Cdp.__new__(proof._Cdp)
    browser._identifier = 0
    browser._read_only = True
    sent: list[dict[str, object]] = []
    requests = [
        ("local", "GET", ORIGIN + "/v1/admin/session"),
        ("write", "POST", ORIGIN + "/v1/admin/services"),
        ("redirect", "GET", "http://example.invalid/redirect"),
        ("credentials", "GET", "http://user:synthetic-control@127.0.0.1:5174/"),
    ]
    replies = [
        json.dumps(
            {
                "method": "Fetch.requestPaused",
                "params": {
                    "requestId": request_id,
                    "request": {"method": method, "url": url},
                },
            }
        )
        for request_id, method, url in requests
    ]
    command_result = json.dumps({"id": 1, "result": {"ready": True}})
    interception_results = [
        json.dumps({"id": identifier, "result": {}})
        for identifier in range(2, len(requests) + 2)
    ]
    replies.extend(
        [command_result, *interception_results]
        if outer_response_first
        else [*interception_results, command_result]
    )
    incoming = iter(replies)
    monkeypatch.setattr(browser, "_send", lambda value: sent.append(json.loads(value)))
    monkeypatch.setattr(browser, "_receive", lambda: next(incoming))
    assert browser.command("Runtime.evaluate", {"expression": "true"}) == {
        "ready": True
    }
    decisions = sent[1:]
    assert [item["method"] for item in decisions] == [
        "Fetch.continueRequest",
        "Fetch.failRequest",
        "Fetch.failRequest",
        "Fetch.failRequest",
    ]
    assert [item["params"]["requestId"] for item in decisions] == [
        item[0] for item in requests
    ]
    assert len({item["id"] for item in sent}) == len(sent)
    assert "synthetic-control" not in json.dumps(sent)


def test_cdp_error_omits_remote_control_values(
    proof: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not copy remote command errors into the proof report."""
    browser = proof._Cdp.__new__(proof._Cdp)
    browser._identifier = 0
    browser._read_only = True
    monkeypatch.setattr(browser, "_send", lambda _value: None)
    monkeypatch.setattr(
        browser,
        "_receive",
        lambda: json.dumps({"id": 1, "error": {"message": "synthetic-control"}}),
    )
    with pytest.raises(AssertionError) as failure:
        browser.command("Runtime.evaluate")
    assert "synthetic-control" not in str(failure.value)


def test_navigation_requires_current_main_heading_and_route(proof: ModuleType) -> None:
    """Reject navigation text, a stale route, and an obsolete page heading."""
    browser = Mock()
    browser.evaluate.return_value = True
    proof._navigate(browser, "/overview", "Overview")
    expression = browser.evaluate.call_args.args[0]
    node = shutil.which("node")
    assert node is not None
    runner = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const expression = JSON.parse(fs.readFileSync(0, "utf8"));
const cases = [
  { path: "/overview", heading: "Overview", ready: "complete", main: "Overview" },
  { path: "/overview", heading: "Services", ready: "complete", main: "Services" },
  { path: "/services", heading: "Overview", ready: "complete", main: "Overview" },
  {
    path: "/overview", heading: "Router overview", ready: "complete", main: "Overview"
  },
  { path: "/overview", heading: null, ready: "complete", main: "Overview" },
  { path: "/overview", heading: "Overview", ready: "loading", main: "Overview" },
];
const results = cases.map((state) => {
  const heading = state.heading === null ? null : {
    textContent: state.heading, innerText: state.heading
  };
  const main = {
    innerText: state.main,
    textContent: state.main,
    querySelector: (selector) => selector === "h1" ? heading : null
  };
  const document = {
    readyState: state.ready,
    body: { innerText: "Overview Services LLM configuration Logs" },
    querySelector: (selector) =>
      selector === "main" ? main : selector === "main h1" ? heading : null
  };
  const location = new URL("http://127.0.0.1:5174" + state.path);
  return Boolean(vm.runInNewContext(expression, { document, location, URL }));
});
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(  # noqa: S603 - Fixed Node runner; input is local proof source.
        [node, "-e", runner],
        input=json.dumps(expression),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == [True, False, False, False, False, False]
    browser.command.assert_called_once_with(
        "Page.navigate", {"url": ORIGIN + "/overview"}
    )


@pytest.mark.parametrize("violation_on_second_scan", [False, True])
def test_repeated_axe_scan_stays_strict(
    proof: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    violation_on_second_scan: bool,
) -> None:
    """Reuse an installed Axe engine and still reject later violations."""
    repository = tmp_path / "router"
    source_path = tmp_path / "opendle-ui/node_modules/axe-core/axe.min.js"
    source_path.parent.mkdir(parents=True)
    source = "/* controlled Axe installation */"
    source_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(proof, "REPOSITORY_ROOT", repository)
    browser = Mock()
    installed = False
    installations = 0
    scans = 0

    def evaluate(expression: str) -> object:
        nonlocal installed, installations, scans
        if expression == source:
            installed = True
            installations += 1
            return None
        if "typeof globalThis.axe" in expression:
            return installed
        if "globalThis.axe.run(" in expression:
            assert installed
            scans += 1
            if violation_on_second_scan and scans > 1:
                return [{"id": "region", "nodes": [{"target": ["main"]}]}]
            return []
        if "__llmrouterProofErrors" in expression:
            return []
        if "getAnimations()" in expression:
            return True
        pytest.fail("The controlled Axe browser received an unknown expression.")

    browser.evaluate.side_effect = evaluate
    proof._assert_axe(browser)
    if violation_on_second_scan:
        with pytest.raises(AssertionError):
            proof._assert_axe(browser)
    else:
        proof._assert_axe(browser)
    assert installations == 1
    assert scans == EXPECTED_AXE_SCANS


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://127.0.0.1:synthetic-control/devtools/page/fixture",
        "ws://[synthetic-control]/devtools/page/fixture",
        "ws://user:synthetic-control@127.0.0.1:9222/devtools/page/fixture",
        "ws://127.0.0.1:9222@example.invalid/devtools/page/fixture",
        "ws://example.invalid:9222/devtools/page/fixture",
        "wss://127.0.0.1:9222/devtools/page/fixture",
    ],
)
def test_cdp_rejects_unsafe_endpoint_before_socket_use(
    proof: ModuleType, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """Reject credentials and foreign debugging endpoints before connection."""
    connect = Mock()
    monkeypatch.setattr(proof.socket, "create_connection", connect)
    with pytest.raises(AssertionError) as failure:
        proof._Cdp(endpoint, read_only=True)
    connect.assert_not_called()
    assert "synthetic-control" not in str(failure.value)


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["relative-output"],
        ["https://example.invalid/synthetic-control"],
        ["/tmp/llmrouter-browser-proof-one/../llmrouter-browser-proof-two"],
        ["/tmp/llmrouter-browser-proof-synthetic-control", "unexpected"],
        ["/tmp/synthetic-control"],
    ],
)
def test_fixture_builder_rejects_unsafe_arguments(arguments: list[str]) -> None:
    """Reject unsafe output paths without building or exposing input values."""
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603 - Fixed local builder; all inputs are invalid.
        [node, REPOSITORY_ROOT / "scripts/tests/browser-proof-fixture.mjs", *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Use a fresh /tmp/llmrouter-browser-proof-<id>" in result.stderr
    assert "synthetic-control" not in result.stderr


@pytest.mark.parametrize(
    ("key", "native_key", "virtual_code"),
    [("Tab", "Tab", 9), ("Enter", "Enter", 13), ("Space", " ", 32)],
)
def test_key_events_preserve_native_control_activation(
    proof: ModuleType, key: str, native_key: str, virtual_code: int
) -> None:
    """Send native codes so keyboard proof performs the browser default action."""
    browser = Mock()
    proof._press_key(browser, key)
    text = {"Enter": "\r", "Space": " "}.get(key)
    base_fields = {
        "type",
        "key",
        "code",
        "windowsVirtualKeyCode",
        "nativeVirtualKeyCode",
    }
    assert [
        (
            entry.args[0],
            {
                name: value
                for name, value in entry.args[1].items()
                if name in base_fields
            },
        )
        for entry in browser.command.call_args_list
    ] == [
        (
            "Input.dispatchKeyEvent",
            {
                "type": event_type,
                "key": native_key,
                "code": key,
                "windowsVirtualKeyCode": virtual_code,
                "nativeVirtualKeyCode": virtual_code,
            },
        )
        for event_type in ("keyDown", "keyUp")
    ]
    down = browser.command.call_args_list[0].args[1]
    assert down.get("text") == text
    assert down.get("unmodifiedText") == text
    up = browser.command.call_args_list[1].args[1]
    assert "text" not in up
    assert "unmodifiedText" not in up


@pytest.mark.parametrize(
    "path",
    [
        "/@vite/client",
        "/@react-refresh",
        "/@fs/home/example",
        "/logs?administrator=Fixture%20Person%40example.invalid",
        "/logs?reference=https%3A%2F%2Fexample.invalid%2Fwith%20space",
        "/logs?reference=https://example.invalid/@person",
    ],
)
def test_local_development_assets_and_query_values_stay_on_origin(
    proof: ModuleType, path: str
) -> None:
    """Permit development assets and query data without changing authority."""
    assert proof._local_application_url(path) == ORIGIN + path
    assert proof._read_only_request_allowed("GET", ORIGIN + path) is True


@pytest.mark.parametrize("failure_source", ["console", "axe"])
def test_browser_scan_errors_do_not_expose_control_values(
    proof: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure_source: str,
) -> None:
    """Reject browser and Axe failures without copying their sensitive detail."""
    repository = tmp_path / "router"
    source_path = tmp_path / "opendle-ui/node_modules/axe-core/axe.min.js"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("/* controlled Axe installation */", encoding="utf-8")
    monkeypatch.setattr(proof, "REPOSITORY_ROOT", repository)
    browser = Mock()
    if failure_source == "console":
        browser.evaluate.side_effect = [True, ["synthetic-control"]]
    else:
        browser.evaluate.side_effect = [
            True,
            [],
            True,
            [{"id": "region", "nodes": [{"failure": "synthetic-control"}]}],
        ]
    with pytest.raises(AssertionError) as failure:
        proof._assert_axe(browser)
    captured = capsys.readouterr()
    assert "synthetic-control" not in str(failure.value) + captured.out + captured.err


@pytest.mark.parametrize("policy_failure", [False, True])
@pytest.mark.parametrize("outer_response_first", [False, True])
def test_read_only_document_policy_preserves_headers_and_rejects_policy_failure(
    proof: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy_failure: bool,
    outer_response_first: bool,
) -> None:
    """Apply the HTTP-only CSP before document scripts can open channels."""
    browser = proof._Cdp.__new__(proof._Cdp)
    browser._identifier = 0
    browser._read_only = True
    sent: list[dict[str, object]] = []
    headers = [
        {"name": "Content-Type", "value": "text/html"},
        {"name": "Content-Security-Policy", "value": "object-src 'none'"},
    ]
    replies = [
        json.dumps(
            {
                "method": "Fetch.requestPaused",
                "params": {
                    "requestId": "document",
                    "responseStatusCode": 200,
                    "responseHeaders": headers,
                },
            }
        ),
        json.dumps(
            {"id": 2, "error": {"message": "synthetic-control"}}
            if policy_failure
            else {"id": 2, "result": {}}
        ),
        json.dumps({"id": 1, "result": {"ready": True}}),
    ]
    if outer_response_first:
        replies[1], replies[2] = replies[2], replies[1]
    replies = iter(replies)
    monkeypatch.setattr(browser, "_send", lambda value: sent.append(json.loads(value)))
    monkeypatch.setattr(browser, "_receive", lambda: next(replies))
    if policy_failure:
        with pytest.raises(AssertionError) as failure:
            browser.command("Runtime.evaluate")
        assert "synthetic-control" not in str(failure.value)
    else:
        assert browser.command("Runtime.evaluate") == {"ready": True}
    assert sent[1] == {
        "id": 2,
        "method": "Fetch.continueResponse",
        "params": {
            "requestId": "document",
            "responseCode": 200,
            "responseHeaders": [
                *headers,
                {
                    "name": "Content-Security-Policy",
                    "value": "connect-src http://127.0.0.1:5174",
                },
            ],
        },
    }


@pytest.mark.parametrize("failure_source", ["none", "console", "axe"])
def test_loading_animation_keeps_scans_strict(
    proof: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_source: str,
) -> None:
    """Permit an infinite loader, wait for finite motion, and retain failures."""
    repository = tmp_path / "router"
    source_path = tmp_path / "opendle-ui/node_modules/axe-core/axe.min.js"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("/* installed test engine */", encoding="utf-8")
    monkeypatch.setattr(proof, "REPOSITORY_ROOT", repository)
    browser = Mock()
    expressions: list[str] = []

    def evaluate(expression: str) -> object:
        if "getAnimations()" in expression:
            expressions.append(expression)
            return True
        if "__llmrouterProofErrors" in expression:
            return ["synthetic-control"] if failure_source == "console" else []
        if "typeof globalThis.axe" in expression:
            return True
        if "globalThis.axe.run(" in expression:
            return [{"id": "region"}] if failure_source == "axe" else []
        pytest.fail("The loading scan received an unknown expression.")

    browser.evaluate.side_effect = evaluate
    if failure_source == "none":
        proof._assert_axe(browser)
    else:
        with pytest.raises(AssertionError) as failure:
            proof._assert_axe(browser)
        assert "synthetic-control" not in str(failure.value)

    node = shutil.which("node")
    assert node is not None
    script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const expression = JSON.parse(fs.readFileSync(0, "utf8"));
const animation = (endTime, playState = "running") => ({
  playState, effect: { getComputedTiming: () => ({endTime}) }
});
const cases = [
  [animation(Infinity)],
  [animation(Infinity), animation(120)],
  [animation(Infinity), animation(120, "finished")],
  [animation(120)],
  [animation(120, "paused")],
  [{playState: "running", effect: null}],
  [],
];
const results = cases.map((animations) =>
  vm.runInNewContext(expression, {document: {getAnimations: () => animations}})
);
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(  # noqa: S603 - Fixed Node code evaluates the real local wait expression.
        [node, "-e", script],
        input=json.dumps(expressions[0]),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == [True, False, True, False, True, False, True]


@pytest.mark.parametrize("policy_failure", [False, True])
@pytest.mark.parametrize("outer_response_first", [False, True])
def test_fixture_collects_response_acknowledgments(
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy_failure: bool,
    outer_response_first: bool,
) -> None:
    """Collect fixture acknowledgments before returning any final result."""
    specification = importlib.util.spec_from_file_location(
        "controlled_browser_safety_test",
        REPOSITORY_ROOT / "scripts/tests/run_browser_proof.py",
    )
    assert specification is not None
    assert specification.loader is not None
    runner = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(runner)
    browser = runner.FixtureBrowser.__new__(runner.FixtureBrowser)
    browser._identifier = 0
    browser._read_only = False
    browser._pending_interceptions = set()
    browser.interceptions = browser._pending_interceptions
    browser.assets = {}
    browser.requests = []
    browser.failures = []
    sent: list[dict[str, object]] = []
    paused = {
        "method": "Fetch.requestPaused",
        "params": {
            "requestId": "fixture",
            "request": {"method": "GET", "url": ORIGIN + "/favicon.ico"},
        },
    }
    outer = {"id": 1, "result": {"ready": True}}
    acknowledgement = (
        {"id": 2, "error": {"message": "synthetic-control"}}
        if policy_failure
        else {"id": 2, "result": {}}
    )
    replies = iter(
        json.dumps(item)
        for item in [
            paused,
            *(
                [outer, acknowledgement]
                if outer_response_first
                else [acknowledgement, outer]
            ),
        ]
    )
    monkeypatch.setattr(runner.proof._Cdp, "_receive", lambda _self: next(replies))
    monkeypatch.setattr(browser, "_send", lambda value: sent.append(json.loads(value)))
    if policy_failure:
        with pytest.raises(AssertionError) as failure:
            browser.command("Runtime.evaluate")
        assert "synthetic-control" not in str(failure.value)
    else:
        assert browser.command("Runtime.evaluate") == {"ready": True}
        assert not browser._pending_interceptions
        assert not browser.interceptions
    assert sent[1]["method"] == "Fetch.fulfillRequest"
    assert browser.failures == []


def test_injected_http_error_uses_the_actual_client_parser(proof: ModuleType) -> None:
    """Distinguish an accepted HTTP error envelope from an untyped fixture Error."""
    node = shutil.which("node")
    assert node is not None
    script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const ts = require("typescript");
const bootstrap = JSON.parse(fs.readFileSync(0, "utf8"));
let networkCalls = 0;
const browser = {
  console: {error() {}}, addEventListener() {}, URL, Response,
  location: {href: "http://127.0.0.1:5174/overview?proof_mode=error"},
  fetch: async () => { networkCalls++; throw Error("Unexpected network"); },
};
vm.runInNewContext(bootstrap, browser);
const moduleValue = {exports: {}};
const source = fs.readFileSync("apps/admin/src/api.ts", "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}
}).outputText;
vm.runInNewContext(compiled, {
  module: moduleValue, exports: moduleValue.exports,
  Headers, Response, URLSearchParams, AbortController, AbortSignal,
  setTimeout, clearTimeout,
});
(async () => {
  const api = moduleValue.exports;
  let observed = null;
  try { await api.createAdministrationClient(browser.fetch).providers(); }
  catch (error) {
    observed = {
      typed: error instanceof api.AdministrationApiError,
      status: error.status,
      message: api.errorMessage(error),
    };
  }
  process.stdout.write(JSON.stringify({observed, networkCalls}));
})().catch(() => {process.exitCode = 1});
"""
    result = subprocess.run(  # noqa: S603 - Fixed offline code runs the actual bootstrap and API parser.
        [node, "-e", script],
        cwd=REPOSITORY_ROOT,
        input=json.dumps(proof._BROWSER_PROOF_BOOTSTRAP),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {
        "observed": {
            "typed": True,
            "status": 503,
            "message": "Injected proof failure.",
        },
        "networkCalls": 0,
    }
