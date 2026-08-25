import type {
  PlaygroundControl,
  PlaygroundOperation,
  PlaygroundTargetOperation,
} from "@opendle/ui";
import {
  AdministrationApiError,
  clientDeadlineMilliseconds,
  type AdministrationClient,
  type AdministratorPlaygroundSelector,
  type AdministratorPlaygroundMediaJob,
  type Assignment,
  type Model,
  type Provider,
  type ProviderModel,
} from "./api.js";

export interface PlaygroundTargetSnapshot {
  readonly kind: "assignment" | "provider-model";
  readonly id: string;
  readonly label: string;
  readonly detail: string;
  readonly serviceContext?: string;
  readonly selector: AdministratorPlaygroundSelector;
  readonly operations: readonly PlaygroundTargetOperation[];
  readonly supportsStreaming: boolean;
  readonly supportsStructuredOutput: boolean;
  readonly requiresStructuredOutput: boolean;
  readonly supportsTools: boolean;
  readonly signature: string;
}

export function playgroundTargetKey(target: PlaygroundTargetSnapshot): string {
  return target.kind === "provider-model"
    ? `provider-model:${target.id}`
    : `assignment:${target.serviceContext ?? ""}:${target.id}`;
}

export function updateMediaRecovery(
  current: ReadonlyMap<string, AdministratorPlaygroundMediaJob>,
  target: PlaygroundTargetSnapshot,
  job: AdministratorPlaygroundMediaJob | null,
): ReadonlyMap<string, AdministratorPlaygroundMediaJob> {
  const next = new Map(current);
  const key = playgroundTargetKey(target);
  if (job === null) next.delete(key);
  else next.set(key, job);
  return next;
}

function delay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("The operation was aborted.", "AbortError"));
      return;
    }
    const abort = () => {
      globalThis.clearTimeout(timer);
      reject(new DOMException("The operation was aborted.", "AbortError"));
    };
    const timer = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", abort, { once: true });
  });
}

export async function pollMediaJob(
  client: Required<Pick<AdministrationClient, "playgroundMediaJob">>,
  initial: AdministratorPlaygroundMediaJob,
  signal: AbortSignal,
  onUpdate: (job: AdministratorPlaygroundMediaJob) => void,
): Promise<AdministratorPlaygroundMediaJob> {
  let job = initial;
  const timeoutController = new AbortController();
  const pollSignal = AbortSignal.any([signal, timeoutController.signal]);
  const timeout = globalThis.setTimeout(() => {
    timeoutController.abort();
  }, clientDeadlineMilliseconds.playgroundMediaPoll);
  try {
    while (job.state === "pending" || job.state === "running") {
      await delay(1000, pollSignal);
      job = await client.playgroundMediaJob(job.id, pollSignal);
      onUpdate(job);
    }
    return job;
  } catch (error) {
    if (!signal.aborted && timeoutController.signal.aborted)
      throw new AdministrationApiError(
        408,
        "client_timeout",
        "The browser stopped polling before the media job became terminal.",
        {
          reason: `Query administrator media job ${job.id} again. Do not create a replacement job.`,
        },
        {
          logical_call_id: job.logical_call_id,
          selector: job.selector,
          attempts: job.attempts,
        },
      );
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

const operationOrder: readonly PlaygroundOperation[] = [
  "model",
  "embedding",
  "image",
  "video",
  "audio",
];

function operationsForMappings(
  mappings: readonly ProviderModel[],
): readonly PlaygroundTargetOperation[] {
  const operationControls = new Map<
    PlaygroundOperation,
    Set<PlaygroundControl>
  >();
  for (const mapping of mappings) {
    const inputModalities = new Set(mapping.input_modalities);
    const outputModalities = new Set(mapping.output_modalities);
    if (!inputModalities.has("text")) continue;
    const modelOperation =
      outputModalities.has("text") || outputModalities.has("structured_json");
    if (modelOperation) {
      const controls = operationControls.get("model") ?? new Set();
      controls.add("system-prompt");
      controls.add("temperature");
      controls.add("output-limit");
      if (inputModalities.has("image")) controls.add("input-images");
      operationControls.set("model", controls);
    }
    if (outputModalities.has("embedding"))
      operationControls.set("embedding", new Set());
    for (const operation of ["image", "video", "audio"] as const) {
      if (!outputModalities.has(operation)) continue;
      const controls = operationControls.get(operation) ?? new Set();
      if (operation !== "audio" && inputModalities.has("image"))
        controls.add("input-images");
      operationControls.set(operation, controls);
    }
  }
  return operationOrder.flatMap((operation) => {
    const controls = operationControls.get(operation);
    return controls === undefined
      ? []
      : [{ operation, controls: [...controls] }];
  });
}

function usableMappings(
  mappings: readonly ProviderModel[],
  providers: readonly Provider[],
): readonly ProviderModel[] {
  const enabledProviders = new Set(
    providers.flatMap((provider) =>
      provider.enabled ? [provider.api_name] : [],
    ),
  );
  return mappings.filter(
    (mapping) =>
      mapping.enabled &&
      mapping.cooldown == null &&
      enabledProviders.has(mapping.provider_api_name),
  );
}

function capabilities(mappings: readonly ProviderModel[]) {
  const supportsStructuredOutput = mappings.some((mapping) =>
    new Set(mapping.output_modalities).has("structured_json"),
  );
  return {
    supportsStreaming: mappings.some((mapping) =>
      new Set(mapping.capabilities).has("streaming"),
    ),
    supportsStructuredOutput,
    requiresStructuredOutput:
      supportsStructuredOutput &&
      !mappings.some((mapping) =>
        new Set(mapping.output_modalities).has("text"),
      ),
    supportsTools: mappings.some((mapping) =>
      new Set(mapping.capabilities).has("tool_calling"),
    ),
  };
}

function targetSignature(
  target: Omit<PlaygroundTargetSnapshot, "signature">,
  routeConfiguration: unknown,
): string {
  return JSON.stringify({
    selector: target.selector,
    routeConfiguration,
    operations: target.operations,
    supportsStreaming: target.supportsStreaming,
    supportsStructuredOutput: target.supportsStructuredOutput,
    requiresStructuredOutput: target.requiresStructuredOutput,
    supportsTools: target.supportsTools,
  });
}

function mappingConfiguration(
  mapping: ProviderModel,
  provider: Provider | undefined,
) {
  return {
    mapping: {
      api_name: mapping.api_name,
      provider_api_name: mapping.provider_api_name,
      model_api_name: mapping.model_api_name,
      provider_model_name: mapping.provider_model_name,
      enabled: mapping.enabled,
      input_modalities: mapping.input_modalities,
      output_modalities: mapping.output_modalities,
      capabilities: mapping.capabilities,
      constraints: mapping.constraints,
      reasoning_mappings: mapping.reasoning_mappings,
      configured_price_source: mapping.configured_price_source,
      configured_price_lookup_key: mapping.configured_price_lookup_key,
      configured_manual_price: mapping.configured_manual_price,
      price_source: mapping.price_source,
      price_lookup_key: mapping.price_lookup_key,
      effective_price: mapping.effective_price,
    },
    provider:
      provider === undefined
        ? null
        : {
            api_name: provider.api_name,
            adapter: provider.adapter,
            endpoint: provider.endpoint,
            credential_api_name: provider.credential_api_name,
            enabled: provider.enabled,
          },
  };
}

export function mappingPlaygroundTarget(
  mappingName: string,
  mappings: readonly ProviderModel[],
  providers: readonly Provider[],
  models: readonly Model[],
): PlaygroundTargetSnapshot | null {
  const mapping = usableMappings(mappings, providers).find(
    (item) => item.api_name === mappingName,
  );
  if (mapping === undefined) return null;
  const operations = operationsForMappings([mapping]);
  if (operations.length === 0) return null;
  const model = models.find((item) => item.api_name === mapping.model_api_name);
  const value = {
    kind: "provider-model" as const,
    id: mapping.api_name,
    label: model?.display_name ?? mapping.api_name,
    detail: `${mapping.provider_api_name} · ${mapping.provider_model_name}`,
    selector: { provider_model_api_name: mapping.api_name },
    operations,
    ...capabilities([mapping]),
  };
  const routeConfiguration = mappingConfiguration(
    mapping,
    providers.find(
      (provider) => provider.api_name === mapping.provider_api_name,
    ),
  );
  return { ...value, signature: targetSignature(value, routeConfiguration) };
}

export function assignmentPlaygroundTarget(
  assignmentName: string,
  serviceApiName: string,
  assignments: readonly Assignment[],
  mappings: readonly ProviderModel[],
  providers: readonly Provider[],
): PlaygroundTargetSnapshot | null {
  if (serviceApiName === "") return null;
  const assignment = assignments.find(
    (item) => item.api_name === assignmentName,
  );
  if (assignment === undefined) return null;
  const mappingByName = new Map(
    usableMappings(mappings, providers).map((item) => [item.api_name, item]),
  );
  const candidates = assignment.effective_chain.flatMap((candidate) => {
    const mapping = mappingByName.get(candidate.provider_model_api_name);
    return mapping === undefined ? [] : [mapping];
  });
  const operations = operationsForMappings(candidates);
  if (operations.length === 0) return null;
  const value = {
    kind: "assignment" as const,
    id: assignment.api_name,
    label: assignment.display_name,
    detail: `${String(candidates.length)} eligible route${candidates.length === 1 ? "" : "s"}`,
    serviceContext: serviceApiName,
    selector: {
      assignment_api_name: assignment.api_name,
      service_api_name: serviceApiName,
    },
    operations,
    ...capabilities(candidates),
  };
  const routeConfiguration = {
    assignment: {
      api_name: assignment.api_name,
      definition_kind: assignment.definition_kind,
      defined_by_service_api_name: assignment.defined_by_service_api_name,
      inherits_assignment_api_name: assignment.inherits_assignment_api_name,
      direct_chain: assignment.direct_chain,
      effective_chain: assignment.effective_chain,
      reasoning_level: assignment.reasoning_level,
    },
    candidates: candidates.map((mapping) =>
      mappingConfiguration(
        mapping,
        providers.find(
          (provider) => provider.api_name === mapping.provider_api_name,
        ),
      ),
    ),
  };
  return { ...value, signature: targetSignature(value, routeConfiguration) };
}

export function currentPlaygroundTarget(
  snapshot: PlaygroundTargetSnapshot,
  assignments: readonly Assignment[],
  mappings: readonly ProviderModel[],
  providers: readonly Provider[],
  models: readonly Model[],
): PlaygroundTargetSnapshot | null {
  return snapshot.kind === "provider-model"
    ? mappingPlaygroundTarget(snapshot.id, mappings, providers, models)
    : assignmentPlaygroundTarget(
        snapshot.id,
        snapshot.serviceContext ?? "",
        assignments,
        mappings,
        providers,
      );
}

export function targetUnavailableMessage(
  snapshot: PlaygroundTargetSnapshot,
  current: PlaygroundTargetSnapshot | null,
): string | null {
  if (current === null)
    return "The target is disabled, deleted, or has no eligible operation.";
  if (current.signature !== snapshot.signature)
    return "The target configuration changed after this playground opened. Close it and review the current graph before you run it.";
  return null;
}

export function parseTags(value: string): readonly string[] {
  const tags = Array.from(
    new Set(
      value.split(",").flatMap((item) => {
        const trimmed = item.trim();
        return trimmed === "" ? [] : [trimmed];
      }),
    ),
  );
  tags.sort();
  if (tags.length > 32) throw new Error("Enter no more than 32 tags.");
  const encoded = tags.map((tag) => new TextEncoder().encode(tag).byteLength);
  if (encoded.some((length) => length < 1 || length > 128))
    throw new Error("Each tag must contain 1 through 128 UTF-8 bytes.");
  if (encoded.reduce((total, length) => total + length, 0) > 2048)
    throw new Error("The complete tag set must not exceed 2048 UTF-8 bytes.");
  return tags;
}

export function nonBlankInputLines(value: string): readonly string[] {
  return value
    .split("\n")
    .flatMap((line) => (line.trim() === "" ? [] : [line]));
}

export function parseToolDefinitions(value: string) {
  if (value.trim() === "") return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("Tool definitions must be valid JSON.");
  }
  if (!Array.isArray(parsed) || parsed.length > 128)
    throw new Error(
      "Tool definitions must be one JSON array with no more than 128 items.",
    );
  const tools = parsed.map((item) => {
    const candidate = item as Record<string, unknown>;
    if (
      typeof item !== "object" ||
      item === null ||
      Array.isArray(item) ||
      typeof candidate.name !== "string" ||
      typeof candidate.description !== "string" ||
      typeof candidate.input_schema_json !== "string"
    )
      throw new Error(
        "Each tool needs name, description, and input_schema_json string fields.",
      );
    JSON.parse(candidate.input_schema_json);
    return {
      name: candidate.name,
      description: candidate.description,
      input_schema_json: candidate.input_schema_json,
    };
  });
  if (new Set(tools.map((tool) => tool.name)).size !== tools.length)
    throw new Error("Each tool name must be unique.");
  return tools;
}
