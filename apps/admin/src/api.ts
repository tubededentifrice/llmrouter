export type AdministrationMode = "global" | "service";

export interface BootstrapScope {
  readonly audiences: readonly string[];
  readonly operations: readonly string[];
  readonly workspace_limit?: "all_service_workspaces" | "explicit_only";
}

export interface ServiceSummary {
  readonly service_id: string;
  readonly display_name: string;
  readonly parent_service_id?: string | null;
  readonly state: "active" | "disabled" | "retired";
  readonly revision: string;
  readonly bootstrap_state: "ready" | "revoked" | "missing";
  readonly credential_generation: number | null;
  readonly prior_generation_expires_at?: string;
  readonly bootstrap_scope: BootstrapScope | null;
}

export interface ServiceCreated {
  readonly service_id: string;
  readonly state: "active";
  readonly state_revision: string;
  readonly bootstrap_secret?: string;
  readonly bootstrap_secret_available: boolean;
  readonly credential_generation: 1;
}

export interface ServiceAdministrationResult {
  readonly resource_id: string;
  readonly state: string;
  readonly revision: string;
  readonly operation_id: string;
  readonly bootstrap_secret?: string;
  readonly prior_generation_expires_at?: string;
}

export interface ScopeSelection {
  readonly mode: AdministrationMode;
  readonly serviceId: string;
  readonly workspaceId: string;
}

export interface ScopedState {
  readonly kind: "service" | "workspace";
  readonly service_id: string;
  readonly workspace_id?: string | null;
  readonly display_name: string;
  readonly state: "active" | "disabled" | "retired";
  readonly revision: string;
  readonly parent_service_id?: string | null;
}

export interface Credential {
  readonly credential_id: string;
  readonly owner_scope: string;
  readonly provider_catalog_id: string;
  readonly state: "active" | "disabled" | "retired";
  readonly revision: string;
  readonly created_at: string;
  readonly fingerprint: string;
}

export interface RegisteredDocument {
  readonly schema_name: string;
  readonly major_version: number;
  readonly document: Record<string, unknown>;
}

export interface ProviderInstance {
  readonly provider_instance_id: string;
  readonly owner_scope: string;
  readonly source_layer: string;
  readonly provider_catalog_id: string;
  readonly display_name: string;
  readonly endpoint: string;
  readonly credential_id: string;
  readonly state: "active" | "disabled" | "retired";
  readonly active_revision: string;
  readonly inherited: boolean;
  readonly settings: RegisteredDocument;
}

export interface PriceComponent {
  readonly unit: string;
  readonly price: string;
  readonly currency: string;
  readonly raw_source_value: string;
  readonly unit_quantity: string;
}

export interface ProviderModelRoute {
  readonly provider_model_route_id: string;
  readonly owner_scope: string;
  readonly source_layer: string;
  readonly provider_instance_id: string;
  readonly canonical_model_id: string;
  readonly wire_model: string;
  readonly capabilities: readonly string[];
  readonly settings: RegisteredDocument;
  readonly price_authority: {
    readonly mode: "manual" | "source";
    readonly source_name: string | null;
    readonly lookup_identifier: string | null;
  };
  readonly prices: readonly PriceComponent[];
  readonly synchronization_schedule: string;
  readonly stale_after_seconds: number;
  readonly state: "active" | "disabled" | "retired";
  readonly active_revision: string;
  readonly inherited: boolean;
}

export interface AssignmentCandidate {
  readonly provider_model_route_id: string;
  readonly attempt_timeout_ms: number;
}

export interface Assignment {
  readonly name: string;
  readonly owner_scope: string;
  readonly source_layer: string;
  readonly state: "active" | "disabled" | "retired";
  readonly inherited: boolean;
  readonly active_revision: string;
  readonly candidates: readonly AssignmentCandidate[];
  readonly required_capabilities: readonly string[];
}

export interface RequestStatus {
  readonly request_id: string;
  readonly workspace_id?: string | null;
  readonly assignment?: string;
  readonly state: string;
  readonly state_revision?: number | string;
  readonly created_at?: string;
  readonly updated_at?: string;
  readonly error?: { readonly code?: string; readonly message?: string } | null;
}

export interface AccountingSummary {
  readonly from: string;
  readonly to: string;
  readonly currency: string;
  readonly logical_requests: number;
  readonly attempts: number;
  readonly usage: readonly {
    readonly unit: string;
    readonly quantity: string;
  }[];
  readonly cost: string;
  readonly corrections: string;
}

export interface AdministrationSnapshot {
  readonly state: ScopedState;
  readonly credentials: readonly Credential[];
  readonly providers: readonly ProviderInstance[];
  readonly routes: readonly ProviderModelRoute[];
  readonly assignments: readonly Assignment[];
  readonly requests: readonly RequestStatus[];
  readonly accounting: AccountingSummary;
}

export function configurationRevisionForScope(
  snapshot: AdministrationSnapshot,
  scope: ScopeSelection,
): string | null {
  const sourceLayer = scope.workspaceId === "" ? "service" : "workspace";
  const effectiveItems = [
    ...snapshot.providers,
    ...snapshot.routes,
    ...snapshot.assignments,
  ];
  return (
    effectiveItems.find(
      (item) => !item.inherited && item.source_layer === sourceLayer,
    )?.active_revision ?? null
  );
}

export interface ConfigurationWriteResult {
  readonly resource_id: string;
  readonly active_revision: string;
  readonly distribution_state: string;
  readonly operation_id: string;
}

interface Page<T> {
  readonly items: readonly T[];
  readonly next_cursor: string | null;
}

interface ApiErrorDocument {
  readonly error?: {
    readonly code?: string;
    readonly message?: string;
    readonly request_id?: string;
    readonly retryable?: boolean;
  };
}

export class AdministrationApiError extends Error {
  public readonly code: string;
  public readonly requestId: string | null;
  public readonly status: number;
  public readonly staleRevision: boolean;

  public constructor(
    message: string,
    options: {
      readonly code: string;
      readonly requestId: string | null;
      readonly status: number;
    },
  ) {
    super(message);
    this.name = "AdministrationApiError";
    this.code = options.code;
    this.requestId = options.requestId;
    this.status = options.status;
    this.staleRevision =
      options.status === 409 && options.code.includes("revision");
  }
}

export interface AdministrationClient {
  listServices(signal?: AbortSignal): Promise<readonly ServiceSummary[]>;
  listCredentials(signal?: AbortSignal): Promise<readonly Credential[]>;
  createService(input: {
    readonly displayName: string;
    readonly parentServiceId: string | null;
    readonly bootstrapScope: BootstrapScope;
  }): Promise<ServiceCreated>;
  putService(
    serviceId: string,
    input: {
      readonly expectedRevision: string;
      readonly reason: string;
      readonly displayName?: string;
      readonly newParentServiceId?: string | null;
    },
  ): Promise<ServiceAdministrationResult>;
  changeService(
    serviceId: string,
    action: "disable" | "restore" | "retire",
    input: { readonly expectedRevision: string; readonly reason: string },
  ): Promise<ServiceAdministrationResult>;
  load(
    scope: ScopeSelection,
    signal?: AbortSignal,
  ): Promise<AdministrationSnapshot>;
  createCredential(input: {
    readonly ownerScope: string;
    readonly secret: string;
    readonly safeLabel: string;
  }): Promise<Credential>;
  putProvider(
    scope: ScopeSelection,
    id: string | null,
    input: Record<string, unknown>,
  ): Promise<ConfigurationWriteResult>;
  putRoute(
    scope: ScopeSelection,
    id: string | null,
    input: Record<string, unknown>,
  ): Promise<ConfigurationWriteResult>;
  putAssignment(
    scope: ScopeSelection,
    name: string,
    input: Record<string, unknown>,
  ): Promise<ConfigurationWriteResult>;
}

export interface FetchAdministrationClientOptions {
  readonly baseUrl?: string;
  readonly csrfToken?: string;
  readonly fetcher?: typeof fetch;
  readonly now?: () => Date;
  readonly onRecentAuthenticationRequired?: () => Promise<void>;
}

const jsonHeaders = { "Content-Type": "application/json" } as const;

export type LocalAdministratorSession =
  | {
      readonly state: "active";
      readonly csrfToken: string;
      readonly authenticationMode: "local" | "oidc";
      readonly identityAccountUrl?: string;
    }
  | { readonly state: "required" }
  | { readonly state: "oidc_required" }
  | { readonly state: "unavailable" };

export async function inspectLocalAdministratorSession(
  fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
  origin: string = typeof window === "undefined"
    ? "http://127.0.0.1:5174"
    : window.location.origin,
): Promise<LocalAdministratorSession> {
  let localAvailable = false;
  if (origin === "http://127.0.0.1:5174") {
    let capability: Response;
    try {
      capability = await fetcher("/v1/admin/local-session", {
        method: "HEAD",
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch {
      return { state: "unavailable" };
    }
    localAvailable = capability.ok;
  }
  const response = await fetcher("/v1/admin/session", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 404) return { state: "unavailable" };
  if (response.status === 401)
    return { state: localAvailable ? "required" : "oidc_required" };
  if (!response.ok)
    throw new AdministrationApiError(
      "The local administrator session is not available.",
      {
        code: "local_session_unavailable",
        requestId: null,
        status: response.status,
      },
    );
  const document = (await response.json()) as {
    readonly csrf_token?: unknown;
    readonly authentication_mode?: unknown;
    readonly identity_account_url?: unknown;
  };
  if (
    typeof document.csrf_token !== "string" ||
    document.csrf_token.length < 20
  )
    throw new AdministrationApiError(
      "The local administrator session is not available.",
      { code: "local_session_invalid", requestId: null, status: 500 },
    );
  const mode = document.authentication_mode === "oidc" ? "oidc" : "local";
  return {
    state: "active",
    csrfToken: document.csrf_token,
    authenticationMode: mode,
    ...(typeof document.identity_account_url === "string"
      ? { identityAccountUrl: document.identity_account_url }
      : {}),
  };
}

export async function startPocketIDAdministratorSession(
  trustedGrantToken?: string,
  fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<string> {
  return startPocketIDSession("login", trustedGrantToken, fetcher);
}

export async function startPocketIDRecentAuthentication(
  fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<string> {
  return startPocketIDSession("recent_authentication", undefined, fetcher);
}

async function startPocketIDSession(
  purpose: "login" | "recent_authentication",
  trustedGrantToken: string | undefined,
  fetcher: typeof fetch,
): Promise<string> {
  const response = await fetcher("/v1/admin/session-starts", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: jsonHeaders,
    body: JSON.stringify({
      purpose,
      return_path: "/",
      ...(trustedGrantToken === undefined
        ? {}
        : { trusted_grant_token: trustedGrantToken }),
    }),
  });
  if (!response.ok)
    throw new AdministrationApiError("Pocket ID sign-in did not start.", {
      code: "administrator_sign_in_failed",
      requestId: null,
      status: response.status,
    });
  const document = (await response.json()) as {
    readonly authorization_url?: unknown;
  };
  if (typeof document.authorization_url !== "string")
    throw new AdministrationApiError("Pocket ID sign-in did not start.", {
      code: "administrator_sign_in_invalid",
      requestId: null,
      status: 500,
    });
  return document.authorization_url;
}

export function consumeTrustedGrantToken(
  location: Pick<Location, "hash" | "pathname" | "search"> = window.location,
  history: Pick<History, "replaceState"> = window.history,
): string | undefined {
  const fragment = location.hash;
  if (fragment === "") return undefined;
  history.replaceState({}, "", `${location.pathname}${location.search}`);
  const parameters = new URLSearchParams(fragment.slice(1));
  const values = parameters.getAll("token");
  if (
    [...parameters.keys()].some((key) => key !== "token") ||
    values.length !== 1 ||
    !/^[A-Za-z0-9_-]{43}$/.test(values[0] ?? "")
  )
    return undefined;
  return values[0];
}

export async function endAdministratorSession(
  csrfToken: string,
  fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<void> {
  const response = await fetcher("/v1/admin/session", {
    method: "DELETE",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "X-CSRF-Token": csrfToken },
  });
  if (!response.ok)
    throw new AdministrationApiError("Administrator sign-out failed.", {
      code: "administrator_sign_out_failed",
      requestId: null,
      status: response.status,
    });
}

export async function activateLocalAdministrator(
  secret: string,
  fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<string> {
  const response = await fetcher("/v1/admin/local-session", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: jsonHeaders,
    body: JSON.stringify({ secret }),
  });
  if (!response.ok)
    throw new AdministrationApiError(
      "The local administrator session was not activated.",
      {
        code: "local_administrator_activation_failed",
        requestId: null,
        status: response.status,
      },
    );
  const document = (await response.json()) as {
    readonly authenticated?: unknown;
    readonly csrf_token?: unknown;
  };
  if (
    document.authenticated !== true ||
    typeof document.csrf_token !== "string" ||
    document.csrf_token.length < 20
  )
    throw new AdministrationApiError(
      "The local administrator session was not activated.",
      { code: "local_session_invalid", requestId: null, status: 500 },
    );
  return document.csrf_token;
}

function randomKey(): string {
  return globalThis.crypto.randomUUID();
}

export function createFetchAdministrationClient({
  baseUrl = "",
  csrfToken: suppliedCsrfToken,
  fetcher = globalThis.fetch.bind(globalThis),
  now = () => new Date(),
  onRecentAuthenticationRequired,
}: FetchAdministrationClientOptions = {}): AdministrationClient {
  let csrfToken = suppliedCsrfToken ?? null;
  let recentAuthentication: Promise<void> | null = null;

  async function request<T>(
    path: string,
    init: RequestInit = {},
    mutation = false,
  ): Promise<T> {
    if (mutation && csrfToken === null) {
      const session = await request<{ readonly csrf_token: string }>(
        "/v1/admin/session",
      );
      csrfToken = session.csrf_token;
    }
    const headers = new Headers(init.headers);
    if (init.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", jsonHeaders["Content-Type"]);
    }
    if (mutation) {
      headers.set("X-CSRF-Token", csrfToken ?? "");
      headers.set("Idempotency-Key", randomKey());
    }
    let response: Response;
    try {
      response = await fetcher(`${baseUrl}${path}`, {
        ...init,
        credentials: "same-origin",
        headers,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      throw new AdministrationApiError(
        "The administration service is offline. No change was sent.",
        { code: "offline", requestId: null, status: 0 },
      );
    }
    if (!response.ok) {
      let document: ApiErrorDocument = {};
      try {
        document = (await response.json()) as ApiErrorDocument;
      } catch {
        // The safe generic error below does not expose an upstream response.
      }
      if (
        document.error?.code === "recent_auth_required" &&
        onRecentAuthenticationRequired !== undefined
      ) {
        if (recentAuthentication === null) {
          const attempt = onRecentAuthenticationRequired();
          recentAuthentication = attempt;
          try {
            await attempt;
          } catch (error) {
            if (recentAuthentication === attempt) recentAuthentication = null;
            throw error;
          }
        } else {
          await recentAuthentication;
        }
      }
      throw new AdministrationApiError(
        document.error?.message ??
          "The administration request did not complete. No unsafe detail is available.",
        {
          code: document.error?.code ?? "administration_request_failed",
          requestId: document.error?.request_id ?? null,
          status: response.status,
        },
      );
    }
    return (await response.json()) as T;
  }

  function servicePath(
    scope: ScopeSelection,
    suffix: string,
    includeWorkspace = false,
  ): string {
    const separator = suffix.indexOf("?");
    const suffixPath = separator === -1 ? suffix : suffix.slice(0, separator);
    const suffixQuery = separator === -1 ? "" : suffix.slice(separator + 1);
    const query = new URLSearchParams(suffixQuery);
    if (includeWorkspace && scope.workspaceId !== "") {
      query.set("workspace_id", scope.workspaceId);
    }
    const encodedService = encodeURIComponent(scope.serviceId);
    const serialized = query.toString();
    return `/v1/admin/services/${encodedService}/${suffixPath}${serialized === "" ? "" : `?${serialized}`}`;
  }

  function configurationPath(
    scope: ScopeSelection,
    collection: string,
    id: string | null,
    includeWorkspace = false,
  ): string {
    const base = servicePath(scope, collection, includeWorkspace);
    if (id === null) return base;
    const separator = base.indexOf("?");
    const path = separator === -1 ? base : base.slice(0, separator);
    const query = separator === -1 ? "" : base.slice(separator);
    return `${path}/${encodeURIComponent(id)}${query}`;
  }

  async function page<T>(
    path: string,
    signal?: AbortSignal,
  ): Promise<readonly T[]> {
    const value = await request<Page<T>>(
      path,
      signal === undefined ? {} : { signal },
    );
    return value.items;
  }

  async function allPages<T>(
    initialPath: string,
    signal?: AbortSignal,
  ): Promise<readonly T[]> {
    const items: T[] = [];
    const cursors = new Set<string>();
    let path = initialPath;
    for (let pageNumber = 0; pageNumber < 100; pageNumber += 1) {
      const value = await request<Page<T>>(
        path,
        signal === undefined ? {} : { signal },
      );
      items.push(...value.items);
      if (value.next_cursor === null) return items;
      if (cursors.has(value.next_cursor)) {
        throw new AdministrationApiError(
          "The administration list did not complete. Try again.",
          { code: "invalid_pagination", requestId: null, status: 502 },
        );
      }
      cursors.add(value.next_cursor);
      const next = new URL(path, "http://administration.local");
      next.searchParams.set("cursor", value.next_cursor);
      path = `${next.pathname}${next.search}`;
    }
    throw new AdministrationApiError(
      "The administration list is too large to load safely.",
      { code: "pagination_limit", requestId: null, status: 502 },
    );
  }

  return {
    listServices(signal) {
      return allPages<ServiceSummary>("/v1/admin/services", signal);
    },

    listCredentials(signal) {
      return allPages<Credential>("/v1/admin/credentials", signal);
    },

    createService(input) {
      return request<ServiceCreated>(
        "/v1/admin/services",
        {
          method: "POST",
          body: JSON.stringify({
            display_name: input.displayName,
            parent_service_id: input.parentServiceId,
            bootstrap_scope: input.bootstrapScope,
          }),
        },
        true,
      );
    },

    putService(serviceId, input) {
      return request<ServiceAdministrationResult>(
        `/v1/admin/services/${encodeURIComponent(serviceId)}`,
        {
          method: "PUT",
          body: JSON.stringify({
            expected_revision: input.expectedRevision,
            reason: input.reason,
            ...(input.displayName === undefined
              ? {}
              : { display_name: input.displayName }),
            ...(input.newParentServiceId === undefined
              ? {}
              : { new_parent_service_id: input.newParentServiceId }),
          }),
        },
        true,
      );
    },

    changeService(serviceId, action, input) {
      return request<ServiceAdministrationResult>(
        `/v1/admin/services/${encodeURIComponent(serviceId)}/${action}`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_revision: input.expectedRevision,
            reason: input.reason,
          }),
        },
        true,
      );
    },

    async load(scope, signal) {
      const end = now();
      const start = new Date(end.getTime() - 7 * 24 * 60 * 60 * 1000);
      const accountingQuery = new URLSearchParams({
        from: start.toISOString(),
        to: end.toISOString(),
      });
      if (scope.workspaceId !== "") {
        accountingQuery.set("workspace_id", scope.workspaceId);
      }
      const serviceBase = `/v1/admin/services/${encodeURIComponent(scope.serviceId)}`;
      const [
        state,
        credentials,
        providers,
        routes,
        assignments,
        requests,
        accounting,
      ] = await Promise.all([
        request<ScopedState>(
          servicePath(scope, "state", true),
          signal === undefined ? {} : { signal },
        ),
        scope.mode === "global" && scope.workspaceId === ""
          ? page<Credential>("/v1/admin/credentials", signal)
          : Promise.resolve([]),
        page<ProviderInstance>(
          servicePath(scope, "provider-instances"),
          signal,
        ),
        page<ProviderModelRoute>(
          servicePath(scope, "provider-model-routes"),
          signal,
        ),
        page<Assignment>(servicePath(scope, "assignments", true), signal),
        page<RequestStatus>(servicePath(scope, "model-requests", true), signal),
        request<AccountingSummary>(
          `${serviceBase}/accounting/summary?${accountingQuery.toString()}`,
          signal === undefined ? {} : { signal },
        ),
      ]);
      return {
        state,
        credentials,
        providers,
        routes,
        assignments,
        requests,
        accounting,
      };
    },

    createCredential(input) {
      return request<Credential>(
        "/v1/admin/credentials",
        {
          method: "POST",
          body: JSON.stringify({
            owner_scope: input.ownerScope,
            provider_catalog_id: "openai_compatible.v1",
            secret: input.secret,
            safe_label: input.safeLabel === "" ? null : input.safeLabel,
          }),
        },
        true,
      );
    },

    putProvider(scope, id, input) {
      return request<ConfigurationWriteResult>(
        configurationPath(scope, "provider-instances", id),
        { method: id === null ? "POST" : "PUT", body: JSON.stringify(input) },
        true,
      );
    },

    putRoute(scope, id, input) {
      return request<ConfigurationWriteResult>(
        configurationPath(scope, "provider-model-routes", id),
        { method: id === null ? "POST" : "PUT", body: JSON.stringify(input) },
        true,
      );
    },

    putAssignment(scope, name, input) {
      return request<ConfigurationWriteResult>(
        configurationPath(scope, "assignments", name, true),
        { method: "PUT", body: JSON.stringify(input) },
        true,
      );
    },
  };
}

export function errorMessage(error: unknown): string {
  if (error instanceof AdministrationApiError) {
    const request =
      error.requestId === null ? "" : ` Request ${error.requestId}.`;
    return `${error.message}${request}`;
  }
  return "The administration request did not complete. No unsafe detail is available.";
}
