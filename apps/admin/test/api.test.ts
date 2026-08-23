import { describe, expect, it, vi } from "vitest";
import {
  AdministrationApiError,
  activateLocalAdministrator,
  consumeTrustedGrantToken,
  createFetchAdministrationClient,
  errorMessage,
  inspectLocalAdministratorSession,
  newLogicalRequestId,
  startPocketIDAdministratorSession,
  startPocketIDRecentAuthentication,
  type ScopeSelection,
} from "../src/api.js";

const scope: ScopeSelection = {
  mode: "global",
  serviceId: "service-one",
  workspaceId: "workspace-one",
};

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestUrl(input: string | URL | Request): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.toString() : input.url;
}

describe("administration API client", () => {
  it("creates a canonical opaque UUIDv7 for one logical request", () => {
    const value = newLogicalRequestId(
      1_777_777_777_777,
      new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
    );
    expect(value).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });
  it("consumes only one generated trusted-grant fragment before requests", () => {
    const replaceState = vi.fn();
    const token = "A".repeat(43);
    expect(
      consumeTrustedGrantToken(
        { hash: `#token=${token}`, pathname: "/trusted-grant", search: "" },
        { replaceState },
      ),
    ).toBe(token);
    expect(replaceState).toHaveBeenCalledWith({}, "", "/trusted-grant");
  });

  it.each(["", "#token=", "#token=short", `#token=${"A".repeat(43)}&extra=1`])(
    "rejects an empty or malformed trusted-grant fragment %s",
    (hash) => {
      const replaceState = vi.fn();
      expect(
        consumeTrustedGrantToken(
          { hash, pathname: "/trusted-grant", search: "?safe=1" },
          { replaceState },
        ),
      ).toBeUndefined();
      expect(replaceState).toHaveBeenCalledTimes(hash === "" ? 0 : 1);
    },
  );

  it("activates only the hidden local administrator session", async () => {
    let received: RequestInit | undefined;
    const fetcher = vi.fn(
      (_input: string | URL | Request, init?: RequestInit) => {
        received = init;
        return Promise.resolve(
          json({
            authenticated: true,
            csrf_token: "generated-local-csrf-value-with-safe-length",
          }),
        );
      },
    );
    const secret = "generated-local-administrator-secret";
    const csrf = await activateLocalAdministrator(secret, fetcher);
    expect(csrf).toBe("generated-local-csrf-value-with-safe-length");
    expect(received?.method).toBe("POST");
    expect(received?.credentials).toBe("same-origin");
    expect(received?.cache).toBe("no-store");
    expect(received?.body).toBe(JSON.stringify({ secret }));
  });

  it("distinguishes local activation from a production administration path", async () => {
    const localPaths: string[] = [];
    const required = await inspectLocalAdministratorSession(
      vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        localPaths.push(path);
        return Promise.resolve(
          path.endsWith("/local-session")
            ? new Response(null, { status: 204 })
            : json({ error: {} }, 401),
        );
      }),
    );
    const productionPaths: string[] = [];
    const unavailable = await inspectLocalAdministratorSession(
      vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        productionPaths.push(path);
        return Promise.resolve(
          path.endsWith("/local-session")
            ? json({ error: {} }, 404)
            : json({ error: {} }, 401),
        );
      }),
      "https://llmrouter.opendle.dev",
    );
    expect(required).toEqual({ state: "required" });
    expect(localPaths).toEqual([
      "/v1/admin/local-session",
      "/v1/admin/session",
    ]);
    expect(unavailable).toEqual({ state: "oidc_required" });
    expect(productionPaths).toEqual(["/v1/admin/session"]);
  });

  it("starts Pocket ID login with a one-use trusted grant token", async () => {
    let body: string | undefined;
    const authorizationUrl = await startPocketIDAdministratorSession(
      "trusted-grant-token",
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        body = typeof init?.body === "string" ? init.body : undefined;
        return Promise.resolve(
          json(
            { authorization_url: "https://auth.opendle.dev/authorize" },
            201,
          ),
        );
      }),
    );
    expect(authorizationUrl).toBe("https://auth.opendle.dev/authorize");
    expect(JSON.parse(body ?? "null")).toEqual({
      purpose: "login",
      return_path: "/",
      trusted_grant_token: "trusted-grant-token",
    });
  });

  it("starts Pocket ID recent authentication without a trusted grant", async () => {
    let body: string | undefined;
    await startPocketIDRecentAuthentication(
      vi.fn((_input: string | URL | Request, init?: RequestInit) => {
        body = typeof init?.body === "string" ? init.body : undefined;
        return Promise.resolve(
          json(
            { authorization_url: "https://auth.opendle.dev/authorize" },
            201,
          ),
        );
      }),
    );
    expect(JSON.parse(body ?? "null")).toEqual({
      purpose: "recent_authentication",
      return_path: "/",
    });
  });

  it("starts one safe recent-authentication flow for a protected failure", async () => {
    const onRecentAuthenticationRequired = vi.fn(() => Promise.resolve());
    const client = createFetchAdministrationClient({
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      onRecentAuthenticationRequired,
      fetcher: vi.fn(() =>
        Promise.resolve(
          json(
            {
              error: {
                code: "recent_auth_required",
                message: "Recent authentication is required.",
              },
            },
            401,
          ),
        ),
      ),
    });
    await expect(
      client.createCredential({
        ownerScope: "service-one",
        secret: "write-only",
        safeLabel: "test",
      }),
    ).rejects.toMatchObject({ code: "recent_auth_required" });
    expect(onRecentAuthenticationRequired).toHaveBeenCalledOnce();
  });

  it("keeps failed local activation errors safe", async () => {
    const secret = "generated-secret-that-must-not-return";
    await expect(
      activateLocalAdministrator(
        secret,
        vi.fn(() => Promise.resolve(json({ private: secret }, 401))),
      ),
    ).rejects.toMatchObject({
      code: "local_administrator_activation_failed",
      status: 401,
    });
  });

  it("loads only bounded exact-scope routes", async () => {
    const paths: string[] = [];
    const fetcher = vi.fn((input: string | URL | Request) => {
      const path = requestUrl(input);
      paths.push(path);
      if (path.includes("/state"))
        return Promise.resolve(
          json({
            kind: "workspace",
            service_id: "service-one",
            workspace_id: "workspace-one",
            display_name: "Workspace",
            state: "active",
            revision: "revision-1",
          }),
        );
      if (path.includes("/accounting/summary"))
        return Promise.resolve(
          json({
            from: "2026-08-13T00:00:00.000Z",
            to: "2026-08-20T00:00:00.000Z",
            currency: "USD",
            logical_requests: 0,
            attempts: 0,
            usage: [],
            cost: "0",
            corrections: "0",
          }),
        );
      return Promise.resolve(json({ items: [], next_cursor: null }));
    });
    const client = createFetchAdministrationClient({
      fetcher,
      now: () => new Date("2026-08-20T00:00:00Z"),
    });
    const result = await client.load(scope);
    expect(result.state?.workspace_id).toBe("workspace-one");
    expect(paths).toHaveLength(7);
    expect(paths.every((path) => !path.includes("/credentials"))).toBe(true);
    expect(paths.every((path) => path.includes("/v1/admin/"))).toBe(true);
    expect(paths).toContain(
      "/v1/admin/services/service-one/budgets?workspace_id=workspace-one",
    );
    expect(
      paths
        .filter(
          (path) =>
            path.includes("provider-instances") ||
            path.includes("provider-model-routes"),
        )
        .every((path) => !path.includes("workspace_id=")),
    ).toBe(true);
    expect(
      paths
        .filter(
          (path) =>
            path.includes("/state") ||
            path.includes("assignments") ||
            path.includes("model-requests") ||
            path.includes("accounting/summary"),
        )
        .every((path) => path.includes("workspace_id=workspace-one")),
    ).toBe(true);
    expect(
      paths.some((path) =>
        path.includes("model-requests?workspace_id=workspace-one"),
      ),
    ).toBe(true);
  });

  it("keeps setup data when accounting has no configured currency", async () => {
    const client = createFetchAdministrationClient({
      fetcher: vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        if (path.includes("/state")) {
          return Promise.resolve(
            json({
              kind: "service",
              service_id: "service-one",
              display_name: "Service one",
              state: "active",
              revision: "state-revision-1",
            }),
          );
        }
        if (path.includes("/accounting/summary")) {
          return Promise.resolve(
            json(
              {
                error: {
                  code: "invalid_request",
                  message: "The request is invalid.",
                },
              },
              400,
            ),
          );
        }
        return Promise.resolve(
          json({
            items: [],
            next_cursor: null,
            configuration_revision: null,
          }),
        );
      }),
    });

    const result = await client.load({
      mode: "global",
      serviceId: "service-one",
      workspaceId: "",
    });

    expect(result.state?.display_name).toBe("Service one");
    expect(result.providers).toEqual([]);
    expect(result.accounting).toBeNull();
    expect(result.failures.accounting).toContain("no configured currency");
    expect(result.failures.providers).toBeUndefined();
  });

  it("loads all pages for every selected snapshot collection", async () => {
    const collectionPaths = new Map<string, string[]>([
      ["provider-instances", []],
      ["provider-model-routes", []],
      ["assignments", []],
      ["model-requests", []],
    ]);
    const client = createFetchAdministrationClient({
      fetcher: vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        if (path.includes("/state")) {
          return Promise.resolve(
            json({
              kind: "service",
              service_id: "service-one",
              display_name: "Service one",
              state: "active",
              revision: "state-revision-1",
            }),
          );
        }
        if (path.includes("/accounting/summary")) {
          return Promise.resolve(
            json({
              from: "2026-08-13T00:00:00.000Z",
              to: "2026-08-20T00:00:00.000Z",
              currency: "USD",
              logical_requests: 0,
              attempts: 0,
              usage: [],
              cost: "0",
              corrections: "0",
            }),
          );
        }
        const collection = [...collectionPaths.keys()].find((name) =>
          path.includes(name),
        );
        if (collection !== undefined) {
          collectionPaths.get(collection)?.push(path);
          return Promise.resolve(
            json({
              items: [],
              next_cursor: path.includes("cursor=page-one") ? null : "page-one",
              ...(collection === "model-requests"
                ? {}
                : { configuration_revision: "configuration-revision-1" }),
            }),
          );
        }
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }),
    });

    const result = await client.load({
      mode: "service",
      serviceId: "service-one",
      workspaceId: "",
    });

    expect(result.configuration_revision).toBe("configuration-revision-1");
    for (const paths of collectionPaths.values()) {
      expect(paths).toHaveLength(2);
      expect(paths[1]).toContain("cursor=page-one");
    }
  });

  it("lists the named services available to the administrator", async () => {
    const paths: string[] = [];
    const client = createFetchAdministrationClient({
      fetcher: vi.fn((input: string | URL | Request) => {
        paths.push(requestUrl(input));
        return Promise.resolve(
          json({
            items: [
              {
                service_id: "service-one",
                display_name: "Xbot",
                state: "active",
              },
            ],
            next_cursor: null,
          }),
        );
      }),
    });
    await expect(client.listServices()).resolves.toEqual([
      {
        service_id: "service-one",
        display_name: "Xbot",
        state: "active",
      },
    ]);
    expect(paths).toEqual(["/v1/admin/services"]);
  });

  it("loads every service registry page without hiding retained services", async () => {
    const paths: string[] = [];
    const client = createFetchAdministrationClient({
      fetcher: vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        paths.push(path);
        return Promise.resolve(
          path.includes("cursor=service-one")
            ? json({
                items: [
                  {
                    service_id: "service-two",
                    display_name: "Ontology",
                    state: "retired",
                  },
                ],
                next_cursor: null,
              })
            : json({
                items: [
                  {
                    service_id: "service-one",
                    display_name: "Xbot",
                    state: "active",
                  },
                ],
                next_cursor: "service-one",
              }),
        );
      }),
    });
    const services = await client.listServices();
    expect(services.map((service) => service.display_name)).toEqual([
      "Xbot",
      "Ontology",
    ]);
    expect(paths).toEqual([
      "/v1/admin/services",
      "/v1/admin/services?cursor=service-one",
    ]);
  });

  it("loads every named catalog page without exposing a catalog ID control", async () => {
    const paths: string[] = [];
    const client = createFetchAdministrationClient({
      fetcher: vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        paths.push(path);
        return Promise.resolve(
          json({
            items: path.includes("cursor=model-one")
              ? [
                  {
                    stable_id: "model-two",
                    kind: "model",
                    display_name: "Model two",
                    capabilities: ["chat.complete"],
                    state: "active",
                    settings: null,
                    active_revision: "revision-1",
                  },
                ]
              : [
                  {
                    stable_id: "model-one",
                    kind: "model",
                    display_name: "Model one",
                    capabilities: ["chat.complete"],
                    state: "active",
                    settings: null,
                    active_revision: "revision-1",
                  },
                ],
            next_cursor: path.includes("cursor=model-one") ? null : "model-one",
            configuration_revision: "revision-1",
          }),
        );
      }),
    });

    await expect(client.listCatalog("models")).resolves.toHaveLength(2);
    expect(paths).toEqual([
      "/v1/admin/catalog/models",
      "/v1/admin/catalog/models?cursor=model-one",
    ]);
  });

  it("rejects catalog pages from different configuration revisions", async () => {
    const client = createFetchAdministrationClient({
      fetcher: vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        return Promise.resolve(
          json({
            items: [],
            next_cursor: path.includes("cursor=model-one") ? null : "model-one",
            configuration_revision: path.includes("cursor=model-one")
              ? "revision-2"
              : "revision-1",
          }),
        );
      }),
    });

    await expect(client.listCatalog("models")).rejects.toMatchObject({
      code: "configuration_revision_conflict",
      staleRevision: true,
    });
  });

  it("treats a missing exact-scope limit as unconfigured", async () => {
    const client = createFetchAdministrationClient({
      fetcher: vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        if (path.includes("/budgets")) {
          return Promise.resolve(
            json({ error: { code: "not_found", message: "Not found." } }, 404),
          );
        }
        if (path.includes("/state")) {
          return Promise.resolve(
            json({
              kind: "service",
              service_id: "service-one",
              display_name: "Service one",
              state: "active",
              revision: "state-revision-1",
            }),
          );
        }
        if (path.includes("/accounting/summary")) {
          return Promise.resolve(json({}, 400));
        }
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }),
    });

    const result = await client.load({
      mode: "global",
      serviceId: "service-one",
      workspaceId: "",
    });
    expect(result.budget).toBeNull();
    expect(result.failures.budget).toBeUndefined();
  });

  it("does not treat a missing budget as unconfigured when scope state fails", async () => {
    const client = createFetchAdministrationClient({
      fetcher: vi.fn((input: string | URL | Request) => {
        const path = requestUrl(input);
        if (path.includes("/budgets")) {
          return Promise.resolve(
            json({ error: { code: "not_found", message: "Not found." } }, 404),
          );
        }
        if (path.includes("/state")) {
          return Promise.resolve(
            json({ error: { code: "insufficient_scope" } }, 403),
          );
        }
        if (path.includes("/accounting/summary")) {
          return Promise.resolve(json({}, 400));
        }
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }),
    });

    const result = await client.load(scope);
    expect(result.state).toBeNull();
    expect(result.budget).toBeNull();
    expect(result.failures.state).toBeDefined();
    expect(result.failures.budget).toContain("exact service or workspace");
  });

  it("sends protected mutation headers and does not expect a secret response", async () => {
    let received: RequestInit | undefined;
    const fetcher = vi.fn(
      (_input: string | URL | Request, init?: RequestInit) => {
        received = init;
        return Promise.resolve(
          json(
            {
              credential_id: "credential-1",
              owner_scope: "service-one",
              provider_catalog_id: "openai_compatible.v1",
              state: "active",
              revision: "revision-1",
              created_at: "2026-08-20T00:00:00Z",
              fingerprint: "safe",
            },
            201,
          ),
        );
      },
    );
    const client = createFetchAdministrationClient({
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      fetcher,
    });
    const result = await client.createCredential({
      ownerScope: "service-one",
      safeLabel: "Live OpenRouter",
      secret: "test-secret-never-returned",
    });
    const headers = new Headers(received?.headers);
    expect(headers.get("X-CSRF-Token")).toBe(
      "csrf-token-with-at-least-thirty-two-characters",
    );
    expect(headers.get("Idempotency-Key")?.length).toBeGreaterThan(15);
    expect(received?.credentials).toBe("same-origin");
    const receivedBody = received?.body;
    expect(typeof receivedBody === "string" ? receivedBody : "").toContain(
      "test-secret-never-returned",
    );
    expect(JSON.stringify(result)).not.toContain("test-secret-never-returned");
  });

  it("sends a write-only credential replacement with its exact revision", async () => {
    let path = "";
    let received: RequestInit | undefined;
    const client = createFetchAdministrationClient({
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      fetcher: vi.fn((input: string | URL | Request, init?: RequestInit) => {
        path = requestUrl(input);
        received = init;
        return Promise.resolve(
          json({
            credential_id: "credential-1",
            owner_scope: "global",
            provider_catalog_id: "openai_compatible.v1",
            state: "active",
            revision: "revision-2",
            created_at: "2026-08-20T00:00:00Z",
            fingerprint: "safe-new-fingerprint",
          }),
        );
      }),
    });

    const result = await client.changeCredential("credential-1", "rotate", {
      expectedRevision: "revision-1",
      reason: "Replace the provider credential",
      replacementSecret: "replacement-secret-never-returned",
    });

    expect(path).toBe("/v1/admin/credentials/credential-1/rotate");
    if (typeof received?.body !== "string") {
      throw new Error("The credential change body was not serialized.");
    }
    expect(JSON.parse(received.body)).toEqual({
      expected_revision: "revision-1",
      reason: "Replace the provider credential",
      replacement_secret: "replacement-secret-never-returned",
    });
    expect(
      new Headers(received.headers).get("Idempotency-Key")?.length,
    ).toBeGreaterThan(15);
    expect(JSON.stringify(result)).not.toContain(
      "replacement-secret-never-returned",
    );
  });

  it("reports safe API, stale revision, and offline errors", async () => {
    const staleClient = createFetchAdministrationClient({
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      fetcher: vi.fn(() =>
        Promise.resolve(
          json(
            {
              error: {
                code: "configuration_revision_conflict",
                message: "Read the current active revision.",
                request_id: "safe-request-1",
              },
            },
            409,
          ),
        ),
      ),
    });
    await expect(
      staleClient.putAssignment(scope, "general", {}),
    ).rejects.toMatchObject({
      code: "configuration_revision_conflict",
      staleRevision: true,
      status: 409,
    });

    const offlineClient = createFetchAdministrationClient({
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      fetcher: vi.fn(() =>
        Promise.reject(new TypeError("private network detail")),
      ),
    });
    const offlineSnapshot = await offlineClient.load(scope);
    expect(offlineSnapshot.state).toBeNull();
    expect(offlineSnapshot.failures.providers).toBe(
      "The administration service is offline.",
    );

    await expect(
      offlineClient.putAssignment(scope, "general", {}),
    ).rejects.toMatchObject({
      code: "offline",
      status: 0,
      outcomeUncertain: true,
    });
    expect(errorMessage(new Error("private detail"))).not.toContain(
      "private detail",
    );
    expect(
      errorMessage(
        new AdministrationApiError("Safe message", {
          code: "safe",
          requestId: "request-2",
          status: 422,
        }),
      ),
    ).toBe("Safe message Request request-2.");
  });

  it("preserves abort signals during an exact-scope switch", async () => {
    const fetcher = vi.fn(() =>
      Promise.reject(
        new DOMException("The request was aborted.", "AbortError"),
      ),
    );
    const client = createFetchAdministrationClient({ fetcher });
    await expect(client.load(scope)).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it("keeps workspace identity only on workspace-capable mutations", async () => {
    const paths: string[] = [];
    const fetcher = vi.fn((input: string | URL | Request) => {
      paths.push(requestUrl(input));
      return Promise.resolve(
        json({
          resource_id: "resource-1",
          active_revision: "revision-1",
          distribution_state: "current",
          operation_id: "operation-1",
        }),
      );
    });
    const client = createFetchAdministrationClient({
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      fetcher,
    });
    await client.putProvider(scope, null, {});
    await client.putRoute(scope, null, {});
    await client.putAssignment(scope, "general", {});
    await client.putBudget(scope, {
      hardLimit: "25",
      currency: "USD",
      warningThreshold: "20",
      resetPeriod: "monthly",
      expectedRevision: "3",
    });
    expect(paths[0]).not.toContain("workspace_id=");
    expect(paths[1]).not.toContain("workspace_id=");
    expect(paths[2]).toContain("workspace_id=workspace-one");
    expect(paths[3]).toBe(
      "/v1/admin/services/service-one/budgets?workspace_id=workspace-one",
    );
  });

  it("loads one request detail only in the exact selected scope", async () => {
    const paths: string[] = [];
    const fetcher = vi.fn((input: string | URL | Request) => {
      paths.push(requestUrl(input));
      return Promise.resolve(
        json({
          request_id: "request/with a space",
          state: "succeeded",
          state_revision: 4,
          attempts: [],
        }),
      );
    });
    const client = createFetchAdministrationClient({ fetcher });
    const detail = await client.getRequest(scope, "request/with a space");
    expect(detail.state).toBe("succeeded");
    expect(paths).toEqual([
      "/v1/admin/services/service-one/model-requests/request%2Fwith%20a%20space?workspace_id=workspace-one",
    ]);
  });

  it("runs a diagnostic with matching request and idempotency identities", async () => {
    let receivedUrl = "";
    let received: RequestInit | undefined;
    const fetcher = vi.fn(
      (input: string | URL | Request, init?: RequestInit) => {
        receivedUrl = requestUrl(input);
        received = init;
        return Promise.resolve(
          json({
            request_id: "0198a080-0000-7000-8000-000000000032",
            service_id: scope.serviceId,
            workspace_id: scope.workspaceId,
            exact_route: "route-1",
            route_configuration_revision: "revision-1",
            authorization_expires_at: "2026-08-23T07:05:00Z",
            state: "active",
            phases: [],
            status_url:
              "/v1/model-requests/0198a080-0000-7000-8000-000000000032",
          }),
        );
      },
    );
    const client = createFetchAdministrationClient({
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      fetcher,
    });
    const requestId = "0198a080-0000-7000-8000-000000000032";
    const controller = new AbortController();

    await client.runDiagnostic(
      scope,
      {
        requestId,
        exactRoute: "route-1",
        reason: "Verify route",
      },
      controller.signal,
    );

    expect(receivedUrl).toBe(
      "/v1/admin/services/service-one/diagnostics?workspace_id=workspace-one",
    );
    const headers = new Headers(received?.headers);
    expect(headers.get("Idempotency-Key")).toBe(requestId);
    expect(headers.get("X-CSRF-Token")).toBe(
      "csrf-token-with-at-least-thirty-two-characters",
    );
    expect(received?.body).toBe(
      JSON.stringify({
        request_id: requestId,
        exact_route: "route-1",
        reason: "Verify route",
      }),
    );
    expect(received?.signal).toBe(controller.signal);
  });

  it.each([
    [403, "insufficient_scope"],
    [404, "request_not_found"],
  ])(
    "keeps a safe request-detail failure for HTTP %i",
    async (status, code) => {
      const client = createFetchAdministrationClient({
        fetcher: vi.fn(() =>
          Promise.resolve(
            json(
              {
                error: {
                  code,
                  message: "The safe request detail is not available.",
                },
              },
              status,
            ),
          ),
        ),
      });
      await expect(
        client.getRequest(scope, "request-one"),
      ).rejects.toMatchObject({
        code,
        status,
      });
    },
  );
});
