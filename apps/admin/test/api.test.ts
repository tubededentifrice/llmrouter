import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AdministrationApiError,
  administratorStreamLimits,
  clientDeadlineMilliseconds,
  createAdministrationClient,
  errorMessage,
  isoRange,
} from "../src/api.js";

afterEach(() => {
  vi.useRealTimers();
});

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json",
    },
  });
}

function url(input: string | URL | Request): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.toString() : input.url;
}

function validRequestLog() {
  return {
    summary: {
      id: "administrator-log",
      logical_call_id: "call-1",
      call_actor: "administrator",
      administrator_subject: "pocket-id-subject",
      provider_model_api_name: "primary-text",
      kind: "model",
      outcome: "succeeded",
      started_at: "2026-08-25T00:00:00Z",
    },
    request_json: '{"messages":[]}',
    response_json: '{"content":[]}',
    attempts: [
      {
        provider_model_api_name: "primary-text",
        outcome: "succeeded",
        started_at: "2026-08-25T00:00:00Z",
        completed_at: "2026-08-25T00:00:01Z",
        applied_prices: {
          currency: "USD",
          unit_prices: [{ unit: "request", amount: "0.01" }],
          source: "catalog",
          synchronized_at: "2026-08-24T00:00:00Z",
        },
      },
    ],
    media: [
      {
        id: "media-1",
        media_type: "image/png",
        role: "output",
        size_bytes: 123,
      },
    ],
  };
}

describe("native administration client", () => {
  it("validates a complete request log with optional attempt usage absent", async () => {
    const log = validRequestLog();
    const result = await createAdministrationClient(
      vi.fn(() => Promise.resolve(json(log))),
    ).requestLog("administrator-log");

    expect(result).toEqual(log);
    expect(result.attempts[0]?.usage).toBeUndefined();
  });

  it("rejects request-log detail for a different selected identifier", async () => {
    const log = validRequestLog();
    const client = createAdministrationClient(
      vi.fn(() => Promise.resolve(json(log))),
    );

    await expect(client.requestLog("different-log")).rejects.toMatchObject({
      code: "invalid_response",
      status: 502,
      details: {
        reason: "The request-log detail does not match the selected log.",
      },
    });
  });

  it.each([
    {
      name: "attempt usage",
      mutate: (log: ReturnType<typeof validRequestLog>) => {
        Object.assign(log.attempts[0] ?? {}, {
          usage: {
            units: [{ unit: "untrusted-unit", quantity: "1" }],
            cost: "0.01",
            currency: "USD",
          },
        });
      },
    },
    {
      name: "media role",
      mutate: (log: ReturnType<typeof validRequestLog>) => {
        Object.assign(log.media[0] ?? {}, { role: "preview" });
      },
    },
  ])(
    "rejects malformed request-log $name before rendering",
    async ({ mutate }) => {
      const log = validRequestLog();
      mutate(log);
      const client = createAdministrationClient(
        vi.fn(() => Promise.resolve(json(log))),
      );

      await expect(
        client.requestLog("administrator-log"),
      ).rejects.toMatchObject({
        code: "invalid_response",
        status: 502,
        message: "The Router returned an invalid request-log response.",
      });
    },
  );

  it("accepts each closed service and administrator request-log summary", async () => {
    const common = {
      logical_call_id: "call-1",
      kind: "model",
      outcome: "succeeded",
      tags: ["scheduled"],
      started_at: "2026-08-25T00:00:00Z",
    };
    const items = [
      {
        ...common,
        id: "service-log",
        call_actor: "service",
        service_api_name: "billing",
        workspace_api_name: "production",
        assignment_api_name: "summarize",
      },
      {
        ...common,
        id: "administrator-exact-log",
        call_actor: "administrator",
        administrator_subject: "pocket-id-subject",
        provider_model_api_name: "primary-text",
      },
      {
        ...common,
        id: "administrator-assignment-log",
        call_actor: "administrator",
        administrator_subject: "pocket-id-subject",
        assignment_api_name: "summarize",
        configuration_service_api_name: "billing",
        provider_model_api_name: "fallback-text",
      },
    ];
    const result = await createAdministrationClient(
      vi.fn(() =>
        Promise.resolve(
          json({ items, page: { has_more: false, next_cursor: null } }),
        ),
      ),
    ).requestLogs("from", "to");

    expect(result.items).toEqual(items);
  });

  it("rejects an administrator request-log summary with service ownership", async () => {
    const client = createAdministrationClient(
      vi.fn(() =>
        Promise.resolve(
          json({
            items: [
              {
                id: "invalid-log",
                logical_call_id: "call-1",
                call_actor: "administrator",
                administrator_subject: "pocket-id-subject",
                service_api_name: "billing",
                workspace_api_name: "production",
                provider_model_api_name: "primary-text",
                kind: "model",
                outcome: "succeeded",
                started_at: "2026-08-25T00:00:00Z",
              },
            ],
            page: { has_more: false, next_cursor: null },
          }),
        ),
      ),
    );

    await expect(client.requestLogs("from", "to")).rejects.toMatchObject({
      code: "invalid_response",
      status: 502,
      message: "The Router returned an invalid request-log response.",
    });
  });

  it("uses the current administrator session and bounded native lists", async () => {
    const paths: string[] = [];
    const fetcher = vi.fn((input: string | URL | Request) => {
      paths.push(url(input));
      if (url(input).endsWith("/session")) {
        return Promise.resolve(
          json({
            subject: "admin-subject",
            display_name: "Administrator",
            expires_at: "2026-08-25T00:00:00Z",
            csrf_token: "csrf-token-with-safe-length",
          }),
        );
      }
      return Promise.resolve(
        json({
          items: [],
          page: { has_more: false, next_cursor: null },
        }),
      );
    });
    const client = createAdministrationClient(fetcher);
    await client.session();
    await client.services();
    await client.workspaces("service");
    await client.keys("service");
    await client.assignments("service");
    await client.providers();
    await client.models();
    await client.providerModels();
    await client.credentials();
    await client.requestLogs("from", "to");
    await client.activity("from", "to");
    expect(paths).toEqual([
      "/v1/admin/session",
      "/v1/admin/services?limit=200",
      "/v1/admin/services/service/workspaces?limit=200",
      "/v1/admin/services/service/keys?limit=200",
      "/v1/admin/services/service/assignments?limit=200",
      "/v1/admin/providers?limit=200",
      "/v1/admin/models?limit=200",
      "/v1/admin/provider-models?limit=200",
      "/v1/admin/credentials?limit=200",
      "/v1/admin/request-logs?from=from&to=to&limit=200",
      "/v1/admin/activity?from=from&to=to&limit=200",
    ]);
  });

  it("reads one incremental log or activity page with an encoded cursor", async () => {
    const paths: string[] = [];
    const fetcher = vi.fn((input: string | URL | Request) => {
      paths.push(url(input));
      return Promise.resolve(
        json({ items: [], page: { has_more: false, next_cursor: null } }),
      );
    });
    const client = createAdministrationClient(fetcher);

    await client.requestLogsPage("from", "to", "next log");
    await client.activityPage("from", "to", "next activity");

    expect(paths).toEqual([
      "/v1/admin/request-logs?from=from&to=to&limit=200&cursor=next+log",
      "/v1/admin/activity?from=from&to=to&limit=200&cursor=next+activity",
    ]);
  });

  it("rejects an unsafe continuation page before it reaches the table", async () => {
    const client = createAdministrationClient(
      vi.fn(() =>
        Promise.resolve(
          json({ items: [], page: { has_more: true, next_cursor: "" } }),
        ),
      ),
    );

    const error: unknown = await client
      .requestLogsPage("from", "to")
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AdministrationApiError);
    if (!(error instanceof AdministrationApiError)) throw error;
    expect(error.code).toBe("invalid_response");
    expect(error.status).toBe(502);
    expect(error.details?.reason).toContain("cursor contract");
  });

  it("rejects extra fields in a closed request-log page", async () => {
    const client = createAdministrationClient(
      vi.fn(() =>
        Promise.resolve(
          json({
            items: [],
            page: { has_more: false, next_cursor: null },
            unexpected: true,
          }),
        ),
      ),
    );

    await expect(client.requestLogsPage("from", "to")).rejects.toMatchObject({
      code: "invalid_response",
      status: 502,
      details: {
        reason: "The list page does not match the native cursor contract.",
      },
    });
  });

  it("rejects activity records that do not match the closed event contract", async () => {
    const client = createAdministrationClient(
      vi.fn(() =>
        Promise.resolve(
          json({
            items: [
              {
                id: "activity-1",
                actor_subject: "administrator",
                action: "service.updated",
                resource_type: "service",
                result: "succeeded",
                occurred_at: "2026-08-25T00:00:00Z",
                unexpected: "unsafe",
              },
            ],
            page: { has_more: false, next_cursor: null },
          }),
        ),
      ),
    );

    await expect(client.activityPage("from", "to")).rejects.toMatchObject({
      code: "invalid_response",
      status: 502,
      details: {
        reason: "An activity event does not match its closed contract.",
      },
    });
  });

  it("accepts a bounded activity record with a stable resource identity", async () => {
    const event = {
      id: "activity-1",
      actor_subject: "administrator",
      action: "service.updated",
      resource_type: "service",
      resource_api_name: "billing",
      result: "succeeded",
      occurred_at: "2026-08-25T00:00:00Z",
    };
    const client = createAdministrationClient(
      vi.fn(() =>
        Promise.resolve(
          json({
            items: [event],
            page: { has_more: false, next_cursor: null },
          }),
        ),
      ),
    );

    await expect(client.activityPage("from", "to")).resolves.toEqual({
      items: [event],
      page: { has_more: false, next_cursor: null },
    });
  });

  it("collects every bounded cursor page in order", async () => {
    const paths: string[] = [];
    const fetcher = vi.fn((input: string | URL | Request) => {
      const path = url(input);
      paths.push(path);
      return Promise.resolve(
        path.includes("cursor=next+page")
          ? json({
              items: [{ api_name: "second" }],
              page: { has_more: false },
            })
          : json({
              items: [{ api_name: "first" }],
              page: { has_more: true, next_cursor: "next page" },
            }),
      );
    });
    const result = await createAdministrationClient(fetcher).services();
    expect(result).toEqual({
      items: [{ api_name: "first" }, { api_name: "second" }],
      page: { has_more: false, next_cursor: null },
      retrieval: { complete: true, loaded_items: 2, loaded_pages: 2 },
    });
    expect(paths).toEqual([
      "/v1/admin/services?limit=200",
      "/v1/admin/services?limit=200&cursor=next+page",
    ]);
  });

  it("uses the same cursor contract for scoped and filtered lists", async () => {
    const paths: string[] = [];
    const pages = new Map<string, number>();
    const fetcher = vi.fn((input: string | URL | Request) => {
      const path = url(input);
      paths.push(path);
      const base = path.split("&cursor=", 1)[0] ?? path;
      const page = pages.get(base) ?? 0;
      pages.set(base, page + 1);
      const item = path.startsWith("/v1/admin/activity")
        ? {
            id: `activity-${String(page)}`,
            actor_subject: "administrator",
            action: "service.updated",
            resource_type: "service",
            resource_api_name: "billing",
            result: "succeeded",
            occurred_at: "2026-08-25T00:00:00Z",
          }
        : { page };
      return Promise.resolve(
        json({
          items: [item],
          page:
            page === 0
              ? { has_more: true, next_cursor: "next" }
              : { has_more: false, next_cursor: null },
        }),
      );
    });
    const client = createAdministrationClient(fetcher);
    await client.workspaces("service/name");
    await client.activity("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z");
    expect(paths).toEqual([
      "/v1/admin/services/service%2Fname/workspaces?limit=200",
      "/v1/admin/services/service%2Fname/workspaces?limit=200&cursor=next",
      "/v1/admin/activity?from=2026-08-01T00%3A00%3A00Z&to=2026-08-02T00%3A00%3A00Z&limit=200",
      "/v1/admin/activity?from=2026-08-01T00%3A00%3A00Z&to=2026-08-02T00%3A00%3A00Z&limit=200&cursor=next",
    ]);
  });

  it("fails closed when a list repeats its cursor", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        json({ items: [], page: { has_more: true, next_cursor: "repeat" } }),
      )
      .mockResolvedValueOnce(
        json({ items: [], page: { has_more: true, next_cursor: "repeat" } }),
      );
    const error: unknown = await createAdministrationClient(fetcher)
      .providers()
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AdministrationApiError);
    if (!(error instanceof AdministrationApiError)) throw error;
    expect(error.code).toBe("invalid_response");
    expect(error.status).toBe(502);
    expect(error.details?.reason).toContain("repeated a cursor");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("fails closed when a list exceeds the item safety limit", async () => {
    let page = 0;
    const fetcher = vi.fn(() => {
      page += 1;
      return Promise.resolve(
        json({
          items: Array.from({ length: 200 }, (_, index) => ({
            id: `log-${String(page)}-${String(index)}`,
            logical_call_id: `call-${String(page)}-${String(index)}`,
            call_actor: "service",
            service_api_name: "billing",
            workspace_api_name: "production",
            kind: "model",
            outcome: "succeeded",
            started_at: "2026-08-25T00:00:00Z",
          })),
          page: { has_more: true, next_cursor: `cursor-${String(page)}` },
        }),
      );
    });
    const client = createAdministrationClient(fetcher);
    const error: unknown = await client
      .requestLogs("from", "to")
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AdministrationApiError);
    if (!(error instanceof AdministrationApiError)) throw error;
    expect(error.code).toBe("invalid_response");
    expect(error.status).toBe(502);
    expect(error.details?.reason).toContain("item safety limit");
    expect(fetcher).toHaveBeenCalledTimes(100);
  });

  it("fails closed when one page exceeds the requested item limit", async () => {
    const fetcher = vi.fn(() =>
      Promise.resolve(
        json({
          items: Array.from({ length: 201 }, (_, index) => ({ index })),
          page: { has_more: false, next_cursor: null },
        }),
      ),
    );
    const error: unknown = await createAdministrationClient(fetcher)
      .services()
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AdministrationApiError);
    if (!(error instanceof AdministrationApiError)) throw error;
    expect(error.details?.reason).toContain(
      "exceeds the requested 200 item limit",
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("fails closed when a list exceeds the page safety limit", async () => {
    let page = 0;
    const fetcher = vi.fn(() => {
      page += 1;
      return Promise.resolve(
        json({
          items: [],
          page: { has_more: true, next_cursor: `cursor-${String(page)}` },
        }),
      );
    });
    const error: unknown = await createAdministrationClient(fetcher)
      .credentials()
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AdministrationApiError);
    if (!(error instanceof AdministrationApiError)) throw error;
    expect(error.details?.reason).toContain("page safety limit");
    expect(fetcher).toHaveBeenCalledTimes(100);
  });

  it("fails closed when a continuing list omits its cursor", async () => {
    const client = createAdministrationClient(
      vi.fn(() =>
        Promise.resolve(
          json({ items: [], page: { has_more: true, next_cursor: null } }),
        ),
      ),
    );
    const error: unknown = await client
      .models()
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AdministrationApiError);
    if (!(error instanceof AdministrationApiError)) throw error;
    expect(error.details?.reason).toContain("no next cursor");
  });

  it("fails closed when a page does not match the cursor contract", async () => {
    const client = createAdministrationClient(
      vi.fn(() => Promise.resolve(json({ items: "not-a-list", page: null }))),
    );
    const error: unknown = await client
      .keys("service")
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AdministrationApiError);
    if (!(error instanceof AdministrationApiError)) throw error;
    expect(error.details?.reason).toContain("native cursor contract");
  });

  it("starts Pocket ID with one local absolute return path", async () => {
    let body = "";
    const client = createAdministrationClient(
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        body = typeof init?.body === "string" ? init.body : "";
        return Promise.resolve(
          json({ authorization_url: "https://id.example/authorize" }),
        );
      }),
    );
    await expect(client.startSession("/models?service=crewday")).resolves.toBe(
      "https://id.example/authorize",
    );
    expect(JSON.parse(body)).toEqual({
      return_to: "/models?service=crewday",
    });
  });

  it("adds the session CSRF token to each administrator write", async () => {
    let received: RequestInit | undefined;
    const client = createAdministrationClient(
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        received = init;
        return Promise.resolve(new Response(null, { status: 204 }));
      }),
    );
    await client.deleteWorkspace("crewday", "production", "session-csrf");
    expect(received?.method).toBe("DELETE");
    expect(new Headers(received?.headers).get("X-CSRF-Token")).toBe(
      "session-csrf",
    );
    expect(received?.credentials).toBe("same-origin");
  });

  it("sends a closed assignment replacement body", async () => {
    let body = "";
    const client = createAdministrationClient(
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        body = typeof init?.body === "string" ? init.body : "";
        return Promise.resolve(
          json({
            api_name: "general",
            display_name: "General",
            definition_kind: "direct_chain",
            direct_chain: [{ provider_model_api_name: "primary" }],
            effective_chain: [{ provider_model_api_name: "primary" }],
            observed_requirements: [],
          }),
        );
      }),
    );
    await client.putAssignment(
      "crewday",
      "general",
      { direct_chain: [{ provider_model_api_name: "primary" }] },
      "csrf",
    );
    expect(JSON.parse(body)).toEqual({
      direct_chain: [{ provider_model_api_name: "primary" }],
    });
  });

  it("sends the OpenRouter preview input unchanged and confirms exact reviewed objects", async () => {
    const calls: { readonly path: string; readonly body: unknown }[] = [];
    const reviewed = {
      source_model_id: "vendor/model",
      model: {
        api_name: "vendor-model",
        display_name: "Vendor model",
        input_modalities: ["text" as const],
        output_modalities: ["text" as const],
        capabilities: ["reasoning" as const],
        constraints: {
          max_context_tokens: 128_000,
          max_output_tokens: 16_384,
        },
        price_source: "openrouter",
        price_lookup_key: "vendor/model",
      },
      reviewed_price: {
        currency: "USD",
        unit_prices: [{ unit: "input_token" as const, amount: "0.000001" }],
        source: "openrouter",
      },
      provider_models: [
        {
          api_name: "openrouter-vendor-model",
          provider_api_name: "openrouter-main",
          model_api_name: "vendor-model",
          provider_model_name: "vendor/model",
          enabled: true,
        },
      ],
    };
    const client = createAdministrationClient(
      vi.fn((input: string | URL | Request, init?: RequestInit) => {
        calls.push({
          path: url(input),
          body: JSON.parse(typeof init?.body === "string" ? init.body : "null"),
        });
        return Promise.resolve(json({}));
      }),
    );
    const exactInput = "https://openrouter.ai/vendor/model?tab=parameters";
    await client.previewOpenRouterModel(exactInput, "csrf");
    await client.importOpenRouterModel(reviewed, "csrf");
    expect(calls).toEqual([
      {
        path: "/v1/admin/openrouter-model-imports/preview",
        body: { model_id_or_url: exactInput },
      },
      {
        path: "/v1/admin/openrouter-model-imports",
        body: reviewed,
      },
    ]);
  });

  it("keeps every typed manual-price unit in the exact model body", async () => {
    let body = "";
    const client = createAdministrationClient(
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        body = typeof init?.body === "string" ? init.body : "";
        return Promise.resolve(
          json({
            api_name: "priced-model",
            display_name: "Priced model",
            input_modalities: ["text"],
            output_modalities: ["text"],
            capabilities: [],
            created_at: "2026-08-24T00:00:00Z",
          }),
        );
      }),
    );
    await client.createModel(
      {
        api_name: "priced-model",
        display_name: "Priced model",
        input_modalities: ["text"],
        output_modalities: ["text"],
        capabilities: [],
        manual_price: {
          currency: "USD",
          unit_prices: [
            { unit: "input_token", amount: "0.001" },
            { unit: "output_token", amount: "0.002" },
          ],
        },
      },
      "csrf",
    );
    expect(JSON.parse(body)).toEqual({
      api_name: "priced-model",
      display_name: "Priced model",
      input_modalities: ["text"],
      output_modalities: ["text"],
      capabilities: [],
      manual_price: {
        currency: "USD",
        unit_prices: [
          { unit: "input_token", amount: "0.001" },
          { unit: "output_token", amount: "0.002" },
        ],
      },
    });
  });

  it("sends the exact direct provider-model price replacement", async () => {
    let path = "";
    let body = "";
    const client = createAdministrationClient(
      vi.fn((input: string | URL | Request, init?: RequestInit) => {
        path = url(input);
        body = typeof init?.body === "string" ? init.body : "";
        return Promise.resolve(
          json({
            api_name: "priced-mapping",
            provider_api_name: "openrouter-main",
            model_api_name: "priced-model",
            provider_model_name: "vendor/model",
            enabled: true,
            input_modalities: ["text"],
            output_modalities: ["text"],
            capabilities: [],
            reasoning_mappings: [],
            configured_price_source: "openrouter",
            configured_price_lookup_key: "vendor/model",
            created_at: "2026-08-25T00:00:00Z",
          }),
        );
      }),
    );
    const replacement = {
      api_name: "priced-mapping",
      provider_api_name: "openrouter-main",
      model_api_name: "priced-model",
      provider_model_name: "vendor/model",
      enabled: true,
      price_source: "openrouter",
      price_lookup_key: "vendor/model",
    } as const;
    await client.putProviderModel("priced-mapping", replacement, "csrf");
    expect(path).toBe("/v1/admin/provider-models/priced-mapping");
    expect(JSON.parse(body)).toEqual(replacement);
  });

  it("omits top-level null values from closed administrator writes", async () => {
    const calls: { readonly path: string; readonly body: unknown }[] = [];
    const client = createAdministrationClient(
      vi.fn((input: string | URL | Request, init?: RequestInit) => {
        calls.push({
          path: url(input),
          body: JSON.parse(typeof init?.body === "string" ? init.body : "null"),
        });
        return Promise.resolve(json({}));
      }),
    );
    await client.createService(
      {
        api_name: "root",
        display_name: "Root",
        parent_service_api_name: null,
      },
      "csrf",
    );
    await client.createModel(
      {
        api_name: "model",
        display_name: "Model",
        input_modalities: ["text"],
        output_modalities: ["text"],
        capabilities: [],
        manual_price: null,
      },
      "csrf",
    );
    await client.putProviderModel(
      "mapping",
      {
        api_name: "mapping",
        provider_api_name: "provider",
        model_api_name: "model",
        provider_model_name: "wire-model",
        enabled: true,
        price_source: null,
        price_lookup_key: null,
        manual_price: null,
      },
      "csrf",
    );
    await client.synchronizePrices(null, "csrf");
    expect(calls).toEqual([
      {
        path: "/v1/admin/services",
        body: { api_name: "root", display_name: "Root" },
      },
      {
        path: "/v1/admin/models",
        body: {
          api_name: "model",
          display_name: "Model",
          input_modalities: ["text"],
          output_modalities: ["text"],
          capabilities: [],
        },
      },
      {
        path: "/v1/admin/provider-models/mapping",
        body: {
          api_name: "mapping",
          provider_api_name: "provider",
          model_api_name: "model",
          provider_model_name: "wire-model",
          enabled: true,
        },
      },
      { path: "/v1/admin/prices/synchronize", body: {} },
    ]);
  });

  it("keeps provider errors safe and corrective", async () => {
    const client = createAdministrationClient(
      vi.fn(() =>
        Promise.resolve(
          json(
            {
              error: {
                code: "conflict",
                message: "The service still has children.",
                details: { field: "parent", reason: "Move each child first." },
              },
            },
            409,
          ),
        ),
      ),
    );
    await expect(client.deleteService("root", "csrf")).rejects.toMatchObject({
      code: "conflict",
      status: 409,
    });
    expect(
      errorMessage(
        new AdministrationApiError(409, "conflict", "Cannot delete.", {
          reason: "Move each child first.",
        }),
      ),
    ).toBe("Cannot delete. Move each child first.");
  });

  it("preserves exact whitespace in an opaque provider credential", async () => {
    let body = "";
    const client = createAdministrationClient(
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        body = typeof init?.body === "string" ? init.body : "";
        return Promise.resolve(
          json({
            api_name: "opaque-secret",
            fingerprint: "fingerprint",
            created_at: "2026-08-24T00:00:00Z",
            updated_at: "2026-08-24T00:00:00Z",
          }),
        );
      }),
    );
    await client.createCredential(
      "opaque-secret",
      "  exact secret with space and newline\n ",
      "csrf",
    );
    expect(JSON.parse(body)).toEqual({
      api_name: "opaque-secret",
      secret: "  exact secret with space and newline\n ",
    });
  });

  it("bounds stalled administration reads and writes with safe errors", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const fetcher = vi.fn(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.signal !== null && init?.signal !== undefined)
          signals.push(init.signal);
        return new Promise<Response>(() => undefined);
      },
    );
    const client = createAdministrationClient(fetcher);
    const read = client.services().catch((error: unknown) => error);
    const write = client
      .createCredential("credential", "exact-secret", "csrf")
      .catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(
      clientDeadlineMilliseconds.administration,
    );
    for (const error of [await read, await write]) {
      expect(error).toMatchObject({
        code: "client_timeout",
        message: "The Router did not respond before the browser deadline.",
        status: 408,
      });
      expect(error).toBeInstanceOf(AdministrationApiError);
      if (!(error instanceof AdministrationApiError)) throw error;
      expect(error.details?.reason).toContain(
        "Refresh the affected data before you retry a write.",
      );
    }
    expect(signals).toHaveLength(2);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
  });

  it("uses one administration deadline for a complete cursor walk", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        json({
          items: [{ api_name: "first" }],
          page: { has_more: true, next_cursor: "next" },
        }),
      )
      .mockImplementationOnce(
        (_input: string | URL | Request, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("Aborted", "AbortError"));
            });
          }),
      );
    const result = createAdministrationClient(fetcher)
      .services()
      .catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(
      clientDeadlineMilliseconds.administration,
    );
    await expect(result).resolves.toMatchObject({
      code: "client_timeout",
      status: 408,
    });
  });

  it("creates a stable bounded UTC range", () => {
    expect(isoRange(7, new Date("2026-08-24T12:00:00Z"))).toEqual({
      from: "2026-08-17T12:00:00.000Z",
      to: "2026-08-24T12:00:00.000Z",
    });
  });
});

describe("administrator playground client", () => {
  const exactSelector = { provider_model_api_name: "fake-model" } as const;
  const usage = {
    units: [{ unit: "output_token" as const, quantity: "2" }],
    cost: "0.01",
    currency: "USD",
  };
  const attempt = {
    provider_model_api_name: "fake-model",
    outcome: "succeeded" as const,
    elapsed_ms: 4,
    usage,
  };
  const streamStart =
    'event: start\ndata: {"logical_call_id":"logical-call","selector":{"provider_model_api_name":"fake-model"},"provider_model_api_name":"fake-model"}\n\n';

  function streamClient(
    body: ReadableStream<Uint8Array> | string,
    limits: Partial<typeof administratorStreamLimits> = {},
  ) {
    return createAdministrationClient(
      vi.fn().mockResolvedValue(
        new Response(body, {
          headers: {
            "Cache-Control": "no-store",
            "Content-Type": "text/event-stream",
            "X-LLMRouter-Logical-Call-Id": "logical-call",
          },
        }),
      ),
      { ...administratorStreamLimits, ...limits },
    );
  }

  async function streamError(
    body: ReadableStream<Uint8Array> | string,
    limits: Partial<typeof administratorStreamLimits> = {},
  ) {
    return streamClient(body, limits)
      .playgroundModelStream?.(
        {
          selector: exactSelector,
          messages: [
            { role: "user", content: [{ type: "text", text: "Hello" }] },
          ],
        },
        "csrf",
      )
      .catch((value: unknown) => value);
  }

  it("uses only native administrator routes and browser controls", async () => {
    const calls: { path: string; init: RequestInit }[] = [];
    const fetcher = vi.fn(
      (input: string | URL | Request, init: RequestInit = {}) => {
        const path = url(input);
        calls.push({ path, init });
        if (path.endsWith("/content"))
          return Promise.resolve(
            new Response(new Uint8Array([1, 2, 3]), {
              headers: {
                "Cache-Control": "no-store",
                "Content-Type": "image/png",
              },
            }),
          );
        if (path.includes("media-jobs/job"))
          return Promise.resolve(
            json({
              id: "job",
              logical_call_id: "call",
              selector: exactSelector,
              provider_model_api_name: "fake-model",
              kind: "image",
              state: "succeeded",
              attempts: [attempt],
              elapsed_ms: 5,
              usage,
              content: { media_type: "image/png", size_bytes: 3 },
              created_at: "2026-08-25T00:00:00Z",
              completed_at: "2026-08-25T00:00:01Z",
            }),
          );
        if (path.endsWith("media-jobs"))
          return Promise.resolve(
            json(
              {
                id: "job",
                logical_call_id: "call",
                selector: exactSelector,
                provider_model_api_name: "fake-model",
                kind: "image",
                state: "pending",
                attempts: [],
                created_at: "2026-08-25T00:00:00Z",
              },
              202,
            ),
          );
        if (path.endsWith("embeddings"))
          return Promise.resolve(
            json({
              logical_call_id: "call",
              selector: exactSelector,
              elapsed_ms: 5,
              attempts: [attempt],
              result: {
                provider_model_api_name: "fake-model",
                embeddings: [{ index: 0, values: [0.1] }],
                usage,
              },
            }),
          );
        return Promise.resolve(
          json({
            logical_call_id: "call",
            selector: exactSelector,
            elapsed_ms: 5,
            attempts: [attempt],
            result: {
              output_type: "standard",
              provider_model_api_name: "fake-model",
              content: [{ type: "text", text: "ok" }],
              usage,
            },
          }),
        );
      },
    );
    const client = createAdministrationClient(fetcher);
    await client.playgroundModel?.(
      {
        selector: exactSelector,
        messages: [
          { role: "user", content: [{ type: "text", text: "Hello" }] },
        ],
      },
      "csrf",
    );
    await client.playgroundEmbedding?.(
      { selector: exactSelector, inputs: ["Hello"] },
      "csrf",
    );
    await client.playgroundCreateMedia?.(
      {
        selector: exactSelector,
        kind: "image",
        prompt: "Draw",
      },
      "csrf",
    );
    await client.playgroundMediaJob?.("job");
    await client.playgroundMediaContent?.("job");

    expect(calls.map((item) => item.path)).toEqual([
      "/v1/admin/playground/model-calls",
      "/v1/admin/playground/embeddings",
      "/v1/admin/playground/media-jobs",
      "/v1/admin/playground/media-jobs/job",
      "/v1/admin/playground/media-jobs/job/content",
    ]);
    for (const [index, call] of calls.entries()) {
      const { body, cache, credentials, headers: suppliedHeaders } = call.init;
      const headers = new Headers(suppliedHeaders);
      expect(credentials).toBe("same-origin");
      expect(cache).toBe("no-store");
      expect(headers.has("Authorization")).toBe(false);
      expect(headers.has("Origin")).toBe(false);
      expect(headers.get("X-CSRF-Token")).toBe(index < 3 ? "csrf" : null);
      if (typeof body === "string") {
        expect(body).not.toContain("workspace");
        expect(body).not.toContain("service_key");
      }
    }
  });

  it("preserves safe post-admission error context", async () => {
    const client = createAdministrationClient(
      vi.fn().mockResolvedValue(
        json(
          {
            logical_call_id: "logical-call",
            selector: exactSelector,
            elapsed_ms: 17,
            attempts: [
              {
                provider_model_api_name: "fake-model",
                outcome: "failed",
                elapsed_ms: 12,
                error: {
                  code: "upstream_failed",
                  message: "The provider attempt failed.",
                },
              },
            ],
            error: {
              code: "upstream_failed",
              message: "The selected provider-model failed.",
            },
          },
          502,
        ),
      ),
    );
    const error = await client
      .playgroundModel?.(
        {
          selector: exactSelector,
          messages: [
            { role: "user", content: [{ type: "text", text: "Hello" }] },
          ],
        },
        "csrf",
      )
      .catch((value: unknown) => value);
    expect(error).toBeInstanceOf(AdministrationApiError);
    expect(error).toMatchObject({
      code: "upstream_failed",
      context: {
        logical_call_id: "logical-call",
        selector: exactSelector,
        elapsed_ms: 17,
        attempts: [
          {
            provider_model_api_name: "fake-model",
            outcome: "failed",
          },
        ],
      },
    });
  });

  it("keeps connection attempt bounds for media error context", async () => {
    const envelope = (elapsedMs: number) => ({
      logical_call_id: "logical-call",
      selector: exactSelector,
      elapsed_ms: 900_000,
      attempts: [
        {
          provider_model_api_name: "fake-model",
          outcome: "failed",
          elapsed_ms: elapsedMs,
          error: {
            code: "upstream_failed",
            message: "The provider attempt failed.",
          },
        },
      ],
      error: {
        code: "upstream_failed",
        message: "The selected provider-model failed.",
      },
    });
    const call = (elapsedMs: number) =>
      createAdministrationClient(
        vi.fn().mockResolvedValue(json(envelope(elapsedMs), 502)),
      )
        .playgroundCreateMedia?.(
          { selector: exactSelector, kind: "image", prompt: "Draw" },
          "csrf",
        )
        .catch((value: unknown) => value);

    await expect(call(600_000)).resolves.toMatchObject({
      code: "upstream_failed",
      context: { attempts: [{ elapsed_ms: 600_000 }] },
    });
    await expect(call(600_001)).resolves.toMatchObject({
      code: "invalid_response",
    });
  });

  it("rejects malformed and uncorrelated playground JSON results", async () => {
    const modelResult = {
      logical_call_id: "call",
      selector: exactSelector,
      elapsed_ms: 5,
      attempts: [attempt],
      result: {
        output_type: "standard",
        provider_model_api_name: "fake-model",
        content: [{ type: "text", text: "ok" }],
        usage,
      },
    };
    const invalidModelResults = [
      { ...modelResult, unexpected: true },
      {
        ...modelResult,
        result: {
          ...modelResult.result,
          provider_model_api_name: "other-model",
        },
      },
      {
        ...modelResult,
        result: {
          ...modelResult.result,
          usage: { ...usage, cost: "0.02" },
        },
      },
    ];
    const modelErrors = await Promise.all(
      invalidModelResults.map((body) => {
        const client = createAdministrationClient(
          vi.fn().mockResolvedValue(json(body)),
        );
        return Promise.resolve(
          client.playgroundModel?.(
            {
              selector: exactSelector,
              messages: [
                { role: "user", content: [{ type: "text", text: "Hello" }] },
              ],
            },
            "csrf",
          ),
        ).catch((error: unknown) => error);
      }),
    );
    for (const error of modelErrors)
      expect(error).toMatchObject({ code: "invalid_response" });

    const embeddingClient = createAdministrationClient(
      vi.fn().mockResolvedValue(
        json({
          logical_call_id: "call",
          selector: exactSelector,
          elapsed_ms: 5,
          attempts: [attempt],
          result: {
            provider_model_api_name: "fake-model",
            embeddings: [{ index: 1, values: [0.1, Number.POSITIVE_INFINITY] }],
            usage,
          },
        }),
      ),
    );
    await expect(
      embeddingClient
        .playgroundEmbedding?.(
          { selector: exactSelector, inputs: ["one"] },
          "csrf",
        )
        .catch((error: unknown) => error),
    ).resolves.toMatchObject({ code: "invalid_response" });

    const mediaClient = createAdministrationClient(
      vi.fn().mockResolvedValue(
        json(
          {
            id: "job",
            logical_call_id: "call",
            selector: exactSelector,
            provider_model_api_name: "fake-model",
            kind: "image",
            state: "succeeded",
            attempts: [attempt],
            elapsed_ms: 5,
            created_at: "2026-08-25T00:00:00Z",
            completed_at: "2026-08-25T00:00:01Z",
          },
          202,
        ),
      ),
    );
    await expect(
      mediaClient
        .playgroundCreateMedia?.(
          { selector: exactSelector, kind: "image", prompt: "Draw" },
          "csrf",
        )
        .catch((error: unknown) => error),
    ).resolves.toMatchObject({ code: "invalid_response" });
  });

  it("rejects malformed and mismatched playground error envelopes", async () => {
    const bodies = [
      {
        logical_call_id: "call",
        selector: { provider_model_api_name: "other-model" },
        elapsed_ms: 5,
        attempts: [],
        error: { code: "upstream_failed", message: "Failed." },
      },
      {
        logical_call_id: "call",
        error: { code: "upstream_failed", message: "Failed." },
      },
      {
        error: {
          code: "upstream_failed",
          message: "Failed.",
          unexpected: true,
        },
      },
      {
        error: {
          code: "conflict",
          message: "A configuration conflict is not a playground error.",
        },
      },
      {
        logical_call_id: "call",
        selector: exactSelector,
        elapsed_ms: 900_001,
        attempts: [],
        error: { code: "upstream_failed", message: "Failed." },
      },
    ];
    const errors = await Promise.all(
      bodies.map((body) => {
        const client = createAdministrationClient(
          vi.fn().mockResolvedValue(json(body, 502)),
        );
        return Promise.resolve(
          client.playgroundModel?.(
            {
              selector: exactSelector,
              messages: [
                { role: "user", content: [{ type: "text", text: "Hello" }] },
              ],
            },
            "csrf",
          ),
        ).catch((error: unknown) => error);
      }),
    );
    for (const error of errors)
      expect(error).toMatchObject({ code: "invalid_response" });
  });

  it("rejects an exact non-2xx stream attempt for another route", async () => {
    const client = createAdministrationClient(
      vi.fn().mockResolvedValue(
        json(
          {
            logical_call_id: "logical-call",
            selector: exactSelector,
            elapsed_ms: 17,
            attempts: [
              {
                provider_model_api_name: "other-model",
                outcome: "failed",
                elapsed_ms: 12,
                error: {
                  code: "upstream_failed",
                  message: "The provider attempt failed.",
                },
              },
            ],
            error: {
              code: "upstream_failed",
              message: "The selected provider-model failed.",
            },
          },
          502,
        ),
      ),
    );

    await expect(
      client
        .playgroundModelStream?.(
          {
            selector: exactSelector,
            messages: [
              { role: "user", content: [{ type: "text", text: "Hello" }] },
            ],
          },
          "csrf",
        )
        .catch((value: unknown) => value),
    ).resolves.toMatchObject({ code: "invalid_response" });
  });

  it("requires no-store JSON and exact media-job identity", async () => {
    const pending = {
      id: "other-job",
      logical_call_id: "call",
      selector: exactSelector,
      provider_model_api_name: "fake-model",
      kind: "image",
      state: "pending",
      attempts: [],
      created_at: "2026-08-25T00:00:00Z",
    };
    const mismatched = createAdministrationClient(
      vi.fn().mockResolvedValue(json(pending)),
    );
    await expect(
      mismatched.playgroundMediaJob?.("job").catch((error: unknown) => error),
    ).resolves.toMatchObject({ code: "invalid_response" });

    const missingNoStore = createAdministrationClient(
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ...pending, id: "job" }), {
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await expect(
      missingNoStore
        .playgroundMediaJob?.("job")
        .catch((error: unknown) => error),
    ).resolves.toMatchObject({ code: "invalid_response" });
  });

  it("preserves succeeded attempt facts on a failed media job", async () => {
    const client = createAdministrationClient(
      vi.fn().mockResolvedValue(
        json({
          id: "job",
          logical_call_id: "call",
          selector: exactSelector,
          provider_model_api_name: "fake-model",
          kind: "image",
          state: "failed",
          attempts: [attempt],
          elapsed_ms: 5,
          usage,
          error: {
            code: "content_unavailable",
            message: "The generated media could not be retained.",
          },
          created_at: "2026-08-25T00:00:00Z",
          completed_at: "2026-08-25T00:00:01Z",
        }),
      ),
    );

    await expect(client.playgroundMediaJob?.("job")).resolves.toMatchObject({
      state: "failed",
      attempts: [{ outcome: "succeeded", usage }],
      usage,
      error: { code: "content_unavailable" },
    });
  });

  it("bounds and cancels administrator playground calls", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const client = createAdministrationClient(
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        if (init?.signal !== null && init?.signal !== undefined)
          signals.push(init.signal);
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        });
      }),
    );
    const timedOut = client
      .playgroundModel?.(
        {
          selector: exactSelector,
          messages: [
            { role: "user", content: [{ type: "text", text: "Hello" }] },
          ],
        },
        "csrf",
      )
      .catch((value: unknown) => value);
    await vi.advanceTimersByTimeAsync(clientDeadlineMilliseconds.runtimeCall);
    await expect(timedOut).resolves.toMatchObject({
      code: "client_timeout",
      status: 408,
    });

    const controller = new AbortController();
    const cancelled = client.playgroundEmbedding?.(
      { selector: exactSelector, inputs: ["Hello"] },
      "csrf",
      controller.signal,
    );
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    await expect(cancelled).rejects.toMatchObject({ name: "AbortError" });
    expect(signals).toHaveLength(2);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
  });

  it("parses split UTF-8 model streams with text and tool output", async () => {
    const streamText = [
      'event: start\r\ndata: {"logical_call_id":"logical-call","selector":{"provider_model_api_name":"fake-model"},"provider_model_api_name":"fake-model"}\r\n\r\n',
      'event: text_delta\r\ndata: {"delta":"Hello 🌍"}\r\n\r\n',
      'event: tool_call\r\ndata: {"tool_call":{"type":"tool_call","id":"tool-1","name":"lookup","arguments_json":"{}"}}\r\n\r\n',
      `event: completed\r\ndata: ${JSON.stringify({
        logical_call_id: "logical-call",
        selector: exactSelector,
        provider_model_api_name: "fake-model",
        elapsed_ms: 9,
        attempts: [attempt],
        usage,
      })}\r\n\r\n`,
    ].join("");
    const bytes = new TextEncoder().encode(streamText);
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let offset = 0; offset < bytes.length; offset += 7)
          controller.enqueue(bytes.slice(offset, offset + 7));
        controller.close();
      },
    });
    const client = createAdministrationClient(
      vi.fn().mockResolvedValue(
        new Response(body, {
          headers: {
            "Cache-Control": "no-store",
            "Content-Type": "text/event-stream",
            "X-LLMRouter-Logical-Call-Id": "logical-call",
          },
        }),
      ),
    );
    const result = await client.playgroundModelStream?.(
      {
        selector: exactSelector,
        messages: [
          { role: "user", content: [{ type: "text", text: "Hello" }] },
        ],
      },
      "csrf",
    );
    expect(result).toMatchObject({
      logical_call_id: "logical-call",
      provider_model_api_name: "fake-model",
      elapsed_ms: 9,
      content: [
        { type: "text", text: "Hello 🌍" },
        {
          type: "tool_call",
          id: "tool-1",
          name: "lookup",
          arguments_json: "{}",
        },
      ],
    });
  });

  it("rejects incomplete and mismatched model streams", async () => {
    const errors = await Promise.all(
      [
        {
          header: "different-call",
          body:
            'event: start\ndata: {"logical_call_id":"logical-call","selector":{"provider_model_api_name":"fake-model"},"provider_model_api_name":"fake-model"}\n\n' +
            `event: completed\ndata: ${JSON.stringify({
              logical_call_id: "logical-call",
              selector: exactSelector,
              provider_model_api_name: "fake-model",
              elapsed_ms: 1,
              attempts: [attempt],
              usage,
            })}\n\n`,
        },
        {
          header: "logical-call",
          body: 'event: start\ndata: {"logical_call_id":"logical-call","selector":{"provider_model_api_name":"fake-model"},"provider_model_api_name":"fake-model"}\n\n',
        },
      ].map(async (fixture) => {
        const client = createAdministrationClient(
          vi.fn().mockResolvedValue(
            new Response(fixture.body, {
              headers: {
                "Cache-Control": "no-store",
                "Content-Type": "text/event-stream",
                "X-LLMRouter-Logical-Call-Id": fixture.header,
              },
            }),
          ),
        );
        return client
          .playgroundModelStream?.(
            {
              selector: exactSelector,
              messages: [
                { role: "user", content: [{ type: "text", text: "Hello" }] },
              ],
            },
            "csrf",
          )
          .catch((value: unknown) => value);
      }),
    );
    for (const error of errors)
      expect(error).toMatchObject({ code: "invalid_response" });
  });

  it("requires the exact two-line SSE frame and final terminator", async () => {
    const startData =
      '{"logical_call_id":"logical-call","selector":{"provider_model_api_name":"fake-model"},"provider_model_api_name":"fake-model"}';
    const completedData = JSON.stringify({
      logical_call_id: "logical-call",
      selector: exactSelector,
      provider_model_api_name: "fake-model",
      elapsed_ms: 1,
      attempts: [attempt],
      usage,
    });
    const results = await Promise.all([
      streamError(`data: ${startData}\nevent: start\n\n`),
      streamError(`event: start\nevent: start\ndata: ${startData}\n\n`),
      streamError(`event: start\ndata: ${startData}\ndata: {}\n\n`),
      streamError(`event: start\nid: one\ndata: ${startData}\n\n`),
      streamError(`${streamStart}event: completed\ndata: ${completedData}`),
    ]);
    for (const result of results)
      expect(result).toMatchObject({ code: "invalid_response" });
  });

  it("rejects mismatched start, completed, and error correlation facts", async () => {
    const completed = (
      selector: string,
      provider: string,
      attempts = [attempt],
    ) =>
      `event: completed\ndata: ${JSON.stringify({
        logical_call_id: "logical-call",
        selector: { provider_model_api_name: selector },
        provider_model_api_name: provider,
        elapsed_ms: 1,
        attempts,
        usage,
      })}\n\n`;
    const errors = await Promise.all([
      streamError(
        'event: start\ndata: {"logical_call_id":"logical-call","selector":{"provider_model_api_name":"other-model"},"provider_model_api_name":"other-model"}\n\n',
      ),
      streamError(streamStart + completed("other-model", "fake-model")),
      streamError(streamStart + completed("fake-model", "other-model")),
      streamError(
        streamStart +
          completed("fake-model", "fake-model", [
            { ...attempt, provider_model_api_name: "other-model" },
          ]),
      ),
      streamError(
        streamStart +
          'event: error\ndata: {"logical_call_id":"other-call","selector":{"provider_model_api_name":"fake-model"},"elapsed_ms":1,"attempts":[],"error":{"code":"upstream_failed","message":"Failed."}}\n\n',
      ),
      streamError(
        streamStart +
          'event: error\ndata: {"logical_call_id":"logical-call","selector":{"provider_model_api_name":"other-model"},"elapsed_ms":1,"attempts":[],"error":{"code":"upstream_failed","message":"Failed."}}\n\n',
      ),
    ]);
    for (const error of errors)
      expect(error).toMatchObject({ code: "invalid_response" });
  });

  it("correlates an assignment final route with its succeeded attempt", async () => {
    const assignmentSelector = {
      assignment_api_name: "default",
      service_api_name: "crewday",
    } as const;
    const body =
      `event: start\ndata: ${JSON.stringify({
        logical_call_id: "logical-call",
        selector: assignmentSelector,
        provider_model_api_name: "fake-model",
      })}\n\n` +
      `event: completed\ndata: ${JSON.stringify({
        logical_call_id: "logical-call",
        selector: assignmentSelector,
        provider_model_api_name: "fake-model",
        elapsed_ms: 1,
        attempts: [{ ...attempt, provider_model_api_name: "other-model" }],
        usage,
      })}\n\n`;
    const client = createAdministrationClient(
      vi.fn().mockResolvedValue(
        new Response(body, {
          headers: {
            "Cache-Control": "no-store",
            "Content-Type": "text/event-stream",
            "X-LLMRouter-Logical-Call-Id": "logical-call",
          },
        }),
      ),
    );
    const result = await client
      .playgroundModelStream?.(
        {
          selector: assignmentSelector,
          messages: [
            { role: "user", content: [{ type: "text", text: "Hello" }] },
          ],
        },
        "csrf",
      )
      .catch((error: unknown) => error);
    expect(result).toMatchObject({ code: "invalid_response" });
  });

  it("bounds unterminated events and cancels their reader", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("x".repeat(65)));
      },
      cancel() {
        cancelled = true;
      },
    });
    await expect(
      streamError(body, { pendingEventBytes: 64 }),
    ).resolves.toMatchObject({ code: "invalid_response" });
    expect(cancelled).toBe(true);
  });

  it("bounds event count, text output, and tool calls", async () => {
    const delta = 'event: text_delta\ndata: {"delta":"a"}\n\n';
    const tool =
      'event: tool_call\ndata: {"tool_call":{"type":"tool_call","id":"tool","name":"lookup","arguments_json":"{}"}}\n\n';
    const cases = [
      streamError(streamStart + delta + delta, { eventCount: 2 }),
      streamError(
        streamStart + 'event: text_delta\ndata: {"delta":"four"}\n\n',
        { textOutputBytes: 3 },
      ),
      streamError(streamStart + tool + tool, { toolCallCount: 1 }),
    ];
    for (const result of await Promise.all(cases))
      expect(result).toMatchObject({ code: "invalid_response" });
  });

  it("bounds one event, tool JSON, and terminal attempts", async () => {
    const oversizedEvent =
      streamStart +
      `event: text_delta\ndata: {"delta":"${"x".repeat(300)}"}\n\n`;
    const oversizedTool =
      streamStart +
      'event: tool_call\ndata: {"tool_call":{"type":"tool_call","id":"tool","name":"lookup","arguments_json":"12345"}}\n\n';
    const completed =
      streamStart +
      `event: completed\ndata: ${JSON.stringify({
        logical_call_id: "logical-call",
        selector: exactSelector,
        provider_model_api_name: "fake-model",
        elapsed_ms: 1,
        attempts: [attempt, attempt],
        usage,
      })}\n\n`;
    const results = await Promise.all([
      streamError(oversizedEvent, { pendingEventBytes: 256 }),
      streamError(oversizedTool, { toolArgumentsBytes: 4 }),
      streamError(completed, { terminalAttempts: 1 }),
    ]);
    for (const result of results)
      expect(result).toMatchObject({ code: "invalid_response" });
  });

  it("bounds aggregate content across individually valid events", async () => {
    const body =
      streamStart +
      'event: text_delta\ndata: {"delta":"1234"}\n\n' +
      'event: tool_call\ndata: {"tool_call":{"type":"tool_call","id":"tool","name":"lookup","arguments_json":"{}"}}\n\n';
    await expect(
      streamError(body, {
        contentBytes: 10,
        textOutputBytes: 10,
        toolIdBytes: 10,
        toolNameBytes: 10,
        toolArgumentsBytes: 10,
      }),
    ).resolves.toMatchObject({ code: "invalid_response" });
  });

  it("rejects incomplete post-start errors", async () => {
    const complete = {
      logical_call_id: "logical-call",
      selector: exactSelector,
      elapsed_ms: 1,
      attempts: [],
      error: { code: "upstream_failed", message: "Failed." },
    };
    const bodies = Object.keys(complete).map((missing) => {
      const event = Object.fromEntries(
        Object.entries(complete).filter(([key]) => key !== missing),
      );
      return `${streamStart}event: error\ndata: ${JSON.stringify(event)}\n\n`;
    });
    for (const result of await Promise.all(
      bodies.map((body) => streamError(body)),
    ))
      expect(result).toMatchObject({ code: "invalid_response" });
  });

  it("requires the post-start error correlation header", async () => {
    const body =
      streamStart +
      'event: error\ndata: {"logical_call_id":"logical-call","selector":{"provider_model_api_name":"fake-model"},"elapsed_ms":3,"attempts":[],"error":{"code":"upstream_failed","message":"Failed."}}\n\n';
    await expect(streamError(body)).resolves.toMatchObject({
      code: "upstream_failed",
      context: {
        logical_call_id: "logical-call",
        selector: exactSelector,
        elapsed_ms: 3,
        attempts: [],
      },
    });
    const client = createAdministrationClient(
      vi.fn().mockResolvedValue(
        new Response(body, {
          headers: {
            "Cache-Control": "no-store",
            "Content-Type": "text/event-stream",
            "X-LLMRouter-Logical-Call-Id": "other-call",
          },
        }),
      ),
    );
    await expect(
      client
        .playgroundModelStream?.(
          {
            selector: exactSelector,
            messages: [
              { role: "user", content: [{ type: "text", text: "Hello" }] },
            ],
          },
          "csrf",
        )
        .catch((value: unknown) => value),
    ).resolves.toMatchObject({ code: "invalid_response" });
  });

  it("rejects unknown fields and invalid numeric result shapes", async () => {
    const completed = (changes: Record<string, unknown>) =>
      `${streamStart}event: completed\ndata: ${JSON.stringify({
        logical_call_id: "logical-call",
        selector: exactSelector,
        provider_model_api_name: "fake-model",
        elapsed_ms: 1,
        attempts: [attempt],
        usage,
        ...changes,
      })}\n\n`;
    const results = await Promise.all([
      streamError(
        streamStart +
          'event: text_delta\ndata: {"delta":"ok","unexpected":true}\n\n',
      ),
      streamError(completed({ elapsed_ms: -1 })),
      streamError(
        completed({
          attempts: [{ ...attempt, elapsed_ms: Number.POSITIVE_INFINITY }],
        }),
      ),
    ]);
    for (const result of results)
      expect(result).toMatchObject({ code: "invalid_response" });
  });

  it("rejects an invalid stream content type and invalid UTF-8", async () => {
    const wrongContentType = createAdministrationClient(
      vi.fn().mockResolvedValue(
        new Response(streamStart, {
          headers: {
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "X-LLMRouter-Logical-Call-Id": "logical-call",
          },
        }),
      ),
    );
    const request = {
      selector: exactSelector,
      messages: [
        {
          role: "user" as const,
          content: [{ type: "text" as const, text: "Hello" }],
        },
      ],
    };
    await expect(
      wrongContentType
        .playgroundModelStream?.(request, "csrf")
        .catch((value: unknown) => value),
    ).resolves.toMatchObject({ code: "invalid_response" });

    const validPrefix = new TextEncoder().encode(streamStart);
    const invalidUtf8 = new Uint8Array(validPrefix.length + 1);
    invalidUtf8.set(validPrefix);
    invalidUtf8[validPrefix.length] = 0xff;
    const invalidBody = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(invalidUtf8);
        controller.close();
      },
    });
    await expect(streamError(invalidBody)).resolves.toMatchObject({
      code: "invalid_response",
    });
  });
});
