import type {
  Assignment,
  Provider,
  ProviderAdapter,
  ProviderModel,
  Model,
} from "./api.ts";

export type ConfigurationRecordKind =
  "provider" | "model" | "mapping" | "assignment";

export type ConfigurationNodeKind = ConfigurationRecordKind | "rung";

export type ConfigurationLoadPhase =
  "loading" | "ready" | "empty" | "error" | "partial";

export interface ConfigurationNodeIdentity {
  readonly id: string;
  readonly kind: ConfigurationNodeKind;
  readonly apiName: string;
  readonly position?: number;
}

export interface ConfigurationGraphProjection {
  readonly providerIds: readonly string[];
  readonly catalogIds: readonly string[];
  readonly assignmentIds: readonly string[];
  readonly relationships: readonly {
    readonly id: string;
    readonly sourceId: string;
    readonly targetId: string;
  }[];
}

export const configurationNodeId = {
  provider: (apiName: string) => `provider:${apiName}`,
  model: (apiName: string) => `model:${apiName}`,
  mapping: (apiName: string) => `mapping:${apiName}`,
  assignment: (apiName: string) => `assignment:${apiName}`,
  rung: (assignmentApiName: string, position: number) =>
    `rung:${assignmentApiName}:${String(position)}`,
} as const;

export function parseConfigurationNodeId(
  value: string,
): ConfigurationNodeIdentity | null {
  const separator = value.indexOf(":");
  if (separator < 1 || separator === value.length - 1) return null;
  const kind = value.slice(0, separator);
  if (kind === "rung") {
    const payload = value.slice(separator + 1);
    const positionSeparator = payload.lastIndexOf(":");
    if (positionSeparator < 1) return null;
    const apiName = payload.slice(0, positionSeparator);
    const position = Number(payload.slice(positionSeparator + 1));
    if (!Number.isInteger(position) || position < 1) return null;
    return { id: value, kind, apiName, position };
  }
  if (
    kind !== "provider" &&
    kind !== "model" &&
    kind !== "mapping" &&
    kind !== "assignment"
  )
    return null;
  return {
    id: value,
    kind,
    apiName: value.slice(separator + 1),
  };
}

export function projectConfigurationGraph(
  providers: readonly Provider[],
  models: readonly Model[],
  mappings: readonly ProviderModel[],
  assignments: readonly Assignment[],
): ConfigurationGraphProjection {
  // react-doctor-disable-next-line react-doctor/js-tosorted-immutable -- The consumer TypeScript target does not include Array.toSorted yet.
  const sortedProviders = [...providers].sort((left, right) =>
    left.api_name.localeCompare(right.api_name),
  );
  // react-doctor-disable-next-line react-doctor/js-tosorted-immutable -- The consumer TypeScript target does not include Array.toSorted yet.
  const sortedModels = [...models].sort((left, right) =>
    left.api_name.localeCompare(right.api_name),
  );
  // react-doctor-disable-next-line react-doctor/js-tosorted-immutable -- The consumer TypeScript target does not include Array.toSorted yet.
  const sortedMappings = [...mappings].sort((left, right) => {
    const modelOrder = left.model_api_name.localeCompare(right.model_api_name);
    return modelOrder === 0
      ? left.api_name.localeCompare(right.api_name)
      : modelOrder;
  });
  // react-doctor-disable-next-line react-doctor/js-tosorted-immutable -- The consumer TypeScript target does not include Array.toSorted yet.
  const sortedAssignments = [...assignments].sort((left, right) =>
    left.api_name.localeCompare(right.api_name),
  );
  const providerIds = sortedProviders.map((item) =>
    configurationNodeId.provider(item.api_name),
  );
  const mappingsByModel = new Map<string, string[]>();
  for (const mapping of sortedMappings) {
    const ids = mappingsByModel.get(mapping.model_api_name) ?? [];
    ids.push(configurationNodeId.mapping(mapping.api_name));
    mappingsByModel.set(mapping.model_api_name, ids);
  }
  const catalogIds = sortedModels.flatMap((model) => [
    configurationNodeId.model(model.api_name),
    ...(mappingsByModel.get(model.api_name) ?? []),
  ]);
  const knownModels = new Set(sortedModels.map((item) => item.api_name));
  for (const mapping of sortedMappings)
    if (!knownModels.has(mapping.model_api_name))
      catalogIds.push(configurationNodeId.mapping(mapping.api_name));
  const assignmentIds = sortedAssignments.map((item) =>
    configurationNodeId.assignment(item.api_name),
  );
  const rungIds = sortedAssignments.flatMap((assignment) =>
    assignment.effective_chain.map((_, index) =>
      configurationNodeId.rung(assignment.api_name, index + 1),
    ),
  );
  const nodeIds = new Set([
    ...providerIds,
    ...catalogIds,
    ...assignmentIds,
    ...rungIds,
  ]);
  const relationships = [
    ...sortedMappings.map((mapping) => ({
      id: `provider-mapping:${mapping.provider_api_name}:${mapping.api_name}`,
      sourceId: configurationNodeId.provider(mapping.provider_api_name),
      targetId: configurationNodeId.mapping(mapping.api_name),
    })),
    ...sortedAssignments.flatMap((assignment) =>
      assignment.effective_chain.map((candidate, index) => ({
        id: `mapping-assignment:${candidate.provider_model_api_name}:${assignment.api_name}:${String(index)}`,
        sourceId: configurationNodeId.mapping(
          candidate.provider_model_api_name,
        ),
        targetId: configurationNodeId.rung(assignment.api_name, index + 1),
      })),
    ),
  ].filter(
    (relationship) =>
      nodeIds.has(relationship.sourceId) && nodeIds.has(relationship.targetId),
  );
  return { providerIds, catalogIds, assignmentIds, relationships };
}

export interface AdapterFieldPolicy {
  readonly endpoint: "inferred" | "required";
  readonly credential: "required" | "optional" | "none";
}

export const adapterFieldPolicy: Readonly<
  Record<ProviderAdapter, AdapterFieldPolicy>
> = {
  openai: { endpoint: "inferred", credential: "required" },
  openrouter: { endpoint: "inferred", credential: "required" },
  wavespeed: { endpoint: "inferred", credential: "required" },
  local_embeddings: { endpoint: "inferred", credential: "none" },
  fake: { endpoint: "inferred", credential: "none" },
  openai_compatible: { endpoint: "required", credential: "optional" },
  custom: { endpoint: "required", credential: "optional" },
  ollama: { endpoint: "required", credential: "optional" },
};

export function providerModelPriceFormDefaults(
  mapping: ProviderModel | undefined,
) {
  return {
    source: mapping?.configured_price_source ?? "",
    lookupKey: mapping?.configured_price_lookup_key ?? "",
    currency: mapping?.configured_manual_price?.currency ?? "",
    unitPrices:
      mapping?.configured_manual_price?.unit_prices
        .map((item) => `${item.unit}=${item.amount}`)
        .join(", ") ?? "",
  };
}

export function validateAssignmentChain(
  chain: readonly string[],
): string | null {
  if (chain.length < 1 || chain.length > 16)
    return "Enter 1 through 16 provider-model mappings.";
  if (new Set(chain).size !== chain.length)
    return "Use each provider-model mapping only once.";
  if (chain.some((item) => item.trim() === ""))
    return "Select a provider-model mapping for each fallback position.";
  return null;
}

export function includeConfirmedRecords<
  T extends { readonly api_name: string },
>(records: readonly T[], confirmed: readonly T[]): readonly T[] {
  if (confirmed.length === 0) return records;
  const overlays = new Map(confirmed.map((item) => [item.api_name, item]));
  const known = new Set(records.map((item) => item.api_name));
  return [
    ...records.map((item) => overlays.get(item.api_name) ?? item),
    ...confirmed.filter((item) => !known.has(item.api_name)),
  ];
}

export function retainConfirmedRecord<T extends { readonly api_name: string }>(
  records: readonly T[],
  confirmed: T,
): readonly T[] {
  return [
    ...records.filter((item) => item.api_name !== confirmed.api_name),
    confirmed,
  ];
}

export function retainDeletedRecord(
  deleted: readonly string[],
  apiName: string,
): readonly string[] {
  return deleted.includes(apiName) ? deleted : [...deleted, apiName];
}

export function discardDeletedRecord(
  deleted: readonly string[],
  apiName: string,
): readonly string[] {
  const retained = deleted.filter((item) => item !== apiName);
  return retained.length === deleted.length ? deleted : retained;
}

export function excludeDeletedRecords<T extends { readonly api_name: string }>(
  records: readonly T[],
  deleted: readonly string[],
): readonly T[] {
  if (deleted.length === 0) return records;
  const hidden = new Set(deleted);
  return records.filter((item) => !hidden.has(item.api_name));
}

export function pruneAcknowledgedDeletions(
  authoritative: readonly { readonly api_name: string }[],
  deleted: readonly string[],
): readonly string[] {
  if (deleted.length === 0) return deleted;
  const present = new Set(authoritative.map((item) => item.api_name));
  const retained = deleted.filter((apiName) => present.has(apiName));
  return retained.length === deleted.length ? deleted : retained;
}

export function pruneAcknowledgedRecords<
  T extends { readonly api_name: string },
>(authoritative: readonly T[], confirmed: readonly T[]): readonly T[] {
  if (confirmed.length === 0) return confirmed;
  const current = new Map(
    authoritative.map((item) => [item.api_name, JSON.stringify(item)]),
  );
  const retained = confirmed.filter(
    (item) => current.get(item.api_name) !== JSON.stringify(item),
  );
  return retained.length === confirmed.length ? confirmed : retained;
}

export function discardConfirmedRecord<T extends { readonly api_name: string }>(
  records: readonly T[],
  apiName: string,
): readonly T[] {
  const retained = records.filter((item) => item.api_name !== apiName);
  return retained.length === records.length ? records : retained;
}
