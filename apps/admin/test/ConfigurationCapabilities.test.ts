import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { ConfigurationGraph } from "../src/ConfigurationGraph.tsx";
import { createAdministrationClient } from "../src/api.ts";
import type { Assignment, Model, ProviderModel } from "../src/api.ts";
import {
  modelCapabilityKeys,
  modelHasCapability,
} from "../src/configurationCapabilities.ts";
import {
  assignmentChainSource,
  projectConfigurationGraph,
} from "../src/configurationState.ts";

const chain = [{ provider_model_api_name: "route" }];
const source: Assignment = {
  api_name: "source",
  display_name: "Source",
  definition_kind: "direct_chain",
  defined_by_service_api_name: "parent",
  direct_chain: chain,
  effective_chain: chain,
  observed_requirements: [],
};
function inherited(apiName: string, target: string): Assignment {
  return {
    ...source,
    api_name: apiName,
    display_name: apiName,
    definition_kind: "inherited_assignment",
    direct_chain: null,
    inherits_assignment_api_name: target,
  };
}
const model: Model = {
  api_name: "multimodal",
  display_name: "Multimodal",
  input_modalities: ["text", "image"],
  output_modalities: ["text", "image", "structured_json"],
  capabilities: ["reasoning", "tool_calling", "streaming"],
  created_at: "2026-01-01T00:00:00Z",
};
const route: ProviderModel = {
  ...model,
  api_name: "route",
  provider_api_name: "provider",
  model_api_name: "multimodal",
  provider_model_name: "wire",
  enabled: true,
  reasoning_mappings: [],
  output_modalities: ["text"],
};

describe("configuration inheritance cards", () => {
  it("uses one source chain for direct and multi-hop inherited assignments", () => {
    const child = inherited("child", "source");
    const grandchild = inherited("grandchild", "child");
    const result = projectConfigurationGraph(
      [],
      [model],
      [route],
      [grandchild, source, child],
    );
    expect(result.assignmentIds).toHaveLength(3);
    expect(result.assignmentGroups).toEqual([
      {
        sourceId: "assignment:source",
        inheritedIds: ["assignment:child", "assignment:grandchild"],
      },
    ]);
    expect(
      result.relationships.filter((edge) => edge.targetId.startsWith("rung:")),
    ).toEqual([
      {
        id: "mapping-assignment:route:source:0",
        sourceId: "mapping:route",
        targetId: "rung:source:1",
      },
    ]);
    expect(
      assignmentChainSource(
        grandchild,
        new Map(
          [source, child, grandchild].map((item) => [item.api_name, item]),
        ),
      ),
    ).toBe(source);
  });

  it("keeps service origin and child requirement errors in inherited rows", () => {
    const child = {
      ...inherited("child", "source"),
      observed_requirements: ["image_input" as const],
    };
    const markup = renderToStaticMarkup(
      createElement(ConfigurationGraph, {
        assignments: [source, child],
        client: createAdministrationClient(vi.fn()),
        credentials: [],
        csrf: "synthetic-csrf",
        models: [model],
        providerModels: [{ ...route, input_modalities: ["text"] }],
        providers: [
          {
            api_name: "provider",
            display_name: "Provider",
            adapter: "fake",
            enabled: true,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        services: [
          {
            api_name: "parent",
            display_name: "Parent service",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        selectedService: "child-service",
        onAssignmentDirtyChange: vi.fn(),
        onNotice: vi.fn(),
        onRefreshAssignments: vi.fn(),
        onRefreshGlobal: vi.fn(),
      }),
    );
    const childControl =
      /<button[^>]*data-node-id="assignment:child"[\s\S]*?<\/button>/.exec(
        markup,
      )?.[0];
    expect(childControl).toContain(
      'title="Inherited from Parent service (parent)"',
    );
    expect(childControl).toContain("Does not meet observed requirements");
    expect(markup).not.toContain('data-node-id="rung:child:1"');
  });

  it("keeps missing targets and cycles visible without an infinite walk", () => {
    const missing = inherited("missing", "absent");
    const left = inherited("left", "right");
    const right = inherited("right", "left");
    const records = [missing, left, right];
    const byName = new Map(records.map((item) => [item.api_name, item]));
    for (const item of records)
      expect(assignmentChainSource(item, byName)).toBe(item);
    expect(
      projectConfigurationGraph([], [], [], records).assignmentGroups,
    ).toHaveLength(3);
  });
});

describe("configuration capability filters", () => {
  it("keeps input and output distinct on multimodal models", () => {
    expect(modelCapabilityKeys(model)).toEqual([
      "text_input",
      "image_input",
      "text_output",
      "image_output",
    ]);
    expect(modelHasCapability(model, "image_input")).toBe(true);
    expect(modelHasCapability(model, "audio_output")).toBe(false);
    expect(modelHasCapability(model, "structured_json_output")).toBe(true);
    expect(modelHasCapability(model, "tool_calling")).toBe(true);
    expect(modelCapabilityKeys(model)).not.toContain("structured_json_output");
    expect(modelCapabilityKeys(model)).not.toContain("tool_calling");
    expect(modelCapabilityKeys(model)).not.toContain("streaming");
    expect(modelCapabilityKeys(model)).not.toContain("reasoning");
  });
  it("uses route capabilities when the route narrows the model", () => {
    expect(modelHasCapability(model, "image_output")).toBe(true);
    expect(modelHasCapability(route, "image_output")).toBe(false);
    expect(modelHasCapability(route, "image_input")).toBe(true);
  });
});
