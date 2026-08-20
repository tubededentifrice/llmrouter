export interface HostContext {
  readonly revision: string;
  readonly service_id: string;
  readonly host_user_subject: string;
  readonly workspace_id: string;
  readonly permissions: readonly string[];
  readonly membership: boolean;
}

export interface CreatedEmbedSession {
  readonly session_id: string;
  readonly bootstrap_token: string;
  readonly frame_url: string;
  readonly expires_at: string;
  readonly message_version: "1";
}

export type ContextAction =
  | "switch_user"
  | "switch_workspace"
  | "change_permissions"
  | "lose_membership"
  | "restore_membership";

export interface HostApi {
  context(signal?: AbortSignal): Promise<HostContext>;
  changeContext(
    action: ContextAction,
    signal?: AbortSignal,
  ): Promise<HostContext>;
  createSession(
    expectedRevision: string,
    signal?: AbortSignal,
  ): Promise<CreatedEmbedSession>;
  revokeSession(sessionId: string): Promise<void>;
}

export function createHostApi(fetcher: typeof fetch = fetch): HostApi {
  return {
    context: (signal) =>
      requestContext(fetcher, "/api/context", undefined, signal),
    changeContext: (action, signal) =>
      requestContext(fetcher, "/api/context", { action }, signal),
    createSession: async (expectedRevision, signal) => {
      const response = await fetcher("/api/embed-session", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: expectedRevision }),
        ...(signal === undefined ? {} : { signal }),
      });
      if (!response.ok) throw new Error(await safeError(response));
      return (await response.json()) as CreatedEmbedSession;
    },
    revokeSession: async (sessionId) => {
      const response = await fetcher(
        `/api/embed-session/${encodeURIComponent(sessionId)}`,
        { method: "DELETE", credentials: "same-origin" },
      );
      if (!response.ok && response.status !== 404)
        throw new Error(await safeError(response));
    },
  };
}

async function requestContext(
  fetcher: typeof fetch,
  path: string,
  input: { readonly action: ContextAction } | undefined,
  signal: AbortSignal | undefined,
): Promise<HostContext> {
  const response = await fetcher(path, {
    method: input === undefined ? "GET" : "POST",
    credentials: "same-origin",
    ...(input === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
        }),
    ...(signal === undefined ? {} : { signal }),
  });
  if (!response.ok) throw new Error(await safeError(response));
  return (await response.json()) as HostContext;
}

async function safeError(response: Response): Promise<string> {
  try {
    const value = (await response.json()) as { readonly error?: unknown };
    return typeof value.error === "string"
      ? value.error
      : "The example host request failed.";
  } catch {
    return "The example host request failed.";
  }
}
