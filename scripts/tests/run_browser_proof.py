"""Run the changed localhost proof helpers against controlled real-App fixtures."""
# ruff: noqa: E501, EM101, PLR0915, PLR2004, SLF001, TRY003
# No deployment proof entry point, session, provider call, or live request is used.

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
DATE = "2026-08-25T00:00:00Z"
SIZES = ((1440, 1000), (1100, 800), (390, 844))
ROUTES = {
    "/overview": "Overview",
    "/services": "Services",
    "/configuration": "LLM configuration",
    "/logs": "Logs",
    "/statistics": "Usage and cost statistics",
    "/operations": "Activity & health",
}
SERVICES = [
    {
        "api_name": name,
        "display_name": display,
        "parent_service_api_name": parent,
        "is_root": name == "root",
        "created_at": DATE,
    }
    for name, display, parent in (
        ("root", "Root", None),
        ("alpha", "Alpha", "root"),
        ("alpha-child", "Alpha Child", "alpha"),
    )
]


@cache
def _evidence() -> Path:
    """Create one private, exclusive directory for synthetic browser evidence."""
    return Path(tempfile.mkdtemp(prefix="llmrouter-browser-proof-evidence-"))


def _load_proof() -> ModuleType:
    """Import the actual proof functions without calling their entry point."""
    sys.path.insert(0, str(ROOT / "scripts"))
    specification = importlib.util.spec_from_file_location(
        "controlled_localhost_proof", ROOT / "scripts/prove-localhost.py"
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


proof = _load_proof()


def _page(items: list[object]) -> dict[str, object]:
    return {"items": items, "page": {"has_more": False}}


def _shell_values() -> dict[str, object]:
    values: dict[str, object] = {
        "services": _page(SERVICES),
        "workspaces": _page(
            [
                {
                    "api_name": "alpha-private",
                    "display_name": "Alpha private",
                    "created_at": DATE,
                }
            ]
        ),
        "keys": _page(
            [
                {
                    "id": "synthetic-key-id",
                    "name": "localhost proof",
                    "created_at": DATE,
                    "last_used_at": None,
                }
            ]
        ),
    }
    values.update({f"service:{service['api_name']}": service for service in SERVICES})
    return values


class FixtureBrowser(proof._Cdp):
    """Fulfill every browser request before it can reach a live service."""

    def __init__(self, endpoint: str, assets: dict[str, tuple[str, bytes]]) -> None:
        """Use a fresh, unauthenticated browser with controlled assets."""
        super().__init__(endpoint)
        self.assets = assets
        self.fixture = "shell"
        self.boot: dict[str, object] = {}
        self.values: dict[str, object] = _shell_values()
        self.tail = ""
        self.requests: list[dict[str, str]] = []
        self.failures: list[str] = []

    def _html(self) -> bytes:
        boot = dict(self.boot)
        patch = ""
        if self.fixture == "shell":
            boot = {"hold": [], "fail": [], "signedOut": False, **boot}
            held = list(boot["hold"])
            boot["hold"] = ["session", *held]
            patch = (
                "Object.assign(window.shellFixture.values,"
                + json.dumps(self.values).replace("<", "\\u003c")
                + ");window.shellFixture.hold="
                + json.dumps(held)
                + ";while(window.shellFixture.pending.some(p=>p.name==='session'))"
                + "window.shellFixture.finish('session');"
            )
        elif self.fixture == "creation":
            boot = {"hold": ["createService"], "services": SERVICES, **boot}
        else:
            boot = {"hold": [], "fail": [], **boot}
        return (
            '<!doctype html><html lang="en"><head><meta name="viewport" '
            'content="width=device-width, initial-scale=1, viewport-fit=cover">'
            "<title>Controlled browser proof</title>"
            '<link rel="stylesheet" href="/fixture.css"></head><body>'
            '<div id="root"></div><script>window.'
            + self.fixture
            + "Boot="
            + json.dumps(boot).replace("<", "\\u003c")
            + ';</script><script src="/'
            + self.fixture
            + '.js"></script>'
            + "<script>"
            + patch
            + self.tail
            + "</script></body></html>"
        ).encode()

    def __exit__(self, *args: object) -> None:
        """Keep synthetic failure evidence before the debug socket closes."""
        try:
            if args and args[0] is not None:
                detail = self.evaluate(
                    """({path:location.pathname, heading:document.querySelector('main h1')?.textContent, errors:globalThis.__llmrouterProofErrors ?? [], text:document.querySelector('main')?.innerText, active:document.activeElement?.outerHTML, calls:(window.logsFixture??window.shellFixture??window.creationFixture)?.calls})"""
                )
                (_evidence() / "failure.json").write_text(
                    json.dumps(detail, indent=2) + "\n"
                )
                screenshot = self.command("Page.captureScreenshot", {"format": "png"})
                (_evidence() / "failure.png").write_bytes(
                    base64.b64decode(screenshot["data"])
                )
        except AssertionError, OSError:
            (_evidence() / "failure-capture.txt").write_text(
                "The browser closed before failure evidence could be read.\n"
            )
        finally:
            super().__exit__(*args)

    def _receive(self) -> str:
        """Handle intercepted requests without losing outer command replies."""
        while True:
            raw = super()._receive()
            message = json.loads(raw)
            if message.get("method") != "Fetch.requestPaused":
                return raw
            parameters = message["params"]
            request = parameters["request"]
            allowed = proof._read_only_request_allowed(
                request["method"], request["url"]
            )
            path = urlsplit(request["url"]).path if allowed else ""
            response: tuple[str, bytes] | None = None
            if allowed:
                self.requests.append({"method": request["method"], "path": path})
                if path in self.assets:
                    response = self.assets[path]
                elif path == "/favicon.ico":
                    response = ("image/x-icon", b"")
                elif path == "/v1/admin/request-logs/log-c/media/media-fixture/content":
                    response = ("image/png", b"test")
                elif path in {
                    *ROUTES,
                    "/",
                    "/access",
                    "/providers",
                    "/models",
                    "/assignments",
                    "/playground",
                    "/services/alpha",
                }:
                    response = ("text/html", self._html())
            self._identifier += 1
            identifier = self._identifier
            self._pending_interceptions.add(identifier)
            if response is None:
                self.failures.append("Unexpected fixture request blocked.")
                command = {
                    "requestId": parameters["requestId"],
                    "errorReason": "BlockedByClient",
                }
                method = "Fetch.failRequest"
            else:
                content_type, body = response
                command = {
                    "requestId": parameters["requestId"],
                    "responseCode": 200,
                    "responseHeaders": [
                        {"name": "Content-Type", "value": content_type},
                        {"name": "Cache-Control", "value": "no-store"},
                        {
                            "name": "Content-Security-Policy",
                            "value": "connect-src http://127.0.0.1:5174",
                        },
                    ],
                    "body": base64.b64encode(body).decode("ascii"),
                }
                method = "Fetch.fulfillRequest"
            self._send(
                json.dumps({"id": identifier, "method": method, "params": command})
            )

    def use(
        self,
        fixture: str,
        *,
        boot: dict[str, object] | None = None,
        values: dict[str, object] | None = None,
        tail: str = "",
    ) -> None:
        """Select only a known fixture for the next document navigation."""
        assert fixture in {"shell", "creation", "logs"}
        self.fixture = fixture
        self.boot = boot or {}
        self.values = _shell_values() if values is None else values
        self.tail = tail


def _wait(browser: FixtureBrowser, expression: str) -> object:
    return proof._wait_browser(
        browser, expression, "A controlled fixture did not settle."
    )


def _settle(browser: FixtureBrowser) -> None:
    _wait(browser, "document.querySelector('main h1') !== null")
    browser.evaluate(
        "new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))"
    )


def _capture(
    browser: FixtureBrowser,
    name: str,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    """Save synthetic-only measurements and an image after strict validation."""
    _settle(browser)
    geometry = proof._assert_shell_and_graph(browser, mobile=mobile)
    proof._assert_route_controls(browser)
    proof._assert_axe(browser)
    screenshot = browser.command(
        "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
    )
    (_evidence() / f"{name}.png").write_bytes(base64.b64decode(screenshot["data"]))
    results.append({"state": name, "geometry": geometry, "axe": []})
    assert browser.failures == []
    sys.stdout.write(f"Controlled state passed: {name}\n")
    sys.stdout.flush()


def _click(browser: FixtureBrowser, selector: str) -> None:
    proof._click_selector(browser, selector)


def _key(browser: FixtureBrowser, key: str) -> None:
    proof._press_key(browser, key)


def _shell_routes(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    browser.use("shell")
    for path, heading in ROUTES.items():
        proof._navigate(browser, path, heading)
        _capture(browser, f"{width}-{path[1:]}", mobile=mobile, results=results)
    for path in (
        "/",
        "/access",
        "/providers",
        "/models",
        "/assignments",
        "/playground",
    ):
        heading = (
            "Overview"
            if path == "/"
            else "Services"
            if path == "/access"
            else "LLM configuration"
        )
        proof._navigate(browser, path, heading)
        proof._assert_layout(browser, mobile=mobile)
    proof._prove_service_tree(browser, mobile=mobile)
    # The second scan must reuse Axe on this same hydrated document.
    proof._assert_axe(browser)


def _creation(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    doubled: bool,
    results: list[dict[str, object]],
) -> None:
    browser.use("creation")
    proof._navigate(browser, "/services", "Services")
    if doubled:
        browser.evaluate("document.documentElement.style.fontSize='32px'")
    _wait(browser, "document.querySelector('[data-service-create-action]') !== null")
    _settle(browser)
    action = "[data-service-create-action]"
    browser.evaluate("document.querySelector('[data-service-api-name=root]').focus()")
    _key(browser, "ArrowDown")
    assert (
        browser.evaluate(
            "document.activeElement.matches('[data-service-create-action]')"
        )
        is True
    )
    _key(browser, "Enter")
    _wait(browser, "document.activeElement?.textContent==='New service'")
    _key(browser, "Tab")
    assert browser.evaluate("document.activeElement.name==='display_name'") is True
    _key(browser, "Escape")
    _wait(browser, "document.querySelector('.od-graph-inspector[open]')===null")
    assert (
        browser.evaluate(
            "document.activeElement.matches('[data-service-create-action]')"
        )
        is True
    )
    assert (
        browser.evaluate(
            "creationFixture.calls.filter(c=>c.name==='createService').length"
        )
        == 0
    )
    _key(browser, "Enter")
    _wait(browser, "document.activeElement?.textContent==='New service'")
    proof._set_control(browser, "input[name=display_name]", "New child")
    proof._set_control(browser, "input[name=api_name]", "new-child")
    suffix = "200" if doubled else "100"
    _capture(
        browser, f"{width}-creation-draft-{suffix}", mobile=mobile, results=results
    )
    proof._click_text(browser, "Create service", scope=".od-graph-inspector[open]")
    _wait(browser, "creationFixture.pending.some(p=>p.name==='createService')")
    assert (
        browser.evaluate("""(() => {
      const p=document.querySelector('.od-graph-inspector[open]');
      return p.getAttribute('aria-busy')==='true' && p.innerText.includes('Creating service')
        && [...p.querySelectorAll('input,button')].every(e=>e.disabled);
    })()""")
        is True
    )
    _key(browser, "Escape")
    _click(browser, action)
    assert (
        browser.evaluate(
            "creationFixture.calls.filter(c=>c.name==='createService').length"
        )
        == 1
    )
    assert (
        browser.evaluate(
            "document.querySelector('.od-graph-inspector[open] h2').textContent==='New service'"
        )
        is True
    )
    assert browser.evaluate(
        "creationFixture.pending.find(p=>p.name==='createService').args[0]"
    ) == {
        "display_name": "New child",
        "api_name": "new-child",
        "parent_service_api_name": "root",
    }
    _capture(
        browser, f"{width}-creation-pending-{suffix}", mobile=mobile, results=results
    )
    browser.evaluate("creationFixture.finish('createService')")
    _wait(
        browser,
        "document.querySelector('.od-graph-inspector[open] h2')?.textContent==='New child'",
    )
    assert (
        browser.evaluate(
            "location.pathname==='/services' && document.querySelector('[data-service-api-name=new-child]').getAttribute('aria-pressed')==='true'"
        )
        is True
    )
    _capture(
        browser, f"{width}-creation-success-{suffix}", mobile=mobile, results=results
    )


def _conditional_states(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    """Check the old proof's conditional routes at the current viewport."""
    _overview_states(browser, width, mobile=mobile, results=results)
    _service_states(browser, width, mobile=mobile, results=results)
    _configuration_states(browser, width, mobile=mobile, results=results)


def _overview_states(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    """Keep unrelated Overview counts and recover through its own retry."""
    browser.use(
        "shell", boot={"hold": ["services", "providers", "providerModels", "health"]}
    )
    proof._navigate(browser, "/overview", "Loading Overview.")
    _wait(
        browser,
        "document.getAnimations().some(a=>a.effect?.getComputedTiming().endTime===Infinity)",
    )
    assert (
        browser.evaluate(
            "document.querySelector('[aria-label=\"Resource totals\"]')===null"
        )
        is True
    )
    _capture(browser, f"{width}-overview-loading", mobile=mobile, results=results)
    browser.evaluate("""setTimeout(() => {
      shellFixture.hold=[];
      while(shellFixture.pending.length) shellFixture.finish(shellFixture.pending[0].name);
    }, 100)""")
    proof._assert_overview_totals(browser, services=3, providers=1, provider_models=1)
    _wait(
        browser,
        """(() => {
      const text=document.querySelector('main')?.innerText ?? '';
      return !text.includes('Loading Overview.') && text.includes('Services\\n3')
        && text.includes('Provider connections\\n1') && text.includes('Provider-models\\n1');
    })()""",
    )
    _capture(browser, f"{width}-overview-loaded", mobile=mobile, results=results)

    browser.use("shell", boot={"fail": ["providers"]})
    proof._navigate(browser, "/overview", "Overview")
    _wait(
        browser,
        """(() => {
      const text=document.querySelector('main')?.innerText ?? '';
      return text.includes('Provider connections is unavailable.')
        && text.includes('The Router could not complete the operation. Try again.')
        && !text.includes('Controlled providers failure.') && text.includes('Services\\n3')
        && text.includes('Provider-models\\n1') && text.includes('Provider connections\\nUnavailable')
        && [...document.querySelectorAll('main button')].some(e=>e.textContent.trim()==='Retry Overview');
    })()""",
    )
    _capture(browser, f"{width}-overview-partial-error", mobile=mobile, results=results)
    browser.evaluate("shellFixture.fail=[]")
    proof._click_text(browser, "Retry Overview", scope="main")
    _wait(
        browser,
        """(() => {
      const text=document.querySelector('main')?.innerText ?? '';
      return !text.includes('Provider connections is unavailable.')
        && !text.includes('The Router could not complete the operation. Try again.') && text.includes('Provider connections\\n1')
        && shellFixture.calls.filter(c=>c.name==='providers').length===2;
    })()""",
    )
    _capture(browser, f"{width}-overview-retry-ready", mobile=mobile, results=results)


def _service_states(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    """Keep creation unavailable until retry confirms the stored root."""
    browser.use("creation", boot={"hold": ["services", "createService"]})
    proof._navigate(browser, "/services", "Loading Services.")
    assert (
        browser.evaluate(
            "document.querySelector('[data-service-create-action]')===null"
        )
        is True
    )
    _capture(browser, f"{width}-services-loading", mobile=mobile, results=results)
    browser.evaluate("""creationFixture.finish('services',undefined,{
      status:503,code:'unavailable',message:'Controlled services failure.'
    })""")
    _wait(
        browser,
        "document.querySelector('main')?.innerText.includes('Controlled services failure.')",
    )
    assert (
        browser.evaluate(
            "document.querySelector('[data-service-create-action]')===null"
        )
        is True
    )
    _capture(browser, f"{width}-services-error", mobile=mobile, results=results)
    proof._click_text(browser, "Retry services", scope="main")
    _wait(
        browser,
        """creationFixture.pending.some(p=>p.name==='services') &&
      document.querySelector('main')?.innerText.includes('Loading Services.')""",
    )
    assert (
        browser.evaluate("creationFixture.calls.filter(c=>c.name==='services').length")
        == 2
    )
    _capture(browser, f"{width}-services-retry-loading", mobile=mobile, results=results)
    browser.evaluate("creationFixture.finish('services')")
    _wait(
        browser,
        """document.querySelectorAll('[data-service-api-name]').length===3 &&
      document.querySelector('[data-service-api-name=root]')?.getAttribute('aria-pressed')==='true' &&
      document.querySelectorAll('[data-service-create-action]').length===1""",
    )
    _capture(browser, f"{width}-services-retry-ready", mobile=mobile, results=results)

    browser.use("creation", boot={"services": []})
    proof._navigate(
        browser,
        "/services",
        "The Router could not complete the operation. Try again.",
    )
    assert (
        browser.evaluate(
            "document.querySelector('[data-service-create-action]')===null"
        )
        is True
    )
    assert (
        browser.evaluate(
            """document.querySelector('main')?.innerText.includes('Services are unavailable.') &&
      [...document.querySelectorAll('main button')].some(e=>e.textContent.trim()==='Retry services')"""
        )
        is True
    )
    proof._assert_missing_root_state(browser)
    _capture(browser, f"{width}-services-missing-root", mobile=mobile, results=results)
    browser.evaluate(f"creationFixture.services={json.dumps(SERVICES)}")
    proof._click_text(browser, "Retry services", scope="main")
    _wait(
        browser,
        """document.querySelectorAll('[data-service-api-name]').length===3 &&
      document.querySelector('[data-service-api-name=root]')?.getAttribute('aria-pressed')==='true' &&
      document.querySelectorAll('[data-service-create-action]').length===1""",
    )
    assert (
        browser.evaluate("creationFixture.calls.some(c=>c.name==='createService')")
        is False
    )
    _capture(
        browser, f"{width}-services-root-retry-ready", mobile=mobile, results=results
    )


def _configuration_states(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    """Retain confirmed graph records after a failed refresh and retry."""
    browser.use("shell", boot={"fail": ["providers"]})
    proof._navigate(browser, "/configuration", "Unable to load Providers.")
    assert (
        browser.evaluate("""(() => {
      const main=document.querySelector('main');
      return main.querySelector('[data-node-id="model:model"]')!==null
        && main.querySelector('[data-node-id="provider:provider"]')===null
        && main.innerText.includes('Provider connections is unavailable.')
        && main.innerText.includes('The Router could not complete the operation. Try again.')
        && !main.innerText.includes('Controlled providers failure.')
        && !main.innerText.includes('Unable to load Canonical models.')
        && !main.innerText.includes('The administration data is not available')
        && [...main.querySelectorAll('[data-column-id=providers] button')]
          .some(e=>e.textContent.trim()==='Retry');
    })()""")
        is True
    )
    _capture(
        browser, f"{width}-configuration-partial-error", mobile=mobile, results=results
    )
    browser.evaluate("shellFixture.fail=[]")
    proof._click_text(browser, "Retry", scope="[data-column-id=providers]")
    _wait(
        browser,
        """document.querySelector('[data-node-id="provider:provider"]')!==null &&
      !document.querySelector('[data-column-id=providers]')?.innerText.includes('Unable to load Providers.')""",
    )
    assert (
        browser.evaluate("shellFixture.calls.filter(c=>c.name==='providers').length")
        == 2
    )
    _capture(
        browser, f"{width}-configuration-retry-ready", mobile=mobile, results=results
    )

    browser.evaluate("shellFixture.fail=['providers']")
    proof._click_text(browser, "Refresh configuration", scope=".od-graph-toolbar")
    _wait(
        browser,
        """document.querySelector('[data-column-id=providers]')?.innerText.includes('Unable to load Providers.') &&
      document.querySelector('[data-node-id="provider:provider"]')!==null &&
      document.querySelector('[data-node-id="model:model"]')!==null &&
      document.querySelector('main')?.innerText.includes('Provider connections is stale.') &&
      document.querySelector('main')?.innerText.includes('The Router could not complete the operation. Try again.') &&
      !document.querySelector('main')?.innerText.includes('Controlled providers failure.')""",
    )
    _capture(
        browser, f"{width}-configuration-stale-error", mobile=mobile, results=results
    )
    browser.evaluate("shellFixture.fail=[]")
    proof._click_text(browser, "Retry", scope="[data-column-id=providers]")
    _wait(
        browser,
        """document.querySelector('[data-node-id="provider:provider"]')!==null &&
      !document.querySelector('main')?.innerText.includes('Provider connections is stale.') &&
      !document.querySelector('main')?.innerText.includes('The Router could not complete the operation. Try again.') &&
      !document.querySelector('[data-column-id=providers]')?.innerText.includes('Unable to load Providers.')""",
    )
    assert (
        browser.evaluate("shellFixture.calls.filter(c=>c.name==='providers').length")
        == 4
    )
    _capture(
        browser,
        f"{width}-configuration-stale-retry-ready",
        mobile=mobile,
        results=results,
    )

    empty = _shell_values()
    empty.update(
        {
            name: _page([])
            for name in (
                "services",
                "providers",
                "models",
                "providerModels",
                "credentials",
                "assignments",
            )
        }
    )
    browser.use("shell", values=empty)
    proof._navigate(browser, "/configuration", "No providers are configured.")
    assert browser.evaluate("document.querySelectorAll('[data-node-id]').length") == 0
    _capture(browser, f"{width}-configuration-empty", mobile=mobile, results=results)


def _configuration(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    """Use the current graph-local context and refresh controls."""
    browser.use("shell")
    proof._navigate(browser, "/configuration", "LLM configuration")
    proof._assert_route_controls(browser)
    proof._set_control(browser, "select[aria-label='Service context']", "alpha")
    _wait(
        browser,
        "location.search==='?service=alpha' && shellFixture.calls.some(c=>c.name==='assignments' && c.args[0]==='alpha')",
    )
    previous = browser.evaluate(
        "shellFixture.calls.filter(c=>c.name==='providers').length"
    )
    proof._click_text(browser, "Refresh configuration", scope=".od-graph-toolbar")
    _wait(
        browser, f"shellFixture.calls.filter(c=>c.name==='providers').length>{previous}"
    )
    _capture(browser, f"{width}-configuration-context", mobile=mobile, results=results)
    for size in (16, 32):
        browser.evaluate(f"document.documentElement.style.fontSize='{size}px'")
        _settle(browser)
        _click(browser, '[data-node-id="provider:provider"]')
        _wait(browser, "document.querySelector('.od-graph-inspector[open]')!==null")
        _capture(
            browser,
            f"{width}-configuration-inspector-{size}",
            mobile=mobile,
            results=results,
        )
        proof._close_graph_inspector(browser)


def _full_configuration_values() -> dict[str, object]:
    """Supply several assignments so keyboard order does not imply Workflow."""
    values = _shell_values()
    models = [
        {
            "api_name": name,
            "display_name": (
                "Text model with a deliberately long name for responsive proof"
                if name == "text-model"
                else name
            ),
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "capabilities": ["streaming"],
            "constraints": {},
            "price_source": "manual",
            "created_at": DATE,
        }
        for name in ("text-model", "embedding-model", "media-model")
    ]
    mappings = [
        {
            "api_name": name,
            "provider_api_name": "fake-provider",
            "model_api_name": model,
            "provider_model_name": wire,
            "enabled": True,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "capabilities": ["streaming"],
            "reasoning_mappings": [],
            "created_at": DATE,
        }
        for name, model, wire in (
            ("failed-text", "text-model", "fake-error-transport-v1"),
            ("interrupted-text", "text-model", "fake-stream-interruption-v1"),
            ("text", "text-model", "fake-text-v1"),
            ("embedding", "embedding-model", "fake-embedding-v1"),
            ("media", "media-model", "fake-media-v1"),
        )
    ]
    values.update(
        providers=_page(
            [
                {
                    "api_name": "fake-provider",
                    "display_name": "Fake provider",
                    "adapter": "fake",
                    "enabled": True,
                    "created_at": DATE,
                }
            ]
        ),
        models=_page(models),
        providerModels=_page(mappings),
        assignments=_page(
            [
                {
                    "api_name": name,
                    "display_name": name.title(),
                    "definition_kind": "direct_chain",
                    "defined_by_service_api_name": "alpha",
                    "direct_chain": [
                        {"provider_model_api_name": route} for route in routes
                    ],
                    "effective_chain": [
                        {"provider_model_api_name": route} for route in routes
                    ],
                    "observed_requirements": ["text_input", "text_output"],
                }
                for name, routes in (
                    ("default", ["text"]),
                    ("embedding", ["embedding"]),
                    ("workflow", ["failed-text", "text"]),
                )
            ]
        ),
    )
    return values


def _full_configuration_sequence(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    """Run the real configuration proof through its first provider-call boundary."""
    browser.use("shell", values=_full_configuration_values())
    original = proof._click_text

    class ProviderCallBoundaryError(Exception):
        """Stop before the controlled fixture's first provider call."""

    def click(browser: FixtureBrowser, text_value: str, *, scope: str = "body") -> None:
        if text_value == "Run operation":
            raise ProviderCallBoundaryError
        original(browser, text_value, scope=scope)

    proof._click_text = click
    try:
        try:
            proof._prove_configuration_graph(browser, mobile=mobile)
        except ProviderCallBoundaryError:
            _capture(
                browser,
                f"{width}-full-configuration-sequence",
                mobile=mobile,
                results=results,
            )
        else:
            raise AssertionError(
                "The configuration proof did not reach the provider-call boundary."
            )
    finally:
        proof._click_text = original


def _websocket_boundary(
    browser: FixtureBrowser, *, results: list[dict[str, object]]
) -> None:
    """Prove that even a loopback WebSocket cannot send a handshake."""
    browser.use("shell")
    proof._navigate(browser, "/overview", "Overview")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.5)
        port = listener.getsockname()[1]
        failed = browser.evaluate(f"""new Promise(resolve=>{{
          const socket=new WebSocket('ws://127.0.0.1:{port}/controlled-boundary');
          socket.onerror=()=>resolve(true);socket.onopen=()=>{{socket.close();resolve(false)}};
          setTimeout(()=>{{socket.close();resolve(false)}},1500);
        }})""")
        try:
            connection, _address = listener.accept()
        except TimeoutError:
            pass
        else:
            connection.close()
            raise AssertionError("The browser sent a forbidden WebSocket handshake.")
        assert failed is True
    results.append({"websocket_handshakes": 0, "blocked": True})


def _oversized(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    services = [SERVICES[0]]
    keys = [
        f"service-{index}" + ("-" + "a" * 45 if index % 2 else "")
        for index in range(24)
    ]
    for index, name in enumerate(keys):
        services.append(
            {
                "api_name": name,
                "display_name": f"Service {index} with a long display name",
                "parent_service_api_name": "root"
                if index < 12
                else keys[0]
                if index == 12
                else keys[index - 1],
                "created_at": DATE,
                "is_root": False,
            }
        )
    browser.use("creation", boot={"services": services})
    proof._navigate(browser, "/services", "Services")
    for size in (16, 32):
        browser.evaluate(f"document.documentElement.style.fontSize='{size}px'")
        _settle(browser)
        scroll = browser.evaluate("""(() => {
          const v=document.querySelector('.od-graph-viewport');
          const t=document.querySelector('.od-graph-toolbar');
          const before=t.getBoundingClientRect().top;
          v.scrollLeft=v.scrollWidth;v.scrollTop=v.scrollHeight;
          return {x:v.scrollLeft,y:v.scrollTop,stable:before===t.getBoundingClientRect().top};
        })()""")
        assert scroll["x"] > 0
        assert scroll["y"] > 0
        assert scroll["stable"] is True
        _capture(
            browser, f"{width}-local-scroll-{size}", mobile=mobile, results=results
        )


def _logs(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    browser.use(
        "logs",
        tail="""
      Object.defineProperty(logsFixture.values,'requestLogMedia',{get(){
        return fetch('/v1/admin/request-logs/log-c/media/media-fixture/content').then(r=>r.blob());
      }});
    """,
    )
    proof._navigate(browser, "/logs", "Logs")
    _wait(
        browser,
        """document.querySelector('[aria-label="Inspect Logs details for request log-c"]')!==null""",
    )
    selector = 'button[aria-label="Inspect Logs details for request log-c"]'
    browser.evaluate(
        f"[...document.querySelectorAll({json.dumps(selector)})].find(e=>e.getBoundingClientRect().width>0).focus()"
    )
    _key(browser, "Enter")
    _wait(
        browser,
        "document.querySelector('#logs-details-region')?.innerText.includes('Request content')",
    )
    proof._assert_logs_content(browser, marker="WWWWWWWWWW")
    assert (
        browser.evaluate("""(() => {
      const d=document.querySelector('#logs-details-region');
      return d.innerText.includes('Synthetic retained content') && d.innerText.includes('WWWWWWWWWW')
        && !d.querySelector('script,iframe,a[href^="https:"]')
        && globalThis.logMarkupExecuted!==true
        && document.activeElement.textContent==='Logs details for request log-c'
        && document.documentElement.scrollWidth<=innerWidth;
    })()""")
        is True
    )
    proof._click_text(
        browser, "Prepare retained media download", scope="#logs-details-region"
    )
    _wait(
        browser,
        "document.querySelector('#logs-details-region a[download]')?.href.startsWith('blob:')",
    )
    assert {
        "method": "GET",
        "path": "/v1/admin/request-logs/log-c/media/media-fixture/content",
    } in browser.requests
    _capture(browser, f"{width}-logs-details", mobile=mobile, results=results)
    _key(browser, "Escape")
    _wait(browser, "document.querySelector('#logs-details-region')===null")
    assert (
        browser.evaluate(
            "document.activeElement.getAttribute('aria-label')==='Inspect Logs details for request log-c'"
        )
        is True
    )


def _statistics(
    browser: FixtureBrowser,
    width: int,
    *,
    mobile: bool,
    results: list[dict[str, object]],
) -> None:
    browser.use("shell")
    proof._navigate(browser, "/statistics", "Usage and cost statistics")
    assert (
        browser.evaluate("""(() => {
      const from=document.querySelector('input[name=from]'), through=document.querySelector('input[name=through]');
      return from?.type==='date' && through?.type==='date'
        && from.labels[0].textContent==='From' && through.labels[0].textContent==='Through'
        && !document.querySelector('input[name=to]')
        && document.querySelector('main').innerText.includes('UTC dates. From and Through include the selected dates.');
    })()""")
        is True
    )
    proof._set_control(browser, "input[name=from]", "2026-03-08")
    proof._set_control(browser, "input[name=through]", "2026-03-08")
    proof._click_text(browser, "Run statistics")
    _wait(browser, "shellFixture.calls.some(c=>c.name==='statistics')")
    query = browser.evaluate(
        "shellFixture.calls.filter(c=>c.name==='statistics').at(-1).args[0]"
    )
    assert query["from"] == "2026-03-08T00:00:00Z"
    assert query["to"] == "2026-03-09T00:00:00Z"
    _capture(browser, f"{width}-statistics-dates", mobile=mobile, results=results)


def _overview_rejections(
    browser: FixtureBrowser, *, results: list[dict[str, object]]
) -> None:
    """Reject wrong, missing, and duplicate real-App totals without page content."""
    mutations = {
        "wrong": "card.querySelector('strong').textContent='31'",
        "missing": "card.remove()",
        "duplicate": "card.after(card.cloneNode(true))",
    }
    original = proof._wait_browser

    def bounded_wait(browser: FixtureBrowser, expression: str, message: str) -> object:
        return original(browser, expression, message, attempts=2)

    for name, mutation in mutations.items():
        browser.use("shell")
        proof._navigate(browser, "/overview", "Overview")
        proof._assert_overview_totals(
            browser, services=3, providers=1, provider_models=1
        )
        browser.evaluate(
            """(() => {
          const card = [...document.querySelectorAll('[aria-label="Resource totals"] article')]
            .find(item => item.querySelector('.od-stat-label').textContent === 'Services');
        """
            + mutation
            + ";return true;})()"
        )
        proof._wait_browser = bounded_wait
        try:
            try:
                proof._assert_overview_totals(
                    browser, services=3, providers=1, provider_models=1
                )
            except AssertionError as failure:
                if (
                    str(failure)
                    != "The Overview resource totals did not match the proof fixture"
                ):
                    raise AssertionError(
                        "Overview failure context was not safe."
                    ) from None
                results.append({"overview_total": name, "rejected": True})
            else:
                raise AssertionError("An incorrect Overview total passed.")
        finally:
            proof._wait_browser = original


def _legacy_rejections(
    browser: FixtureBrowser, *, results: list[dict[str, object]]
) -> None:
    """Mutate controlled DOM output and require actual proof helpers to reject it."""
    mutations = {
        "topbar": "const e=document.createElement('header');e.className='od-application-topbar';document.querySelector('.od-application-column').prepend(e)",
        "global-selector": "const e=document.createElement('select');e.setAttribute('aria-label','Selected service');document.querySelector('main').append(e)",
        "graph-name": "document.querySelector('.od-graph-viewport').setAttribute('aria-label','Service graph viewport')",
        "graph-height": "document.querySelector('.od-graph-workspace').style.height='700px'",
        "graph-inset": "document.querySelector('.od-graph-workspace').style.marginLeft='24px'",
        "old-heading": "document.querySelector('main h1').textContent='Service management'",
        "wide-inspector": "document.querySelector('.od-graph-inspector[open]').style.width='400px'",
    }
    for name, mutation in mutations.items():
        browser.use("shell")
        proof._navigate(browser, "/services?service=alpha", "Services")
        _wait(browser, "document.querySelector('.od-graph-inspector[open]')!==null")
        _settle(browser)
        proof._assert_shell_and_graph(browser, mobile=False)
        browser.evaluate("(()=>{" + mutation + ";return true})()")
        try:
            proof._assert_shell_and_graph(browser, mobile=False)
        except AssertionError:
            results.append({"legacy": name, "baseline_passed": True, "rejected": True})
        else:
            raise AssertionError("An obsolete shell or graph mutation passed.")
    # The full service proof must pass before its graph-wide action mutation.
    browser.use("shell")
    proof._prove_service_tree(browser, mobile=False)
    browser.use(
        "shell",
        tail="""
      new MutationObserver((_,o)=>{
        const t=document.querySelector('.od-graph-toolbar');
        if(t){const b=document.createElement('button');b.textContent='Create service';t.append(b);o.disconnect();}
      }).observe(document.getElementById('root'),{childList:true,subtree:true});
    """,
    )
    try:
        proof._prove_service_tree(browser, mobile=False)
    except AssertionError:
        results.append(
            {"legacy": "toolbar-creation", "baseline_passed": True, "rejected": True}
        )
    else:
        raise AssertionError("The obsolete graph-wide creation action passed.")
    _legacy_route_rejections(browser, results=results)


def _legacy_route_rejections(
    browser: FixtureBrowser, *, results: list[dict[str, object]]
) -> None:
    """Reject obsolete route labels and the invalid normal empty-root state."""
    for path, name, mutation in (
        (
            "/logs",
            "logs-action",
            "[...document.querySelectorAll('main button')].find(e=>e.textContent.trim()==='Refresh Logs').textContent='Load logs'",
        ),
        (
            "/statistics",
            "statistics-to-label",
            "document.querySelector('input[name=through]').labels[0].textContent='To'",
        ),
        (
            "/statistics",
            "statistics-local-datetime",
            "document.querySelector('input[name=from]').type='datetime-local'",
        ),
        (
            "/configuration",
            "configuration-context",
            "document.querySelector('select[aria-label=\"Service context\"]').options[0].text='Select a service'",
        ),
    ):
        browser.use("shell")
        proof._navigate(browser, path, ROUTES[path])
        proof._assert_route_controls(browser)
        browser.evaluate("(()=>{" + mutation + ";return true})()")
        try:
            proof._assert_route_controls(browser)
        except AssertionError:
            results.append({"legacy": name, "baseline_passed": True, "rejected": True})
        else:
            raise AssertionError("An obsolete route control passed.")
    for path, heading in (
        ("/overview", "Router overview"),
        ("/logs", "Detailed request logs"),
    ):
        browser.use("shell")
        proof._navigate(browser, path, ROUTES[path])
        browser.use(
            "shell",
            tail=f"""
          new MutationObserver((_,o)=>{{
            const h=document.querySelector('main h1');
            if(h){{h.textContent={json.dumps(heading)};o.disconnect();}}
          }}).observe(document.getElementById('root'),{{childList:true,subtree:true}});
        """,
        )
        try:
            proof._navigate(browser, path, ROUTES[path])
        except AssertionError:
            results.append(
                {
                    "legacy": path[1:] + "-heading",
                    "baseline_passed": True,
                    "rejected": True,
                }
            )
        else:
            raise AssertionError("An obsolete route heading passed.")
    browser.use("creation", boot={"services": []})
    proof._navigate(
        browser, "/services", "The Router could not complete the operation. Try again."
    )
    proof._assert_missing_root_state(browser)
    assert (
        browser.evaluate("""(() => {
      const alert=document.querySelector('main [role=alert]');
      if (!alert) return false;
      alert.textContent='No services';
      return true;
    })()""")
        is True
    )
    try:
        proof._assert_missing_root_state(browser)
    except AssertionError:
        results.append(
            {"legacy": "normal-empty-root", "baseline_passed": True, "rejected": True}
        )
    else:
        raise AssertionError("The obsolete normal empty-service state passed.")


def run() -> None:
    """Run only controlled browser assertions and collect all temporary resources."""
    if len(sys.argv) != 1:
        raise SystemExit("The controlled browser proof does not accept arguments.")
    _evidence()
    assets_dir = Path("/tmp") / f"llmrouter-browser-proof-{uuid4().hex}"  # noqa: S108 - The builder creates this random path exclusively.
    profile = Path(tempfile.mkdtemp(prefix="llmrouter-controlled-chrome-"))
    results: list[dict[str, object]] = []
    process: subprocess.Popen[bytes] | None = None
    try:
        node = shutil.which("node")
        assert node is not None
        subprocess.run(  # noqa: S603 - Fixed local builder and generated temporary path.
            [
                node,
                str(ROOT / "scripts/tests/browser-proof-fixture.mjs"),
                str(assets_dir),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=60,
        )
        manifest = json.loads((assets_dir / "manifest.json").read_text())
        assets = {
            path: (item["content_type"], (assets_dir / item["file"]).read_bytes())
            for path, item in manifest["assets"].items()
        }
        port = proof._unused_port()
        process = subprocess.Popen(  # noqa: S603 - Fixed Chrome with a private profile and loopback debugging.
            [
                "/usr/bin/google-chrome",
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                f"--remote-allow-origins=http://127.0.0.1:{port}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with FixtureBrowser(proof._debugging_endpoint(port), assets) as browser:
            for domain in ("Network", "Page", "Accessibility"):
                browser.command(domain + ".enable")
            browser.command("Network.setBypassServiceWorker", {"bypass": True})
            browser.command("Network.setCacheDisabled", {"cacheDisabled": True})
            browser.command("Network.setBlockedURLs", {"urls": ["ws://*", "wss://*"]})
            browser.command(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
            )
            browser.command(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": proof._BROWSER_PROOF_BOOTSTRAP},
            )
            for width, height in SIZES:
                mobile = width == 390
                browser.command(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": 1,
                        "mobile": mobile,
                    },
                )
                _shell_routes(browser, width, mobile=mobile, results=results)
                _configuration(browser, width, mobile=mobile, results=results)
                _full_configuration_sequence(
                    browser, width, mobile=mobile, results=results
                )
                _creation(browser, width, mobile=mobile, doubled=False, results=results)
                _creation(browser, width, mobile=mobile, doubled=True, results=results)
                _oversized(browser, width, mobile=mobile, results=results)
                _logs(browser, width, mobile=mobile, results=results)
                _statistics(browser, width, mobile=mobile, results=results)
                _conditional_states(browser, width, mobile=mobile, results=results)
            browser.command(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 1440,
                    "height": 1000,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )
            _websocket_boundary(browser, results=results)
            _overview_rejections(browser, results=results)
            _legacy_rejections(browser, results=results)
            assert browser.failures == []
            assert not browser._pending_interceptions, (
                "A fixture interception acknowledgment is still pending."
            )
            results.append({"network": browser.requests, "unexpected_requests": 0})
        (_evidence() / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        proof._remove_chrome_profile(profile)
        if assets_dir.exists():
            shutil.rmtree(assets_dir)
        sys.stdout.write(f"Controlled evidence: {_evidence()}\n")
        (_evidence() / "measurements.json").write_text(
            json.dumps(results, indent=2) + "\n"
        )
    sys.stdout.write(f"Controlled browser proof passed. Evidence: {_evidence()}\n")


if __name__ == "__main__":
    run()
