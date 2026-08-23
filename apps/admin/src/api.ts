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

export function scopeFromSearch(search: string): ScopeSelection {
  const query = new URLSearchParams(search);
  return {
    mode: "global",
    serviceId: query.get("service_id") ?? "",
    workspaceId: query.get("workspace_id") ?? "",
  };
}

export function scopeSearch(scope: ScopeSelection): string {
  const query = new URLSearchParams();
  if (scope.serviceId !== "") query.set("service_id", scope.serviceId);
  if (scope.workspaceId !== "") query.set("workspace_id", scope.workspaceId);
  return query.toString();
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

export interface CatalogEntry {
  readonly stable_id: string;
  readonly kind: "provider" | "model";
  readonly display_name: string;
  readonly capabilities: readonly string[];
  readonly state: "active" | "disabled" | "retired";
  readonly settings: RegisteredDocument | null;
  readonly active_revision: string;
}

export interface Money {
  readonly amount: string;
  readonly currency: string;
}

export interface BudgetSummary {
  readonly scope: "service" | "workspace";
  readonly limit: Money;
  readonly warning_threshold: Money | null;
  readonly reserved: Money;
  readonly used: Money;
  readonly corrected: Money;
  readonly remaining: Money;
  readonly enforcement_state:
    "available" | "warning" | "exhausted" | "allowance_unavailable";
  readonly reset_period: "none" | "daily" | "monthly";
  readonly revision: string;
}

export interface BudgetLimitWriteResult {
  readonly scope: "service" | "workspace";
  readonly limit: Money;
  readonly warning_threshold: Money | null;
  readonly reset_period: "none" | "daily" | "monthly";
  readonly revision: string;
  readonly effective_at: string;
}

export interface ProviderInstance {
  readonly provider_instance_id: string;
  readonly owner_scope: string;
  readonly source_layer: string;
  readonly provider_catalog_id: string;
  readonly display_name: string;
  readonly endpoint: string;
  readonly credential_id: string;
  readonly eligible_service_ids: readonly string[];
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
  readonly eligible_service_ids: readonly string[];
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

export type RequestFailureClass =
  | "authentication"
  | "policy"
  | "budget"
  | "rate_limit"
  | "timeout"
  | "transport"
  | "provider_unavailable"
  | "invalid_provider_response"
  | "incompatible_request"
  | "cancelled"
  | "uncertain_effect"
  | "router_internal";

export type RequestFailureScope =
  | "attempt"
  | "provider_model_route"
  | "provider_instance"
  | "credential"
  | "assignment_candidate"
  | "logical_request";

export interface SafeRequestError {
  readonly class: RequestFailureClass;
  readonly affected_scope: RequestFailureScope;
  readonly message: string;
  readonly safe_provider_code?: string;
}

export interface RequestAttemptStatus {
  readonly attempt_id: string;
  readonly provider_model_route_id: string;
  readonly state:
    "running" | "succeeded" | "failed" | "cancelled" | "uncertain";
  readonly started_at: string;
  readonly ended_at?: string;
  readonly assignment_revision: string;
  readonly decision?:
    | "next_candidate"
    | "stop_request"
    | "commit_boundary"
    | "cancelled"
    | "succeeded";
  readonly error?: SafeRequestError;
  readonly usage?: readonly {
    readonly unit: string;
    readonly quantity: string;
  }[];
  readonly price_version?: string;
}

export interface RequestAccounting {
  readonly estimated: string;
  readonly reserved: string;
  readonly used: string;
  readonly corrected: string;
  readonly currency: string;
}

export interface RequestStatus {
  readonly request_id: string;
  readonly assignment?: string;
  readonly exact_route?: string;
  readonly state:
    | "admitted"
    | "running"
    | "succeeded"
    | "failed"
    | "interrupted"
    | "cancel_requested"
    | "cancelled"
    | "uncertain";
  readonly state_revision: number;
  readonly admitted_at: string;
  readonly last_transition_at: string;
  readonly terminal_at?: string;
  readonly partial_output: boolean;
  readonly committed_effects: boolean;
  readonly configuration_revision: string;
  readonly attempts: readonly RequestAttemptStatus[];
  readonly accounting: RequestAccounting;
  readonly error?: SafeRequestError | null;
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
  readonly state: ScopedState | null;
  readonly credentials: readonly Credential[];
  readonly providers: readonly ProviderInstance[];
  readonly routes: readonly ProviderModelRoute[];
  readonly assignments: readonly Assignment[];
  readonly requests: readonly RequestStatus[];
  readonly accounting: AccountingSummary | null;
  readonly budget: BudgetSummary | null;
  readonly configuration_revision: string | null;
  readonly failures: Readonly<
    Partial<
      Record<
        | "state"
        | "credentials"
        | "providers"
        | "routes"
        | "assignments"
        | "requests"
        | "accounting"
        | "budget",
        string
      >
    >
  >;
}

export function configurationRevisionForScope(
  snapshot: AdministrationSnapshot,
  scope: ScopeSelection,
): string | null {
  if (snapshot.configuration_revision !== null) {
    return snapshot.configuration_revision;
  }
  const sourceLayer =
    scope.workspaceId === "" ? scope.serviceId : scope.workspaceId;
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
  readonly configuration_revision?: string | null;
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
  public readonly outcomeUncertain: boolean;

  public constructor(
    message: string,
    options: {
      readonly code: string;
      readonly requestId: string | null;
      readonly status: number;
      readonly outcomeUncertain?: boolean;
    },
  ) {
    super(message);
    this.name = "AdministrationApiError";
    this.code = options.code;
    this.requestId = options.requestId;
    this.status = options.status;
    this.staleRevision =
      options.status === 409 && options.code.includes("revision");
    this.outcomeUncertain = options.outcomeUncertain ?? false;
  }
}

export interface AdministrationClient {
  listServices(signal?: AbortSignal): Promise<readonly ServiceSummary[]>;
  listCredentials(signal?: AbortSignal): Promise<readonly Credential[]>;
  listCatalog(
    kind: "providers" | "models",
    signal?: AbortSignal,
  ): Promise<readonly CatalogEntry[]>;
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
  getRequest(
    scope: ScopeSelection,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<RequestStatus>;
  createCredential(input: {
    readonly ownerScope: string;
    readonly secret: string;
    readonly safeLabel: string;
  }): Promise<Credential>;
  changeCredential(
    credentialId: string,
    action: "rotate" | "disable" | "retire",
    input: {
      readonly expectedRevision: string;
      readonly reason: string;
      readonly replacementSecret?: string;
    },
  ): Promise<Credential>;
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
  putBudget(
    scope: ScopeSelection,
    input: {
      readonly hardLimit: string;
      readonly currency: string;
      readonly warningThreshold: string | null;
      readonly resetPeriod: "none" | "daily" | "monthly";
      readonly expectedRevision: string;
    },
  ): Promise<BudgetLimitWriteResult>;
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

export function scheduleAdministrationSessionInspection(
  inspect: () => void,
  schedule: (callback: () => void) => void = queueMicrotask,
): () => void {
  let active = true;
  schedule(() => {
    if (active) inspect();
  });
  return () => {
    active = false;
  };
}

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
        mutation
          ? "The connection failed during the change. The outcome is uncertain. Refresh current data before you try another change."
          : "The administration service is offline.",
        {
          code: "offline",
          requestId: null,
          status: 0,
          outcomeUncertain: mutation,
        },
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

  // The caller supplies T because the closed HTTP contract owns each item shape.
  // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-parameters
  async function allPages<T>(
    initialPath: string,
    signal?: AbortSignal,
  ): Promise<{
    readonly items: readonly T[];
    readonly configurationRevision: string | null | undefined;
  }> {
    const items: T[] = [];
    const cursors = new Set<string>();
    let configurationRevision: string | null | undefined;
    let path = initialPath;
    for (let pageNumber = 0; pageNumber < 100; pageNumber += 1) {
      const value = await request<Page<T>>(
        path,
        signal === undefined ? {} : { signal },
      );
      if (value.configuration_revision !== undefined) {
        if (
          configurationRevision !== undefined &&
          value.configuration_revision !== configurationRevision
        ) {
          throw new AdministrationApiError(
            "The configuration changed while its list was loading. Refresh and try again.",
            {
              code: "configuration_revision_conflict",
              requestId: null,
              status: 409,
            },
          );
        }
        configurationRevision = value.configuration_revision;
      }
      items.push(...value.items);
      if (value.next_cursor === null) return { items, configurationRevision };
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
      return allPages<ServiceSummary>("/v1/admin/services", signal).then(
        (result) => result.items,
      );
    },

    listCredentials(signal) {
      return allPages<Credential>("/v1/admin/credentials", signal).then(
        (result) => result.items,
      );
    },

    listCatalog(kind, signal) {
      return allPages<CatalogEntry>(`/v1/admin/catalog/${kind}`, signal).then(
        (result) => result.items,
      );
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
      const budgetPath = servicePath(scope, "budgets", true);
      const results = await Promise.allSettled([
        request<ScopedState>(
          servicePath(scope, "state", true),
          signal === undefined ? {} : { signal },
        ),
        scope.mode === "global" && scope.workspaceId === ""
          ? allPages<Credential>("/v1/admin/credentials", signal)
          : Promise.resolve({ items: [], configurationRevision: undefined }),
        allPages<ProviderInstance>(
          servicePath(scope, "provider-instances"),
          signal,
        ),
        allPages<ProviderModelRoute>(
          servicePath(scope, "provider-model-routes"),
          signal,
        ),
        allPages<Assignment>(servicePath(scope, "assignments", true), signal),
        allPages<RequestStatus>(
          servicePath(scope, "model-requests", true),
          signal,
        ),
        request<AccountingSummary>(
          `${serviceBase}/accounting/summary?${accountingQuery.toString()}`,
          signal === undefined ? {} : { signal },
        ),
        request<BudgetSummary>(
          budgetPath,
          signal === undefined ? {} : { signal },
        ).catch((error: unknown) => {
          if (error instanceof AdministrationApiError && error.status === 404) {
            return null;
          }
          throw error;
        }),
      ] as const);
      const names = [
        "state",
        "credentials",
        "providers",
        "routes",
        "assignments",
        "requests",
        "accounting",
        "budget",
      ] as const;
      const failures: Partial<Record<(typeof names)[number], string>> = {};
      for (const [index, settled] of results.entries()) {
        const result: PromiseSettledResult<unknown> = settled;
        const name = names[index];
        if (name === undefined) {
          throw new Error("The selected administration read is invalid.");
        }
        if (result.status !== "rejected") continue;
        if (
          result.reason instanceof DOMException &&
          result.reason.name === "AbortError"
        ) {
          throw result.reason;
        }
        failures[name] =
          name === "accounting" &&
          result.reason instanceof AdministrationApiError &&
          result.reason.status === 400
            ? "Accounting is not available because this scope has no configured currency."
            : errorMessage(result.reason);
      }
      const state = results[0].status === "fulfilled" ? results[0].value : null;
      const credentials =
        results[1].status === "fulfilled" ? results[1].value.items : [];
      const providers =
        results[2].status === "fulfilled" ? results[2].value.items : [];
      const routes =
        results[3].status === "fulfilled" ? results[3].value.items : [];
      const assignments =
        results[4].status === "fulfilled" ? results[4].value.items : [];
      const requests =
        results[5].status === "fulfilled" ? results[5].value.items : [];
      const accounting =
        results[6].status === "fulfilled" ? results[6].value : null;
      const budget =
        results[7].status === "fulfilled" ? results[7].value : null;
      if (
        state === null &&
        results[7].status === "fulfilled" &&
        results[7].value === null
      ) {
        failures.budget =
          "The budget scope is not available because the exact service or workspace did not load.";
      }
      const configurationRevisions = [
        results[2],
        results[3],
        results[4],
      ].flatMap((result) =>
        result.status === "fulfilled" &&
        result.value.configurationRevision != null
          ? [result.value.configurationRevision]
          : [],
      );
      const configurationRevision = configurationRevisions[0] ?? null;
      if (
        configurationRevisions.some(
          (revision) => revision !== configurationRevision,
        )
      ) {
        failures.providers =
          "The configuration changed while selected data was loading. Refresh the service.";
        failures.routes = failures.providers;
        failures.assignments = failures.providers;
      }
      return {
        state,
        credentials,
        providers,
        routes,
        assignments,
        requests,
        accounting,
        budget,
        configuration_revision: configurationRevision,
        failures,
      };
    },

    getRequest(scope, requestId, signal) {
      return request<RequestStatus>(
        servicePath(
          scope,
          `model-requests/${encodeURIComponent(requestId)}`,
          true,
        ),
        signal === undefined ? {} : { signal },
      );
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

    changeCredential(credentialId, action, input) {
      return request<Credential>(
        `/v1/admin/credentials/${encodeURIComponent(credentialId)}/${action}`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_revision: input.expectedRevision,
            reason: input.reason,
            ...(input.replacementSecret === undefined
              ? {}
              : { replacement_secret: input.replacementSecret }),
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

    putBudget(scope, input) {
      return request<BudgetLimitWriteResult>(
        servicePath(scope, "budgets", true),
        {
          method: "PUT",
          body: JSON.stringify({
            hard_limit: input.hardLimit,
            currency: input.currency,
            warning_threshold: input.warningThreshold,
            reset_period: input.resetPeriod,
            expected_revision: input.expectedRevision,
          }),
        },
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
