import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AdministrationApiError,
  clientDeadlineMilliseconds,
  createAdministrationClient,
  createRuntimeClient,
  errorMessage,
  isoRange,
} from "../src/api.js";

afterEach(() => {
  vi.useRealTimers();
});

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function url(input: string | URL | Request): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.toString() : input.url;
}

describe("native administration client", () => {
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
      return Promise.resolve(
        json({
          items: [{ page }],
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
          items: Array.from({ length: 200 }, (_, index) => ({ index })),
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

  it("uses operation-specific runtime and media client deadlines", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const runtime = createRuntimeClient(
      "service-secret",
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        if (init?.signal !== null && init?.signal !== undefined)
          signals.push(init.signal);
        return new Promise<Response>(() => undefined);
      }),
    );
    const model = runtime
      .model(
        "workspace",
        { assignment_api_name: "chat" },
        "Hello",
        "",
        [],
        null,
        null,
        [],
      )
      .catch((error: unknown) => error);
    const admission = runtime
      .createMedia(
        "workspace",
        { assignment_api_name: "image" },
        "image",
        "Draw",
        [],
        [],
      )
      .catch((error: unknown) => error);
    const status = runtime.mediaJob("job-id").catch((error: unknown) => error);
    const content = runtime
      .mediaContent("job-id")
      .catch((error: unknown) => error);

    await vi.advanceTimersByTimeAsync(clientDeadlineMilliseconds.mediaStatus);
    expect(await status).toMatchObject({
      code: "client_timeout",
      message: "The media-job status did not load before the browser deadline.",
    });
    await vi.advanceTimersByTimeAsync(
      clientDeadlineMilliseconds.mediaAdmission -
        clientDeadlineMilliseconds.mediaStatus,
    );
    expect(await admission).toMatchObject({
      code: "client_timeout",
      message:
        "The media-job request did not finish before the browser deadline.",
    });
    await vi.advanceTimersByTimeAsync(
      clientDeadlineMilliseconds.mediaContent -
        clientDeadlineMilliseconds.mediaAdmission,
    );
    expect(await content).toMatchObject({
      code: "client_timeout",
      message: "The media content did not load before the browser deadline.",
    });
    await vi.advanceTimersByTimeAsync(
      clientDeadlineMilliseconds.runtimeCall -
        clientDeadlineMilliseconds.mediaContent,
    );
    const modelError = await model;
    expect(modelError).toMatchObject({
      code: "client_timeout",
      message: "The runtime call did not finish before the browser deadline.",
    });
    expect(modelError).toBeInstanceOf(AdministrationApiError);
    if (!(modelError instanceof AdministrationApiError)) throw modelError;
    expect(modelError.details?.reason).toContain(
      "Check the detailed logs before you submit the same work again.",
    );
    expect(signals).toHaveLength(4);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
  });

  it("uses a service bearer key only for native runtime operations", async () => {
    let received: RequestInit | undefined;
    const runtime = createRuntimeClient(
      "service-secret",
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        received = init;
        return Promise.resolve(
          json({
            provider_model_api_name: "fake-model",
            embeddings: [{ index: 0, values: [0.1, 0.2] }],
            usage: { units: [], cost: "0", currency: "USD" },
          }),
        );
      }),
    );
    await runtime.embedding(
      "production",
      { assignment_api_name: "embedding" },
      ["one"],
      ["manual"],
    );
    expect(new Headers(received?.headers).get("Authorization")).toBe(
      "Bearer service-secret",
    );
    expect(received?.credentials).toBe("omit");
    expect(typeof received?.body).toBe("string");
    expect(
      JSON.parse(typeof received?.body === "string" ? received.body : "null"),
    ).toEqual({
      workspace_api_name: "production",
      selector: { assignment_api_name: "embedding" },
      inputs: ["one"],
      tags: ["manual"],
    });
  });

  it("sends system prompts and input images through native runtime contracts", async () => {
    const calls: { readonly path: string; readonly body: unknown }[] = [];
    const runtime = createRuntimeClient(
      "service-secret",
      vi.fn((input: string | URL | Request, init?: RequestInit) => {
        calls.push({
          path:
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.href
                : input.url,
          body: JSON.parse(typeof init?.body === "string" ? init.body : "null"),
        });
        return Promise.resolve(
          json({
            id: "job-1",
            workspace_api_name: "production",
            provider_model_api_name: "fake-image",
            kind: "image",
            state: "pending",
            created_at: "2026-08-24T12:00:00Z",
          }),
        );
      }),
    );
    const image = {
      media_type: "image/png" as const,
      data_base64: "aGVsbG8=",
      id: "ui-only",
    };
    await runtime.model(
      "production",
      { assignment_api_name: "chat" },
      "Hello",
      "Be concise.",
      [image],
      0.2,
      120,
      ["manual"],
    );
    await runtime.createMedia(
      "production",
      { provider_model_api_name: "fake-image" },
      "image",
      "A blue square",
      [image],
      ["manual"],
    );
    expect(calls).toEqual([
      {
        path: "/v1/model-calls",
        body: {
          workspace_api_name: "production",
          selector: { assignment_api_name: "chat" },
          messages: [
            { role: "system", content: "Be concise." },
            {
              role: "user",
              content: [
                { type: "text", text: "Hello" },
                {
                  type: "image",
                  media_type: image.media_type,
                  data_base64: image.data_base64,
                },
              ],
            },
          ],
          temperature: 0.2,
          output_limit: 120,
          tags: ["manual"],
        },
      },
      {
        path: "/v1/media-jobs",
        body: {
          workspace_api_name: "production",
          selector: { provider_model_api_name: "fake-image" },
          kind: "image",
          prompt: "A blue square",
          input_images: [
            {
              type: "image",
              media_type: image.media_type,
              data_base64: image.data_base64,
            },
          ],
          tags: ["manual"],
        },
      },
    ]);
  });

  it("creates a stable bounded UTC range", () => {
    expect(isoRange(7, new Date("2026-08-24T12:00:00Z"))).toEqual({
      from: "2026-08-17T12:00:00.000Z",
      to: "2026-08-24T12:00:00.000Z",
    });
  });
});
