export const EMBED_PROTOCOL = "llmrouter-admin-embed" as const;
export const EMBED_VERSION = "1" as const;

export type EmbedSection =
  "configuration" | "assignments" | "requests" | "accounting";

export interface EmbedTheme {
  readonly mode: "light" | "dark" | "system";
  readonly density: "comfortable" | "compact";
  readonly corner_style: "square" | "rounded";
}

export interface BootstrapResult {
  readonly expires_at: string;
  readonly service_id: string;
  readonly workspace_id?: string | null;
  readonly permissions: readonly string[];
  readonly theme: EmbedTheme;
}

export interface FrameEnvelope {
  readonly protocol: typeof EMBED_PROTOCOL;
  readonly version: typeof EMBED_VERSION;
  readonly session_id: string;
  readonly message_id: string;
  readonly type: string;
  readonly payload: Record<string, unknown>;
}

interface MessageEventLike {
  readonly origin: string;
  readonly source: unknown;
  readonly data: unknown;
}

interface ParentWindowLike {
  postMessage(message: FrameEnvelope, targetOrigin: string): void;
}

export interface FrameProtocolOptions {
  readonly sessionId: string;
  readonly hostOrigin: string;
  readonly parentWindow: ParentWindowLike;
  readonly fetchBootstrap: (
    sessionId: string,
    input: {
      readonly bootstrap_token: string;
      readonly frame_nonce: string;
      readonly host_origin: string;
    },
  ) => Promise<BootstrapResult>;
  readonly randomId?: () => string;
  readonly now?: () => number;
  readonly schedule?: (callback: () => void, delay: number) => number;
  readonly cancelSchedule?: (id: number) => void;
  readonly onBootstrapped?: (result: BootstrapResult) => void;
  readonly onNavigate?: (
    section: EmbedSection,
    recordId: string | null,
  ) => void;
  readonly onTheme?: (theme: EmbedTheme) => void;
  readonly onDispose?: () => void;
  readonly onExpired?: () => void;
  readonly onError?: (message: string) => void;
}

const sections = new Set<EmbedSection>([
  "configuration",
  "assignments",
  "requests",
  "accounting",
]);
const maximumMessageIds = 256;

export class FrameProtocolController {
  readonly frameNonce: string;
  private readonly seen = new Set<string>();
  private expiryTimer: number | null = null;
  private state: "ready" | "bootstrapping" | "active" | "disposed" = "ready";

  constructor(private readonly options: FrameProtocolOptions) {
    this.frameNonce = (options.randomId ?? (() => crypto.randomUUID()))();
    if (
      this.frameNonce.length < 16 ||
      options.sessionId === "" ||
      !isExactOrigin(options.hostOrigin)
    ) {
      throw new Error("The frame integration values are invalid.");
    }
  }

  start(): void {
    this.send("frame.ready", { frame_nonce: this.frameNonce });
  }

  async receive(event: MessageEventLike): Promise<void> {
    if (
      this.state === "disposed" ||
      event.origin !== this.options.hostOrigin ||
      event.source !== this.options.parentWindow
    ) {
      return;
    }
    const envelope = parseEnvelope(event.data, this.options.sessionId);
    if (envelope === null || this.seen.has(envelope.message_id)) return;
    if (!knownHostType(envelope.type)) return;
    this.remember(envelope.message_id);
    if (envelope.type === "host.dispose") {
      if (Object.keys(envelope.payload).length !== 0) return;
      this.dispose();
      return;
    }
    if (envelope.type === "host.bootstrap") {
      await this.bootstrap(envelope.payload);
      return;
    }
    if (this.state !== "active") return;
    if (envelope.type === "host.navigate") {
      const navigation = parseNavigation(envelope.payload);
      if (navigation !== null)
        this.options.onNavigate?.(navigation.section, navigation.recordId);
      return;
    }
    const theme = parseTheme(envelope.payload);
    if (theme !== null) this.options.onTheme?.(theme);
  }

  heightChanged(height: number): void {
    if (this.state !== "active" || !Number.isInteger(height)) return;
    this.send("frame.height_changed", {
      height_px: Math.max(240, Math.min(4096, height)),
    });
  }

  navigationChanged(
    section: EmbedSection,
    recordId: string | null = null,
  ): void {
    if (this.state !== "active" || !sections.has(section)) return;
    const payload: Record<string, unknown> = { section };
    if (recordId !== null && safeRecordId(recordId))
      payload.record_id = recordId;
    this.send("frame.navigation_changed", payload);
  }

  dispose(): void {
    if (this.state === "disposed") return;
    this.state = "disposed";
    if (this.expiryTimer !== null)
      (this.options.cancelSchedule ?? clearTimeout)(this.expiryTimer);
    this.expiryTimer = null;
    this.seen.clear();
    this.options.onDispose?.();
  }

  private async bootstrap(payload: Record<string, unknown>): Promise<void> {
    if (this.state !== "ready") return;
    if (
      !hasExactKeys(payload, ["bootstrap_token", "frame_nonce", "host_origin"])
    )
      return;
    if (
      typeof payload.bootstrap_token !== "string" ||
      payload.bootstrap_token.length < 43 ||
      payload.frame_nonce !== this.frameNonce ||
      payload.host_origin !== this.options.hostOrigin
    ) {
      return;
    }
    this.state = "bootstrapping";
    let bootstrapToken = payload.bootstrap_token;
    try {
      const result = await this.options.fetchBootstrap(this.options.sessionId, {
        bootstrap_token: bootstrapToken,
        frame_nonce: this.frameNonce,
        host_origin: this.options.hostOrigin,
      });
      this.completeBootstrap(result);
    } catch {
      this.send("frame.error", {
        code: "temporarily_unavailable",
        message: "The embedded administration session did not start.",
        retryable: false,
      });
      this.state = "disposed";
      this.seen.clear();
      this.options.onError?.(
        "The embedded administration session did not start. Ask the host service for a new session.",
      );
    } finally {
      bootstrapToken = "";
    }
  }

  private completeBootstrap(result: BootstrapResult): void {
    if (this.isDisposed()) return;
    this.state = "active";
    this.options.onBootstrapped?.(result);
    this.options.onTheme?.(result.theme);
    this.send("frame.bootstrapped", {
      expires_at: result.expires_at,
      service_id: result.service_id,
      ...(result.workspace_id == null
        ? {}
        : { workspace_id: result.workspace_id }),
    });
    const delay = Math.max(
      0,
      Date.parse(result.expires_at) - (this.options.now ?? Date.now)(),
    );
    this.expiryTimer = (this.options.schedule ?? window.setTimeout)(() => {
      if (this.state !== "active") return;
      this.state = "disposed";
      this.options.onExpired?.();
      this.send("frame.session_expired", { expired_at: result.expires_at });
    }, delay);
  }

  private remember(messageId: string): void {
    if (this.seen.size >= maximumMessageIds) {
      const oldest = this.seen.values().next().value;
      if (oldest !== undefined) this.seen.delete(oldest);
    }
    this.seen.add(messageId);
  }

  private isDisposed(): boolean {
    return this.state === "disposed";
  }

  private send(type: string, payload: Record<string, unknown>): void {
    this.options.parentWindow.postMessage(
      {
        protocol: EMBED_PROTOCOL,
        version: EMBED_VERSION,
        session_id: this.options.sessionId,
        message_id: (this.options.randomId ?? (() => crypto.randomUUID()))(),
        type,
        payload,
      },
      this.options.hostOrigin,
    );
  }
}

function parseEnvelope(
  value: unknown,
  sessionId: string,
): FrameEnvelope | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "protocol",
      "version",
      "session_id",
      "message_id",
      "type",
      "payload",
    ])
  )
    return null;
  if (
    value.protocol !== EMBED_PROTOCOL ||
    value.version !== EMBED_VERSION ||
    value.session_id !== sessionId ||
    typeof value.message_id !== "string" ||
    value.message_id === "" ||
    value.message_id.length > 200 ||
    typeof value.type !== "string" ||
    !isRecord(value.payload)
  )
    return null;
  return value as unknown as FrameEnvelope;
}

function knownHostType(value: string): boolean {
  return (
    value === "host.bootstrap" ||
    value === "host.navigate" ||
    value === "host.theme_changed" ||
    value === "host.dispose"
  );
}

function parseNavigation(
  payload: Record<string, unknown>,
): { section: EmbedSection; recordId: string | null } | null {
  if (
    !hasOnlyKeys(payload, ["section", "record_id"]) ||
    typeof payload.section !== "string" ||
    !sections.has(payload.section as EmbedSection)
  )
    return null;
  const recordId = payload.record_id;
  if (
    recordId !== undefined &&
    (typeof recordId !== "string" || !safeRecordId(recordId))
  )
    return null;
  return {
    section: payload.section as EmbedSection,
    recordId: recordId ?? null,
  };
}

function parseTheme(payload: Record<string, unknown>): EmbedTheme | null {
  const value = isRecord(payload.theme) ? payload.theme : payload;
  if (!hasExactKeys(value, ["mode", "density", "corner_style"])) return null;
  if (
    !(["light", "dark", "system"] as const).includes(value.mode as never) ||
    !(["comfortable", "compact"] as const).includes(value.density as never) ||
    !(["square", "rounded"] as const).includes(value.corner_style as never)
  )
    return null;
  return value as unknown as EmbedTheme;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  return Object.keys(value).length === keys.length && hasOnlyKeys(value, keys);
}

function hasOnlyKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  return Object.keys(value).every((key) => keys.includes(key));
}

function safeRecordId(value: string): boolean {
  return (
    value.length > 0 && value.length <= 200 && /^[A-Za-z0-9._:-]+$/.test(value)
  );
}

function isExactOrigin(value: string): boolean {
  try {
    return new URL(value).origin === value;
  } catch {
    return false;
  }
}

export function embedFrameParameters(
  search = window.location.search,
): { sessionId: string; hostOrigin: string } | null {
  const query = new URLSearchParams(search);
  const sessionId = query.get("session_id");
  const hostOrigin = query.get("host_origin");
  if (
    sessionId === null ||
    hostOrigin === null ||
    query.getAll("session_id").length !== 1 ||
    query.getAll("host_origin").length !== 1
  )
    return null;
  try {
    if (new URL(hostOrigin).origin !== hostOrigin) return null;
  } catch {
    return null;
  }
  return { sessionId, hostOrigin };
}
