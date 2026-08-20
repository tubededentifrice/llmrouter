/// <reference types="node" />

import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { InvalidEmbedFrame } from "../src/EmbedFrame.js";
import { EmbedSnapshotLoader } from "../src/embedSnapshotLoader.js";
import { framePolicyForUrl } from "../vite.config.js";
import {
  EMBED_PROTOCOL,
  EMBED_VERSION,
  FrameProtocolController,
  embedFrameParameters,
  type FrameEnvelope,
} from "../src/embedProtocol.js";

const sessionId = "session-one";
const hostOrigin = "https://host.example";
const bootstrapToken = "b".repeat(43);
const theme = {
  mode: "dark" as const,
  density: "compact" as const,
  corner_style: "rounded" as const,
};

function envelope(
  type: string,
  payload: Record<string, unknown>,
  messageId = "message-one",
): FrameEnvelope {
  return {
    protocol: EMBED_PROTOCOL,
    version: EMBED_VERSION,
    session_id: sessionId,
    message_id: messageId,
    type,
    payload,
  };
}

function fixture() {
  const messages: { message: FrameEnvelope; origin: string }[] = [];
  const parentWindow = {
    postMessage: vi.fn((message: FrameEnvelope, origin: string) => {
      messages.push({ message, origin });
    }),
  };
  const fetchBootstrap = vi.fn(() =>
    Promise.resolve({
      expires_at: "2026-08-20T12:05:00Z",
      service_id: "service-one",
      workspace_id: "workspace-one",
      permissions: ["configuration.read"],
      theme,
    }),
  );
  let random = 0;
  const controller = new FrameProtocolController({
    sessionId,
    hostOrigin,
    parentWindow,
    fetchBootstrap,
    randomId: () => `nonce-message-${String(++random).padStart(4, "0")}`,
    now: () => Date.parse("2026-08-20T12:00:00Z"),
    schedule: vi.fn(() => 1),
  });
  return { controller, fetchBootstrap, messages, parentWindow };
}

describe("administration frame protocol", () => {
  it("uses the exact target origin and accepts one exact bootstrap", async () => {
    const value = fixture();
    value.controller.start();
    expect(value.messages[0]?.origin).toBe(hostOrigin);
    expect(value.messages[0]?.message.type).toBe("frame.ready");
    const nonce = value.controller.frameNonce;
    await value.controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope("host.bootstrap", {
        bootstrap_token: bootstrapToken,
        frame_nonce: nonce,
        host_origin: hostOrigin,
      }),
    });
    expect(value.fetchBootstrap).toHaveBeenCalledTimes(1);
    expect(value.messages.at(-1)?.message.type).toBe("frame.bootstrapped");
    expect(JSON.stringify(value.messages)).not.toContain(bootstrapToken);
  });

  it("rejects wrong origin, source, session, version, nonce, and duplicate IDs", async () => {
    const value = fixture();
    const payload = {
      bootstrap_token: bootstrapToken,
      frame_nonce: value.controller.frameNonce,
      host_origin: hostOrigin,
    };
    await value.controller.receive({
      origin: "https://wrong.example",
      source: value.parentWindow,
      data: envelope("host.bootstrap", payload),
    });
    await value.controller.receive({
      origin: hostOrigin,
      source: {},
      data: envelope("host.bootstrap", payload),
    });
    await value.controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: { ...envelope("host.bootstrap", payload), session_id: "wrong" },
    });
    await value.controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: { ...envelope("host.bootstrap", payload), version: "2" },
    });
    await value.controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope("host.bootstrap", {
        ...payload,
        frame_nonce: "wrong-nonce-value",
      }),
    });
    expect(value.fetchBootstrap).not.toHaveBeenCalled();
    await value.controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope("host.bootstrap", payload, "accepted"),
    });
    await value.controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope("host.bootstrap", payload, "accepted"),
    });
    expect(value.fetchBootstrap).toHaveBeenCalledTimes(1);
  });

  it("applies bounded navigation and theme, then clears on dispose", async () => {
    const navigated = vi.fn();
    const themed = vi.fn();
    const disposed = vi.fn();
    const value = fixture();
    const controller = new FrameProtocolController({
      sessionId,
      hostOrigin,
      parentWindow: value.parentWindow,
      fetchBootstrap: value.fetchBootstrap,
      randomId: () => "nonce-message-000001",
      now: () => Date.parse("2026-08-20T12:00:00Z"),
      schedule: vi.fn(() => 1),
      onNavigate: navigated,
      onTheme: themed,
      onDispose: disposed,
    });
    await controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope(
        "host.bootstrap",
        {
          bootstrap_token: bootstrapToken,
          frame_nonce: controller.frameNonce,
          host_origin: hostOrigin,
        },
        "bootstrap",
      ),
    });
    await controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope(
        "host.navigate",
        { section: "requests", record_id: "safe-record:1" },
        "navigate",
      ),
    });
    await controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope("host.theme_changed", { theme }, "theme"),
    });
    await controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope("host.navigate", { section: "global-secrets" }, "unsafe"),
    });
    await controller.receive({
      origin: hostOrigin,
      source: value.parentWindow,
      data: envelope("host.dispose", {}, "dispose"),
    });
    expect(navigated).toHaveBeenCalledWith("requests", "safe-record:1");
    expect(navigated).toHaveBeenCalledTimes(1);
    expect(themed).toHaveBeenLastCalledWith(theme);
    expect(disposed).toHaveBeenCalledOnce();
  });

  it("requires one non-secret frame context and strict frame policy", () => {
    expect(
      embedFrameParameters(
        `?session_id=${sessionId}&host_origin=${encodeURIComponent(hostOrigin)}`,
      ),
    ).toEqual({ sessionId, hostOrigin });
    expect(
      embedFrameParameters(
        `?session_id=${sessionId}&session_id=two&host_origin=${encodeURIComponent(hostOrigin)}`,
      ),
    ).toBeNull();
    const config = readFileSync(
      new URL("../vite.config.ts", import.meta.url),
      "utf8",
    );
    const styles = readFileSync(
      new URL("../src/embedStyles.css", import.meta.url),
      "utf8",
    );
    expect(config).toContain('"Cache-Control": "no-store"');
    expect(config).toContain("frame-ancestors");
    expect(config).toContain("object-src 'none'");
    expect(styles).toContain("@media (max-width: 600px)");
    expect(styles).toContain(":focus-visible");
    expect(renderToStaticMarkup(InvalidEmbedFrame())).not.toContain(
      "Global administrator",
    );
    expect(framePolicyForUrl("/admin")).toContain("frame-ancestors 'self'");
    expect(
      framePolicyForUrl(
        `/service-administration?host_origin=${encodeURIComponent(hostOrigin)}`,
      ),
    ).toContain(`frame-ancestors ${hostOrigin}`);
    expect(framePolicyForUrl("/service-administration")).toContain(
      "frame-ancestors 'none'",
    );
  });

  it("makes uncertain bootstrap terminal and ignores stale scope completion", async () => {
    const parentWindow = { postMessage: vi.fn() };
    const fetchBootstrap = vi.fn(() => Promise.reject(new Error("uncertain")));
    const controller = new FrameProtocolController({
      sessionId,
      hostOrigin,
      parentWindow,
      fetchBootstrap,
      randomId: () => "nonce-message-000001",
    });
    const first = envelope("host.bootstrap", {
      bootstrap_token: bootstrapToken,
      frame_nonce: controller.frameNonce,
      host_origin: hostOrigin,
    });
    await controller.receive({
      origin: hostOrigin,
      source: parentWindow,
      data: first,
    });
    await controller.receive({
      origin: hostOrigin,
      source: parentWindow,
      data: { ...first, message_id: "retry" },
    });
    expect(fetchBootstrap).toHaveBeenCalledTimes(1);

    let resolve: ((value: string) => void) | undefined;
    const loader = new EmbedSnapshotLoader();
    const accepted = vi.fn();
    loader.load(
      () =>
        new Promise<string>((done) => {
          resolve = done;
        }),
      accepted,
      vi.fn(),
    );
    loader.cancel();
    resolve?.("old-scope");
    await Promise.resolve();
    expect(accepted).not.toHaveBeenCalled();
  });

  it("does not report a stale bootstrap failure after host disposal", async () => {
    const parentWindow = { postMessage: vi.fn() };
    let rejectBootstrap: ((error: Error) => void) | undefined;
    const controller = new FrameProtocolController({
      sessionId,
      hostOrigin,
      parentWindow,
      fetchBootstrap: () =>
        new Promise((_resolve, reject) => {
          rejectBootstrap = reject;
        }),
      randomId: () => "nonce-message-000001",
    });
    const pending = controller.receive({
      origin: hostOrigin,
      source: parentWindow,
      data: envelope("host.bootstrap", {
        bootstrap_token: bootstrapToken,
        frame_nonce: controller.frameNonce,
        host_origin: hostOrigin,
      }),
    });
    await controller.receive({
      origin: hostOrigin,
      source: parentWindow,
      data: envelope("host.dispose", {}, "dispose"),
    });
    rejectBootstrap?.(new Error("stale failure"));
    await pending;
    expect(parentWindow.postMessage).not.toHaveBeenCalled();
  });
});
