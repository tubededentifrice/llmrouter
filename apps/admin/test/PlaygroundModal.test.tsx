import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PlaygroundModal } from "../src/PlaygroundModal.js";
import {
  clientDeadlineMilliseconds,
  createAdministrationClient,
} from "../src/api.js";
import type {
  AdministratorPlaygroundMediaJob,
  Assignment,
  Model,
  Provider,
  ProviderModel,
} from "../src/api.js";
import {
  assignmentPlaygroundTarget,
  mappingPlaygroundTarget,
  nonBlankInputLines,
  parseTags,
  parseToolDefinitions,
  pollMediaJob,
  playgroundTargetKey,
  targetUnavailableMessage,
  updateMediaRecovery,
} from "../src/playgroundState.js";

afterEach(() => {
  vi.useRealTimers();
});

const provider: Provider = {
  api_name: "fake-provider",
  display_name: "Fake provider",
  adapter: "fake",
  enabled: true,
  created_at: "2026-08-25T00:00:00Z",
};

const model: Model = {
  api_name: "complete-model",
  display_name: "Complete model",
  input_modalities: ["text", "image"],
  output_modalities: [
    "text",
    "structured_json",
    "embedding",
    "image",
    "video",
    "audio",
  ],
  capabilities: ["tool_calling", "streaming"],
  constraints: { max_output_tokens: 4096 },
  created_at: "2026-08-25T00:00:00Z",
};

const mapping: ProviderModel = {
  api_name: "fake-complete",
  provider_api_name: provider.api_name,
  model_api_name: model.api_name,
  provider_model_name: "complete",
  enabled: true,
  input_modalities: model.input_modalities,
  output_modalities: model.output_modalities,
  capabilities: model.capabilities,
  reasoning_mappings: [],
  created_at: "2026-08-25T00:00:00Z",
};

const assignment: Assignment = {
  api_name: "default",
  display_name: "Default route",
  definition_kind: "direct_chain",
  defined_by_service_api_name: "crewday",
  direct_chain: [{ provider_model_api_name: mapping.api_name }],
  effective_chain: [{ provider_model_api_name: mapping.api_name }],
  observed_requirements: [],
};

describe("administrator playground target projection", () => {
  it("infers exact operations and controls from effective mapping facts", () => {
    const target = mappingPlaygroundTarget(
      mapping.api_name,
      [mapping],
      [provider],
      [model],
    );
    expect(target).not.toBeNull();
    expect(target?.selector).toEqual({
      provider_model_api_name: mapping.api_name,
    });
    expect(target?.operations).toEqual([
      {
        operation: "model",
        controls: [
          "system-prompt",
          "temperature",
          "output-limit",
          "input-images",
        ],
      },
      { operation: "embedding", controls: [] },
      { operation: "image", controls: ["input-images"] },
      { operation: "video", controls: ["input-images"] },
      { operation: "audio", controls: [] },
    ]);
    expect(target).toMatchObject({
      supportsStreaming: true,
      supportsStructuredOutput: true,
      supportsTools: true,
    });
  });

  it("does not apply media-only capabilities to model controls", () => {
    const target = assignmentPlaygroundTarget(
      assignment.api_name,
      "crewday",
      [
        {
          ...assignment,
          effective_chain: [
            { provider_model_api_name: "fake-model" },
            { provider_model_api_name: "fake-media" },
          ],
        },
      ],
      [
        { ...mapping, capabilities: [], output_modalities: ["text"] },
        {
          ...mapping,
          api_name: "fake-media",
          capabilities: ["streaming", "tool_calling"],
          output_modalities: ["image"],
        },
      ],
      [provider],
    );
    expect(target).toMatchObject({
      supportsStreaming: false,
      supportsTools: false,
    });
  });

  it("uses one service only as assignment configuration context", () => {
    const target = assignmentPlaygroundTarget(
      assignment.api_name,
      "crewday",
      [assignment],
      [mapping],
      [provider],
    );
    expect(target).toMatchObject({
      kind: "assignment",
      serviceContext: "crewday",
      selector: {
        assignment_api_name: "default",
        service_api_name: "crewday",
      },
    });
    expect(JSON.stringify(target)).not.toContain("workspace");
  });

  it("keeps one admitted media job under its exact target identity", () => {
    const target = assignmentPlaygroundTarget(
      assignment.api_name,
      "crewday",
      [assignment],
      [mapping],
      [provider],
    );
    if (target === null) throw new Error("Missing test target.");
    const job = {
      id: "job-1",
      logical_call_id: "call-1",
      selector: target.selector,
      provider_model_api_name: mapping.api_name,
      kind: "image" as const,
      state: "pending" as const,
      attempts: [],
      created_at: "2026-08-25T00:00:00Z",
    };
    const retained = updateMediaRecovery(new Map(), target, job);
    const reopened = retained.get(playgroundTargetKey(target));
    expect(reopened).toBe(job);
    // A local polling timeout does not mutate the recovery store.
    expect(retained.get(playgroundTargetKey(target))).toBe(job);
    const contentUnavailable = updateMediaRecovery(retained, target, {
      ...job,
      state: "succeeded",
      elapsed_ms: 12,
      content: { media_type: "image/png", size_bytes: 10 },
      completed_at: "2026-08-25T00:00:12Z",
    });
    expect(contentUnavailable.get(playgroundTargetKey(target))?.state).toBe(
      "succeeded",
    );
    expect(updateMediaRecovery(retained, target, null).size).toBe(0);
  });

  it("stops polling at one absolute deadline and preserves job recovery", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const initial: AdministratorPlaygroundMediaJob = {
      id: "job-timeout",
      logical_call_id: "call-timeout",
      selector: { provider_model_api_name: mapping.api_name },
      provider_model_api_name: mapping.api_name,
      kind: "image",
      state: "pending",
      attempts: [],
      created_at: "2026-08-25T00:00:00Z",
    };
    const running: AdministratorPlaygroundMediaJob = {
      ...initial,
      state: "running",
      attempts: [
        {
          provider_model_api_name: mapping.api_name,
          outcome: "failed",
          elapsed_ms: 5,
          error: { code: "upstream_failed", message: "First route failed." },
        },
      ],
    };
    let statusCalls = 0;
    const client = {
      playgroundMediaJob: vi.fn((_id: string, signal?: AbortSignal) => {
        statusCalls += 1;
        if (statusCalls === 1) return Promise.resolve(running);
        return new Promise<AdministratorPlaygroundMediaJob>(
          (_resolve, reject) => {
            signal?.addEventListener(
              "abort",
              () => {
                reject(new DOMException("Aborted", "AbortError"));
              },
              { once: true },
            );
          },
        );
      }),
    };
    const onUpdate = vi.fn();
    const result = pollMediaJob(
      client,
      initial,
      controller.signal,
      onUpdate,
    ).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(
      clientDeadlineMilliseconds.playgroundMediaPoll,
    );
    await expect(result).resolves.toMatchObject({
      code: "client_timeout",
      context: {
        logical_call_id: "call-timeout",
        selector: initial.selector,
        attempts: running.attempts,
      },
    });
    expect(client.playgroundMediaJob).toHaveBeenCalledTimes(2);
    expect(onUpdate).toHaveBeenCalledWith(running);
    vi.useRealTimers();
  });

  it("rejects media status that changes identity or moves backward", async () => {
    vi.useFakeTimers();
    const running = {
      id: "job-one",
      logical_call_id: "call-one",
      selector: { provider_model_api_name: mapping.api_name } as const,
      provider_model_api_name: mapping.api_name,
      kind: "image" as const,
      state: "running" as const,
      attempts: [],
      created_at: "2026-08-25T00:00:00Z",
    };
    const client = {
      playgroundMediaJob: vi.fn().mockResolvedValue({
        ...running,
        logical_call_id: "different-call",
        state: "pending",
      }),
    };
    const result = pollMediaJob(
      client,
      running,
      new AbortController().signal,
      vi.fn(),
    ).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(result).resolves.toMatchObject({ code: "invalid_response" });
  });

  it("rejects media status that changes its selected route", async () => {
    vi.useFakeTimers();
    const running: AdministratorPlaygroundMediaJob = {
      id: "job-one",
      logical_call_id: "call-one",
      selector: { provider_model_api_name: mapping.api_name },
      provider_model_api_name: mapping.api_name,
      kind: "image",
      state: "running",
      attempts: [],
      created_at: "2026-08-25T00:00:00Z",
    };
    const client = {
      playgroundMediaJob: vi.fn().mockResolvedValue({
        ...running,
        provider_model_api_name: "different-route",
      }),
    };
    const result = pollMediaJob(
      client,
      running,
      new AbortController().signal,
      vi.fn(),
    ).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(result).resolves.toMatchObject({ code: "invalid_response" });
    vi.useRealTimers();
  });

  it("requires structured output for a structured-only route", () => {
    const target = mappingPlaygroundTarget(
      mapping.api_name,
      [{ ...mapping, output_modalities: ["structured_json"] }],
      [provider],
      [model],
    );
    expect(target).toMatchObject({
      supportsStructuredOutput: true,
      requiresStructuredOutput: true,
      operations: [
        {
          operation: "model",
          controls: [
            "system-prompt",
            "temperature",
            "output-limit",
            "input-images",
          ],
        },
      ],
    });
  });

  it("does not offer disabled, empty, or changed targets", () => {
    expect(
      mappingPlaygroundTarget(
        mapping.api_name,
        [{ ...mapping, enabled: false }],
        [provider],
        [model],
      ),
    ).toBeNull();
    expect(
      mappingPlaygroundTarget(
        mapping.api_name,
        [
          {
            ...mapping,
            cooldown: {
              until: "2026-08-25T00:01:00Z",
              reason: "rate_limited",
            },
          },
        ],
        [provider],
        [model],
      ),
    ).toBeNull();
    expect(
      mappingPlaygroundTarget(
        mapping.api_name,
        [{ ...mapping, input_modalities: ["image"] }],
        [provider],
        [model],
      ),
    ).toBeNull();
    expect(
      assignmentPlaygroundTarget(
        assignment.api_name,
        "crewday",
        [{ ...assignment, effective_chain: [] }],
        [mapping],
        [provider],
      ),
    ).toBeNull();
    const target = mappingPlaygroundTarget(
      mapping.api_name,
      [mapping],
      [provider],
      [model],
    );
    if (target === null) throw new Error("Missing test target.");
    const changed = mappingPlaygroundTarget(
      mapping.api_name,
      [{ ...mapping, capabilities: ["tool_calling"] }],
      [provider],
      [model],
    );
    expect(targetUnavailableMessage(target, changed)).toContain(
      "configuration changed",
    );
    const priceChanged = mappingPlaygroundTarget(
      mapping.api_name,
      [
        {
          ...mapping,
          effective_price: {
            currency: "USD",
            unit_prices: [{ unit: "input_token", amount: "0.01" }],
          },
        },
      ],
      [provider],
      [model],
    );
    expect(targetUnavailableMessage(target, priceChanged)).toContain(
      "configuration changed",
    );
    expect(targetUnavailableMessage(target, null)).toContain("disabled");
  });

  it("invalidates an assignment when route order changes", () => {
    const second = { ...mapping, api_name: "fake-second" };
    const opened = assignmentPlaygroundTarget(
      assignment.api_name,
      "crewday",
      [
        {
          ...assignment,
          effective_chain: [
            { provider_model_api_name: mapping.api_name },
            { provider_model_api_name: second.api_name },
          ],
        },
      ],
      [mapping, second],
      [provider],
    );
    const reordered = assignmentPlaygroundTarget(
      assignment.api_name,
      "crewday",
      [
        {
          ...assignment,
          effective_chain: [
            { provider_model_api_name: second.api_name },
            { provider_model_api_name: mapping.api_name },
          ],
        },
      ],
      [mapping, second],
      [provider],
    );
    if (opened === null) throw new Error("Missing test assignment target.");
    expect(targetUnavailableMessage(opened, reordered)).toContain(
      "configuration changed",
    );
  });
});

describe("administrator playground input", () => {
  it("preserves every byte in each nonblank embedding line", () => {
    expect(nonBlankInputLines("  first  \n\t\n\tsecond \n")).toEqual([
      "  first  ",
      "\tsecond ",
    ]);
  });

  it("enforces the exact embedding batch bounds", () => {
    expect(() => nonBlankInputLines("\n \n")).toThrow("1 through 32");
    expect(() => nonBlankInputLines(Array(33).fill("one").join("\n"))).toThrow(
      "1 through 32",
    );
    expect(() => nonBlankInputLines("x".repeat(32_769))).toThrow("32,768");
    expect(
      nonBlankInputLines(Array(8).fill("x".repeat(32_768)).join("\n")),
    ).toHaveLength(8);
    expect(() =>
      nonBlankInputLines(`${Array(8).fill("x".repeat(32_768)).join("\n")}\ny`),
    ).toThrow("262,144");
  });

  it("normalizes bounded tags and validates tool definition JSON", () => {
    expect(parseTags("zeta, alpha, zeta")).toEqual(["alpha", "zeta"]);
    expect(parseTags("𐀀, ")).toEqual(["", "𐀀"]);
    expect(() => parseTags("a".repeat(129))).toThrow("128 UTF-8");
    expect(
      parseToolDefinitions(
        '[{"name":"lookup","description":"Find a value","input_schema_json":"{\\"type\\":\\"object\\"}"}]',
      ),
    ).toEqual([
      {
        name: "lookup",
        description: "Find a value",
        input_schema_json: '{"type":"object"}',
      },
    ]);
    expect(() =>
      parseToolDefinitions(
        '[{"name":"lookup","description":"One","input_schema_json":"{}"},{"name":"lookup","description":"Two","input_schema_json":"{}"}]',
      ),
    ).toThrow("unique");
    expect(() =>
      parseToolDefinitions(
        '[{"name":"lookup","description":"Find","input_schema_json":"{}","extra":true}]',
      ),
    ).toThrow("only bounded");
  });

  it("renders one fixed-target modal without key, workspace, or scope controls", () => {
    const target = assignmentPlaygroundTarget(
      assignment.api_name,
      "crewday",
      [assignment],
      [mapping],
      [provider],
    );
    if (target === null) throw new Error("Missing test target.");
    const markup = renderToStaticMarkup(
      <PlaygroundModal
        client={createAdministrationClient(vi.fn())}
        csrf="csrf"
        currentTarget={target}
        onClose={vi.fn()}
        onMediaJobChange={vi.fn()}
        onUncertainMediaAdmissionChange={vi.fn()}
        retainedMediaJob={null}
        retainedUncertainMediaAdmission={false}
        returnFocusRef={{ current: null }}
        target={target}
      />,
    );
    expect(markup).toContain("Global administrator playground");
    expect(markup).toContain("Service configuration context");
    expect(markup).toContain("crewday");
    expect(markup).toContain("Stream model output");
    expect(markup).toContain("Validate structured JSON output");
    expect(markup).toContain("Tool definitions JSON");
    expect(markup).not.toContain("Route selection");
    expect(markup).not.toContain("service key");
    expect(markup).not.toContain("Workspace");
    expect(markup).not.toContain("Permission scope");
  });

  it("blocks duplicate generation and offers recovery for an admitted job", () => {
    const target = mappingPlaygroundTarget(
      mapping.api_name,
      [mapping],
      [provider],
      [model],
    );
    if (target === null) throw new Error("Missing test target.");
    const markup = renderToStaticMarkup(
      <PlaygroundModal
        client={createAdministrationClient(vi.fn())}
        csrf="csrf"
        currentTarget={target}
        onClose={vi.fn()}
        onMediaJobChange={vi.fn()}
        onUncertainMediaAdmissionChange={vi.fn()}
        retainedMediaJob={{
          id: "job-retained",
          logical_call_id: "call-retained",
          selector: target.selector,
          provider_model_api_name: mapping.api_name,
          kind: "image",
          state: "running",
          attempts: [],
          created_at: "2026-08-25T00:00:00Z",
        }}
        retainedUncertainMediaAdmission={false}
        returnFocusRef={{ current: null }}
        target={target}
      />,
    );
    expect(markup).toContain("Resume media job");
    expect(markup).toContain("job-retained");
    expect(markup).toContain("will not submit a replacement job");
    expect(markup).toContain("disabled");
  });

  it("blocks another submission after an uncertain media admission", () => {
    const target = mappingPlaygroundTarget(
      mapping.api_name,
      [mapping],
      [provider],
      [model],
    );
    if (target === null) throw new Error("Missing test target.");
    const markup = renderToStaticMarkup(
      <PlaygroundModal
        client={createAdministrationClient(vi.fn())}
        csrf="csrf"
        currentTarget={target}
        onClose={vi.fn()}
        onMediaJobChange={vi.fn()}
        onUncertainMediaAdmissionChange={vi.fn()}
        retainedMediaJob={null}
        retainedUncertainMediaAdmission
        returnFocusRef={{ current: null }}
        target={target}
      />,
    );
    expect(markup).toContain("Uncertain media admission");
    expect(markup).toContain("can still have created a job");
    expect(markup).toContain("I checked; allow a new submission");
    expect(markup).toContain("disabled");
  });

  it("keeps a succeeded job recoverable when its content is unavailable", () => {
    const target = mappingPlaygroundTarget(
      mapping.api_name,
      [mapping],
      [provider],
      [model],
    );
    if (target === null) throw new Error("Missing test target.");
    const markup = renderToStaticMarkup(
      <PlaygroundModal
        client={createAdministrationClient(vi.fn())}
        csrf="csrf"
        currentTarget={target}
        onClose={vi.fn()}
        onMediaJobChange={vi.fn()}
        onUncertainMediaAdmissionChange={vi.fn()}
        retainedMediaJob={{
          id: "job-content-retry",
          logical_call_id: "call-content-retry",
          selector: target.selector,
          provider_model_api_name: mapping.api_name,
          kind: "image",
          state: "succeeded",
          elapsed_ms: 20,
          attempts: [
            {
              provider_model_api_name: mapping.api_name,
              outcome: "succeeded",
              elapsed_ms: 19,
            },
          ],
          content: { media_type: "image/png", size_bytes: 10 },
          created_at: "2026-08-25T00:00:00Z",
          completed_at: "2026-08-25T00:00:20Z",
        }}
        retainedUncertainMediaAdmission={false}
        returnFocusRef={{ current: null }}
        target={target}
      />,
    );
    expect(markup).toContain("Resume media job");
    expect(markup).toContain("job-content-retry");
    expect(markup).toContain("will not submit a replacement job");
    expect(markup).toContain("disabled");
  });

  it("keeps failed-job attempts and usage until explicit dismissal", () => {
    const target = mappingPlaygroundTarget(
      mapping.api_name,
      [mapping],
      [provider],
      [model],
    );
    if (target === null) throw new Error("Missing test target.");
    const markup = renderToStaticMarkup(
      <PlaygroundModal
        client={createAdministrationClient(vi.fn())}
        csrf="csrf"
        currentTarget={target}
        onClose={vi.fn()}
        onMediaJobChange={vi.fn()}
        onUncertainMediaAdmissionChange={vi.fn()}
        retainedMediaJob={{
          id: "job-failed",
          logical_call_id: "call-failed",
          selector: target.selector,
          provider_model_api_name: mapping.api_name,
          kind: "image",
          state: "failed",
          elapsed_ms: 20,
          usage: {
            units: [{ unit: "request", quantity: "1" }],
            cost: "0.30",
            currency: "USD",
          },
          attempts: [
            {
              provider_model_api_name: mapping.api_name,
              outcome: "failed",
              elapsed_ms: 19,
              usage: {
                units: [{ unit: "image", quantity: "1" }],
                cost: "0.25",
                currency: "USD",
              },
              error: {
                code: "upstream_failed",
                message: "The fake provider failed.",
              },
            },
          ],
          error: {
            code: "upstream_failed",
            message: "The fake media job failed.",
          },
          created_at: "2026-08-25T00:00:00Z",
          completed_at: "2026-08-25T00:00:20Z",
        }}
        retainedUncertainMediaAdmission={false}
        returnFocusRef={{ current: null }}
        target={target}
      />,
    );
    expect(markup).toContain("Dismiss failed media job");
    expect(markup).toContain("0.30 USD");
    expect(markup).toContain("0.25 USD");
    expect(markup).toContain("upstream_failed");
    expect(markup).toContain("The fake provider failed.");
    expect(markup).toContain("The fake media job failed.");
  });
});
