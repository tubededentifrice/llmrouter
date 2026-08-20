/* global WebSocket, fetch */

import { Buffer } from "node:buffer";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";
import { setTimeout } from "node:timers";
import { fileURLToPath, URL } from "node:url";

const routerOrigin = "http://127.0.0.1:5175";
const hostOrigin = "http://127.0.0.1:5176";
const browserPort = 9223;
const serviceId = "service-browser-proof";
const hostToken = "local-browser-proof-authority";
const sessions = new Map();
let createCount = 0;
let revokeCount = 0;

const router = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", routerOrigin);
  if (
    request.method === "POST" &&
    url.pathname === `/v1/services/${serviceId}/administration/embed-sessions`
  ) {
    if (request.headers.authorization !== `Bearer ${hostToken}`) {
      response.writeHead(401).end();
      return;
    }
    const body = await jsonBody(request);
    const sessionId = `browser-session-${++createCount}`;
    const bootstrapToken = `${String(createCount).repeat(43)}`.slice(0, 43);
    const expiresAt = new Date(Date.now() + 60_000).toISOString();
    sessions.set(sessionId, {
      bootstrapToken,
      expiresAt,
      workspaceId: body.workspace_id,
    });
    sendJson(response, 201, {
      session_id: sessionId,
      bootstrap_token: bootstrapToken,
      frame_url: `${routerOrigin}/service-administration?session_id=${sessionId}&host_origin=${encodeURIComponent(hostOrigin)}`,
      expires_at: expiresAt,
      message_version: "1",
    });
    return;
  }
  const revoke = new RegExp(
    `^/v1/services/${serviceId}/administration/embed-sessions/([^/]+)$`,
  ).exec(url.pathname);
  if (request.method === "DELETE" && revoke !== null) {
    sessions.delete(decodeURIComponent(revoke[1] ?? ""));
    ++revokeCount;
    response.writeHead(204).end();
    return;
  }
  if (request.method === "GET" && url.pathname === "/service-administration") {
    const sessionId = url.searchParams.get("session_id") ?? "";
    response.writeHead(200, {
      "Cache-Control": "no-store",
      "Content-Security-Policy": `default-src 'self'; script-src 'self'; frame-ancestors ${hostOrigin}; object-src 'none'`,
      "Content-Type": "text/html; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
    });
    response.end(
      `<!doctype html><html lang="en"><body><main><h1>Router frame proof</h1><p id="scope">Waiting</p></main><script src="/frame.js?session_id=${encodeURIComponent(sessionId)}"></script></body></html>`,
    );
    return;
  }
  if (request.method === "GET" && url.pathname === "/frame.js") {
    const sessionId = url.searchParams.get("session_id") ?? "";
    const session = sessions.get(sessionId);
    response.writeHead(200, {
      "Cache-Control": "no-store",
      "Content-Type": "text/javascript; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
    });
    response.end(frameScript(sessionId, session));
    return;
  }
  response.writeHead(404).end();
});

const repositoryRoot = fileURLToPath(new URL("../../..", import.meta.url));
const exampleRoot = fileURLToPath(new URL("..", import.meta.url));
const vitePath = join(repositoryRoot, "node_modules/vite/bin/vite.js");
const browserProfile = mkdtempSync(join(tmpdir(), "llmrouter-embed-browser-"));
let host;
let chrome;

try {
  await listen(router, 5175);
  host = spawn(process.execPath, [vitePath, "--host", "127.0.0.1"], {
    cwd: exampleRoot,
    env: {
      ...process.env,
      LLMROUTER_EXAMPLE_HOST_TOKEN: hostToken,
      LLMROUTER_EXAMPLE_ROUTER_ORIGIN: routerOrigin,
      LLMROUTER_EXAMPLE_SERVICE_ID: serviceId,
      LLMROUTER_EXAMPLE_WORKSPACE_ID: "workspace-browser-a",
      LLMROUTER_EXAMPLE_SECOND_WORKSPACE_ID: "workspace-browser-b",
      VITE_LLMROUTER_FRAME_ORIGIN: routerOrigin,
    },
    stdio: "ignore",
  });
  await waitForUrl(hostOrigin);
  const raceContextResponse = await fetch(`${hostOrigin}/api/context`);
  const raceContext = await raceContextResponse.json();
  const raceCookie = raceContextResponse.headers
    .get("set-cookie")
    ?.split(";", 1)[0];
  assert(
    raceCookie !== undefined,
    "The example host did not set its state cookie.",
  );
  const raceCreate = fetch(`${hostOrigin}/api/embed-session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: raceCookie,
      Origin: hostOrigin,
    },
    body: JSON.stringify({ expected_revision: raceContext.revision }),
  });
  await delay(10);
  const raceChange = fetch(`${hostOrigin}/api/context`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: raceCookie,
      Origin: hostOrigin,
    },
    body: JSON.stringify({ action: "switch_workspace" }),
  });
  const [raceCreateResponse, raceChangeResponse] = await Promise.all([
    raceCreate,
    raceChange,
  ]);
  assert(
    raceCreateResponse.status === 201 && raceChangeResponse.status === 200,
    "The serialized context race did not complete.",
  );
  assert(
    sessions.size === 0,
    "A concurrent context change left an old-scope Router session active.",
  );
  sessions.clear();
  createCount = 0;
  revokeCount = 0;
  const wrongOrigin = await fetch(`${hostOrigin}/api/context`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: "http://127.0.0.1:5999",
    },
    body: JSON.stringify({ action: "switch_user" }),
  });
  assert(wrongOrigin.status === 403, "The host accepted a wrong API origin.");
  chrome = spawn(
    "/usr/bin/google-chrome",
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      `--remote-debugging-port=${browserPort}`,
      `--user-data-dir=${browserProfile}`,
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  const page = await createBrowserPage();
  const cdp = await connectCdp(page.webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  const navigation = await cdp.send("Page.navigate", { url: hostOrigin });
  assert(
    navigation.errorText === undefined,
    `The browser could not navigate to the example host: ${String(navigation.errorText)}.`,
  );
  await waitForExpression(
    cdp,
    `location.origin === ${JSON.stringify(hostOrigin)} && document.readyState === "complete" && document.querySelector("#root") !== null`,
  );
  try {
    await waitForExpression(
      cdp,
      `document.body.textContent.includes("authorized this exact Router scope") && document.querySelector("iframe") !== null`,
    );
  } catch {
    const visibleState = await evaluate(cdp, "document.body.innerText");
    throw new Error(
      `The first frame did not bootstrap. Session creates: ${createCount}. Visible state: ${String(visibleState)}`,
    );
  }
  const firstFrame = await evaluate(
    cdp,
    `document.querySelector("iframe").getAttribute("src")`,
  );
  assert(
    String(firstFrame).includes("browser-session-1"),
    "The first frame did not start.",
  );
  const firstHeight = await evaluate(
    cdp,
    `document.querySelector("iframe").getAttribute("height")`,
  );
  assert(firstHeight === "360", "The frame height message was not applied.");
  await waitForExpression(
    cdp,
    `document.querySelector("iframe")?.dataset.section === "requests"`,
  );
  const rejectedMessageState = await evaluate(
    cdp,
    `(() => {
      const frame = document.querySelector("iframe");
      const source = frame.contentWindow;
      const base = {protocol: "llmrouter-admin-embed", version: "1", session_id: "browser-session-1", type: "frame.height_changed", payload: {height_px: 999}};
      dispatchEvent(new MessageEvent("message", {origin: "http://127.0.0.1:5999", source, data: {...base, message_id: "wrong-origin"}}));
      dispatchEvent(new MessageEvent("message", {origin: ${JSON.stringify(routerOrigin)}, source: window, data: {...base, message_id: "wrong-source"}}));
      dispatchEvent(new MessageEvent("message", {origin: ${JSON.stringify(routerOrigin)}, source, data: {...base, session_id: "wrong-session", message_id: "wrong-session"}}));
      dispatchEvent(new MessageEvent("message", {origin: ${JSON.stringify(routerOrigin)}, source, data: {...base, version: "2", message_id: "wrong-version"}}));
      dispatchEvent(new MessageEvent("message", {origin: ${JSON.stringify(routerOrigin)}, source, data: {...base, message_id: "frame-height"}}));
      return {height: frame.getAttribute("height"), section: frame.dataset.section};
    })()`,
  );
  assert(
    rejectedMessageState.height === "360" &&
      rejectedMessageState.section === "requests",
    `The host accepted an invalid or replayed frame message: ${JSON.stringify(rejectedMessageState)}.`,
  );
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 320,
    height: 800,
    deviceScaleFactor: 1,
    mobile: true,
  });
  const phoneMetrics = await evaluate(
    cdp,
    `({scrollWidth: document.documentElement.scrollWidth, buttonHeights: [...document.querySelectorAll("button")].map((button) => button.getBoundingClientRect().height)})`,
  );
  assert(
    phoneMetrics.scrollWidth <= 320 &&
      phoneMetrics.buttonHeights.every((height) => height >= 44),
    `The example host does not fit the phone layout: ${JSON.stringify(phoneMetrics)}.`,
  );
  const keyboardControl = await evaluate(
    cdp,
    `(() => { const button = document.querySelector("button"); button.focus(); return document.activeElement === button; })()`,
  );
  assert(
    keyboardControl === true,
    "A host control cannot receive keyboard focus.",
  );
  await evaluate(
    cdp,
    `([...document.querySelectorAll("button")].find((button) => button.textContent.includes("Switch workspace"))).click()`,
  );
  await waitForExpression(
    cdp,
    `document.body.textContent.includes("workspace-browser-b") && document.querySelector("iframe")?.getAttribute("src").includes("browser-session-2")`,
  );
  assert(
    createCount === 2,
    "The workspace switch did not create one new session.",
  );
  assert(
    revokeCount >= 1,
    "The workspace switch did not revoke the old session.",
  );
  for (const label of ["Switch user", "Change permissions", "Renew session"]) {
    const previousCreateCount = createCount;
    await evaluate(
      cdp,
      `([...document.querySelectorAll("button")].find((button) => button.textContent.includes(${JSON.stringify(label)}))).click()`,
    );
    try {
      await waitForExpression(
        cdp,
        `document.body.textContent.includes("authorized this exact Router scope") && document.querySelector("iframe")?.getAttribute("src").includes("browser-session-${previousCreateCount + 1}")`,
      );
    } catch {
      const state = await evaluate(
        cdp,
        `({text: document.body.innerText, frame: document.querySelector("iframe")?.getAttribute("src")})`,
      );
      throw new Error(
        `${label} did not activate its replacement frame: ${JSON.stringify(state)}.`,
      );
    }
    assert(
      createCount === previousCreateCount + 1,
      `${label} did not create one replacement session.`,
    );
  }
  const expiringSessionId = `browser-session-${createCount}`;
  const expiringSession = sessions.get(expiringSessionId);
  assert(expiringSession !== undefined, "The active session cannot expire.");
  const beforeExpiry = createCount;
  await evaluate(
    cdp,
    `(() => {
      const frame = document.querySelector("iframe");
      dispatchEvent(new MessageEvent("message", {
        origin: ${JSON.stringify(routerOrigin)},
        source: frame.contentWindow,
        data: {
          protocol: "llmrouter-admin-embed",
          version: "1",
          session_id: ${JSON.stringify(expiringSessionId)},
          message_id: "browser-expiry",
          type: "frame.session_expired",
          payload: {expired_at: ${JSON.stringify(expiringSession.expiresAt)}}
        }
      }));
    })()`,
  );
  await waitForExpression(
    cdp,
    `document.body.textContent.includes("authorized this exact Router scope") && document.querySelector("iframe")?.getAttribute("src").includes("browser-session-${beforeExpiry + 1}")`,
  );
  assert(createCount === beforeExpiry + 1, "Frame expiry did not renew once.");
  const beforeMembershipLoss = createCount;
  await evaluate(
    cdp,
    `([...document.querySelectorAll("button")].find((button) => button.textContent.includes("Remove membership"))).click()`,
  );
  await waitForExpression(
    cdp,
    `document.body.textContent.includes("No membership") && document.querySelector("iframe") === null`,
  );
  assert(
    createCount === beforeMembershipLoss,
    "Membership loss created a Router session.",
  );
  await evaluate(
    cdp,
    `([...document.querySelectorAll("button")].find((button) => button.textContent.includes("Restore membership"))).click()`,
  );
  await waitForExpression(
    cdp,
    `document.body.textContent.includes("authorized this exact Router scope") && document.querySelector("iframe")?.getAttribute("src").includes("browser-session-${beforeMembershipLoss + 1}")`,
  );
  assert(
    [...sessions.keys()].length === 1,
    "The host left an old Router session after authority changes.",
  );
  cdp.close();
  process.stdout.write(
    "Embed example browser proof passed: context changes, renewal, membership loss and restore, bounded sizing, keyboard focus, and 320 px layout.\n",
  );
} finally {
  chrome?.kill("SIGTERM");
  host?.kill("SIGTERM");
  await close(router);
  rmSync(browserProfile, { recursive: true, force: true });
}

function frameScript(sessionId, session) {
  const serialized = JSON.stringify({ sessionId, session });
  return `
    const proof = ${serialized};
    const protocol = "llmrouter-admin-embed";
    const nonce = "browser-frame-nonce-0001";
    const send = (type, payload, messageId) => parent.postMessage({protocol, version: "1", session_id: proof.sessionId, message_id: messageId, type, payload}, ${JSON.stringify(hostOrigin)});
    addEventListener("message", (event) => {
      const value = event.data;
      if (event.origin !== ${JSON.stringify(hostOrigin)} || event.source !== parent || value?.protocol !== protocol || value?.version !== "1" || value?.session_id !== proof.sessionId || value?.type !== "host.bootstrap" || value?.payload?.frame_nonce !== nonce || value?.payload?.bootstrap_token !== proof.session.bootstrapToken || value?.payload?.host_origin !== ${JSON.stringify(hostOrigin)}) return;
      proof.session.bootstrapToken = "";
      document.querySelector("#scope").textContent = proof.session.workspaceId;
      send("frame.bootstrapped", {expires_at: proof.session.expiresAt, service_id: ${JSON.stringify(serviceId)}, workspace_id: proof.session.workspaceId}, "frame-bootstrapped");
      send("frame.height_changed", {height_px: 360}, "frame-height");
      send("frame.navigation_changed", {section: "requests"}, "frame-navigation");
    });
    send("frame.ready", {frame_nonce: nonce}, "frame-ready");
  `;
}

async function jsonBody(request) {
  const parts = [];
  for await (const part of request) parts.push(part);
  return JSON.parse(Buffer.concat(parts).toString("utf8"));
}

function sendJson(response, status, value) {
  response.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(JSON.stringify(value));
}

function listen(server, port) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
}

function close(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

async function waitForUrl(url) {
  for (let attempt = 0; attempt < 80; ++attempt) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // The local server can refuse connections while it starts.
    }
    await delay(100);
  }
  throw new Error(`The localhost server did not start: ${url}`);
}

async function createBrowserPage() {
  const endpoint = `http://127.0.0.1:${browserPort}/json/new?about%3Ablank`;
  for (let attempt = 0; attempt < 100; ++attempt) {
    try {
      const response = await fetch(endpoint, { method: "PUT" });
      if (response.ok) return response.json();
    } catch {
      // The browser debugging endpoint can refuse connections while it starts.
    }
    await delay(100);
  }
  throw new Error("The local browser did not start.");
}

async function connectCdp(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let id = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id === undefined) return;
    const waiter = pending.get(message.id);
    if (waiter === undefined) return;
    pending.delete(message.id);
    if (message.error === undefined) waiter.resolve(message.result);
    else waiter.reject(new Error(message.error.message));
  });
  return {
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const messageId = ++id;
        pending.set(messageId, { resolve, reject });
        socket.send(JSON.stringify({ id: messageId, method, params }));
      });
    },
    close() {
      socket.close();
    },
  };
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
  });
  if (result.exceptionDetails !== undefined)
    throw new Error("The browser expression failed.");
  return result.result.value;
}

async function waitForExpression(cdp, expression) {
  for (let attempt = 0; attempt < 100; ++attempt) {
    if ((await evaluate(cdp, expression)) === true) return;
    await delay(100);
  }
  throw new Error("The browser proof did not reach its expected state.");
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
