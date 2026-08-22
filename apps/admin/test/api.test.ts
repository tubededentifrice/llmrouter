import { describe, expect, it, vi } from "vitest";
import {
  AdministrationApiError,
  activateLocalAdministrator,
  consumeTrustedGrantToken,
  createFetchAdministrationClient,
  errorMessage,
  inspectLocalAdministratorSession,
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
    expect(result.state.workspace_id).toBe("workspace-one");
    expect(paths).toHaveLength(6);
    expect(paths.every((path) => !path.includes("/credentials"))).toBe(true);
    expect(paths.every((path) => path.includes("/v1/admin/"))).toBe(true);
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
      fetcher: vi.fn(() =>
        Promise.reject(new TypeError("private network detail")),
      ),
    });
    await expect(offlineClient.load(scope)).rejects.toMatchObject({
      code: "offline",
      status: 0,
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
    expect(paths[0]).not.toContain("workspace_id=");
    expect(paths[1]).not.toContain("workspace_id=");
    expect(paths[2]).toContain("workspace_id=workspace-one");
  });
});
