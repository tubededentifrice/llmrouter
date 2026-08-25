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
  /** Client-side collection facts. Omitted only by test or embedded clients. */
  readonly retrieval?: {
    readonly complete: boolean;
    readonly loaded_items: number;
    readonly loaded_pages: number;
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
  readonly max_context_tokens?: number | null;
  readonly max_output_tokens?: number | null;
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
  readonly configured_price_source?: string | null;
  readonly configured_price_lookup_key?: string | null;
  readonly configured_manual_price?: Price | null;
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
export type OpenRouterSupportedConstraint =
  | "maximum_output_tokens"
  | "temperature"
  | "top_p"
  | "top_k"
  | "min_p"
  | "seed"
  | "stop"
  | "frequency_penalty"
  | "presence_penalty"
  | "repetition_penalty"
  | "logit_bias"
  | "logprobs"
  | "top_logprobs";
export interface OpenRouterReasoningPreview {
  readonly supported: boolean;
  readonly mandatory?: boolean | null;
  readonly source_configuration_available: boolean;
  readonly default_enabled?: boolean | null;
  readonly default_effort?: string | null;
  readonly supported_efforts?: readonly string[] | null;
  readonly supports_max_tokens?: boolean | null;
}
export interface OpenRouterImportIssue {
  readonly code:
    | "display_name_shortened"
    | "input_modality_unsupported"
    | "output_modality_unsupported"
    | "embedding_dimensions_unknown"
    | "media_duration_unknown"
    | "reasoning_mapping_incomplete"
    | "price_unit_unsupported"
    | "source_price_zero_omitted"
    | "conditional_price_unsupported"
    | "router_input_limits_applied";
  readonly field: string;
  readonly source_value?: string | null;
  readonly message: string;
}
export interface OpenRouterImportConflict {
  readonly kind: "model" | "provider_model";
  readonly api_name: string;
  readonly provider_api_name?: string | null;
  readonly message: string;
}
export interface OpenRouterProviderModelOption {
  readonly provider_api_name: string;
  readonly provider_display_name: string;
  readonly provider_enabled: boolean;
  readonly selectable: boolean;
  readonly unavailable_reason?: string | null;
  readonly provider_model: ProviderModelWrite;
}
export interface OpenRouterModelImportPreview {
  readonly source_model_id: string;
  readonly model: ModelWrite;
  readonly reviewed_price?: Price | null;
  readonly reasoning: OpenRouterReasoningPreview;
  readonly supported_constraints: readonly OpenRouterSupportedConstraint[];
  readonly provider_options: readonly OpenRouterProviderModelOption[];
  readonly conflicts: readonly OpenRouterImportConflict[];
  readonly issues: readonly OpenRouterImportIssue[];
  readonly can_confirm: boolean;
}
export interface OpenRouterModelImportRequest {
  readonly source_model_id: string;
  readonly model: ModelWrite;
  readonly reviewed_price?: Price | null;
  readonly provider_models: readonly ProviderModelWrite[];
}
export interface OpenRouterModelImportResult {
  readonly source_model_id: string;
  readonly model: Model;
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
interface RequestLogSummaryBase {
  readonly id: string;
  readonly logical_call_id: string;
  readonly kind: "model" | "embedding" | "media";
  readonly outcome: Outcome;
  readonly tags?: readonly string[];
  readonly started_at: string;
}
export type RequestLogSummary =
  | (RequestLogSummaryBase & {
      readonly call_actor: "service";
      readonly service_api_name: string;
      readonly workspace_api_name: string;
      readonly administrator_subject?: never;
      readonly configuration_service_api_name?: never;
      readonly assignment_api_name?: string;
      readonly provider_model_api_name?: string;
    })
  | (RequestLogSummaryBase & {
      readonly call_actor: "administrator";
      readonly administrator_subject: string;
      readonly provider_model_api_name: string;
      readonly service_api_name?: never;
      readonly workspace_api_name?: never;
      readonly assignment_api_name?: never;
      readonly configuration_service_api_name?: never;
    })
  | (RequestLogSummaryBase & {
      readonly call_actor: "administrator";
      readonly administrator_subject: string;
      readonly assignment_api_name: string;
      readonly configuration_service_api_name: string;
      readonly provider_model_api_name?: string;
      readonly service_api_name?: never;
      readonly workspace_api_name?: never;
    });
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
  readonly completed_at: string;
  readonly usage?: Usage;
  readonly applied_prices: Price;
  readonly response_json?: string;
  readonly error?: SafeError;
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
  readonly response_json?: string;
  readonly attempts: readonly RequestAttempt[];
  readonly media?: readonly LogMedia[];
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
export type ModelCallResult =
  | {
      readonly output_type: "standard";
      readonly provider_model_api_name: string;
      readonly content: readonly (
        | { readonly type: "text"; readonly text: string }
        | {
            readonly type: "tool_call";
            readonly id: string;
            readonly name: string;
            readonly arguments_json: string;
          }
      )[];
      readonly usage: Usage;
      readonly structured_output_json?: never;
    }
  | {
      readonly output_type: "structured_json";
      readonly provider_model_api_name: string;
      readonly structured_output_json: string;
      readonly usage: Usage;
      readonly content?: never;
    };
export interface EmbeddingResult {
  readonly provider_model_api_name: string;
  readonly embeddings: readonly {
    readonly index: number;
    readonly values: readonly number[];
  }[];
  readonly usage: Usage;
}

export type AdministratorPlaygroundSelector =
  | {
      readonly assignment_api_name: string;
      readonly service_api_name: string;
      readonly provider_model_api_name?: never;
    }
  | {
      readonly provider_model_api_name: string;
      readonly assignment_api_name?: never;
      readonly service_api_name?: never;
    };

export interface RuntimeInputImage {
  readonly media_type: "image/jpeg" | "image/png" | "image/webp";
  readonly data_base64: string;
}

export interface PlaygroundToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly input_schema_json: string;
}

export interface AdministratorPlaygroundModelRequest {
  readonly selector: AdministratorPlaygroundSelector;
  readonly messages: readonly (
    | { readonly role: "system"; readonly content: string }
    | {
        readonly role: "user";
        readonly content: readonly (
          | { readonly type: "text"; readonly text: string }
          | ({ readonly type: "image" } & RuntimeInputImage)
        )[];
      }
  )[];
  readonly tools?: readonly PlaygroundToolDefinition[];
  readonly output_format?:
    | { readonly type: "text" }
    | { readonly type: "json_schema"; readonly schema_json: string };
  readonly output_limit?: number;
  readonly temperature?: number;
  readonly tags?: readonly string[];
}

export interface AdministratorPlaygroundEmbeddingRequest {
  readonly selector: AdministratorPlaygroundSelector;
  readonly inputs: readonly string[];
  readonly tags?: readonly string[];
}

interface AdministratorPlaygroundMediaRequestBase {
  readonly selector: AdministratorPlaygroundSelector;
  readonly prompt: string;
  readonly tags?: readonly string[];
}

export type AdministratorPlaygroundMediaRequest =
  | (AdministratorPlaygroundMediaRequestBase & {
      readonly kind: "image" | "video";
      readonly input_images?: readonly ({
        readonly type: "image";
      } & RuntimeInputImage)[];
    })
  | (AdministratorPlaygroundMediaRequestBase & {
      readonly kind: "audio";
      readonly input_images?: never;
    });

export interface AdministratorPlaygroundAttempt {
  readonly provider_model_api_name: string;
  readonly outcome: Outcome;
  readonly elapsed_ms: number;
  readonly usage?: Usage;
  readonly error?: SafeError;
}

interface AdministratorPlaygroundResultFacts {
  readonly logical_call_id: string;
  readonly selector: AdministratorPlaygroundSelector;
  readonly elapsed_ms: number;
  readonly attempts: readonly AdministratorPlaygroundAttempt[];
}

export interface AdministratorPlaygroundModelResult extends AdministratorPlaygroundResultFacts {
  readonly result: ModelCallResult;
}

export interface AdministratorPlaygroundEmbeddingResult extends AdministratorPlaygroundResultFacts {
  readonly result: EmbeddingResult;
}

export interface AdministratorPlaygroundStreamResult extends AdministratorPlaygroundResultFacts {
  readonly provider_model_api_name: string;
  readonly content: readonly (
    | { readonly type: "text"; readonly text: string }
    | {
        readonly type: "tool_call";
        readonly id: string;
        readonly name: string;
        readonly arguments_json: string;
      }
  )[];
  readonly usage: Usage;
}

export interface AdministratorPlaygroundMediaJob {
  readonly id: string;
  readonly logical_call_id: string;
  readonly selector: AdministratorPlaygroundSelector;
  readonly provider_model_api_name: string;
  readonly kind: "image" | "video" | "audio";
  readonly state: "pending" | "running" | "succeeded" | "failed";
  readonly attempts: readonly AdministratorPlaygroundAttempt[];
  readonly elapsed_ms?: number;
  readonly usage?: Usage;
  readonly content?: {
    readonly media_type: string;
    readonly size_bytes: number;
  };
  readonly error?: SafeError;
  readonly created_at: string;
  readonly completed_at?: string;
}

export interface AdministratorPlaygroundErrorContext {
  readonly logical_call_id?: string;
  readonly selector?: AdministratorPlaygroundSelector;
  readonly elapsed_ms?: number;
  readonly attempts?: readonly AdministratorPlaygroundAttempt[];
}

export class AdministrationApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: { readonly field?: string; readonly reason?: string },
    readonly context?: AdministratorPlaygroundErrorContext,
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
  playgroundMediaPoll: 16 * 60_000,
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
  try {
    const envelope = requireClosedObject(
      value,
      ["error"],
      ["logical_call_id", "selector", "elapsed_ms", "attempts"],
      "administrator error envelope",
    );
    const error = parseStreamError(envelope.error);
    const contextKeys = [
      "logical_call_id",
      "selector",
      "elapsed_ms",
      "attempts",
    ] as const;
    const presentContextKeys = contextKeys.filter((key) => key in envelope);
    if (
      presentContextKeys.length !== 0 &&
      presentContextKeys.length !== contextKeys.length
    )
      throw invalidStream("The administrator error context is incomplete.");
    let context: AdministratorPlaygroundErrorContext | undefined;
    if (presentContextKeys.length !== 0) {
      if (
        !isBoundedString(envelope.logical_call_id, 200) ||
        !isIntegerInRange(envelope.elapsed_ms, 0, 900_000)
      )
        throw invalidStream("The administrator error context is invalid.");
      const selector = parseAdministratorSelector(envelope.selector);
      const attempts = parseStreamAttempts(
        envelope.attempts,
        16,
        false,
        "provider_model_api_name" in selector,
      );
      if (
        "provider_model_api_name" in selector &&
        attempts.some(
          (attempt) =>
            attempt.provider_model_api_name !==
            selector.provider_model_api_name,
        )
      )
        throw invalidStream(
          "The exact administrator error contains an attempt for a different route.",
        );
      context = {
        logical_call_id: envelope.logical_call_id,
        selector,
        elapsed_ms: envelope.elapsed_ms,
        attempts,
      };
    }
    return new AdministrationApiError(
      response.status,
      error.code,
      error.message,
      error.details ?? undefined,
      context,
    );
  } catch (error) {
    if (error instanceof AdministrationApiError)
      return new AdministrationApiError(
        response.status,
        "invalid_response",
        "The Router returned an invalid error envelope.",
        error.details,
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

interface ServerSentEvent {
  readonly event: string;
  readonly data: unknown;
}

export interface AdministratorStreamLimits {
  readonly pendingEventBytes: number;
  readonly eventCount: number;
  readonly textOutputBytes: number;
  readonly contentBytes: number;
  readonly toolCallCount: number;
  readonly toolIdBytes: number;
  readonly toolNameBytes: number;
  readonly toolArgumentsBytes: number;
  readonly terminalAttempts: number;
}

export const administratorStreamLimits: AdministratorStreamLimits = {
  pendingEventBytes: 64 * 1024 * 1024,
  eventCount: 1_100_010,
  textOutputBytes: 64 * 1024 * 1024,
  contentBytes: 64 * 1024 * 1024,
  toolCallCount: 65_536,
  toolIdBytes: 800,
  toolNameBytes: 800,
  toolArgumentsBytes: 4_000_000,
  terminalAttempts: 16,
};

function invalidStream(reason: string): AdministrationApiError {
  return new AdministrationApiError(
    502,
    "invalid_response",
    "The Router returned an invalid model stream.",
    { reason },
  );
}

function parseServerSentEvent(block: string): ServerSentEvent {
  const lines = block.split(/\r?\n/);
  if (
    lines.length !== 2 ||
    !lines[0]?.startsWith("event:") ||
    !lines[1]?.startsWith("data:")
  )
    throw invalidStream(
      "One stream event must have exactly one event line followed by one data line.",
    );
  const eventName = lines[0].slice(6).trim();
  const data = lines[1].slice(5).trimStart();
  if (eventName === "" || data === "")
    throw invalidStream("One stream event is missing its event or data value.");
  try {
    return { event: eventName, data: JSON.parse(data) };
  } catch {
    throw invalidStream("One stream event contains invalid JSON data.");
  }
}

function objectValue(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw invalidStream("One stream event has an invalid data object.");
  return value as Record<string, unknown>;
}

function requireClosedObject(
  value: unknown,
  required: readonly string[],
  optional: readonly string[] = [],
  description: string,
): Record<string, unknown> {
  const object = objectValue(value);
  const allowed = new Set([...required, ...optional]);
  if (
    required.some((key) => !(key in object)) ||
    Object.keys(object).some((key) => !allowed.has(key))
  )
    throw invalidStream(`The ${description} does not match its closed schema.`);
  return object;
}

const apiNamePattern = /^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const assignmentNamePattern = /^[a-z0-9][a-z0-9._-]{0,126}$/;
const decimalPattern = /^[0-9]+(?:\.[0-9]+)?$/;
const currencyPattern = /^[A-Z]{3}$/;
const usageUnits = new Set<UsageUnit>([
  "input_token",
  "output_token",
  "cached_input_token",
  "image",
  "video_second",
  "audio_second",
  "request",
  "provider_unit",
]);
const streamErrorCodes = new Set([
  "authentication_required",
  "permission_denied",
  "invalid_request",
  "not_found",
  "conflict",
  "assignment_cycle",
  "provider_unavailable",
  "upstream_failed",
  "content_unavailable",
  "rate_limited",
  "internal_error",
]);

function isBoundedString(value: unknown, maximum: number): value is string {
  return (
    typeof value === "string" && value.length > 0 && value.length <= maximum
  );
}

function isIntegerInRange(
  value: unknown,
  minimum: number,
  maximum: number,
): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    Number.isInteger(value) &&
    value >= minimum &&
    value <= maximum
  );
}

function selectorKey(value: unknown): string {
  const selector = objectValue(value);
  if ("provider_model_api_name" in selector) {
    requireClosedObject(
      selector,
      ["provider_model_api_name"],
      [],
      "model stream selector",
    );
    if (
      typeof selector.provider_model_api_name !== "string" ||
      !apiNamePattern.test(selector.provider_model_api_name)
    )
      throw invalidStream("One stream event has an invalid selector.");
    return `provider-model:${selector.provider_model_api_name}`;
  }
  requireClosedObject(
    selector,
    ["assignment_api_name", "service_api_name"],
    [],
    "model stream selector",
  );
  if (
    typeof selector.assignment_api_name !== "string" ||
    !assignmentNamePattern.test(selector.assignment_api_name) ||
    typeof selector.service_api_name !== "string" ||
    !apiNamePattern.test(selector.service_api_name)
  )
    throw invalidStream("One stream event has an invalid selector.");
  return `assignment:${selector.service_api_name}:${selector.assignment_api_name}`;
}

function parseAdministratorSelector(
  value: unknown,
): AdministratorPlaygroundSelector {
  selectorKey(value);
  return value as AdministratorPlaygroundSelector;
}

function parseStreamError(value: unknown): SafeError {
  const error = requireClosedObject(
    value,
    ["code", "message"],
    ["details"],
    "model stream error",
  );
  if (
    typeof error.code !== "string" ||
    !streamErrorCodes.has(error.code) ||
    !isBoundedString(error.message, 1_000)
  )
    throw invalidStream("The model stream contains an invalid error.");
  let details: SafeError["details"];
  if (error.details !== undefined) {
    const parsed = requireClosedObject(
      error.details,
      [],
      ["field", "reason"],
      "model stream error details",
    );
    if (
      (parsed.field !== undefined && !isBoundedString(parsed.field, 200)) ||
      (parsed.reason !== undefined && !isBoundedString(parsed.reason, 500))
    )
      throw invalidStream("The model stream contains invalid error details.");
    details = {
      ...(typeof parsed.field === "string" ? { field: parsed.field } : {}),
      ...(typeof parsed.reason === "string" ? { reason: parsed.reason } : {}),
    };
  }
  return {
    code: error.code,
    message: error.message,
    ...(details === undefined ? {} : { details }),
  };
}

function parseStreamUsage(value: unknown): Usage {
  const usage = requireClosedObject(
    value,
    ["units", "cost", "currency"],
    [],
    "model stream usage",
  );
  if (
    !Array.isArray(usage.units) ||
    usage.units.length > 128 ||
    typeof usage.cost !== "string" ||
    usage.cost.length > 200 ||
    !decimalPattern.test(usage.cost) ||
    typeof usage.currency !== "string" ||
    !currencyPattern.test(usage.currency)
  )
    throw invalidStream("The model stream contains invalid usage.");
  const units = usage.units.map((value) => {
    const item = requireClosedObject(
      value,
      ["unit", "quantity"],
      [],
      "model stream usage item",
    );
    if (
      typeof item.unit !== "string" ||
      !usageUnits.has(item.unit as UsageUnit) ||
      typeof item.quantity !== "string" ||
      !decimalPattern.test(item.quantity)
    )
      throw invalidStream("The model stream contains an invalid usage item.");
    return { unit: item.unit as UsageUnit, quantity: item.quantity };
  });
  return { units, cost: usage.cost, currency: usage.currency };
}

function parseStreamAttempts(
  value: unknown,
  maximum: number,
  successful: boolean,
  exact: boolean,
  elapsedMaximum = 600_000,
): readonly AdministratorPlaygroundAttempt[] {
  if (
    !Array.isArray(value) ||
    value.length > maximum ||
    (exact && value.length > 1) ||
    (successful && value.length === 0)
  )
    throw invalidStream("The model stream contains invalid attempts.");
  const attempts = value.map((value) => {
    const attempt = requireClosedObject(
      value,
      ["provider_model_api_name", "outcome", "elapsed_ms"],
      ["usage", "error"],
      "model stream attempt",
    );
    if (
      typeof attempt.provider_model_api_name !== "string" ||
      !apiNamePattern.test(attempt.provider_model_api_name) ||
      (attempt.outcome !== "succeeded" && attempt.outcome !== "failed") ||
      !isIntegerInRange(attempt.elapsed_ms, 0, elapsedMaximum) ||
      (attempt.outcome === "succeeded" && attempt.error !== undefined) ||
      (attempt.outcome === "failed" && attempt.error === undefined)
    )
      throw invalidStream("The model stream contains an invalid attempt.");
    return {
      provider_model_api_name: attempt.provider_model_api_name,
      outcome: attempt.outcome,
      elapsed_ms: attempt.elapsed_ms,
      ...(attempt.usage === undefined
        ? {}
        : { usage: parseStreamUsage(attempt.usage) }),
      ...(attempt.error === undefined
        ? {}
        : { error: parseStreamError(attempt.error) }),
    } satisfies AdministratorPlaygroundAttempt;
  });
  if (
    successful &&
    (attempts.filter((attempt) => attempt.outcome === "succeeded").length !==
      1 ||
      attempts.at(-1)?.outcome !== "succeeded")
  )
    throw invalidStream("The model stream completed attempts are invalid.");
  return attempts;
}

function invalidPlaygroundResponse(reason: string): AdministrationApiError {
  return new AdministrationApiError(
    502,
    "invalid_response",
    "The Router returned an invalid administrator playground response.",
    { reason },
  );
}

function sameUsage(left: Usage, right: Usage): boolean {
  const units = (usage: Usage) =>
    usage.units.map((item) => `${item.unit}:${item.quantity}`).sort();
  return (
    left.cost === right.cost &&
    left.currency === right.currency &&
    JSON.stringify(units(left)) === JSON.stringify(units(right))
  );
}

function parseSuccessfulResultFacts(
  value: Record<string, unknown>,
  expectedSelector: AdministratorPlaygroundSelector,
): {
  readonly logicalCallId: string;
  readonly selector: AdministratorPlaygroundSelector;
  readonly elapsedMs: number;
  readonly attempts: readonly AdministratorPlaygroundAttempt[];
} {
  if (
    !isBoundedString(value.logical_call_id, 200) ||
    !isIntegerInRange(value.elapsed_ms, 0, 900_000)
  )
    throw invalidPlaygroundResponse(
      "The response has invalid call identity or elapsed time.",
    );
  const selector = parseAdministratorSelector(value.selector);
  if (selectorKey(selector) !== selectorKey(expectedSelector))
    throw invalidPlaygroundResponse(
      "The response selector does not match the request target.",
    );
  const attempts = parseStreamAttempts(
    value.attempts,
    16,
    true,
    "provider_model_api_name" in selector,
  );
  if (
    "provider_model_api_name" in selector &&
    attempts.some(
      (attempt) =>
        attempt.provider_model_api_name !== selector.provider_model_api_name,
    )
  )
    throw invalidPlaygroundResponse(
      "An exact response contains an attempt for a different route.",
    );
  return {
    logicalCallId: value.logical_call_id,
    selector,
    elapsedMs: value.elapsed_ms,
    attempts,
  };
}

function parseAssistantContent(value: unknown) {
  if (!Array.isArray(value) || value.length === 0)
    throw invalidPlaygroundResponse("The model content is empty or invalid.");
  return value.map((part) => {
    const candidate = objectValue(part);
    if (candidate.type === "text") {
      requireClosedObject(candidate, ["type", "text"], [], "model text");
      if (typeof candidate.text !== "string")
        throw invalidPlaygroundResponse("The model text is invalid.");
      return { type: "text" as const, text: candidate.text };
    }
    requireClosedObject(
      candidate,
      ["type", "id", "name", "arguments_json"],
      [],
      "model tool call",
    );
    if (
      candidate.type !== "tool_call" ||
      !isBoundedString(candidate.id, 200) ||
      !isBoundedString(candidate.name, 200) ||
      !isBoundedString(candidate.arguments_json, 1_000_000)
    )
      throw invalidPlaygroundResponse("The model tool call is invalid.");
    return {
      type: "tool_call" as const,
      id: candidate.id,
      name: candidate.name,
      arguments_json: candidate.arguments_json,
    };
  });
}

function parseAdministratorModelResult(
  value: unknown,
  expectedSelector: AdministratorPlaygroundSelector,
): AdministratorPlaygroundModelResult {
  const response = requireClosedObject(
    value,
    ["logical_call_id", "selector", "elapsed_ms", "attempts", "result"],
    [],
    "administrator model result",
  );
  const facts = parseSuccessfulResultFacts(response, expectedSelector);
  const resultObject = objectValue(response.result);
  let result: ModelCallResult;
  if (resultObject.output_type === "standard") {
    requireClosedObject(
      resultObject,
      ["output_type", "provider_model_api_name", "content", "usage"],
      [],
      "administrator standard model result",
    );
    if (
      typeof resultObject.provider_model_api_name !== "string" ||
      !apiNamePattern.test(resultObject.provider_model_api_name)
    )
      throw invalidPlaygroundResponse("The final model route is invalid.");
    result = {
      output_type: "standard",
      provider_model_api_name: resultObject.provider_model_api_name,
      content: parseAssistantContent(resultObject.content),
      usage: parseStreamUsage(resultObject.usage),
    };
  } else {
    requireClosedObject(
      resultObject,
      [
        "output_type",
        "provider_model_api_name",
        "structured_output_json",
        "usage",
      ],
      [],
      "administrator structured model result",
    );
    if (
      resultObject.output_type !== "structured_json" ||
      typeof resultObject.provider_model_api_name !== "string" ||
      !apiNamePattern.test(resultObject.provider_model_api_name) ||
      !isBoundedString(resultObject.structured_output_json, 1_000_000)
    )
      throw invalidPlaygroundResponse(
        "The structured model result is invalid.",
      );
    result = {
      output_type: "structured_json",
      provider_model_api_name: resultObject.provider_model_api_name,
      structured_output_json: resultObject.structured_output_json,
      usage: parseStreamUsage(resultObject.usage),
    };
  }
  const finalAttempt = facts.attempts.at(-1);
  if (
    finalAttempt?.provider_model_api_name !== result.provider_model_api_name ||
    (finalAttempt.usage !== undefined &&
      !sameUsage(finalAttempt.usage, result.usage))
  )
    throw invalidPlaygroundResponse(
      "The model result does not match its final succeeded attempt.",
    );
  return {
    logical_call_id: facts.logicalCallId,
    selector: facts.selector,
    elapsed_ms: facts.elapsedMs,
    attempts: facts.attempts,
    result,
  };
}

function parseAdministratorEmbeddingResult(
  value: unknown,
  expectedSelector: AdministratorPlaygroundSelector,
  expectedInputCount: number,
): AdministratorPlaygroundEmbeddingResult {
  const response = requireClosedObject(
    value,
    ["logical_call_id", "selector", "elapsed_ms", "attempts", "result"],
    [],
    "administrator embedding result",
  );
  const facts = parseSuccessfulResultFacts(response, expectedSelector);
  const result = requireClosedObject(
    response.result,
    ["provider_model_api_name", "embeddings", "usage"],
    [],
    "administrator embedding value",
  );
  if (
    typeof result.provider_model_api_name !== "string" ||
    !apiNamePattern.test(result.provider_model_api_name) ||
    !Array.isArray(result.embeddings) ||
    result.embeddings.length !== expectedInputCount
  )
    throw invalidPlaygroundResponse("The embedding result is incomplete.");
  let dimensions: number | undefined;
  const embeddings = result.embeddings.map((value, index) => {
    const embedding = requireClosedObject(
      value,
      ["index", "values"],
      [],
      "embedding vector",
    );
    if (
      embedding.index !== index ||
      !Array.isArray(embedding.values) ||
      embedding.values.length < 1 ||
      embedding.values.length > 65_536 ||
      embedding.values.some(
        (item) => typeof item !== "number" || !Number.isFinite(item),
      ) ||
      (dimensions !== undefined && embedding.values.length !== dimensions)
    )
      throw invalidPlaygroundResponse(
        "The embedding vectors do not match the request batch.",
      );
    dimensions = embedding.values.length;
    return { index, values: embedding.values as readonly number[] };
  });
  const usage = parseStreamUsage(result.usage);
  const finalAttempt = facts.attempts.at(-1);
  if (
    finalAttempt?.provider_model_api_name !== result.provider_model_api_name ||
    (finalAttempt.usage !== undefined && !sameUsage(finalAttempt.usage, usage))
  )
    throw invalidPlaygroundResponse(
      "The embedding result does not match its final succeeded attempt.",
    );
  return {
    logical_call_id: facts.logicalCallId,
    selector: facts.selector,
    elapsed_ms: facts.elapsedMs,
    attempts: facts.attempts,
    result: {
      provider_model_api_name: result.provider_model_api_name,
      embeddings,
      usage,
    },
  };
}

function validTimestamp(value: unknown): value is string {
  return isBoundedString(value, 200) && Number.isFinite(Date.parse(value));
}

function parseAdministratorMediaJob(
  value: unknown,
  expected?: {
    readonly id?: string;
    readonly selector?: AdministratorPlaygroundSelector;
  },
): AdministratorPlaygroundMediaJob {
  const response = requireClosedObject(
    value,
    [
      "id",
      "logical_call_id",
      "selector",
      "provider_model_api_name",
      "kind",
      "state",
      "attempts",
      "created_at",
    ],
    ["elapsed_ms", "usage", "content", "error", "completed_at"],
    "administrator media job",
  );
  const selector = parseAdministratorSelector(response.selector);
  if (
    !isBoundedString(response.id, 200) ||
    !isBoundedString(response.logical_call_id, 200) ||
    typeof response.provider_model_api_name !== "string" ||
    !apiNamePattern.test(response.provider_model_api_name) ||
    (response.kind !== "image" &&
      response.kind !== "video" &&
      response.kind !== "audio") ||
    (response.state !== "pending" &&
      response.state !== "running" &&
      response.state !== "succeeded" &&
      response.state !== "failed") ||
    !validTimestamp(response.created_at) ||
    (expected?.id !== undefined && response.id !== expected.id) ||
    (expected?.selector !== undefined &&
      selectorKey(selector) !== selectorKey(expected.selector))
  )
    throw invalidPlaygroundResponse("The media job identity is invalid.");
  if (
    "provider_model_api_name" in selector &&
    response.provider_model_api_name !== selector.provider_model_api_name
  )
    throw invalidPlaygroundResponse(
      "The exact media job uses a different provider-model route.",
    );
  const terminal =
    response.state === "succeeded" || response.state === "failed";
  if (
    terminal !== (response.elapsed_ms !== undefined) ||
    terminal !== (response.completed_at !== undefined) ||
    (response.elapsed_ms !== undefined &&
      !isIntegerInRange(response.elapsed_ms, 0, 86_400_000)) ||
    (response.completed_at !== undefined &&
      !validTimestamp(response.completed_at)) ||
    (response.state === "succeeded" &&
      (response.content === undefined || response.error !== undefined)) ||
    (response.state === "failed" &&
      (response.error === undefined || response.content !== undefined)) ||
    (!terminal &&
      (response.content !== undefined || response.error !== undefined))
  )
    throw invalidPlaygroundResponse("The media job state facts are invalid.");
  const attempts = parseStreamAttempts(
    response.attempts,
    16,
    response.state === "succeeded",
    "provider_model_api_name" in selector,
    86_400_000,
  );
  if (
    "provider_model_api_name" in selector &&
    attempts.some(
      (attempt) =>
        attempt.provider_model_api_name !== selector.provider_model_api_name,
    )
  )
    throw invalidPlaygroundResponse(
      "The exact media job contains an attempt for a different route.",
    );
  if (
    terminal &&
    attempts.length > 0 &&
    attempts.at(-1)?.provider_model_api_name !==
      response.provider_model_api_name
  )
    throw invalidPlaygroundResponse(
      "The media job route does not match its final attempt.",
    );
  let content: AdministratorPlaygroundMediaJob["content"];
  if (response.content !== undefined) {
    const parsed = requireClosedObject(
      response.content,
      ["media_type", "size_bytes"],
      [],
      "media content facts",
    );
    if (
      !isBoundedString(parsed.media_type, 200) ||
      !isIntegerInRange(parsed.size_bytes, 0, Number.MAX_SAFE_INTEGER)
    )
      throw invalidPlaygroundResponse("The media content facts are invalid.");
    content = { media_type: parsed.media_type, size_bytes: parsed.size_bytes };
  }
  const usage =
    response.usage === undefined ? undefined : parseStreamUsage(response.usage);
  const finalUsage = attempts.at(-1)?.usage;
  if (
    usage !== undefined &&
    finalUsage !== undefined &&
    !sameUsage(usage, finalUsage)
  )
    throw invalidPlaygroundResponse(
      "The media job usage does not match its final attempt.",
    );
  return {
    id: response.id,
    logical_call_id: response.logical_call_id,
    selector,
    provider_model_api_name: response.provider_model_api_name,
    kind: response.kind,
    state: response.state,
    attempts,
    ...(response.elapsed_ms === undefined
      ? {}
      : { elapsed_ms: response.elapsed_ms }),
    ...(usage === undefined ? {} : { usage }),
    ...(content === undefined ? {} : { content }),
    ...(response.error === undefined
      ? {}
      : { error: parseStreamError(response.error) }),
    created_at: response.created_at,
    ...(response.completed_at === undefined
      ? {}
      : { completed_at: response.completed_at }),
  };
}

function boundedUtf8(
  encoder: TextEncoder,
  value: string,
  maximum: number,
  description: string,
): number {
  const bytes = encoder.encode(value).byteLength;
  if (bytes > maximum)
    throw invalidStream(`The model stream exceeds the ${description} limit.`);
  return bytes;
}

async function readAdministratorModelStream(
  response: Response,
  expectedSelector: AdministratorPlaygroundSelector,
  limits: AdministratorStreamLimits,
): Promise<AdministratorPlaygroundStreamResult> {
  if (response.body === null)
    throw invalidStream("The model stream has no response body.");
  const reader = response.body
    .pipeThrough(new TextDecoderStream("utf-8", { fatal: true }))
    .getReader();
  let buffer = "";
  let started = false;
  let logicalCallId: string | undefined;
  let startSelectorKey: string | undefined;
  let startProviderModel: string | undefined;
  let completed: AdministratorPlaygroundStreamResult | null = null;
  let eventCount = 0;
  let textOutputBytes = 0;
  let toolCallCount = 0;
  let pendingBytes = 0;
  let delimiterSearchFrom = 0;
  const encoder = new TextEncoder();
  const expectedSelectorKey = selectorKey(expectedSelector);
  const content: (
    | { readonly type: "text"; readonly chunks: string[] }
    | {
        readonly type: "tool_call";
        readonly id: string;
        readonly name: string;
        readonly arguments_json: string;
      }
  )[] = [];
  let contentBytes = 0;

  const accept = (
    parsed: ServerSentEvent,
    afterCompletion: boolean,
  ): AdministratorPlaygroundStreamResult | null => {
    eventCount += 1;
    if (eventCount > limits.eventCount)
      throw invalidStream("The model stream contains too many events.");
    if (afterCompletion)
      throw invalidStream("The model stream sent an event after completion.");
    if (parsed.event === "start") {
      const data = requireClosedObject(
        parsed.data,
        ["logical_call_id", "selector", "provider_model_api_name"],
        [],
        "model stream start event",
      );
      if (started)
        throw invalidStream("The model stream contains two start events.");
      if (
        !isBoundedString(data.logical_call_id, 200) ||
        typeof data.provider_model_api_name !== "string" ||
        !apiNamePattern.test(data.provider_model_api_name)
      )
        throw invalidStream("The model stream start event is incomplete.");
      started = true;
      logicalCallId = data.logical_call_id;
      startSelectorKey = selectorKey(data.selector);
      startProviderModel = data.provider_model_api_name;
      if (startSelectorKey !== expectedSelectorKey)
        throw invalidStream(
          "The model stream selector does not match its request target.",
        );
      if (
        "provider_model_api_name" in expectedSelector &&
        startProviderModel !== expectedSelector.provider_model_api_name
      )
        throw invalidStream(
          "The model stream provider-model does not match its exact request target.",
        );
      return null;
    }
    if (!started)
      throw invalidStream(
        "The model stream sent output before its start event.",
      );
    if (parsed.event === "text_delta") {
      const data = requireClosedObject(
        parsed.data,
        ["delta"],
        [],
        "model stream text-delta event",
      );
      if (typeof data.delta !== "string" || data.delta === "")
        throw invalidStream("The model stream contains an invalid text delta.");
      const deltaBytes = encoder.encode(data.delta).byteLength;
      textOutputBytes += deltaBytes;
      if (textOutputBytes > limits.textOutputBytes)
        throw invalidStream("The model stream exceeds the text output limit.");
      contentBytes += deltaBytes;
      if (contentBytes > limits.contentBytes)
        throw invalidStream("The model stream exceeds the content limit.");
      const previous = content.at(-1);
      if (previous?.type === "text") previous.chunks.push(data.delta);
      else content.push({ type: "text", chunks: [data.delta] });
      return null;
    }
    if (parsed.event === "tool_call") {
      const data = requireClosedObject(
        parsed.data,
        ["tool_call"],
        [],
        "model stream tool-call event",
      );
      const tool = requireClosedObject(
        data.tool_call,
        ["type", "id", "name", "arguments_json"],
        [],
        "model stream tool call",
      );
      if (
        tool.type !== "tool_call" ||
        !isBoundedString(tool.id, 200) ||
        !isBoundedString(tool.name, 200) ||
        !isBoundedString(tool.arguments_json, 1_000_000)
      )
        throw invalidStream("The model stream contains an invalid tool call.");
      toolCallCount += 1;
      if (toolCallCount > limits.toolCallCount)
        throw invalidStream("The model stream contains too many tool calls.");
      const toolBytes =
        boundedUtf8(encoder, tool.id, limits.toolIdBytes, "tool-call ID") +
        boundedUtf8(
          encoder,
          tool.name,
          limits.toolNameBytes,
          "tool-call name",
        ) +
        boundedUtf8(
          encoder,
          tool.arguments_json,
          limits.toolArgumentsBytes,
          "tool-call arguments",
        );
      contentBytes += toolBytes;
      if (contentBytes > limits.contentBytes)
        throw invalidStream("The model stream exceeds the content limit.");
      content.push({
        type: "tool_call",
        id: tool.id,
        name: tool.name,
        arguments_json: tool.arguments_json,
      });
      return null;
    }
    if (parsed.event === "error") {
      const data = requireClosedObject(
        parsed.data,
        ["error", "logical_call_id", "selector", "elapsed_ms", "attempts"],
        [],
        "model stream error event",
      );
      const error = parseStreamError(data.error);
      if (error.code === "conflict" || error.code === "assignment_cycle")
        throw invalidStream(
          "The model stream returned a configuration-only error code.",
        );
      if (
        !isBoundedString(data.logical_call_id, 200) ||
        data.logical_call_id !== logicalCallId
      )
        throw invalidStream(
          "The model stream error logical-call ID does not match its start event.",
        );
      if (selectorKey(data.selector) !== startSelectorKey)
        throw invalidStream(
          "The model stream error selector does not match its start event.",
        );
      if (!isIntegerInRange(data.elapsed_ms, 0, 900_000))
        throw invalidStream("The model stream error elapsed time is invalid.");
      const attempts = parseStreamAttempts(
        data.attempts,
        limits.terminalAttempts,
        false,
        startSelectorKey.startsWith("provider-model:"),
      );
      if (
        startSelectorKey.startsWith("provider-model:") &&
        attempts.some(
          (attempt) =>
            `provider-model:${attempt.provider_model_api_name}` !==
            startSelectorKey,
        )
      )
        throw invalidStream(
          "The exact model stream contains an attempt for a different route.",
        );
      if (
        attempts.length > 0 &&
        attempts.at(-1)?.provider_model_api_name !== startProviderModel
      )
        throw invalidStream(
          "The model stream error route does not match its final attempt.",
        );
      const headerLogicalCallId = response.headers.get(
        "X-LLMRouter-Logical-Call-Id",
      );
      if (
        headerLogicalCallId === null ||
        headerLogicalCallId !== logicalCallId ||
        headerLogicalCallId !== data.logical_call_id
      )
        throw invalidStream(
          "The model stream correlation header does not match its events.",
        );
      throw new AdministrationApiError(
        502,
        error.code,
        error.message,
        error.details ?? undefined,
        {
          logical_call_id: data.logical_call_id,
          selector: data.selector as AdministratorPlaygroundSelector,
          elapsed_ms: data.elapsed_ms,
          attempts,
        },
      );
    }
    if (parsed.event !== "completed")
      throw invalidStream(
        `The model stream contains unknown event ${parsed.event}.`,
      );
    const data = requireClosedObject(
      parsed.data,
      [
        "logical_call_id",
        "provider_model_api_name",
        "selector",
        "elapsed_ms",
        "attempts",
        "usage",
      ],
      [],
      "model stream completed event",
    );
    if (
      !isBoundedString(data.logical_call_id, 200) ||
      data.logical_call_id !== logicalCallId ||
      typeof data.provider_model_api_name !== "string" ||
      !apiNamePattern.test(data.provider_model_api_name) ||
      data.provider_model_api_name !== startProviderModel ||
      !isIntegerInRange(data.elapsed_ms, 0, 900_000) ||
      selectorKey(data.selector) !== startSelectorKey ||
      typeof data.usage !== "object" ||
      data.usage === null
    )
      throw invalidStream("The model stream completed event is incomplete.");
    const attempts = parseStreamAttempts(
      data.attempts,
      limits.terminalAttempts,
      true,
      startSelectorKey.startsWith("provider-model:"),
    );
    if (
      attempts.at(-1)?.provider_model_api_name !== data.provider_model_api_name
    )
      throw invalidStream(
        "The model stream final route does not match its succeeded attempt.",
      );
    const usage = parseStreamUsage(data.usage);
    const headerLogicalCallId = response.headers.get(
      "X-LLMRouter-Logical-Call-Id",
    );
    if (
      headerLogicalCallId === null ||
      headerLogicalCallId !== logicalCallId ||
      headerLogicalCallId !== data.logical_call_id
    )
      throw invalidStream(
        "The model stream correlation header does not match its events.",
      );
    return {
      logical_call_id: data.logical_call_id,
      selector: data.selector as AdministratorPlaygroundSelector,
      provider_model_api_name: data.provider_model_api_name,
      elapsed_ms: data.elapsed_ms,
      attempts,
      usage,
      content: content.map((part) =>
        part.type === "text"
          ? { type: "text" as const, text: part.chunks.join("") }
          : part,
      ),
    } satisfies AdministratorPlaygroundStreamResult;
  };

  try {
    let done = false;
    while (!done) {
      // react-doctor-disable-next-line react-doctor/async-await-in-loop -- Stream chunks are ordered and depend on the previous parser state.
      const result = await reader.read();
      const chunk = result.value ?? "";
      buffer += chunk;
      pendingBytes += encoder.encode(chunk).byteLength;
      const delimiter = /\r?\n\r?\n/g;
      delimiter.lastIndex = delimiterSearchFrom;
      const blocks: string[] = [];
      let consumedCharacters = 0;
      let match = delimiter.exec(buffer);
      while (match !== null) {
        blocks.push(buffer.slice(consumedCharacters, match.index));
        consumedCharacters = delimiter.lastIndex;
        match = delimiter.exec(buffer);
      }
      if (consumedCharacters > 0) {
        buffer = buffer.slice(consumedCharacters);
        pendingBytes = encoder.encode(buffer).byteLength;
      }
      delimiterSearchFrom = Math.max(0, buffer.length - 3);
      if (pendingBytes > limits.pendingEventBytes)
        throw invalidStream(
          "The model stream has an oversized unterminated event.",
        );
      for (const block of blocks) {
        if (block === "")
          throw invalidStream("The model stream contains an empty event.");
        boundedUtf8(encoder, block, limits.pendingEventBytes, "single event");
        const value = accept(parseServerSentEvent(block), completed !== null);
        if (value !== null) completed = value;
      }
      done = result.done;
    }
    if (buffer !== "")
      throw invalidStream("The model stream ended with an unterminated event.");
    if (completed !== null) return completed;
    throw invalidStream("The model stream ended before a terminal event.");
  } catch (error) {
    const reportedError =
      error instanceof AdministrationApiError ||
      (error instanceof DOMException && error.name === "AbortError")
        ? error
        : invalidStream(
            "The model stream ended with a transport or UTF-8 failure.",
          );
    try {
      await reader.cancel();
    } catch {
      // Preserve the parser or caller-abort error instead of a cancel failure.
    }
    throw reportedError;
  } finally {
    reader.releaseLock();
  }
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
  previewOpenRouterModel(
    modelIdOrUrl: string,
    csrf: string,
  ): Promise<OpenRouterModelImportPreview>;
  importOpenRouterModel(
    reviewed: OpenRouterModelImportRequest,
    csrf: string,
  ): Promise<OpenRouterModelImportResult>;
  synchronizePrices(
    names: readonly string[] | null,
    csrf: string,
  ): Promise<PriceSyncResult>;
  playgroundModel?(
    value: AdministratorPlaygroundModelRequest,
    csrf: string,
    signal?: AbortSignal,
  ): Promise<AdministratorPlaygroundModelResult>;
  playgroundModelStream?(
    value: AdministratorPlaygroundModelRequest,
    csrf: string,
    signal?: AbortSignal,
  ): Promise<AdministratorPlaygroundStreamResult>;
  playgroundEmbedding?(
    value: AdministratorPlaygroundEmbeddingRequest,
    csrf: string,
    signal?: AbortSignal,
  ): Promise<AdministratorPlaygroundEmbeddingResult>;
  playgroundCreateMedia?(
    value: AdministratorPlaygroundMediaRequest,
    csrf: string,
    signal?: AbortSignal,
  ): Promise<AdministratorPlaygroundMediaJob>;
  playgroundMediaJob?(
    id: string,
    signal?: AbortSignal,
  ): Promise<AdministratorPlaygroundMediaJob>;
  playgroundMediaContent?(id: string, signal?: AbortSignal): Promise<Blob>;
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
  streamLimits: AdministratorStreamLimits = administratorStreamLimits,
): AdministrationClient {
  const listLimit = 200;
  const maximumListPages = 100;
  const maximumListItems = listLimit * maximumListPages;
  async function request<T>(
    path: string,
    init: RequestInit = {},
    deadline: ClientDeadline = administrationDeadline,
    responseParser?: (value: unknown) => T,
    expectedErrorSelector?: AdministratorPlaygroundSelector,
    requireNoStore = false,
  ): Promise<T> {
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
        if (!response.ok) {
          const error = await parseError(response);
          if (
            expectedErrorSelector !== undefined &&
            (error.code === "conflict" || error.code === "assignment_cycle")
          )
            throw invalidPlaygroundResponse(
              "The playground returned a configuration-only error code.",
            );
          if (
            expectedErrorSelector !== undefined &&
            error.context?.selector !== undefined &&
            selectorKey(error.context.selector) !==
              selectorKey(expectedErrorSelector)
          )
            throw invalidPlaygroundResponse(
              "The error selector does not match the request target.",
            );
          throw error;
        }
        if (response.status === 204) return undefined as T;
        if (
          requireNoStore &&
          response.headers.get("Cache-Control")?.trim().toLowerCase() !==
            "no-store"
        )
          throw invalidPlaygroundResponse(
            "The response is missing its no-store cache control.",
          );
        if (
          requireNoStore &&
          response.headers
            .get("Content-Type")
            ?.split(";", 1)[0]
            ?.trim()
            .toLowerCase() !== "application/json"
        )
          throw invalidPlaygroundResponse(
            "The response has an invalid content type.",
          );
        let value: unknown;
        try {
          value = await response.json();
        } catch {
          if (responseParser !== undefined)
            throw invalidPlaygroundResponse(
              "The response body is not valid JSON.",
            );
          throw new AdministrationApiError(
            502,
            "invalid_response",
            "The Router returned invalid JSON.",
          );
        }
        return responseParser === undefined
          ? (value as T)
          : responseParser(value);
      },
      deadline,
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
  const invalidRequestLogResponse = (reason: string) =>
    new AdministrationApiError(
      502,
      "invalid_response",
      "The Router returned an invalid request-log response.",
      { reason },
    );
  function parseRequestLogSummary(value: unknown): RequestLogSummary {
    if (typeof value !== "object" || value === null || Array.isArray(value))
      throw invalidRequestLogResponse(
        "A request-log summary is not an object.",
      );
    const summary = value as Record<string, unknown>;
    const requiredKeys = [
      "id",
      "logical_call_id",
      "call_actor",
      "kind",
      "outcome",
      "started_at",
    ];
    const allowedKeys = new Set([
      ...requiredKeys,
      "service_api_name",
      "workspace_api_name",
      "administrator_subject",
      "configuration_service_api_name",
      "assignment_api_name",
      "provider_model_api_name",
      "tags",
    ]);
    if (
      requiredKeys.some((key) => !(key in summary)) ||
      Object.keys(summary).some((key) => !allowedKeys.has(key))
    )
      throw invalidRequestLogResponse(
        "A request-log summary does not match its closed schema.",
      );
    if (
      !isBoundedString(summary.id, 200) ||
      !isBoundedString(summary.logical_call_id, 200) ||
      (summary.kind !== "model" &&
        summary.kind !== "embedding" &&
        summary.kind !== "media") ||
      (summary.outcome !== "succeeded" && summary.outcome !== "failed") ||
      !validTimestamp(summary.started_at)
    )
      throw invalidRequestLogResponse(
        "A request-log summary has invalid common fields.",
      );
    for (const [key, pattern] of [
      ["service_api_name", apiNamePattern],
      ["workspace_api_name", apiNamePattern],
      ["configuration_service_api_name", apiNamePattern],
      ["provider_model_api_name", apiNamePattern],
      ["assignment_api_name", assignmentNamePattern],
    ] as const) {
      const field = summary[key];
      if (
        field !== undefined &&
        (typeof field !== "string" || !pattern.test(field))
      )
        throw invalidRequestLogResponse(
          `A request-log summary has an invalid ${key.replaceAll("_", " ")}.`,
        );
    }
    if (summary.tags !== undefined) {
      const encoder = new TextEncoder();
      if (!Array.isArray(summary.tags) || summary.tags.length > 32)
        throw invalidRequestLogResponse(
          "A request-log summary has invalid bounded tags.",
        );
      const tags: readonly unknown[] = summary.tags;
      let totalTagBytes = 0;
      for (const tag of tags) {
        if (!isBoundedString(tag, 128) || encoder.encode(tag).byteLength > 128)
          throw invalidRequestLogResponse(
            "A request-log summary has invalid bounded tags.",
          );
        totalTagBytes += encoder.encode(tag).byteLength;
      }
      if (totalTagBytes > 2_048)
        throw invalidRequestLogResponse(
          "A request-log summary has invalid bounded tags.",
        );
    }
    if (summary.call_actor === "service") {
      if (
        typeof summary.service_api_name !== "string" ||
        typeof summary.workspace_api_name !== "string" ||
        summary.administrator_subject !== undefined ||
        summary.configuration_service_api_name !== undefined
      )
        throw invalidRequestLogResponse(
          "A service request-log summary has invalid ownership fields.",
        );
    } else if (summary.call_actor === "administrator") {
      if (
        !isBoundedString(summary.administrator_subject, 500) ||
        summary.service_api_name !== undefined ||
        summary.workspace_api_name !== undefined
      )
        throw invalidRequestLogResponse(
          "An administrator request-log summary has invalid ownership fields.",
        );
      const isExact =
        typeof summary.provider_model_api_name === "string" &&
        summary.assignment_api_name === undefined &&
        summary.configuration_service_api_name === undefined;
      const isAssignment =
        typeof summary.assignment_api_name === "string" &&
        typeof summary.configuration_service_api_name === "string";
      if (isExact === isAssignment)
        throw invalidRequestLogResponse(
          "An administrator request-log summary must identify one exact or assignment target.",
        );
    } else {
      throw invalidRequestLogResponse(
        "A request-log summary has an invalid call actor.",
      );
    }
    return summary as unknown as RequestLogSummary;
  }
  function requestLogObject(
    value: unknown,
    required: readonly string[],
    optional: readonly string[],
    description: string,
  ): Record<string, unknown> {
    if (typeof value !== "object" || value === null || Array.isArray(value))
      throw invalidRequestLogResponse(`${description} is not an object.`);
    const object = value as Record<string, unknown>;
    const allowed = new Set([...required, ...optional]);
    if (
      required.some((key) => !(key in object)) ||
      Object.keys(object).some((key) => !allowed.has(key))
    )
      throw invalidRequestLogResponse(
        `${description} does not match its closed schema.`,
      );
    return object;
  }
  function parseRequestLogUsage(value: unknown): Usage {
    const usage = requestLogObject(
      value,
      ["units", "cost", "currency"],
      [],
      "Request-log usage",
    );
    if (
      !Array.isArray(usage.units) ||
      typeof usage.cost !== "string" ||
      !decimalPattern.test(usage.cost) ||
      typeof usage.currency !== "string" ||
      !currencyPattern.test(usage.currency)
    )
      throw invalidRequestLogResponse("Request-log usage is invalid.");
    const units = usage.units.map((value) => {
      const item = requestLogObject(
        value,
        ["unit", "quantity"],
        [],
        "Request-log usage item",
      );
      if (
        typeof item.unit !== "string" ||
        !usageUnits.has(item.unit as UsageUnit) ||
        typeof item.quantity !== "string" ||
        !decimalPattern.test(item.quantity)
      )
        throw invalidRequestLogResponse("A request-log usage item is invalid.");
      return { unit: item.unit as UsageUnit, quantity: item.quantity };
    });
    return { units, cost: usage.cost, currency: usage.currency };
  }
  function parseRequestLogPrice(value: unknown): Price {
    const price = requestLogObject(
      value,
      ["currency", "unit_prices"],
      ["source", "synchronized_at"],
      "Request-log applied price",
    );
    if (
      typeof price.currency !== "string" ||
      !currencyPattern.test(price.currency) ||
      !Array.isArray(price.unit_prices) ||
      price.unit_prices.length < 1 ||
      price.unit_prices.length > 16 ||
      (price.source !== undefined &&
        (typeof price.source !== "string" || price.source.length > 500)) ||
      (price.synchronized_at !== undefined &&
        !validTimestamp(price.synchronized_at))
    )
      throw invalidRequestLogResponse(
        "A request-log applied price is invalid.",
      );
    const unitPrices = price.unit_prices.map((value) => {
      const item = requestLogObject(
        value,
        ["unit", "amount"],
        [],
        "Request-log unit price",
      );
      if (
        typeof item.unit !== "string" ||
        !usageUnits.has(item.unit as UsageUnit) ||
        typeof item.amount !== "string" ||
        item.amount.length > 64 ||
        !decimalPattern.test(item.amount)
      )
        throw invalidRequestLogResponse("A request-log unit price is invalid.");
      return { unit: item.unit as UsageUnit, amount: item.amount };
    });
    if (
      new Set(unitPrices.map((item) => `${item.unit}\u0000${item.amount}`))
        .size !== unitPrices.length
    )
      throw invalidRequestLogResponse(
        "A request-log applied price has duplicate unit prices.",
      );
    return {
      currency: price.currency,
      unit_prices: unitPrices,
      ...(typeof price.source === "string" ? { source: price.source } : {}),
      ...(typeof price.synchronized_at === "string"
        ? { synchronized_at: price.synchronized_at }
        : {}),
    };
  }
  function parseRequestLogError(value: unknown): SafeError {
    const error = requestLogObject(
      value,
      ["code", "message"],
      ["details"],
      "Request-log error",
    );
    if (
      typeof error.code !== "string" ||
      !streamErrorCodes.has(error.code) ||
      !isBoundedString(error.message, 1_000)
    )
      throw invalidRequestLogResponse("A request-log error is invalid.");
    let details: SafeError["details"];
    if (error.details !== undefined) {
      const item = requestLogObject(
        error.details,
        [],
        ["field", "reason"],
        "Request-log error details",
      );
      if (
        (item.field !== undefined && !isBoundedString(item.field, 200)) ||
        (item.reason !== undefined && !isBoundedString(item.reason, 500))
      )
        throw invalidRequestLogResponse(
          "Request-log error details are invalid.",
        );
      details = {
        ...(typeof item.field === "string" ? { field: item.field } : {}),
        ...(typeof item.reason === "string" ? { reason: item.reason } : {}),
      };
    }
    return {
      code: error.code,
      message: error.message,
      ...(details === undefined ? {} : { details }),
    };
  }
  function parseRequestLogAttempt(value: unknown): RequestAttempt {
    const attempt = requestLogObject(
      value,
      [
        "provider_model_api_name",
        "outcome",
        "started_at",
        "completed_at",
        "applied_prices",
      ],
      ["usage", "response_json", "error"],
      "Request-log attempt",
    );
    if (
      typeof attempt.provider_model_api_name !== "string" ||
      !apiNamePattern.test(attempt.provider_model_api_name) ||
      (attempt.outcome !== "succeeded" && attempt.outcome !== "failed") ||
      !validTimestamp(attempt.started_at) ||
      !validTimestamp(attempt.completed_at) ||
      (attempt.response_json !== undefined &&
        (typeof attempt.response_json !== "string" ||
          attempt.response_json.length > 10_000_000)) ||
      (attempt.outcome === "succeeded" && attempt.error !== undefined) ||
      (attempt.outcome === "failed" && attempt.error === undefined)
    )
      throw invalidRequestLogResponse("A request-log attempt is invalid.");
    return {
      provider_model_api_name: attempt.provider_model_api_name,
      outcome: attempt.outcome,
      started_at: attempt.started_at,
      completed_at: attempt.completed_at,
      applied_prices: parseRequestLogPrice(attempt.applied_prices),
      ...(attempt.usage === undefined
        ? {}
        : { usage: parseRequestLogUsage(attempt.usage) }),
      ...(typeof attempt.response_json === "string"
        ? { response_json: attempt.response_json }
        : {}),
      ...(attempt.error === undefined
        ? {}
        : { error: parseRequestLogError(attempt.error) }),
    };
  }
  function parseRequestLogMedia(value: unknown): LogMedia {
    const media = requestLogObject(
      value,
      ["id", "media_type", "role", "size_bytes"],
      [],
      "Request-log media",
    );
    if (
      !isBoundedString(media.id, 200) ||
      !isBoundedString(media.media_type, 200) ||
      (media.role !== "input" && media.role !== "output") ||
      !isIntegerInRange(media.size_bytes, 0, Number.MAX_SAFE_INTEGER)
    )
      throw invalidRequestLogResponse("Request-log media is invalid.");
    return {
      id: media.id,
      media_type: media.media_type,
      role: media.role,
      size_bytes: media.size_bytes,
    };
  }
  function parseRequestLog(value: unknown): RequestLog {
    const log = requestLogObject(
      value,
      ["summary", "request_json", "attempts"],
      ["response_json", "media"],
      "Request log",
    );
    if (
      typeof log.request_json !== "string" ||
      log.request_json.length > 5_000_000 ||
      (log.response_json !== undefined &&
        (typeof log.response_json !== "string" ||
          log.response_json.length > 10_000_000)) ||
      !Array.isArray(log.attempts) ||
      log.attempts.length > 16 ||
      (log.media !== undefined && !Array.isArray(log.media))
    )
      throw invalidRequestLogResponse("A request log is invalid.");
    return {
      summary: parseRequestLogSummary(log.summary),
      request_json: log.request_json,
      attempts: log.attempts.map(parseRequestLogAttempt),
      ...(typeof log.response_json === "string"
        ? { response_json: log.response_json }
        : {}),
      ...(Array.isArray(log.media)
        ? { media: log.media.map(parseRequestLogMedia) }
        : {}),
    };
  }
  async function allPages<T>(
    path: string,
    filters: Record<string, string | readonly string[] | null | undefined> = {},
    parseItem?: (value: unknown) => T,
  ): Promise<Page<T>> {
    return withClientDeadline(async (signal) => {
      const items: T[] = [];
      const seenCursors = new Set<string>();
      let cursor: string | undefined;
      let loadedPages = 0;
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
        const current = response as Page<unknown>;
        loadedPages += 1;
        if (current.items.length > listLimit)
          throw invalidListResponse(
            `The list page exceeds the requested ${String(listLimit)} item limit.`,
          );
        items.push(
          ...(parseItem === undefined
            ? (current.items as readonly T[])
            : current.items.map(parseItem)),
        );
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
            retrieval: {
              complete: true,
              loaded_items: items.length,
              loaded_pages: loadedPages,
            },
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
    previewOpenRouterModel: (modelIdOrUrl, csrf) =>
      write("/v1/admin/openrouter-model-imports/preview", "POST", csrf, {
        model_id_or_url: modelIdOrUrl,
      }),
    importOpenRouterModel: (reviewed, csrf) =>
      write("/v1/admin/openrouter-model-imports", "POST", csrf, reviewed),
    synchronizePrices: (names, csrf) =>
      write("/v1/admin/prices/synchronize", "POST", csrf, {
        provider_model_api_names: names,
      }),
    playgroundModel: (value, csrf, signal) =>
      request(
        "/v1/admin/playground/model-calls",
        {
          method: "POST",
          headers: { "X-CSRF-Token": csrf },
          body: JSON.stringify(value),
          ...(signal === undefined ? {} : { signal }),
        },
        runtimeCallDeadline,
        (response) => parseAdministratorModelResult(response, value.selector),
        value.selector,
        true,
      ),
    playgroundModelStream: (value, csrf, callerSignal) =>
      withClientDeadline(
        async (signal) => {
          const response = await fetcher("/v1/admin/playground/model-streams", {
            method: "POST",
            cache: "no-store",
            credentials: "same-origin",
            headers: {
              Accept: "text/event-stream",
              "Content-Type": "application/json",
              "X-CSRF-Token": csrf,
            },
            body: JSON.stringify(value),
            signal,
          });
          if (!response.ok) {
            const error = await parseError(response);
            if (error.code === "conflict" || error.code === "assignment_cycle")
              throw invalidPlaygroundResponse(
                "The stream returned a configuration-only error code.",
              );
            if (
              error.context?.selector !== undefined &&
              selectorKey(error.context.selector) !==
                selectorKey(value.selector)
            )
              throw invalidPlaygroundResponse(
                "The stream error selector does not match the request target.",
              );
            throw error;
          }
          if (
            response.headers
              .get("Content-Type")
              ?.split(";", 1)[0]
              ?.trim()
              .toLowerCase() !== "text/event-stream"
          ) {
            await response.body?.cancel();
            throw invalidStream(
              "The model stream response has an invalid content type.",
            );
          }
          if (
            response.headers.get("Cache-Control")?.trim().toLowerCase() !==
            "no-store"
          ) {
            await response.body?.cancel();
            throw invalidStream(
              "The model stream response is missing its no-store cache control.",
            );
          }
          return readAdministratorModelStream(
            response,
            value.selector,
            streamLimits,
          );
        },
        runtimeCallDeadline,
        callerSignal,
      ),
    playgroundEmbedding: (value, csrf, signal) =>
      request(
        "/v1/admin/playground/embeddings",
        {
          method: "POST",
          headers: { "X-CSRF-Token": csrf },
          body: JSON.stringify(value),
          ...(signal === undefined ? {} : { signal }),
        },
        runtimeCallDeadline,
        (response) =>
          parseAdministratorEmbeddingResult(
            response,
            value.selector,
            value.inputs.length,
          ),
        value.selector,
        true,
      ),
    playgroundCreateMedia: (value, csrf, signal) =>
      request(
        "/v1/admin/playground/media-jobs",
        {
          method: "POST",
          headers: { "X-CSRF-Token": csrf },
          body: JSON.stringify(value),
          ...(signal === undefined ? {} : { signal }),
        },
        mediaAdmissionDeadline,
        (response) =>
          parseAdministratorMediaJob(response, { selector: value.selector }),
        value.selector,
        true,
      ),
    playgroundMediaJob: (id, signal) =>
      request(
        `/v1/admin/playground/media-jobs/${encode(id)}`,
        signal === undefined ? {} : { signal },
        mediaStatusDeadline,
        (response) => parseAdministratorMediaJob(response, { id }),
        undefined,
        true,
      ),
    playgroundMediaContent: (id, callerSignal) =>
      withClientDeadline(
        async (signal) => {
          const response = await fetcher(
            `/v1/admin/playground/media-jobs/${encode(id)}/content`,
            {
              cache: "no-store",
              credentials: "same-origin",
              signal,
            },
          );
          if (!response.ok) throw await parseError(response);
          if (
            response.headers.get("Cache-Control")?.trim().toLowerCase() !==
            "no-store"
          )
            throw invalidPlaygroundResponse(
              "The media response is missing its no-store cache control.",
            );
          return response.blob();
        },
        mediaContentDeadline,
        callerSignal,
      ),
    activity: (from, to) => allPages("/v1/admin/activity", { from, to }),
    statistics: (filters) =>
      request(
        `/v1/admin/statistics${query({ from: filters.from, to: filters.to, service: filters.service, workspace: filters.workspace, assignment: filters.assignment, provider_model: filters.provider_model, outcome: filters.outcome, tag: filters.tag, group_by: filters.group_by })}`,
      ),
    requestLogs: (from, to) =>
      allPages("/v1/admin/request-logs", { from, to }, parseRequestLogSummary),
    requestLog: (id) =>
      request(
        `/v1/admin/request-logs/${encode(id)}`,
        {},
        administrationDeadline,
        parseRequestLog,
      ),
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
