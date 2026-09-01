import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { ConfigurationGraph } from "../src/ConfigurationGraph.tsx";
import {
  adapterFieldPolicy,
  configurationNodeId,
  discardConfirmedRecord,
  discardDeletedRecord,
  excludeDeletedRecords,
  includeConfirmedRecords,
  parseConfigurationNodeId,
  projectConfigurationGraph,
  providerModelPriceFormDefaults,
  pruneAcknowledgedDeletions,
  pruneAcknowledgedRecords,
  retainConfirmedRecord,
  retainDeletedRecord,
  validateAssignmentChain,
} from "../src/configurationState.ts";
import { createAdministrationClient } from "../src/api.ts";
import type {
  Assignment,
  Credential,
  Model,
  Provider,
  ProviderModel,
  Service,
} from "../src/api.ts";
import { configuredPriceValue } from "../src/formContracts.ts";

const provider: Provider = {
  api_name: "openrouter-main",
  display_name: "OpenRouter main",
  adapter: "openrouter",
  credential_api_name: "openrouter-key",
  enabled: true,
  created_at: "2026-08-25T00:00:00Z",
};
const credential: Credential = {
  api_name: "openrouter-key",
  fingerprint: "sha256:proof",
  created_at: "2026-08-25T00:00:00Z",
  updated_at: "2026-08-25T00:00:00Z",
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
const services: readonly Service[] = [
  {
    api_name: "crewday",
    display_name: "Crewday",
    created_at: "2026-08-25T00:00:00Z",
  },
  {
    api_name: "root",
    display_name: "Root service",
    created_at: "2026-08-25T00:00:00Z",
  },
];

describe("configuration graph projection", () => {
  it("uses exact route and assignment rung endpoints", () => {
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
        targetId: "rung:default:1",
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
    expect(parseConfigurationNodeId("rung:default:1")).toEqual({
      id: "rung:default:1",
      kind: "rung",
      apiName: "default",
      position: 1,
    });
    expect(parseConfigurationNodeId("rung:default:0")).toBeNull();
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

  it("keeps assignments but omits edges to mappings that are unavailable", () => {
    const result = projectConfigurationGraph(
      [provider],
      [model],
      [],
      [assignment],
    );

    expect(result.assignmentIds).toEqual(["assignment:default"]);
    expect(result.relationships).toEqual([]);
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

  it("prefills only the price configured directly on a mapping", () => {
    const configuredManualPrice = {
      currency: "EUR",
      unit_prices: [
        { unit: "input_token" as const, amount: "0.004" },
        { unit: "output_token" as const, amount: "0.008" },
      ],
    };
    const direct = {
      ...mapping,
      configured_manual_price: configuredManualPrice,
      effective_price: configuredManualPrice,
    };
    expect(providerModelPriceFormDefaults(direct)).toEqual({
      source: "",
      lookupKey: "",
      currency: "EUR",
      unitPrices: "input_token=0.004, output_token=0.008",
    });
    expect(
      providerModelPriceFormDefaults({
        ...mapping,
        price_source: "openrouter",
        price_lookup_key: "inherited/model",
        effective_price: {
          currency: "USD",
          unit_prices: [{ unit: "input_token", amount: "0.001" }],
          source: "openrouter",
        },
      }),
    ).toEqual({ source: "", lookupKey: "", currency: "", unitPrices: "" });
    const defaults = providerModelPriceFormDefaults(direct);
    expect(
      configuredPriceValue(
        defaults.source,
        defaults.lookupKey,
        defaults.currency,
        defaults.unitPrices,
      ),
    ).toEqual({ manual_price: configuredManualPrice });
    const configuredSource = providerModelPriceFormDefaults({
      ...mapping,
      configured_price_source: "wavespeed",
      configured_price_lookup_key: "media/model",
      price_source: "wavespeed",
      price_lookup_key: "media/model",
    });
    expect(configuredSource).toEqual({
      source: "wavespeed",
      lookupKey: "media/model",
      currency: "",
      unitPrices: "",
    });
    expect(
      configuredPriceValue(
        configuredSource.source,
        configuredSource.lookupKey,
        configuredSource.currency,
        configuredSource.unitPrices,
      ),
    ).toEqual({
      price_source: "wavespeed",
      price_lookup_key: "media/model",
    });
    expect(() => configuredPriceValue("", "orphan", "", "")).toThrow(
      "Enter a price source",
    );
    expect(() => configuredPriceValue("openrouter", "", "", "")).toThrow(
      "Enter the source model identifier",
    );
    expect(() =>
      configuredPriceValue(
        "openrouter",
        "vendor/model",
        "USD",
        "input_token=0.001",
      ),
    ).toThrow("not both");
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
  it("uses the shared compact inspector primitives without local layout", () => {
    const source = readFileSync(
      new URL("../src/ConfigurationGraph.tsx", import.meta.url),
      "utf8",
    );
    const styles = readFileSync(
      new URL("../src/styles.css", import.meta.url),
      "utf8",
    );

    for (const primitive of [
      "GraphInspectorFacts",
      "GraphInspectorFact",
      "GraphInspectorSection",
      "GraphInspectorRows",
      "GraphInspectorRow",
      "GraphInspectorNotice",
    ])
      expect(source).toContain(primitive);
    expect(styles).not.toMatch(
      /\.configuration-graph-page\s+\.od-graph-inspector\s*\{/,
    );
    expect(styles).not.toContain(".configuration-facts");
    expect(styles).not.toContain(".configuration-inspector-section");
    expect(styles).not.toContain(".configuration-inspector-actions");
    expect(styles).not.toContain(".configuration-safe-list");
    const errorFocus = source.slice(
      source.indexOf("function InspectorWriteError"),
      source.indexOf("function ProviderInspector"),
    );
    expect(errorFocus).toContain("errorRef.current?.focus()");
    expect(errorFocus).toContain("tabIndex={-1}");
    expect(errorFocus).not.toContain("preventScroll");
    expect(source).toMatch(
      /rowsActions:[\s\S]*?variant="secondary"[\s\S]*?Add provider route/,
    );
  });

  it("uses shared controls for each supported configuration field type", () => {
    const source = readFileSync(
      new URL("../src/ConfigurationGraph.tsx", import.meta.url),
      "utf8",
    );
    const styles = readFileSync(
      new URL("../src/styles.css", import.meta.url),
      "utf8",
    );

    for (const control of [
      "TextControl",
      "NumberControl",
      "SelectControl",
      "TextareaControl",
      "CheckboxControl",
      "SwitchControl",
    ])
      expect(source).toContain(control);
    expect(source).not.toMatch(/<(?:select|textarea)\b/);
    expect(source.match(/<input\b/g)).toHaveLength(4);
    expect(source.match(/type="hidden"/g)).toHaveLength(2);
    expect(source.match(/type="url"/g)).toHaveLength(1);
    expect(source.match(/type="password"/g)).toHaveLength(1);
    expect(source).toMatch(
      /editable-table-form-control[\s\S]*?od-visually-hidden[\s\S]*?provider route/,
    );
    expect(source).toMatch(
      /Write-only credential[\s\S]*?onReset[\s\S]*?credentialApiName: ""/,
    );
    expect(styles).not.toMatch(/input,\nselect,\ntextarea\s*\{\n\s*width:/);
    expect(styles).not.toContain(".checkbox-field");
  });

  it("keeps the global catalog available without a selected service", () => {
    const client = createAdministrationClient(vi.fn());
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[]}
        client={client}
        credentials={[credential]}
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
    expect(markup).toContain("LLM configuration relationships");
    expect(markup).toContain(">Providers<");
    expect(markup).toContain(">Canonical models<");
    expect(markup).toContain(">Assignments<");
    expect(markup).toContain("Select a service to view assignments.");
    expect(markup).toContain("OpenRouter main");
    expect(markup).toContain("Model ID: reasoning-model");
    expect(markup).toContain("Route ID: openrouter-reasoning");
    expect(markup.match(/tabindex="0"/g)).toHaveLength(1);
    expect(markup).not.toContain("ServiceAssignmentGraph");
  });

  it("labels a partial graph without claiming completeness", () => {
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[assignment]}
        client={createAdministrationClient(vi.fn())}
        credentials={[credential]}
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
    expect(markup).toContain(">Assignments<");
    expect(markup).toContain("Partial");
    expect(markup).toContain('aria-label="Load more Providers"');
    expect(markup).toContain('aria-label="Load more Canonical models"');
    expect(markup).toContain('aria-label="Load more Assignments"');
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
    const errorMarkup = renderToStaticMarkup(
      <ConfigurationGraph {...properties} globalPhase="error" />,
    );
    expect(errorMarkup).toContain("Configuration unavailable");
    expect(errorMarkup).toContain("Unable to load Providers.");
    expect(errorMarkup).toContain("Unable to load Canonical models.");
    expect(errorMarkup).toContain(">Retry</button>");
    expect(errorMarkup).not.toContain("No providers are configured.");
    expect(errorMarkup).not.toContain("No canonical models are configured.");
    expect(
      renderToStaticMarkup(<ConfigurationGraph {...properties} />),
    ).toContain("No providers are configured.");
    expect(
      renderToStaticMarkup(<ConfigurationGraph {...properties} />),
    ).toContain("No canonical models are configured.");
    expect(
      renderToStaticMarkup(<ConfigurationGraph {...properties} />),
    ).toContain("Select a service to view assignments.");
  });

  it("keeps safe board records visible after a refresh error", () => {
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[assignment]}
        client={createAdministrationClient(vi.fn())}
        credentials={[credential]}
        csrf="csrf"
        globalPhase="error"
        models={[model]}
        onAssignmentDirtyChange={vi.fn()}
        onNotice={vi.fn()}
        onRefreshAssignments={vi.fn()}
        onRefreshGlobal={vi.fn()}
        providerModels={[mapping]}
        providers={[provider]}
        selectedService="crewday"
        services={services}
      />,
    );

    expect(markup).not.toContain("Configuration unavailable");
    expect(markup).toContain("Unable to load Providers.");
    expect(markup).toContain("Unable to load Canonical models.");
    expect(markup).toContain("OpenRouter main");
    expect(markup).toContain("Reasoning model");
    expect(markup).toContain("Default");
  });

  it("keeps an unrelated configuration column ready after one initial source failure", () => {
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[]}
        catalogPhase="ready"
        client={createAdministrationClient(vi.fn())}
        credentials={[]}
        csrf="csrf"
        globalPhase="error"
        models={[model]}
        onAssignmentDirtyChange={vi.fn()}
        onNotice={vi.fn()}
        onRefreshAssignments={vi.fn()}
        onRefreshGlobal={vi.fn()}
        providerModels={[mapping]}
        providerPhase="error"
        providers={[]}
        selectedService=""
      />,
    );

    expect(markup).toContain("Unable to load Providers.");
    expect(markup).not.toContain("Unable to load Canonical models.");
    expect(markup).toContain("Reasoning model");
    expect(markup).toContain("Unavailable provider: openrouter-main");
    expect(markup).toContain(">Retry</button>");

    const catalogFailureMarkup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[]}
        catalogPhase="error"
        client={createAdministrationClient(vi.fn())}
        credentials={[credential]}
        csrf="csrf"
        globalPhase="error"
        models={[]}
        onAssignmentDirtyChange={vi.fn()}
        onNotice={vi.fn()}
        onRefreshAssignments={vi.fn()}
        onRefreshGlobal={vi.fn()}
        providerModels={[]}
        providerPhase="ready"
        providers={[provider]}
        selectedService=""
      />,
    );

    expect(catalogFailureMarkup).not.toContain("Unable to load Providers.");
    expect(catalogFailureMarkup).toContain("Unable to load Canonical models.");
    expect(catalogFailureMarkup).toContain("OpenRouter main");
    expect(catalogFailureMarkup).toContain(">Retry</button>");
  });

  it("shows assignment loading and error states without removing safe records", () => {
    const properties = {
      assignments: [assignment],
      client: createAdministrationClient(vi.fn()),
      credentials: [credential],
      csrf: "csrf",
      models: [model],
      onAssignmentDirtyChange: vi.fn(),
      onNotice: vi.fn(),
      onRefreshAssignments: vi.fn(),
      onRefreshGlobal: vi.fn(),
      providerModels: [mapping],
      providers: [provider],
      selectedService: "crewday",
      services,
    } as const;
    const loadingMarkup = renderToStaticMarkup(
      <ConfigurationGraph {...properties} assignmentPhase="loading" />,
    );
    expect(loadingMarkup).toContain(">Loading<");
    expect(loadingMarkup).toContain("Default");
    expect(loadingMarkup).toMatch(
      /data-node-id="assignment:default"[^>]*data-state="loading"/,
    );
    const emptyLoadingMarkup = renderToStaticMarkup(
      <ConfigurationGraph
        {...properties}
        assignmentPhase="loading"
        assignments={[]}
        models={[]}
        providerModels={[]}
        providers={[]}
      />,
    );
    expect(emptyLoadingMarkup).toContain("Loading Assignments.");
    expect(emptyLoadingMarkup).not.toContain(
      "No assignments are configured for this service.",
    );

    const errorMarkup = renderToStaticMarkup(
      <ConfigurationGraph {...properties} assignmentPhase="error" />,
    );
    expect(errorMarkup).toContain("Unable to load Assignments.");
    expect(errorMarkup).toContain(">Retry</button>");
    expect(errorMarkup).toContain("Default");
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
    expect(markup).toContain("Disabled");
    expect(markup.match(/data-state="disabled"/g)).toHaveLength(2);
  });

  it("renders compound cards, exact identities, rungs, and relationships", () => {
    const localProvider: Provider = {
      api_name: "local-proof",
      display_name: "Local proof",
      adapter: "fake",
      enabled: true,
      created_at: "2026-08-25T00:00:00Z",
    };
    const secondRoute: ProviderModel = {
      ...mapping,
      api_name: "local-reasoning",
      provider_api_name: localProvider.api_name,
      provider_model_name: "proof/reasoning",
      enabled: false,
      capabilities: ["streaming"],
    };
    const emptyModel: Model = {
      ...model,
      api_name: "long-empty-model",
      display_name:
        "A very long canonical model name that must wrap inside its card",
      input_modalities: ["image"],
      output_modalities: ["image"],
      capabilities: [],
    };
    const inherited: Assignment = {
      ...assignment,
      api_name: "inherited",
      display_name: "Inherited workflow",
      definition_kind: "direct_chain",
      defined_by_service_api_name: "root",
      direct_chain: null,
      effective_chain: [{ provider_model_api_name: mapping.api_name }],
      observed_requirements: ["image_input"],
      last_used_at: "2026-08-29T12:00:00Z",
    };
    const local: Assignment = {
      ...assignment,
      api_name: "workflow",
      display_name: "Workflow",
      effective_chain: [
        { provider_model_api_name: mapping.api_name },
        { provider_model_api_name: secondRoute.api_name },
      ],
      direct_chain: [
        { provider_model_api_name: mapping.api_name },
        { provider_model_api_name: secondRoute.api_name },
      ],
    };
    const implicit: Assignment = {
      ...assignment,
      api_name: "implicit-default",
      display_name: "Implicit default",
      definition_kind: "implicit",
      defined_by_service_api_name: null,
      direct_chain: null,
      effective_chain: [],
      observed_requirements: [],
    };
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[local, inherited, implicit]}
        client={createAdministrationClient(vi.fn())}
        credentials={[credential]}
        csrf="csrf"
        models={[model, emptyModel]}
        onAssignmentDirtyChange={vi.fn()}
        onNotice={vi.fn()}
        onRefreshAssignments={vi.fn()}
        onRefreshGlobal={vi.fn()}
        providerModels={[mapping, secondRoute]}
        providers={[provider, localProvider]}
        selectedService="crewday"
        services={services}
      />,
    );

    expect(markup).toContain('aria-label="LLM configuration relationships"');
    expect(markup.indexOf(">Providers<")).toBeLessThan(
      markup.indexOf(">Canonical models<"),
    );
    expect(markup.indexOf(">Canonical models<")).toBeLessThan(
      markup.indexOf(">Assignments<"),
    );
    expect(markup).toContain('data-group-id="model:reasoning-model"');
    expect(markup).toContain('data-node-id="mapping:openrouter-reasoning"');
    expect(markup).toContain('data-node-id="mapping:local-reasoning"');
    expect(markup).toContain("Provider routes");
    expect(markup).toContain(
      "Provider ID: openrouter-main · Adapter: OpenRouter",
    );
    expect(markup).toContain("Model ID: reasoning-model");
    expect(markup).toContain(
      "Route ID: openrouter-reasoning · Wire model: vendor/model",
    );
    expect(markup).toContain(
      "Text input · Text output · Reasoning · Streaming",
    );
    expect(markup).toContain('data-node-id="rung:workflow:1"');
    expect(markup).toContain('data-node-id="rung:workflow:2"');
    expect(markup).toContain(">Primary<");
    expect(markup).toContain(">Fallback 2<");
    expect(markup).toContain("Local definition");
    expect(markup).toContain("Inherited from Root service (root)");
    expect(markup).toContain("Implicit root default");
    expect(markup).toContain("Last used: 2026-08-29T12:00:00Z");
    expect(markup).toContain("No observed requirements.");
    expect(markup).toContain("Does not meet observed requirements");
    expect(markup).toContain(
      "OpenRouter main (Provider ID: openrouter-main) provides Route ID: openrouter-reasoning for Reasoning model (Model ID: reasoning-model)",
    );
    expect(markup).toContain(
      "Primary: Route ID: openrouter-reasoning for Workflow (Assignment ID: workflow)",
    );
    expect(markup).toContain(
      "Primary: Route ID: openrouter-reasoning for Inherited workflow (Assignment ID: inherited)",
    );
    expect(markup).toContain("No provider routes.");
    expect(markup).toContain("Add provider route");
    expect(markup).toContain('data-state="ready"');
    expect(markup).toContain('data-state="enabled"');
    expect(markup).toContain('data-state="disabled"');
    expect(markup).toContain('data-state="unavailable"');
    expect(markup).toContain('data-state="empty"');
    expect(markup.match(/tabindex="0"/g)).toHaveLength(1);
  });

  it("keeps missing references explicit and provides retry actions", () => {
    const missingModelRoute: ProviderModel = {
      ...mapping,
      api_name: "missing-model-route",
      model_api_name: "missing-model",
    };
    const missingReferences: Assignment = {
      ...assignment,
      api_name: "missing-references",
      display_name: "Missing references",
      defined_by_service_api_name: "missing-service",
      inherits_assignment_api_name: "missing-assignment",
      effective_chain: [{ provider_model_api_name: "missing-route" }],
    };
    const markup = renderToStaticMarkup(
      <ConfigurationGraph
        assignments={[missingReferences]}
        client={createAdministrationClient(vi.fn())}
        credentials={[credential]}
        csrf="csrf"
        models={[model]}
        onAssignmentDirtyChange={vi.fn()}
        onNotice={vi.fn()}
        onRefreshAssignments={vi.fn()}
        onRefreshGlobal={vi.fn()}
        providerModels={[
          { ...mapping, provider_api_name: "missing-provider" },
          missingModelRoute,
        ]}
        providers={[provider]}
        selectedService="crewday"
        services={services}
      />,
    );

    expect(markup).toContain(
      "Inherited from unavailable service (missing-service)",
    );
    expect(markup).toContain(
      "Inherits unavailable assignment (missing-assignment)",
    );
    expect(markup).toContain("Unavailable provider: missing-provider");
    expect(markup).toContain(
      "Unavailable model: missing-model · Route ID: missing-model-route",
    );
    expect(markup).toContain("Unavailable route: missing-route");
    expect(markup).toMatch(
      /data-node-id="mapping:openrouter-reasoning"[^>]*data-state="unavailable"/,
    );
    expect(markup).toMatch(
      /data-node-id="mapping:missing-model-route"[^>]*data-state="unavailable"/,
    );
    expect(markup).toMatch(
      /data-node-id="assignment:missing-references"[^>]*data-state="unavailable"/,
    );
    expect(
      markup.match(/>Retry<\/button>/g)?.length ?? 0,
    ).toBeGreaterThanOrEqual(3);
  });
});
