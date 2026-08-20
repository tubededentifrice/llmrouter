import type { CreatedEmbedSession, HostApi, HostContext } from "./hostApi.js";

export const EMBED_PROTOCOL = "llmrouter-admin-embed" as const;
export const EMBED_VERSION = "1" as const;

interface Envelope {
  readonly protocol: typeof EMBED_PROTOCOL;
  readonly version: typeof EMBED_VERSION;
  readonly session_id: string;
  readonly message_id: string;
  readonly type: string;
  readonly payload: Record<string, unknown>;
}

interface FrameWindow {
  postMessage(message: Envelope, targetOrigin: string): void;
}

interface MessageEventLike {
  readonly origin: string;
  readonly source: unknown;
  readonly data: unknown;
}

export interface HostFrame {
  readonly sessionId: string;
  readonly frameUrl: string;
  readonly title: string;
}

export interface HostView {
  readonly phase: "empty" | "loading" | "ready" | "active" | "error";
  readonly message: string;
  readonly frame: HostFrame | null;
  readonly height: number;
  readonly section: string;
}

export interface HostProtocolOptions {
  readonly api: HostApi;
  readonly hostOrigin: string;
  readonly routerOrigin: string;
  readonly frameWindow: () => FrameWindow | null;
  readonly onView: (view: HostView) => void;
  readonly randomId?: () => string;
  readonly now?: () => number;
  readonly schedule?: (callback: () => void, delay: number) => number;
  readonly cancelSchedule?: (id: number) => void;
}

interface ActiveSession {
  readonly id: string;
  readonly frameUrl: string;
  readonly expiresAt: string;
  bootstrapToken: string;
  bootstrapSent: boolean;
  bootstrapConfirmed: boolean;
  readonly seen: Set<string>;
}

const maximumMessageIds = 256;
const allowedExamplePermissions = new Set([
  "health.read",
  "configuration.read",
  "request_status.read",
  "accounting.read",
]);

export class HostProtocolController {
  private context: HostContext | null = null;
  private session: ActiveSession | null = null;
  private generation = 0;
  private expiryTimer: number | null = null;
  private stopped = false;
  private revokeUncertain = false;
  private view: HostView = {
    phase: "empty",
    message: "The host context is not loaded.",
    frame: null,
    height: 420,
    section: "configuration",
  };

  constructor(private readonly options: HostProtocolOptions) {
    if (
      !isExactLoopbackOrHttpsOrigin(options.hostOrigin) ||
      !isExactLoopbackOrHttpsOrigin(options.routerOrigin) ||
      options.hostOrigin === options.routerOrigin
    ) {
      throw new Error("The example host origins are invalid.");
    }
  }

  async replaceContext(context: HostContext, force = false): Promise<void> {
    if (this.stopped || this.revokeUncertain) return;
    if (!validContext(context)) {
      await this.clear("The host context is invalid.");
      return;
    }
    if (!force && sameContext(this.context, context) && this.session !== null)
      return;
    this.context = context;
    const generation = ++this.generation;
    const revoked = await this.disposeSession();
    if (!revoked) return;
    if (!this.isCurrent(generation)) return;
    if (!context.membership) {
      this.publish({
        phase: "empty",
        message: "Router administration membership is not active.",
        frame: null,
      });
      return;
    }
    this.publish({
      phase: "loading",
      message: "The host is creating a bounded Router session.",
      frame: null,
    });
    let created: CreatedEmbedSession;
    try {
      created = await this.options.api.createSession(context.revision);
    } catch {
      if (generation === this.generation)
        this.publish({
          phase: "error",
          message: "The host could not create the Router session.",
          frame: null,
        });
      return;
    }
    const createdId = createdSessionId(created);
    if (!this.isCurrent(generation)) {
      eraseCreatedSession(created);
      if (createdId === null) {
        this.failClosedAfterUncertainRevoke();
        return;
      }
      try {
        await this.options.api.revokeSession(createdId);
      } catch {
        this.failClosedAfterUncertainRevoke();
      }
      return;
    }
    const session = parseCreatedSession(created, this.options);
    eraseCreatedSession(created);
    if (session === null) {
      if (createdId === null) {
        this.failClosedAfterUncertainRevoke();
        return;
      }
      try {
        await this.options.api.revokeSession(createdId);
      } catch {
        this.failClosedAfterUncertainRevoke();
        return;
      }
      this.publish({
        phase: "error",
        message: "The Router returned an invalid embed session.",
        frame: null,
      });
      return;
    }
    this.session = session;
    this.publish({
      phase: "ready",
      message: "The isolated Router frame is ready to start.",
      frame: {
        sessionId: session.id,
        frameUrl: session.frameUrl,
        title: "LLM Router administration",
      },
    });
  }

  async receive(event: MessageEventLike): Promise<void> {
    const session = this.session;
    const frameWindow = this.options.frameWindow();
    if (
      this.stopped ||
      session === null ||
      frameWindow === null ||
      event.origin !== this.options.routerOrigin ||
      event.source !== frameWindow
    ) {
      return;
    }
    const envelope = parseEnvelope(event.data, session.id);
    if (
      envelope === null ||
      session.seen.has(envelope.message_id) ||
      !knownFrameType(envelope.type)
    ) {
      return;
    }
    remember(session.seen, envelope.message_id);
    if (envelope.type === "frame.ready") {
      this.bootstrap(session, envelope.payload, frameWindow);
      return;
    }
    if (envelope.type === "frame.bootstrapped") {
      if (!session.bootstrapSent || session.bootstrapConfirmed) return;
      this.bootstrapped(session, envelope.payload);
      return;
    }
    if (!session.bootstrapConfirmed) return;
    if (envelope.type === "frame.height_changed") {
      const height = envelope.payload.height_px;
      if (
        Object.keys(envelope.payload).length === 1 &&
        Number.isInteger(height)
      ) {
        this.publish({ height: Math.max(240, Math.min(4096, Number(height))) });
      }
    } else if (envelope.type === "frame.navigation_changed") {
      const section = envelope.payload.section;
      if (
        (Object.keys(envelope.payload).length === 1 ||
          Object.keys(envelope.payload).length === 2) &&
        typeof section === "string" &&
        ["configuration", "assignments", "requests", "accounting"].includes(
          section,
        )
      ) {
        this.publish({ section });
      }
    } else if (envelope.type === "frame.session_expired") {
      if (
        hasExactKeys(envelope.payload, ["expired_at"]) &&
        envelope.payload.expired_at === session.expiresAt
      )
        await this.renew(
          "The Router session expired. The host is renewing it.",
        );
    } else if (envelope.type === "frame.error") {
      if (validFrameError(envelope.payload))
        this.publish({
          phase: "error",
          message: "The embedded Router view reported a safe error.",
        });
    }
  }

  navigate(
    section: "configuration" | "assignments" | "requests" | "accounting",
  ): void {
    this.send("host.navigate", { section });
  }

  themeChanged(mode: "light" | "dark" | "system"): void {
    this.send("host.theme_changed", {
      theme: { mode, density: "comfortable", corner_style: "rounded" },
    });
  }

  async renew(
    message = "The host is renewing the Router session.",
  ): Promise<void> {
    const context = this.context;
    if (context === null || this.stopped || this.revokeUncertain) return;
    this.publish({ phase: "loading", message, frame: null });
    await this.replaceContext(context, true);
  }

  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    ++this.generation;
    await this.disposeSession();
    this.context = null;
  }

  private bootstrap(
    session: ActiveSession,
    payload: Record<string, unknown>,
    frameWindow: FrameWindow,
  ): void {
    if (
      session.bootstrapToken === "" ||
      !hasExactKeys(payload, ["frame_nonce"]) ||
      typeof payload.frame_nonce !== "string" ||
      payload.frame_nonce.length < 16 ||
      payload.frame_nonce.length > 200
    ) {
      return;
    }
    const token = session.bootstrapToken;
    session.bootstrapToken = "";
    session.bootstrapSent = true;
    frameWindow.postMessage(
      this.envelope("host.bootstrap", session.id, {
        bootstrap_token: token,
        frame_nonce: payload.frame_nonce,
        host_origin: this.options.hostOrigin,
      }),
      this.options.routerOrigin,
    );
  }

  private bootstrapped(
    session: ActiveSession,
    payload: Record<string, unknown>,
  ): void {
    const context = this.context;
    if (
      context === null ||
      !hasOnlyKeys(
        payload,
        ["expires_at", "service_id", "workspace_id"],
        ["expires_at", "service_id"],
      ) ||
      payload.service_id !== context.service_id ||
      (payload.workspace_id ?? "") !== context.workspace_id ||
      typeof payload.expires_at !== "string" ||
      payload.expires_at !== session.expiresAt
    ) {
      void this.clear("The Router frame returned a different scope.");
      return;
    }
    const delay =
      Date.parse(session.expiresAt) - (this.options.now ?? Date.now)();
    if (!Number.isFinite(delay) || delay <= 0) {
      void this.renew();
      return;
    }
    if (this.expiryTimer !== null)
      (this.options.cancelSchedule ?? clearTimeout)(this.expiryTimer);
    this.expiryTimer = (this.options.schedule ?? window.setTimeout)(
      () => void this.renew(),
      Math.max(0, delay - 1_000),
    );
    session.bootstrapConfirmed = true;
    this.publish({
      phase: "active",
      message: "The host authorized this exact Router scope.",
    });
  }

  private send(type: string, payload: Record<string, unknown>): void {
    const session = this.session;
    const frameWindow = this.options.frameWindow();
    if (session === null || !session.bootstrapConfirmed || frameWindow === null)
      return;
    frameWindow.postMessage(
      this.envelope(type, session.id, payload),
      this.options.routerOrigin,
    );
  }

  private envelope(
    type: string,
    sessionId: string,
    payload: Record<string, unknown>,
  ): Envelope {
    return {
      protocol: EMBED_PROTOCOL,
      version: EMBED_VERSION,
      session_id: sessionId,
      message_id: (this.options.randomId ?? (() => crypto.randomUUID()))(),
      type,
      payload,
    };
  }

  private async clear(message: string): Promise<void> {
    ++this.generation;
    await this.disposeSession();
    this.publish({ phase: "error", message, frame: null });
  }

  private async disposeSession(): Promise<boolean> {
    if (this.expiryTimer !== null)
      (this.options.cancelSchedule ?? clearTimeout)(this.expiryTimer);
    this.expiryTimer = null;
    const session = this.session;
    this.session = null;
    if (session === null) return true;
    const frameWindow = this.options.frameWindow();
    if (frameWindow !== null && session.bootstrapSent) {
      frameWindow.postMessage(
        this.envelope("host.dispose", session.id, {}),
        this.options.routerOrigin,
      );
    }
    session.bootstrapToken = "";
    session.seen.clear();
    this.publish({ frame: null });
    try {
      await this.options.api.revokeSession(session.id);
      return true;
    } catch {
      this.failClosedAfterUncertainRevoke();
      return false;
    }
  }

  private failClosedAfterUncertainRevoke(): void {
    this.revokeUncertain = true;
    ++this.generation;
    this.publish({
      phase: "error",
      message:
        "The old Router session state is uncertain. Reload the host before a new session starts.",
      frame: null,
    });
  }

  private publish(change: Partial<HostView>): void {
    this.view = { ...this.view, ...change };
    this.options.onView(this.view);
  }

  private isCurrent(generation: number): boolean {
    return generation === this.generation && !this.stopped;
  }
}

function parseCreatedSession(
  value: CreatedEmbedSession,
  options: Pick<HostProtocolOptions, "hostOrigin" | "now" | "routerOrigin">,
): ActiveSession | null {
  try {
    const messageVersion: unknown = value.message_version;
    const url = new URL(value.frame_url);
    const sessionValues = url.searchParams.getAll("session_id");
    const hostValues = url.searchParams.getAll("host_origin");
    const expiresAt = Date.parse(value.expires_at);
    const now = (options.now ?? Date.now)();
    if (
      messageVersion !== EMBED_VERSION ||
      value.session_id === "" ||
      value.session_id.length > 200 ||
      typeof value.bootstrap_token !== "string" ||
      value.bootstrap_token.length < 43 ||
      value.bootstrap_token.length > 512 ||
      url.origin !== options.routerOrigin ||
      url.pathname !== "/service-administration" ||
      url.username !== "" ||
      url.password !== "" ||
      url.hash !== "" ||
      url.searchParams.has("bootstrap_token") ||
      sessionValues.length !== 1 ||
      sessionValues[0] !== value.session_id ||
      hostValues.length !== 1 ||
      hostValues[0] !== options.hostOrigin ||
      [...url.searchParams.keys()].some(
        (key) => key !== "session_id" && key !== "host_origin",
      ) ||
      !Number.isFinite(expiresAt) ||
      expiresAt <= now ||
      expiresAt > now + 300_000
    ) {
      return null;
    }
    return {
      id: value.session_id,
      frameUrl: url.href,
      expiresAt: value.expires_at,
      bootstrapToken: value.bootstrap_token,
      bootstrapSent: false,
      bootstrapConfirmed: false,
      seen: new Set(),
    };
  } catch {
    return null;
  }
}

function createdSessionId(value: unknown): string | null {
  if (
    !isRecord(value) ||
    typeof value.session_id !== "string" ||
    value.session_id === "" ||
    value.session_id.length > 200
  )
    return null;
  return value.session_id;
}

function eraseCreatedSession(value: CreatedEmbedSession): void {
  try {
    (value as { bootstrap_token: string }).bootstrap_token = "";
  } catch {
    // A frozen response object cannot be erased. The controller keeps no reference to it.
  }
}

function validContext(value: unknown): value is HostContext {
  return (
    isRecord(value) &&
    validBoundedIdentity(value.revision) &&
    validBoundedIdentity(value.service_id) &&
    validBoundedIdentity(value.host_user_subject) &&
    validBoundedIdentity(value.workspace_id) &&
    typeof value.membership === "boolean" &&
    Array.isArray(value.permissions) &&
    value.permissions.length > 0 &&
    value.permissions.length <= allowedExamplePermissions.size &&
    new Set(value.permissions).size === value.permissions.length &&
    value.permissions.every(
      (item) => typeof item === "string" && allowedExamplePermissions.has(item),
    )
  );
}

function validBoundedIdentity(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 200;
}

function sameContext(left: HostContext | null, right: HostContext): boolean {
  return (
    left?.revision === right.revision &&
    left.service_id === right.service_id &&
    left.host_user_subject === right.host_user_subject &&
    left.workspace_id === right.workspace_id &&
    left.membership === right.membership &&
    left.permissions.join("\u0000") === right.permissions.join("\u0000")
  );
}

function parseEnvelope(value: unknown, sessionId: string): Envelope | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "protocol",
      "version",
      "session_id",
      "message_id",
      "type",
      "payload",
    ]) ||
    value.protocol !== EMBED_PROTOCOL ||
    value.version !== EMBED_VERSION ||
    value.session_id !== sessionId ||
    typeof value.message_id !== "string" ||
    value.message_id === "" ||
    value.message_id.length > 200 ||
    typeof value.type !== "string" ||
    !isRecord(value.payload)
  ) {
    return null;
  }
  return value as unknown as Envelope;
}

function knownFrameType(value: string): boolean {
  return [
    "frame.ready",
    "frame.bootstrapped",
    "frame.height_changed",
    "frame.navigation_changed",
    "frame.configuration_changed",
    "frame.session_expired",
    "frame.error",
  ].includes(value);
}

function validFrameError(value: Record<string, unknown>): boolean {
  return (
    hasExactKeys(value, ["code", "message", "retryable"]) &&
    typeof value.code === "string" &&
    [
      "session_expired",
      "origin_mismatch",
      "unsupported_message_version",
      "permission_denied",
      "workspace_unavailable",
      "revision_conflict",
      "temporarily_unavailable",
      "internal_error",
    ].includes(value.code) &&
    typeof value.message === "string" &&
    value.message.length > 0 &&
    value.message.length <= 500 &&
    typeof value.retryable === "boolean"
  );
}

function remember(values: Set<string>, value: string): void {
  if (values.size >= maximumMessageIds) {
    const oldest = values.values().next().value;
    if (oldest !== undefined) values.delete(oldest);
  }
  values.add(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  return (
    Object.keys(value).length === keys.length &&
    keys.every((key) => key in value)
  );
}

function hasOnlyKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
  required: readonly string[],
): boolean {
  return (
    Object.keys(value).every((key) => allowed.includes(key)) &&
    required.every((key) => key in value)
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isExactLoopbackOrHttpsOrigin(value: string): boolean {
  try {
    const url = new URL(value);
    const loopback = ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
    return (
      url.origin === value &&
      (url.protocol === "https:" || (url.protocol === "http:" && loopback))
    );
  } catch {
    return false;
  }
}
