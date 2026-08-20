/// <reference types="node" />

import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/App.js";
import type {
  CreatedEmbedSession,
  HostApi,
  HostContext,
} from "../src/hostApi.js";
import {
  EMBED_PROTOCOL,
  EMBED_VERSION,
  HostProtocolController,
  type HostView,
} from "../src/hostProtocol.js";

const hostOrigin = "http://127.0.0.1:5176";
const routerOrigin = "http://127.0.0.1:5175";
const serviceId = "service-example";
const sessionId = "session-example";
const bootstrapToken = "b".repeat(43);

function context(change: Partial<HostContext> = {}): HostContext {
  return {
    revision: "revision-one",
    service_id: serviceId,
    host_user_subject: "example-user-a",
    workspace_id: "workspace-example-a",
    permissions: ["configuration.read", "accounting.read"],
    membership: true,
    ...change,
  };
}

function created(
  change: Partial<CreatedEmbedSession> = {},
): CreatedEmbedSession {
  return {
    session_id: sessionId,
    bootstrap_token: bootstrapToken,
    frame_url: `${routerOrigin}/service-administration?session_id=${sessionId}&host_origin=${encodeURIComponent(hostOrigin)}`,
    expires_at: "2026-08-20T12:05:00Z",
    message_version: "1",
    ...change,
  };
}

function envelope(
  type: string,
  payload: Record<string, unknown>,
  messageId = "message-one",
  change: Record<string, unknown> = {},
) {
  return {
    protocol: EMBED_PROTOCOL,
    version: EMBED_VERSION,
    session_id: sessionId,
    message_id: messageId,
    type,
    payload,
    ...change,
  };
}

function fixture(session: CreatedEmbedSession = created()) {
  const views: HostView[] = [];
  const messages: { readonly message: unknown; readonly origin: string }[] = [];
  const frameWindow = {
    postMessage: vi.fn((message: unknown, origin: string) => {
      messages.push({ message, origin });
    }),
  };
  const createSession = vi.fn<HostApi["createSession"]>(() =>
    Promise.resolve({ ...session }),
  );
  const revokeSession = vi.fn(() => Promise.resolve());
  const api: HostApi = {
    context: vi.fn(() => Promise.resolve(context())),
    changeContext: vi.fn(() => Promise.resolve(context())),
    createSession,
    revokeSession,
  };
  let id = 0;
  const scheduled: (() => void)[] = [];
  const controller = new HostProtocolController({
    api,
    hostOrigin,
    routerOrigin,
    frameWindow: () => frameWindow,
    onView: (view) => views.push(view),
    randomId: () => `host-message-${String(++id).padStart(4, "0")}`,
    now: () => Date.parse("2026-08-20T12:00:00Z"),
    schedule: (callback) => {
      scheduled.push(callback);
      return scheduled.length;
    },
  });
  return {
    api,
    controller,
    createSession,
    frameWindow,
    messages,
    revokeSession,
    scheduled,
    views,
  };
}

async function start(value: ReturnType<typeof fixture>) {
  await value.controller.replaceContext(context());
  await value.controller.receive({
    origin: routerOrigin,
    source: value.frameWindow,
    data: envelope("frame.ready", { frame_nonce: "frame-nonce-00000001" }),
  });
}

describe("example host protocol", () => {
  it("uses exact origin, source, session, version, nonce, and one bootstrap", async () => {
    const value = fixture();
    await value.controller.replaceContext(context());
    await Promise.all(
      [
        {
          origin: "http://127.0.0.1:5999",
          source: value.frameWindow,
          data: envelope("frame.ready", {
            frame_nonce: "frame-nonce-00000001",
          }),
        },
        {
          origin: routerOrigin,
          source: {},
          data: envelope("frame.ready", {
            frame_nonce: "frame-nonce-00000001",
          }),
        },
        {
          origin: routerOrigin,
          source: value.frameWindow,
          data: envelope(
            "frame.ready",
            { frame_nonce: "frame-nonce-00000001" },
            "wrong-session",
            { session_id: "another-session" },
          ),
        },
        {
          origin: routerOrigin,
          source: value.frameWindow,
          data: envelope(
            "frame.ready",
            { frame_nonce: "frame-nonce-00000001" },
            "wrong-version",
            { version: "2" },
          ),
        },
        {
          origin: routerOrigin,
          source: value.frameWindow,
          data: envelope(
            "frame.ready",
            { frame_nonce: "short" },
            "short-nonce",
          ),
        },
      ].map((event) => value.controller.receive(event)),
    );
    expect(value.messages).toHaveLength(0);
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope("frame.ready", { frame_nonce: "frame-nonce-00000001" }),
    });
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope("frame.ready", { frame_nonce: "frame-nonce-00000001" }),
    });
    expect(value.messages).toHaveLength(1);
    expect(value.messages[0]?.origin).toBe(routerOrigin);
    expect(value.messages[0]?.message).toMatchObject({
      type: "host.bootstrap",
      session_id: sessionId,
      payload: {
        frame_nonce: "frame-nonce-00000001",
        host_origin: hostOrigin,
      },
    });
  });

  it.each([
    ["changed user", { host_user_subject: "example-user-b" }],
    ["workspace switch", { workspace_id: "workspace-example-b" }],
    ["permission change", { permissions: ["health.read"] }],
  ])("disposes and revokes before renewal for %s", async (_name, change) => {
    const value = fixture();
    await start(value);
    value.messages.length = 0;
    await value.controller.replaceContext(
      context({ ...change, revision: `revision-${_name}` }),
    );
    expect(value.messages[0]?.message).toMatchObject({ type: "host.dispose" });
    expect(value.revokeSession).toHaveBeenCalledWith(sessionId);
    expect(value.createSession).toHaveBeenCalledTimes(2);
    expect(value.views.some((item) => item.frame === null)).toBe(true);
  });

  it("clears old scope on membership loss and starts only after restoration", async () => {
    const value = fixture();
    await start(value);
    await value.controller.replaceContext(
      context({ revision: "lost", membership: false }),
    );
    expect(value.views.at(-1)).toMatchObject({
      phase: "empty",
      frame: null,
    });
    expect(value.createSession).toHaveBeenCalledTimes(1);
    await value.controller.renew();
    expect(value.createSession).toHaveBeenCalledTimes(1);
    await value.controller.replaceContext(context({ revision: "restored" }));
    expect(value.createSession).toHaveBeenCalledTimes(2);
  });

  it("fails closed when stale-session cleanup is uncertain", async () => {
    let resolveFirst!: (value: CreatedEmbedSession) => void;
    const first = new Promise<CreatedEmbedSession>((resolve) => {
      resolveFirst = resolve;
    });
    const value = fixture();
    value.createSession.mockReturnValueOnce(first).mockResolvedValueOnce(
      created({
        session_id: "session-new",
        frame_url: `${routerOrigin}/service-administration?session_id=session-new&host_origin=${encodeURIComponent(hostOrigin)}`,
      }),
    );
    value.revokeSession.mockRejectedValueOnce(new Error("uncertain revoke"));
    const oldRequest = value.controller.replaceContext(context());
    await vi.waitFor(() => {
      expect(value.createSession).toHaveBeenCalledOnce();
    });
    const newRequest = value.controller.replaceContext(
      context({ revision: "new", workspace_id: "workspace-example-b" }),
    );
    resolveFirst(created());
    await Promise.all([oldRequest, newRequest]);
    await value.controller.renew();
    expect(value.views.at(-1)).toMatchObject({ phase: "error", frame: null });
    expect(value.createSession).toHaveBeenCalledTimes(2);
  });

  it("fails closed and does not renew when old-session revocation fails", async () => {
    const value = fixture();
    await start(value);
    value.revokeSession.mockRejectedValueOnce(new Error("uncertain revoke"));
    await value.controller.replaceContext(
      context({ revision: "new-user", host_user_subject: "example-user-b" }),
    );
    expect(value.views.at(-1)).toMatchObject({ phase: "error", frame: null });
    expect(value.createSession).toHaveBeenCalledTimes(1);
  });

  it("rejects a frame URL that contains bootstrap authority", async () => {
    const value = fixture(
      created({
        frame_url: `${routerOrigin}/service-administration?session_id=${sessionId}&host_origin=${encodeURIComponent(hostOrigin)}&bootstrap_token=${bootstrapToken}`,
      }),
    );
    await value.controller.replaceContext(context());
    expect(value.views.at(-1)).toMatchObject({ phase: "error", frame: null });
    expect(value.revokeSession).toHaveBeenCalledWith(sessionId);
  });

  it("fails closed for a malformed create response with no cleanup identity", async () => {
    const value = fixture();
    value.createSession.mockResolvedValueOnce(null as never);
    await value.controller.replaceContext(context());
    await value.controller.renew();
    expect(value.views.at(-1)).toMatchObject({ phase: "error", frame: null });
    expect(value.createSession).toHaveBeenCalledOnce();
    expect(value.revokeSession).not.toHaveBeenCalled();
  });

  it("rejects an expired or overlong session before it loads a frame", async () => {
    await Promise.all(
      ["2026-08-20T11:59:59Z", "2026-08-20T12:05:01Z"].map(
        async (expires_at) => {
          const value = fixture(created({ expires_at }));
          await value.controller.replaceContext(context());
          expect(value.views.at(-1)).toMatchObject({
            phase: "error",
            frame: null,
          });
          expect(value.revokeSession).toHaveBeenCalledWith(sessionId);
        },
      ),
    );
  });

  it("rejects unknown or duplicate permissions from the host context", async () => {
    await Promise.all(
      [
        ["configuration.read", "configuration.read"],
        ["configuration.read", "configuration.write"],
      ].map(async (permissions) => {
        const value = fixture();
        await value.controller.replaceContext(context({ permissions }));
        expect(value.views.at(-1)).toMatchObject({
          phase: "error",
          frame: null,
        });
        expect(value.createSession).not.toHaveBeenCalled();
      }),
    );
  });

  it("renews before expiry and after a frame expiry message", async () => {
    const value = fixture();
    await start(value);
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope(
        "frame.bootstrapped",
        {
          expires_at: "2026-08-20T12:05:00Z",
          service_id: serviceId,
          workspace_id: "workspace-example-a",
        },
        "bootstrapped",
      ),
    });
    expect(value.scheduled).toHaveLength(1);
    value.scheduled[0]?.();
    await vi.waitFor(() => {
      expect(value.createSession).toHaveBeenCalledTimes(2);
    });
    expect(value.revokeSession).toHaveBeenCalled();

    const signalled = fixture();
    await start(signalled);
    await signalled.controller.receive({
      origin: routerOrigin,
      source: signalled.frameWindow,
      data: envelope(
        "frame.bootstrapped",
        {
          expires_at: "2026-08-20T12:05:00Z",
          service_id: serviceId,
          workspace_id: "workspace-example-a",
        },
        "signalled-active",
      ),
    });
    await signalled.controller.receive({
      origin: routerOrigin,
      source: signalled.frameWindow,
      data: envelope(
        "frame.session_expired",
        { expired_at: "2026-08-20T12:05:00Z" },
        "expiry-signal",
      ),
    });
    expect(signalled.createSession).toHaveBeenCalledTimes(2);
  });

  it("ignores lifecycle messages before confirmation and malformed expiry", async () => {
    const value = fixture();
    await start(value);
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope(
        "frame.height_changed",
        { height_px: 999 },
        "height-before-confirmation",
      ),
    });
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope("frame.session_expired", {}, "malformed-expiry"),
    });
    expect(value.views.at(-1)?.height).toBe(420);
    expect(value.createSession).toHaveBeenCalledOnce();
  });

  it("applies bounded frame height and safe navigation after bootstrap", async () => {
    const value = fixture();
    await start(value);
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope(
        "frame.bootstrapped",
        {
          expires_at: "2026-08-20T12:05:00Z",
          service_id: serviceId,
          workspace_id: "workspace-example-a",
        },
        "active",
      ),
    });
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope("frame.height_changed", { height_px: 9_999 }, "height"),
    });
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope(
        "frame.navigation_changed",
        { section: "requests" },
        "navigation",
      ),
    });
    expect(value.views.at(-1)).toMatchObject({
      height: 4_096,
      section: "requests",
    });
  });

  it("revokes a stale create result and never restores its old frame", async () => {
    let resolveFirst!: (value: CreatedEmbedSession) => void;
    const first = new Promise<CreatedEmbedSession>((resolve) => {
      resolveFirst = resolve;
    });
    const value = fixture();
    value.createSession.mockReturnValueOnce(first).mockResolvedValueOnce(
      created({
        session_id: "session-new",
        frame_url: `${routerOrigin}/service-administration?session_id=session-new&host_origin=${encodeURIComponent(hostOrigin)}`,
      }),
    );
    const oldRequest = value.controller.replaceContext(context());
    await vi.waitFor(() => {
      expect(value.createSession).toHaveBeenCalledOnce();
    });
    const newRequest = value.controller.replaceContext(
      context({ revision: "new", workspace_id: "workspace-example-b" }),
    );
    resolveFirst(created());
    await Promise.all([oldRequest, newRequest]);
    expect(value.revokeSession).toHaveBeenCalledWith(sessionId);
    expect(value.views.at(-1)?.frame?.sessionId).toBe("session-new");
    expect(value.views.at(-1)?.frame?.frameUrl).not.toContain("bootstrap");
  });

  it("rejects a bootstrapped scope change and clears the frame", async () => {
    const value = fixture();
    await start(value);
    await value.controller.receive({
      origin: routerOrigin,
      source: value.frameWindow,
      data: envelope(
        "frame.bootstrapped",
        {
          expires_at: "2026-08-20T12:05:00Z",
          service_id: serviceId,
          workspace_id: "workspace-example-b",
        },
        "wrong-scope",
      ),
    });
    await vi.waitFor(() => {
      expect(value.views.at(-1)?.frame).toBeNull();
    });
    expect(value.revokeSession).toHaveBeenCalledWith(sessionId);
  });

  it("keeps secrets out of view state and browser source", async () => {
    const value = fixture();
    await start(value);
    expect(JSON.stringify(value.views)).not.toContain(bootstrapToken);
    const sources = ["App.tsx", "hostApi.ts", "main.tsx", "styles.css"]
      .map((file) =>
        readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"),
      )
      .join("\n");
    expect(sources).not.toContain("LLMROUTER_EXAMPLE_HOST_TOKEN");
    expect(sources).not.toContain("Authorization");
    expect(sources).not.toContain("provider credential");
  });

  it("uses semantic controls, a named frame, keyboard focus, and a phone layout", () => {
    const markup = renderToStaticMarkup(createElement(App));
    const styles = readFileSync(
      new URL("../src/styles.css", import.meta.url),
      "utf8",
    );
    expect(markup).toContain("<main");
    expect(markup).toContain("<button");
    expect(markup).toContain('aria-label="Host context changes"');
    expect(markup).toContain('id="embed-heading"');
    expect(styles).toContain(":focus-visible");
    expect(styles).toContain("@media (max-width: 650px)");
    expect(styles).toContain("min-width: 300px");
    expect(styles).toContain("min-height: 44px");
  });
});
