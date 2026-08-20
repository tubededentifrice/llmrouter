export type AdministrationMode = "global" | "service";

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
}

const jsonHeaders = { "Content-Type": "application/json" } as const;

function randomKey(): string {
  return (
    globalThis.crypto?.randomUUID() ??
    `${Date.now()}-${Math.random().toString(36).slice(2)}-administration`
  );
}

export function createFetchAdministrationClient({
  baseUrl = "",
  csrfToken: suppliedCsrfToken,
  fetcher = globalThis.fetch.bind(globalThis),
  now = () => new Date(),
}: FetchAdministrationClientOptions = {}): AdministrationClient {
  let csrfToken = suppliedCsrfToken ?? null;

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

  function servicePath(scope: ScopeSelection, suffix: string): string {
    const [suffixPath, suffixQuery] = suffix.split("?", 2);
    const query = new URLSearchParams(suffixQuery ?? "");
    if (scope.workspaceId !== "") query.set("workspace_id", scope.workspaceId);
    const encodedService = encodeURIComponent(scope.serviceId);
    const serialized = query.toString();
    return `/v1/admin/services/${encodedService}/${suffixPath}${serialized === "" ? "" : `?${serialized}`}`;
  }

  function configurationPath(
    scope: ScopeSelection,
    collection: string,
    id: string | null,
  ): string {
    const base = servicePath(scope, collection);
    if (id === null) return base;
    const [path, query] = base.split("?", 2);
    return `${path}/${encodeURIComponent(id)}${query === undefined ? "" : `?${query}`}`;
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

  return {
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
          servicePath(scope, "state"),
          signal === undefined ? {} : { signal },
        ),
        scope.mode === "global"
          ? page<Credential>("/v1/admin/credentials?limit=100", signal)
          : Promise.resolve([]),
        page<ProviderInstance>(
          servicePath(scope, "provider-instances?limit=100"),
          signal,
        ),
        page<ProviderModelRoute>(
          servicePath(scope, "provider-model-routes?limit=100"),
          signal,
        ),
        page<Assignment>(servicePath(scope, "assignments?limit=100"), signal),
        page<RequestStatus>(
          servicePath(scope, "model-requests?limit=100"),
          signal,
        ),
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
        configurationPath(scope, "assignments", name),
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
