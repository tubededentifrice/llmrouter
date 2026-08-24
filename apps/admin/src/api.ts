export type HealthStatus = "healthy" | "degraded" | "unavailable";
export type Outcome = "succeeded" | "failed";
export type InputModality = "text" | "image";
export type OutputModality =
  "text" | "structured_json" | "embedding" | "image" | "video" | "audio";
export type ModelCapability = "tool_calling" | "streaming" | "reasoning";
export type ReasoningLevel = "none" | "low" | "medium" | "high";
export type UsageUnit =
  | "input_token"
  | "output_token"
  | "cached_input_token"
  | "image"
  | "video_second"
  | "audio_second"
  | "request"
  | "provider_unit";
export interface Page<T> {
  readonly items: readonly T[];
  readonly page: {
    readonly has_more: boolean;
    readonly next_cursor?: string | null;
  };
}
export interface AdministratorSession {
  readonly subject: string;
  readonly display_name: string;
  readonly expires_at: string;
  readonly csrf_token: string;
}
export interface Service {
  readonly api_name: string;
  readonly display_name: string;
  readonly parent_service_api_name?: string | null;
  readonly created_at: string;
}
export interface Workspace {
  readonly api_name: string;
  readonly display_name: string;
  readonly created_at: string;
}
export interface ServiceKey {
  readonly id: string;
  readonly name: string;
  readonly created_at: string;
  readonly last_used_at?: string | null;
}
export interface ServiceKeyCreated {
  readonly key: ServiceKey;
  readonly secret: string;
}
export interface AssignmentCandidate {
  readonly provider_model_api_name: string;
}
export type ObservedRequirement =
  | "text_input"
  | "image_input"
  | "text_output"
  | "structured_json_output"
  | "tool_calling"
  | "streaming"
  | "reasoning"
  | "embedding_output"
  | "image_output"
  | "video_output"
  | "audio_output";
export interface Assignment {
  readonly api_name: string;
  readonly display_name: string;
  readonly definition_kind:
    "implicit" | "inherited_assignment" | "direct_chain";
  readonly defined_by_service_api_name?: string | null;
  readonly inherits_assignment_api_name?: string | null;
  readonly direct_chain?: readonly AssignmentCandidate[] | null;
  readonly effective_chain: readonly AssignmentCandidate[];
  readonly reasoning_level?: ReasoningLevel | null;
  readonly observed_requirements: readonly ObservedRequirement[];
  readonly last_used_at?: string | null;
  readonly created_at?: string | null;
}
export interface AssignmentWrite {
  readonly display_name?: string;
  readonly inherits_assignment_api_name?: string;
  readonly direct_chain?: readonly AssignmentCandidate[];
  readonly reasoning_level?: ReasoningLevel | null;
}
export type ProviderAdapter =
  | "openai"
  | "openai_compatible"
  | "openrouter"
  | "custom"
  | "wavespeed"
  | "ollama"
  | "local_embeddings"
  | "fake";
export interface ProviderWrite {
  readonly api_name: string;
  readonly display_name: string;
  readonly adapter: ProviderAdapter;
  readonly endpoint?: string | null;
  readonly credential_api_name?: string | null;
  readonly enabled: boolean;
}
export interface Provider extends ProviderWrite {
  readonly created_at: string;
}
export interface ModelConstraints {
  readonly embedding_dimensions?: readonly number[] | null;
  readonly max_input_images?: number | null;
  readonly max_input_image_bytes?: number | null;
  readonly max_output_duration_seconds?: number | null;
}
export interface UnitPrice {
  readonly unit: UsageUnit;
  readonly amount: string;
}
export interface Price {
  readonly currency: string;
  readonly unit_prices: readonly UnitPrice[];
  readonly source?: string | null;
  readonly synchronized_at?: string | null;
}
export interface ModelWrite {
  readonly api_name: string;
  readonly display_name: string;
  readonly input_modalities: readonly InputModality[];
  readonly output_modalities: readonly OutputModality[];
  readonly capabilities: readonly ModelCapability[];
  readonly constraints?: ModelConstraints | null;
  readonly price_source?: string | null;
  readonly price_lookup_key?: string | null;
  readonly manual_price?: Price | null;
}
export interface Model extends Omit<ModelWrite, "manual_price"> {
  readonly current_price?: Price | null;
  readonly created_at: string;
}
export interface ReasoningMapping {
  readonly level: ReasoningLevel;
  readonly provider_value: string;
}
export interface Cooldown {
  readonly until: string;
  readonly reason: string;
}
export interface ProviderModelWrite {
  readonly api_name: string;
  readonly provider_api_name: string;
  readonly model_api_name: string;
  readonly provider_model_name: string;
  readonly enabled: boolean;
  readonly input_modalities?: readonly InputModality[] | null;
  readonly output_modalities?: readonly OutputModality[] | null;
  readonly capabilities?: readonly ModelCapability[] | null;
  readonly constraints?: ModelConstraints | null;
  readonly reasoning_mappings?: readonly ReasoningMapping[] | null;
  readonly price_source?: string | null;
  readonly price_lookup_key?: string | null;
  readonly manual_price?: Price | null;
}
export interface ProviderModel {
  readonly api_name: string;
  readonly provider_api_name: string;
  readonly model_api_name: string;
  readonly provider_model_name: string;
  readonly enabled: boolean;
  readonly input_modalities: readonly InputModality[];
  readonly output_modalities: readonly OutputModality[];
  readonly capabilities: readonly ModelCapability[];
  readonly constraints?: ModelConstraints | null;
  readonly reasoning_mappings: readonly ReasoningMapping[];
  readonly price_source?: string | null;
  readonly price_lookup_key?: string | null;
  readonly effective_price?: Price | null;
  readonly cooldown?: Cooldown | null;
  readonly created_at: string;
}
export interface Credential {
  readonly api_name: string;
  readonly fingerprint: string;
  readonly created_at: string;
  readonly updated_at: string;
}
export interface ModelImportCandidate {
  readonly catalog_key: string;
  readonly display_name: string;
  readonly provider_model_name: string;
  readonly input_modalities: readonly InputModality[];
  readonly output_modalities: readonly OutputModality[];
  readonly capabilities: readonly ModelCapability[];
  readonly constraints?: ModelConstraints | null;
}
export interface ModelImportPreview {
  readonly provider_api_name: string;
  readonly candidates: readonly ModelImportCandidate[];
}
export interface ModelImportSelection {
  readonly catalog_key: string;
  readonly model_api_name: string;
  readonly provider_model_api_name: string;
}
export interface ModelImportResult {
  readonly models: readonly Model[];
  readonly provider_models: readonly ProviderModel[];
}
export interface PriceSyncItem {
  readonly provider_model_api_name: string;
  readonly outcome: "updated" | "unchanged" | "missing" | "failed";
  readonly price?: Price | null;
  readonly message?: string | null;
}
export interface PriceSyncResult {
  readonly attempted_at: string;
  readonly items: readonly PriceSyncItem[];
}
export interface UsageItem {
  readonly unit: UsageUnit;
  readonly quantity: string;
}
export interface Usage {
  readonly units: readonly UsageItem[];
  readonly cost: string;
  readonly currency: string;
}
export interface StatisticsBucket {
  readonly dimensions: readonly string[];
  readonly calls: number;
  readonly attempts: number;
  readonly units: readonly UsageItem[];
  readonly cost: string;
  readonly currency: string;
}
export interface StatisticsResult {
  readonly from: string;
  readonly to: string;
  readonly group_by: readonly string[];
  readonly buckets: readonly StatisticsBucket[];
}
export interface ActivityEvent {
  readonly id: string;
  readonly actor_subject: string;
  readonly action: string;
  readonly resource_type: string;
  readonly service_api_name?: string | null;
  readonly resource_api_name?: string | null;
  readonly resource_id?: string | null;
  readonly result: Outcome;
  readonly occurred_at: string;
}
export interface RequestLogSummary {
  readonly id: string;
  readonly service_api_name: string;
  readonly workspace_api_name: string;
  readonly assignment_api_name?: string | null;
  readonly provider_model_api_name?: string | null;
  readonly kind: "model" | "embedding" | "media";
  readonly outcome: Outcome;
  readonly tags?: readonly string[] | null;
  readonly started_at: string;
}
export interface SafeError {
  readonly code: string;
  readonly message: string;
  readonly details?: {
    readonly field?: string;
    readonly reason?: string;
  } | null;
}
export interface RequestAttempt {
  readonly provider_model_api_name: string;
  readonly outcome: Outcome;
  readonly started_at: string;
  readonly completed_at?: string | null;
  readonly usage: Usage;
  readonly applied_prices: Price;
  readonly response_json?: string | null;
  readonly error?: SafeError | null;
}
export interface LogMedia {
  readonly id: string;
  readonly media_type: string;
  readonly role: "input" | "output";
  readonly size_bytes: number;
}
export interface RequestLog {
  readonly summary: RequestLogSummary;
  readonly request_json: string;
  readonly response_json?: string | null;
  readonly attempts: readonly RequestAttempt[];
  readonly media?: readonly LogMedia[] | null;
}
export interface AdministratorHealth {
  readonly status: HealthStatus;
  readonly checked_at: string;
  readonly components: readonly {
    readonly name: string;
    readonly status: HealthStatus;
    readonly message?: string | null;
  }[];
}
export interface MediaJob {
  readonly id: string;
  readonly workspace_api_name: string;
  readonly provider_model_api_name: string;
  readonly kind: "image" | "video" | "audio";
  readonly state: "pending" | "running" | "succeeded" | "failed";
  readonly content?: {
    readonly media_type: string;
    readonly size_bytes: number;
  } | null;
  readonly error?: SafeError | null;
  readonly created_at: string;
  readonly completed_at?: string | null;
}
export interface ModelCallResult {
  readonly output_type: "standard" | "structured_json";
  readonly provider_model_api_name: string;
  readonly content?: readonly (
    | { readonly type: "text"; readonly text: string }
    | {
        readonly type: "tool_call";
        readonly id: string;
        readonly name: string;
        readonly arguments_json: string;
      }
  )[];
  readonly structured_output_json?: string;
  readonly usage: Usage;
}
export interface EmbeddingResult {
  readonly provider_model_api_name: string;
  readonly embeddings: readonly {
    readonly index: number;
    readonly values: readonly number[];
  }[];
  readonly usage: Usage;
}

export class AdministrationApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: { readonly field?: string; readonly reason?: string },
  ) {
    super(message);
    this.name = "AdministrationApiError";
  }
}
type Fetcher = typeof fetch;
export const clientDeadlineMilliseconds = {
  administration: 60_000,
  mediaAdmission: 60_000,
  mediaContent: 130_000,
  mediaStatus: 30_000,
  runtimeCall: 16 * 60_000,
} as const;

interface ClientDeadline {
  readonly milliseconds: number;
  readonly message: string;
  readonly reason: string;
}

async function withClientDeadline<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  deadline: ClientDeadline,
  callerSignal?: AbortSignal | null,
): Promise<T> {
  const controller = new AbortController();
  const signal =
    callerSignal === undefined || callerSignal === null
      ? controller.signal
      : AbortSignal.any([callerSignal, controller.signal]);
  let timer: ReturnType<typeof globalThis.setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = globalThis.setTimeout(() => {
      reject(
        new AdministrationApiError(408, "client_timeout", deadline.message, {
          reason: deadline.reason,
        }),
      );
      controller.abort();
    }, deadline.milliseconds);
  });
  try {
    return await Promise.race([operation(signal), timeout]);
  } finally {
    if (timer !== undefined) globalThis.clearTimeout(timer);
  }
}

const administrationDeadline: ClientDeadline = {
  milliseconds: clientDeadlineMilliseconds.administration,
  message: "The Router did not respond before the browser deadline.",
  reason:
    "The browser stopped waiting. Refresh the affected data before you retry a write.",
};
const runtimeCallDeadline: ClientDeadline = {
  milliseconds: clientDeadlineMilliseconds.runtimeCall,
  message: "The runtime call did not finish before the browser deadline.",
  reason:
    "The Router can still complete the call. Check the detailed logs before you submit the same work again.",
};
const mediaAdmissionDeadline: ClientDeadline = {
  milliseconds: clientDeadlineMilliseconds.mediaAdmission,
  message: "The media-job request did not finish before the browser deadline.",
  reason:
    "The Router can still create a media job. Do not submit the same work again until you confirm the first request state.",
};
const mediaStatusDeadline: ClientDeadline = {
  milliseconds: clientDeadlineMilliseconds.mediaStatus,
  message: "The media-job status did not load before the browser deadline.",
  reason: "Query the same media-job status again. Do not create a new job.",
};
const mediaContentDeadline: ClientDeadline = {
  milliseconds: clientDeadlineMilliseconds.mediaContent,
  message: "The media content did not load before the browser deadline.",
  reason: "Download the same media-job content again. Do not create a new job.",
};

async function parseError(response: Response): Promise<AdministrationApiError> {
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    value = null;
  }
  const candidate =
    typeof value === "object" && value !== null && "error" in value
      ? (value as { error?: unknown }).error
      : null;
  if (typeof candidate === "object" && candidate !== null) {
    const body = candidate as {
      code?: unknown;
      message?: unknown;
      details?: unknown;
    };
    return new AdministrationApiError(
      response.status,
      typeof body.code === "string" ? body.code : "internal_error",
      typeof body.message === "string"
        ? body.message
        : "The Router could not complete the operation.",
      typeof body.details === "object" && body.details !== null
        ? body.details
        : undefined,
    );
  }
  return new AdministrationApiError(
    response.status,
    response.status === 401 ? "authentication_required" : "internal_error",
    response.status === 401
      ? "Your administrator session is not active."
      : "The Router could not complete the operation.",
  );
}

function omitTopLevelNulls(value: unknown): unknown {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return value;
  return Object.fromEntries(
    Object.entries(value).filter(([, item]) => item !== null),
  );
}

function query(
  values: Record<string, string | readonly string[] | null | undefined>,
): string {
  const result = new URLSearchParams();
  for (const [name, value] of Object.entries(values))
    if (value !== null && value !== undefined && typeof value !== "string")
      for (const item of value) result.append(name, item);
    else if (typeof value === "string" && value !== "") result.set(name, value);
  const encoded = result.toString();
  return encoded === "" ? "" : `?${encoded}`;
}
const encode = encodeURIComponent;
export interface StatisticsFilters {
  readonly from: string;
  readonly to: string;
  readonly service?: string;
  readonly workspace?: string;
  readonly assignment?: string;
  readonly provider_model?: string;
  readonly outcome?: Outcome;
  readonly tag?: string;
  readonly group_by?: readonly string[];
}
export interface AdministrationClient {
  session(): Promise<AdministratorSession>;
  startSession(returnTo: string): Promise<string>;
  logout(csrf: string): Promise<void>;
  services(): Promise<Page<Service>>;
  createService(
    value: {
      api_name: string;
      display_name: string;
      parent_service_api_name: string | null;
    },
    csrf: string,
  ): Promise<Service>;
  updateService(
    name: string,
    value: { display_name: string; parent_service_api_name: string | null },
    csrf: string,
  ): Promise<Service>;
  deleteService(name: string, csrf: string): Promise<void>;
  workspaces(service: string): Promise<Page<Workspace>>;
  createWorkspace(
    service: string,
    value: { api_name: string; display_name: string },
    csrf: string,
  ): Promise<Workspace>;
  deleteWorkspace(
    service: string,
    workspace: string,
    csrf: string,
  ): Promise<void>;
  keys(service: string): Promise<Page<ServiceKey>>;
  createKey(
    service: string,
    name: string,
    csrf: string,
  ): Promise<ServiceKeyCreated>;
  revokeKey(service: string, keyId: string, csrf: string): Promise<void>;
  assignments(service: string): Promise<Page<Assignment>>;
  putAssignment(
    service: string,
    name: string,
    value: AssignmentWrite,
    csrf: string,
  ): Promise<Assignment>;
  deleteAssignment(service: string, name: string, csrf: string): Promise<void>;
  removeRequirement(
    service: string,
    name: string,
    requirement: ObservedRequirement,
    csrf: string,
  ): Promise<void>;
  providers(): Promise<Page<Provider>>;
  createProvider(value: ProviderWrite, csrf: string): Promise<Provider>;
  putProvider(
    name: string,
    value: ProviderWrite,
    csrf: string,
  ): Promise<Provider>;
  deleteProvider(name: string, csrf: string): Promise<void>;
  models(): Promise<Page<Model>>;
  createModel(value: ModelWrite, csrf: string): Promise<Model>;
  putModel(name: string, value: ModelWrite, csrf: string): Promise<Model>;
  deleteModel(name: string, csrf: string): Promise<void>;
  providerModels(): Promise<Page<ProviderModel>>;
  createProviderModel(
    value: ProviderModelWrite,
    csrf: string,
  ): Promise<ProviderModel>;
  putProviderModel(
    name: string,
    value: ProviderModelWrite,
    csrf: string,
  ): Promise<ProviderModel>;
  deleteProviderModel(name: string, csrf: string): Promise<void>;
  credentials(): Promise<Page<Credential>>;
  createCredential(
    name: string,
    secret: string,
    csrf: string,
  ): Promise<Credential>;
  replaceCredential(
    name: string,
    secret: string,
    csrf: string,
  ): Promise<Credential>;
  deleteCredential(name: string, csrf: string): Promise<void>;
  previewImport(provider: string, csrf: string): Promise<ModelImportPreview>;
  importModels(
    provider: string,
    selections: readonly ModelImportSelection[],
    csrf: string,
  ): Promise<ModelImportResult>;
  synchronizePrices(
    names: readonly string[] | null,
    csrf: string,
  ): Promise<PriceSyncResult>;
  activity(from: string, to: string): Promise<Page<ActivityEvent>>;
  statistics(filters: StatisticsFilters): Promise<StatisticsResult>;
  requestLogs(from: string, to: string): Promise<Page<RequestLogSummary>>;
  requestLog(id: string): Promise<RequestLog>;
  requestLogMedia(id: string, mediaId: string): Promise<Blob>;
  retention(): Promise<{ readonly duration_days: number }>;
  putRetention(
    days: number,
    csrf: string,
  ): Promise<{ readonly duration_days: number }>;
  health(): Promise<AdministratorHealth>;
}
export function createAdministrationClient(
  fetcher: Fetcher = fetch,
): AdministrationClient {
  const listLimit = 200;
  const maximumListPages = 100;
  const maximumListItems = listLimit * maximumListPages;
  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body !== undefined)
      headers.set("Content-Type", "application/json");
    return withClientDeadline(
      async (signal) => {
        const response = await fetcher(path, {
          ...init,
          cache: "no-store",
          credentials: "same-origin",
          headers,
          signal,
        });
        if (!response.ok) throw await parseError(response);
        if (response.status === 204) return undefined as T;
        return (await response.json()) as T;
      },
      administrationDeadline,
      init.signal,
    );
  }
  const write = <T>(
    path: string,
    method: "POST" | "PUT" | "DELETE",
    csrf: string,
    body?: unknown,
  ) =>
    request<T>(path, {
      method,
      headers: { "X-CSRF-Token": csrf },
      ...(body === undefined
        ? {}
        : { body: JSON.stringify(omitTopLevelNulls(body)) }),
    });
  const invalidListResponse = (reason: string) =>
    new AdministrationApiError(
      502,
      "invalid_response",
      "The Router returned an invalid list response.",
      { reason },
    );
  async function allPages<T>(
    path: string,
    filters: Record<string, string | readonly string[] | null | undefined> = {},
  ): Promise<Page<T>> {
    return withClientDeadline(async (signal) => {
      const items: T[] = [];
      const seenCursors = new Set<string>();
      let cursor: string | undefined;
      for (let pageIndex = 0; pageIndex < maximumListPages; pageIndex += 1) {
        const response: unknown = await request(
          `${path}${query({ ...filters, limit: String(listLimit), cursor })}`,
          { signal },
        );
        if (
          typeof response !== "object" ||
          response === null ||
          !("items" in response) ||
          !Array.isArray(response.items) ||
          !("page" in response) ||
          typeof response.page !== "object" ||
          response.page === null ||
          !("has_more" in response.page) ||
          typeof response.page.has_more !== "boolean" ||
          ("next_cursor" in response.page &&
            response.page.next_cursor !== null &&
            typeof response.page.next_cursor !== "string")
        )
          throw invalidListResponse(
            "The list page does not match the native cursor contract.",
          );
        const current = response as Page<T>;
        if (current.items.length > listLimit)
          throw invalidListResponse(
            `The list page exceeds the requested ${String(listLimit)} item limit.`,
          );
        items.push(...current.items);
        if (items.length > maximumListItems)
          throw invalidListResponse(
            `The list exceeds the ${String(maximumListItems)} item safety limit. Narrow the scope and try again.`,
          );
        if (items.length === maximumListItems && current.page.has_more)
          throw invalidListResponse(
            `The list reaches the ${String(maximumListItems)} item safety limit and has more items. Narrow the scope and try again.`,
          );
        if (!current.page.has_more)
          return {
            items,
            page: { has_more: false, next_cursor: null },
          };
        const nextCursor = current.page.next_cursor;
        if (
          nextCursor === undefined ||
          nextCursor === null ||
          nextCursor === ""
        )
          throw invalidListResponse(
            "The list says that more items exist, but it has no next cursor.",
          );
        if (seenCursors.has(nextCursor))
          throw invalidListResponse(
            "The list repeated a cursor and could not make progress.",
          );
        seenCursors.add(nextCursor);
        cursor = nextCursor;
      }
      throw invalidListResponse(
        `The list exceeds the ${String(maximumListPages)} page safety limit. Narrow the scope and try again.`,
      );
    }, administrationDeadline);
  }
  return {
    session: () => request("/v1/admin/session"),
    async startSession(returnTo) {
      const result = await request<{ authorization_url: string }>(
        "/v1/admin/session/start",
        { method: "POST", body: JSON.stringify({ return_to: returnTo }) },
      );
      return result.authorization_url;
    },
    logout: (csrf) => write("/v1/admin/session", "DELETE", csrf),
    services: () => allPages("/v1/admin/services"),
    createService: (value, csrf) =>
      write("/v1/admin/services", "POST", csrf, value),
    updateService: (name, value, csrf) =>
      write(`/v1/admin/services/${encode(name)}`, "PUT", csrf, value),
    deleteService: (name, csrf) =>
      write(`/v1/admin/services/${encode(name)}`, "DELETE", csrf),
    workspaces: (service) =>
      allPages(`/v1/admin/services/${encode(service)}/workspaces`),
    createWorkspace: (service, value, csrf) =>
      write(
        `/v1/admin/services/${encode(service)}/workspaces`,
        "POST",
        csrf,
        value,
      ),
    deleteWorkspace: (service, workspace, csrf) =>
      write(
        `/v1/admin/services/${encode(service)}/workspaces/${encode(workspace)}`,
        "DELETE",
        csrf,
      ),
    keys: (service) => allPages(`/v1/admin/services/${encode(service)}/keys`),
    createKey: (service, name, csrf) =>
      write(`/v1/admin/services/${encode(service)}/keys`, "POST", csrf, {
        name,
      }),
    revokeKey: (service, id, csrf) =>
      write(
        `/v1/admin/services/${encode(service)}/keys/${encode(id)}`,
        "DELETE",
        csrf,
      ),
    assignments: (service) =>
      allPages(`/v1/admin/services/${encode(service)}/assignments`),
    putAssignment: (service, name, value, csrf) =>
      write(
        `/v1/admin/services/${encode(service)}/assignments/${encode(name)}`,
        "PUT",
        csrf,
        value,
      ),
    deleteAssignment: (service, name, csrf) =>
      write(
        `/v1/admin/services/${encode(service)}/assignments/${encode(name)}`,
        "DELETE",
        csrf,
      ),
    removeRequirement: (service, name, requirement, csrf) =>
      write(
        `/v1/admin/services/${encode(service)}/assignments/${encode(name)}/observed-requirements/${encode(requirement)}`,
        "DELETE",
        csrf,
      ),
    providers: () => allPages("/v1/admin/providers"),
    createProvider: (value, csrf) =>
      write("/v1/admin/providers", "POST", csrf, value),
    putProvider: (name, value, csrf) =>
      write(`/v1/admin/providers/${encode(name)}`, "PUT", csrf, value),
    deleteProvider: (name, csrf) =>
      write(`/v1/admin/providers/${encode(name)}`, "DELETE", csrf),
    models: () => allPages("/v1/admin/models"),
    createModel: (value, csrf) =>
      write("/v1/admin/models", "POST", csrf, value),
    putModel: (name, value, csrf) =>
      write(`/v1/admin/models/${encode(name)}`, "PUT", csrf, value),
    deleteModel: (name, csrf) =>
      write(`/v1/admin/models/${encode(name)}`, "DELETE", csrf),
    providerModels: () => allPages("/v1/admin/provider-models"),
    createProviderModel: (value, csrf) =>
      write("/v1/admin/provider-models", "POST", csrf, value),
    putProviderModel: (name, value, csrf) =>
      write(`/v1/admin/provider-models/${encode(name)}`, "PUT", csrf, value),
    deleteProviderModel: (name, csrf) =>
      write(`/v1/admin/provider-models/${encode(name)}`, "DELETE", csrf),
    credentials: () => allPages("/v1/admin/credentials"),
    createCredential: (name, secret, csrf) =>
      write("/v1/admin/credentials", "POST", csrf, { api_name: name, secret }),
    replaceCredential: (name, secret, csrf) =>
      write(`/v1/admin/credentials/${encode(name)}`, "PUT", csrf, {
        api_name: name,
        secret,
      }),
    deleteCredential: (name, csrf) =>
      write(`/v1/admin/credentials/${encode(name)}`, "DELETE", csrf),
    previewImport: (provider, csrf) =>
      write("/v1/admin/model-imports/preview", "POST", csrf, {
        provider_api_name: provider,
      }),
    importModels: (provider, selections, csrf) =>
      write("/v1/admin/model-imports", "POST", csrf, {
        provider_api_name: provider,
        selections,
      }),
    synchronizePrices: (names, csrf) =>
      write("/v1/admin/prices/synchronize", "POST", csrf, {
        provider_model_api_names: names,
      }),
    activity: (from, to) => allPages("/v1/admin/activity", { from, to }),
    statistics: (filters) =>
      request(
        `/v1/admin/statistics${query({ from: filters.from, to: filters.to, service: filters.service, workspace: filters.workspace, assignment: filters.assignment, provider_model: filters.provider_model, outcome: filters.outcome, tag: filters.tag, group_by: filters.group_by })}`,
      ),
    requestLogs: (from, to) => allPages("/v1/admin/request-logs", { from, to }),
    requestLog: (id) => request(`/v1/admin/request-logs/${encode(id)}`),
    async requestLogMedia(id, mediaId) {
      return withClientDeadline(async (signal) => {
        const response = await fetcher(
          `/v1/admin/request-logs/${encode(id)}/media/${encode(mediaId)}/content`,
          { cache: "no-store", credentials: "same-origin", signal },
        );
        if (!response.ok) throw await parseError(response);
        return response.blob();
      }, mediaContentDeadline);
    },
    retention: () => request("/v1/admin/settings/log-retention"),
    putRetention: (days, csrf) =>
      write("/v1/admin/settings/log-retention", "PUT", csrf, {
        duration_days: days,
      }),
    health: () => request("/v1/admin/health"),
  };
}
export interface RuntimeClient {
  model(
    workspace: string,
    selector:
      { assignment_api_name: string } | { provider_model_api_name: string },
    prompt: string,
    systemPrompt: string,
    inputImages: readonly RuntimeInputImage[],
    temperature: number | null,
    outputLimit: number | null,
    tags: readonly string[],
  ): Promise<ModelCallResult>;
  embedding(
    workspace: string,
    selector:
      { assignment_api_name: string } | { provider_model_api_name: string },
    inputs: readonly string[],
    tags: readonly string[],
  ): Promise<EmbeddingResult>;
  createMedia(
    workspace: string,
    selector:
      { assignment_api_name: string } | { provider_model_api_name: string },
    kind: "image" | "video" | "audio",
    prompt: string,
    inputImages: readonly RuntimeInputImage[],
    tags: readonly string[],
  ): Promise<MediaJob>;
  mediaJob(id: string): Promise<MediaJob>;
  mediaContent(id: string): Promise<Blob>;
}
export interface RuntimeInputImage {
  readonly media_type: "image/jpeg" | "image/png" | "image/webp";
  readonly data_base64: string;
}
export function createRuntimeClient(
  serviceKey: string,
  fetcher: Fetcher = fetch,
): RuntimeClient {
  async function request<T>(
    path: string,
    init: RequestInit = {},
    deadline: ClientDeadline = runtimeCallDeadline,
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    headers.set("Authorization", `Bearer ${serviceKey}`);
    return withClientDeadline(
      async (signal) => {
        const response = await fetcher(path, {
          ...init,
          cache: "no-store",
          credentials: "omit",
          headers,
          signal,
        });
        if (!response.ok) throw await parseError(response);
        return (await response.json()) as T;
      },
      deadline,
      init.signal,
    );
  }
  const post = <T>(
    path: string,
    body: unknown,
    deadline = runtimeCallDeadline,
  ) =>
    request<T>(
      path,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      deadline,
    );
  return {
    model: (
      workspace,
      selector,
      prompt,
      systemPrompt,
      inputImages,
      temperature,
      outputLimit,
      tags,
    ) =>
      post("/v1/model-calls", {
        workspace_api_name: workspace,
        selector,
        messages: [
          ...(systemPrompt === ""
            ? []
            : [{ role: "system", content: systemPrompt }]),
          {
            role: "user",
            content: [
              { type: "text", text: prompt },
              ...inputImages.map((image) => ({
                type: "image",
                media_type: image.media_type,
                data_base64: image.data_base64,
              })),
            ],
          },
        ],
        ...(temperature === null ? {} : { temperature }),
        ...(outputLimit === null ? {} : { output_limit: outputLimit }),
        ...(tags.length === 0 ? {} : { tags }),
      }),
    embedding: (workspace, selector, inputs, tags) =>
      post("/v1/embeddings", {
        workspace_api_name: workspace,
        selector,
        inputs,
        ...(tags.length === 0 ? {} : { tags }),
      }),
    createMedia: (workspace, selector, kind, prompt, inputImages, tags) =>
      post(
        "/v1/media-jobs",
        {
          workspace_api_name: workspace,
          selector,
          kind,
          prompt,
          ...(inputImages.length === 0
            ? {}
            : {
                input_images: inputImages.map((image) => ({
                  media_type: image.media_type,
                  data_base64: image.data_base64,
                  type: "image",
                })),
              }),
          ...(tags.length === 0 ? {} : { tags }),
        },
        mediaAdmissionDeadline,
      ),
    mediaJob: (id) =>
      request(`/v1/media-jobs/${encode(id)}`, {}, mediaStatusDeadline),
    async mediaContent(id) {
      return withClientDeadline(async (signal) => {
        const response = await fetcher(`/v1/media-jobs/${encode(id)}/content`, {
          cache: "no-store",
          credentials: "omit",
          headers: { Authorization: `Bearer ${serviceKey}` },
          signal,
        });
        if (!response.ok) throw await parseError(response);
        return response.blob();
      }, mediaContentDeadline);
    },
  };
}
export function errorMessage(error: unknown): string {
  if (error instanceof AdministrationApiError)
    return error.details?.reason === undefined
      ? error.message
      : `${error.message} ${error.details.reason}`;
  return "The Router could not complete the operation. Try again.";
}
export function isoRange(
  days = 7,
  now = new Date(),
): { readonly from: string; readonly to: string } {
  return {
    from: new Date(now.getTime() - days * 86_400_000).toISOString(),
    to: now.toISOString(),
  };
}
