import { randomUUID } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import process from "node:process";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";
import type {
  ContextAction,
  CreatedEmbedSession,
  HostContext,
} from "./src/hostApi.js";

const hostOrigin = "http://127.0.0.1:5176";
const defaultRouterOrigin = "http://127.0.0.1:5175";
const cookieName = "llmrouter-example-host";
const readPermissions = [
  "health.read",
  "configuration.read",
  "request_status.read",
  "accounting.read",
] as const;

interface ExampleHostConfiguration {
  readonly routerOrigin: string;
  readonly serviceId: string;
  readonly serviceToken: string;
  readonly workspaceIds: readonly [string, string];
}

interface ExampleHostDependencies {
  readonly fetcher: typeof fetch;
  readonly randomId: () => string;
}

interface HostState {
  activeSessionId: string | null;
  context: HostContext;
  permissionVariant: number;
  userVariant: number;
  workspaceVariant: number;
}

interface CreateSessionInput {
  readonly expected_revision: string;
}

export class ExampleHostService {
  constructor(
    private readonly configuration: ExampleHostConfiguration,
    private readonly dependencies: ExampleHostDependencies,
  ) {}

  async createSession(context: HostContext): Promise<CreatedEmbedSession> {
    if (
      !context.membership ||
      context.service_id !== this.configuration.serviceId
    )
      throw new PublicHostError(
        403,
        "Router administration membership is not active.",
      );
    if (this.configuration.serviceToken === "")
      throw new PublicHostError(
        503,
        "The example host token is not configured.",
      );
    const response = await this.dependencies.fetcher(
      `${this.configuration.routerOrigin}/v1/services/${encodeURIComponent(this.configuration.serviceId)}/administration/embed-sessions`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.configuration.serviceToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          host_user_subject: context.host_user_subject,
          workspace_id: context.workspace_id,
          allowed_origin: hostOrigin,
          permissions: context.permissions,
          theme: {
            mode: "system",
            density: "comfortable",
            corner_style: "rounded",
          },
        }),
      },
    );
    if (!response.ok)
      throw new PublicHostError(
        response.status,
        "The Router session request failed.",
      );
    const created: unknown = await response.json();
    if (
      typeof created !== "object" ||
      created === null ||
      !("session_id" in created) ||
      typeof created.session_id !== "string" ||
      created.session_id === "" ||
      created.session_id.length > 200
    )
      throw new PublicHostError(502, "The Router session response is invalid.");
    return created as CreatedEmbedSession;
  }

  async revokeSession(sessionId: string): Promise<void> {
    const response = await this.dependencies.fetcher(
      `${this.configuration.routerOrigin}/v1/services/${encodeURIComponent(this.configuration.serviceId)}/administration/embed-sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${this.configuration.serviceToken}` },
      },
    );
    if (!response.ok && response.status !== 404)
      throw new PublicHostError(
        response.status,
        "The Router session revoke failed.",
      );
  }

  initialState(): HostState {
    return {
      activeSessionId: null,
      context: this.context(0, 0, 0, true),
      permissionVariant: 0,
      userVariant: 0,
      workspaceVariant: 0,
    };
  }

  changeContext(state: HostState, action: ContextAction): HostState {
    let { permissionVariant, userVariant, workspaceVariant } = state;
    let membership = state.context.membership;
    if (action === "switch_user") userVariant = userVariant === 0 ? 1 : 0;
    else if (action === "switch_workspace")
      workspaceVariant = workspaceVariant === 0 ? 1 : 0;
    else if (action === "change_permissions")
      permissionVariant = permissionVariant === 0 ? 1 : 0;
    else if (action === "lose_membership") membership = false;
    else membership = true;
    return {
      activeSessionId: null,
      context: this.context(
        permissionVariant,
        userVariant,
        workspaceVariant,
        membership,
      ),
      permissionVariant,
      userVariant,
      workspaceVariant,
    };
  }

  private context(
    permissionVariant: number,
    userVariant: number,
    workspaceVariant: number,
    membership: boolean,
  ): HostContext {
    return {
      revision: this.dependencies.randomId(),
      service_id: this.configuration.serviceId,
      host_user_subject: `example-user-${userVariant === 0 ? "a" : "b"}`,
      workspace_id: this.configuration.workspaceIds[workspaceVariant] ?? "",
      permissions:
        permissionVariant === 0
          ? readPermissions
          : ["health.read", "configuration.read"],
      membership,
    };
  }
}

class PublicHostError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export default defineConfig(() => {
  const configuration = configurationFromEnvironment();
  return {
    plugins: [react(), exampleHostPlugin(configuration)],
    optimizeDeps: { exclude: ["@opendle/ui"] },
    server: {
      host: "127.0.0.1",
      port: 5176,
      strictPort: true,
      headers: securityHeaders(configuration.routerOrigin, true),
    },
    preview: {
      host: "127.0.0.1",
      port: 5176,
      strictPort: true,
      headers: securityHeaders(configuration.routerOrigin, false),
    },
  };
});

function exampleHostPlugin(configuration: ExampleHostConfiguration): Plugin {
  const states = new Map<string, HostState>();
  const requestLocks = new Map<string, Promise<void>>();
  const service = new ExampleHostService(configuration, {
    fetcher: fetch,
    randomId: randomUUID,
  });
  const middleware = (
    request: IncomingMessage,
    response: ServerResponse,
    next: () => void,
  ) => {
    if (!request.url?.startsWith("/api/")) {
      next();
      return;
    }
    void handleApi(request, response, service, states, requestLocks);
  };
  return {
    name: "llmrouter-embed-example-host",
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}

async function handleApi(
  request: IncomingMessage,
  response: ServerResponse,
  service: ExampleHostService,
  states: Map<string, HostState>,
  requestLocks: Map<string, Promise<void>>,
): Promise<void> {
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("X-Content-Type-Options", "nosniff");
  try {
    if (request.method !== "GET" && request.headers.origin !== hostOrigin) {
      throw new PublicHostError(403, "The example host origin is invalid.");
    }
    const id = stateIdForRequest(request, response, service, states);
    await serializeRequest(id, requestLocks, async () => {
      const state = states.get(id);
      if (state === undefined)
        throw new PublicHostError(
          500,
          "The example host state is unavailable.",
        );
      const url = new URL(request.url ?? "/", hostOrigin);
      if (url.pathname === "/api/context" && request.method === "GET") {
        sendJson(response, 200, state.context);
        return;
      }
      if (url.pathname === "/api/context" && request.method === "POST") {
        const body = await readJson(request);
        const action = parseContextAction(body);
        if (state.activeSessionId !== null) {
          await service.revokeSession(state.activeSessionId);
          state.activeSessionId = null;
        }
        const nextState = service.changeContext(state, action);
        states.set(id, nextState);
        sendJson(response, 200, nextState.context);
        return;
      }
      if (url.pathname === "/api/embed-session" && request.method === "POST") {
        const input = parseCreateSessionInput(await readJson(request));
        if (input.expected_revision !== state.context.revision)
          throw new PublicHostError(
            409,
            "The example host authorization changed before session creation.",
          );
        if (state.activeSessionId !== null) {
          await service.revokeSession(state.activeSessionId);
          state.activeSessionId = null;
        }
        const created = await service.createSession(state.context);
        state.activeSessionId = created.session_id;
        sendJson(response, 201, created);
        return;
      }
      const sessionMatch = /^\/api\/embed-session\/([^/]+)$/.exec(url.pathname);
      if (sessionMatch !== null && request.method === "DELETE") {
        const sessionId = decodeURIComponent(sessionMatch[1] ?? "");
        if (state.activeSessionId !== sessionId) {
          sendJson(response, 404, {
            error: "The example host session is not active.",
          });
          return;
        }
        await service.revokeSession(sessionId);
        state.activeSessionId = null;
        response.statusCode = 204;
        response.end();
        return;
      }
      sendJson(response, 404, {
        error: "The example host route does not exist.",
      });
    });
  } catch (error) {
    const status = error instanceof PublicHostError ? error.status : 400;
    const message =
      error instanceof PublicHostError
        ? error.message
        : "The example host request is invalid.";
    sendJson(response, status, { error: message });
  }
}

function stateIdForRequest(
  request: IncomingMessage,
  response: ServerResponse,
  service: ExampleHostService,
  states: Map<string, HostState>,
): string {
  const id = cookieValue(request.headers.cookie, cookieName) ?? randomUUID();
  const state = states.get(id) ?? service.initialState();
  states.set(id, state);
  if (!request.headers.cookie?.includes(`${cookieName}=`)) {
    response.setHeader(
      "Set-Cookie",
      `${cookieName}=${id}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600`,
    );
  }
  return id;
}

async function serializeRequest(
  id: string,
  requestLocks: Map<string, Promise<void>>,
  operation: () => Promise<void>,
): Promise<void> {
  const previous = requestLocks.get(id) ?? Promise.resolve();
  let release!: () => void;
  const current = new Promise<void>((resolve) => {
    release = resolve;
  });
  requestLocks.set(id, current);
  await previous.catch(() => undefined);
  try {
    await operation();
  } finally {
    release();
    if (requestLocks.get(id) === current) requestLocks.delete(id);
  }
}

function configurationFromEnvironment(): ExampleHostConfiguration {
  const routerOrigin =
    process.env.LLMROUTER_EXAMPLE_ROUTER_ORIGIN ?? defaultRouterOrigin;
  const serviceId =
    process.env.LLMROUTER_EXAMPLE_SERVICE_ID ?? "service-example";
  const serviceToken = process.env.LLMROUTER_EXAMPLE_HOST_TOKEN ?? "";
  const firstWorkspace =
    process.env.LLMROUTER_EXAMPLE_WORKSPACE_ID ?? "workspace-example-a";
  const secondWorkspace =
    process.env.LLMROUTER_EXAMPLE_SECOND_WORKSPACE_ID ?? "workspace-example-b";
  if (!exactOrigin(routerOrigin) || routerOrigin === hostOrigin)
    throw new Error(
      "LLMROUTER_EXAMPLE_ROUTER_ORIGIN must be an exact distinct origin.",
    );
  return {
    routerOrigin,
    serviceId,
    serviceToken,
    workspaceIds: [firstWorkspace, secondWorkspace],
  };
}

function parseContextAction(value: unknown): ContextAction {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    Object.keys(value).length !== 1 ||
    !("action" in value) ||
    ![
      "switch_user",
      "switch_workspace",
      "change_permissions",
      "lose_membership",
      "restore_membership",
    ].includes(String(value.action))
  ) {
    throw new PublicHostError(400, "The example context action is invalid.");
  }
  return value.action as ContextAction;
}

function parseCreateSessionInput(value: unknown): CreateSessionInput {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    Object.keys(value).length !== 1 ||
    !("expected_revision" in value) ||
    typeof value.expected_revision !== "string" ||
    value.expected_revision === "" ||
    value.expected_revision.length > 200
  ) {
    throw new PublicHostError(400, "The example session request is invalid.");
  }
  return { expected_revision: value.expected_revision };
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const parts: Uint8Array[] = [];
  let length = 0;
  for await (const part of request as AsyncIterable<Uint8Array>) {
    length += part.length;
    if (length > 4_096)
      throw new PublicHostError(413, "The example host request is too large.");
    parts.push(part);
  }
  try {
    return JSON.parse(Buffer.concat(parts).toString("utf8") || "{}");
  } catch {
    throw new PublicHostError(400, "The example host request is invalid.");
  }
}

function cookieValue(header: string | undefined, name: string): string | null {
  for (const item of header?.split(";") ?? []) {
    const [key, value] = item.trim().split("=", 2);
    if (
      key === name &&
      value !== undefined &&
      /^[A-Za-z0-9-]{16,80}$/.test(value)
    )
      return value;
  }
  return null;
}

function sendJson(
  response: ServerResponse,
  status: number,
  value: unknown,
): void {
  response.statusCode = status;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.end(JSON.stringify(value));
}

function securityHeaders(
  routerOrigin: string,
  development: boolean,
): Record<string, string> {
  const inlineScript = development ? " 'unsafe-inline'" : "";
  return {
    "Cache-Control": "no-store",
    "Content-Security-Policy": `default-src 'self'; script-src 'self'${inlineScript}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src ${routerOrigin}; frame-ancestors 'self'; base-uri 'none'; form-action 'self'; object-src 'none'`,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
  };
}

function exactOrigin(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      url.origin === value &&
      (url.protocol === "https:" ||
        (url.protocol === "http:" &&
          ["127.0.0.1", "localhost", "::1"].includes(url.hostname)))
    );
  } catch {
    return false;
  }
}
