import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ConfigurationGraph } from "../src/ConfigurationGraph.js";
import {
  adapterFieldPolicy,
  configurationNodeId,
  discardConfirmedRecord,
  discardDeletedRecord,
  excludeDeletedRecords,
  includeConfirmedRecords,
  parseConfigurationNodeId,
  projectConfigurationGraph,
  pruneAcknowledgedDeletions,
  pruneAcknowledgedRecords,
  retainConfirmedRecord,
  retainDeletedRecord,
  validateAssignmentChain,
} from "../src/configurationState.js";
import { createAdministrationClient } from "../src/api.js";
import type { Assignment, Model, Provider, ProviderModel } from "../src/api.js";

const provider: Provider = {
  api_name: "openrouter-main",
  display_name: "OpenRouter main",
  adapter: "openrouter",
  credential_api_name: "openrouter-key",
  enabled: true,
  created_at: "2026-08-25T00:00:00Z",
};
const model: Model = {
  api_name: "reasoning-model",
  display_name: "Reasoning model",
  input_modalities: ["text"],
  output_modalities: ["text"],
  capabilities: ["reasoning", "streaming"],
  constraints: { max_context_tokens: 128_000, max_output_tokens: 16_384 },
  price_source: "openrouter",
  price_lookup_key: "vendor/model",
  created_at: "2026-08-25T00:00:00Z",
};
const mapping: ProviderModel = {
  api_name: "openrouter-reasoning",
  provider_api_name: provider.api_name,
  model_api_name: model.api_name,
  provider_model_name: "vendor/model",
  enabled: true,
  input_modalities: ["text"],
  output_modalities: ["text"],
  capabilities: ["reasoning", "streaming"],
  reasoning_mappings: [
    { level: "none", provider_value: "disabled" },
    { level: "high", provider_value: "high" },
  ],
  created_at: "2026-08-25T00:00:00Z",
};
const assignment: Assignment = {
  api_name: "default",
  display_name: "Default",
  definition_kind: "direct_chain",
  defined_by_service_api_name: "crewday",
  direct_chain: [{ provider_model_api_name: mapping.api_name }],
  effective_chain: [{ provider_model_api_name: mapping.api_name }],
  observed_requirements: ["text_input", "reasoning"],
};

describe("configuration graph projection", () => {
  it("uses separate stable identities and provider to mapping to assignment edges", () => {
    const result = projectConfigurationGraph(
      [provider],
      [model],
      [mapping],
      [assignment],
    );
    expect(result.providerIds).toEqual(["provider:openrouter-main"]);
    expect(result.catalogIds).toEqual([
      "model:reasoning-model",
      "mapping:openrouter-reasoning",
    ]);
    expect(result.assignmentIds).toEqual(["assignment:default"]);
    expect(result.relationships).toEqual([
      {
        id: "provider-mapping:openrouter-main:openrouter-reasoning",
        sourceId: "provider:openrouter-main",
        targetId: "mapping:openrouter-reasoning",
      },
      {
        id: "mapping-assignment:openrouter-reasoning:default:0",
        sourceId: "mapping:openrouter-reasoning",
        targetId: "assignment:default",
      },
    ]);
    expect(configurationNodeId.model("same")).not.toBe(
      configurationNodeId.mapping("same"),
    );
    expect(parseConfigurationNodeId("assignment:default")).toEqual({
      id: "assignment:default",
      kind: "assignment",
      apiName: "default",
    });
    expect(parseConfigurationNodeId("unknown:default")).toBeNull();
  });

  it("groups mappings after their canonical model with stable order", () => {
    const second = { ...mapping, api_name: "a-mapping" };
    expect(
      projectConfigurationGraph([provider], [model], [mapping, second], [])
        .catalogIds,
    ).toEqual([
      "model:reasoning-model",
      "mapping:a-mapping",
      "mapping:openrouter-reasoning",
    ]);
  });
});

describe("configuration policy", () => {
  it("uses the accepted adapter credential and endpoint matrix", () => {
    expect(adapterFieldPolicy.openai).toEqual({
      endpoint: "inferred",
      credential: "required",
    });
    expect(adapterFieldPolicy.openrouter).toEqual(adapterFieldPolicy.openai);
    expect(adapterFieldPolicy.wavespeed).toEqual(adapterFieldPolicy.openai);
    expect(adapterFieldPolicy.fake).toEqual({
      endpoint: "inferred",
      credential: "none",
    });
    expect(adapterFieldPolicy.local_embeddings).toEqual(
      adapterFieldPolicy.fake,
    );
    for (const adapter of ["openai_compatible", "custom", "ollama"] as const)
      expect(adapterFieldPolicy[adapter]).toEqual({
        endpoint: "required",
        credential: "optional",
      });
  });

  it("accepts only 1 through 16 unique ordered assignment candidates", () => {
    expect(validateAssignmentChain(["one"])).toBeNull();
    expect(
      validateAssignmentChain(
        Array.from({ length: 16 }, (_, index) => `mapping-${String(index)}`),
      ),
    ).toBeNull();
    expect(validateAssignmentChain([])).toContain("1 through 16");
    expect(
      validateAssignmentChain(Array.from({ length: 17 }, () => "x")),
    ).toContain("1 through 16");
    expect(validateAssignmentChain(["one", "one"])).toContain("only once");
  });

  it("keeps a confirmed create until the refreshed list contains it", () => {
    const confirmed = { ...provider, api_name: "confirmed-create" };
    expect(includeConfirmedRecords([provider], [confirmed])).toEqual([
      provider,
      confirmed,
    ]);
    expect(includeConfirmedRecords([confirmed], [confirmed])).toEqual([
      confirmed,
    ]);
    const replaced = { ...confirmed, display_name: "Confirmed replacement" };
    expect(retainConfirmedRecord([provider, confirmed], replaced)).toEqual([
      provider,
      replaced,
    ]);
  });

  it("shows a confirmed replacement until the full refreshed value matches it", () => {
    const confirmed = {
      ...provider,
      display_name: "Confirmed replacement",
    };
    expect(includeConfirmedRecords([provider], [confirmed])).toEqual([
      confirmed,
    ]);
    expect(pruneAcknowledgedRecords([provider], [confirmed])).toEqual([
      confirmed,
    ]);
    expect(pruneAcknowledgedRecords([confirmed], [confirmed])).toEqual([]);
  });

  it("does not restore a confirmed record after parent catch-up or local deletion", () => {
    const confirmed = { ...provider, api_name: "confirmed-create" };
    let overlay: readonly Provider[] = [confirmed];
    expect(includeConfirmedRecords([], overlay)).toEqual([confirmed]);

    overlay = pruneAcknowledgedRecords([confirmed], overlay);
    expect(overlay).toEqual([]);
    expect(includeConfirmedRecords([], overlay)).toEqual([]);

    expect(discardConfirmedRecord([confirmed], confirmed.api_name)).toEqual([]);
  });

  it("hides confirmed deletions until the authoritative list removes them", () => {
    let deleted: readonly string[] = [];
    deleted = retainDeletedRecord(deleted, provider.api_name);
    deleted = retainDeletedRecord(deleted, provider.api_name);

    expect(deleted).toEqual([provider.api_name]);
    expect(excludeDeletedRecords([provider], deleted)).toEqual([]);
    expect(pruneAcknowledgedDeletions([provider], deleted)).toEqual(deleted);

    expect(discardDeletedRecord(deleted, provider.api_name)).toEqual([]);

    deleted = pruneAcknowledgedDeletions([], deleted);
    expect(deleted).toEqual([]);
    expect(excludeDeletedRecords([provider], deleted)).toEqual([provider]);
  });
});

describe("configuration graph composition", () => {
  it("keeps the global catalog available without a selected service", () => {
    const client = createAdministrationClient(vi.fn());
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[]}
        client={client}
        credentials={[]}
        csrf="csrf"
        models={[model]}
        onAssignmentDirtyChange={vi.fn()}
        onNotice={vi.fn()}
        onRefreshAssignments={vi.fn()}
        onRefreshGlobal={vi.fn()}
        providerModels={[mapping]}
        providers={[provider]}
        selectedService=""
      />,
    );
    expect(markup).toContain("Global providers");
    expect(markup).toContain("Global models and mappings");
    expect(markup).toContain("Select one service to configure assignments");
    expect(markup).toContain("OpenRouter main");
    expect(markup).toContain("Reasoning model — openrouter-reasoning");
    expect(markup.match(/tabindex="0"/g)).toHaveLength(1);
    expect(markup).not.toContain("ServiceAssignmentGraph");
  });

  it("labels a partial graph without claiming completeness", () => {
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[assignment]}
        client={createAdministrationClient(vi.fn())}
        credentials={[]}
        csrf="csrf"
        globalPhase="partial"
        models={[model]}
        onAssignmentDirtyChange={vi.fn()}
        onNotice={vi.fn()}
        onRefreshAssignments={vi.fn()}
        onRefreshGlobal={vi.fn()}
        providerModels={[mapping]}
        providers={[provider]}
        selectedService="crewday"
      />,
    );
    expect(markup).toContain("Partial configuration graph");
    expect(markup).toContain("does not claim to be complete");
    expect(markup).toContain("crewday assignments");
  });

  it("renders deterministic loading, error, and complete empty states", () => {
    const properties = {
      assignments: [],
      client: createAdministrationClient(vi.fn()),
      credentials: [],
      csrf: "csrf",
      models: [],
      onAssignmentDirtyChange: vi.fn(),
      onNotice: vi.fn(),
      onRefreshAssignments: vi.fn(),
      onRefreshGlobal: vi.fn(),
      providerModels: [],
      providers: [],
      selectedService: "",
    } as const;
    expect(
      renderToStaticMarkup(
        <ConfigurationGraph {...properties} globalPhase="loading" />,
      ),
    ).toContain("Loading configuration");
    expect(
      renderToStaticMarkup(
        <ConfigurationGraph {...properties} globalPhase="error" />,
      ),
    ).toContain("Configuration unavailable");
    expect(
      renderToStaticMarkup(<ConfigurationGraph {...properties} />),
    ).toContain("No configuration records are available");
  });

  it("marks disabled global connections without changing their ownership", () => {
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[]}
        client={createAdministrationClient(vi.fn())}
        credentials={[]}
        csrf="csrf"
        models={[model]}
        onAssignmentDirtyChange={vi.fn()}
        onNotice={vi.fn()}
        onRefreshAssignments={vi.fn()}
        onRefreshGlobal={vi.fn()}
        providerModels={[{ ...mapping, enabled: false }]}
        providers={[{ ...provider, enabled: false }]}
        selectedService="crewday"
      />,
    );
    expect(markup).toContain("Global and disabled");
    expect(markup).toContain("Global provider-model mapping, disabled");
    expect(markup.match(/data-state="disabled"/g)).toHaveLength(2);
  });
});
