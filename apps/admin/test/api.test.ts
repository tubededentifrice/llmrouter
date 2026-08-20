import { describe, expect, it, vi } from "vitest";
import {
  AdministrationApiError,
  createFetchAdministrationClient,
  errorMessage,
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

describe("administration API client", () => {
  it("loads only bounded exact-scope routes", async () => {
    const paths: string[] = [];
    const fetcher = vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      paths.push(path);
      if (path.includes("/state"))
        return json({
          kind: "workspace",
          service_id: "service-one",
          workspace_id: "workspace-one",
          display_name: "Workspace",
          state: "active",
          revision: "revision-1",
        });
      if (path.includes("/accounting/summary"))
        return json({
          from: "2026-08-13T00:00:00.000Z",
          to: "2026-08-20T00:00:00.000Z",
          currency: "USD",
          logical_requests: 0,
          attempts: 0,
          usage: [],
          cost: "0",
          corrections: "0",
        });
      return json({ items: [], next_cursor: null });
    });
    const client = createFetchAdministrationClient({
      fetcher,
      now: () => new Date("2026-08-20T00:00:00Z"),
    });
    const result = await client.load(scope);
    expect(result.state.workspace_id).toBe("workspace-one");
    expect(paths).toHaveLength(7);
    expect(paths.every((path) => path.includes("/v1/admin/"))).toBe(true);
    expect(
      paths
        .filter((path) => path.includes("/services/"))
        .every((path) => path.includes("workspace_id=workspace-one")),
    ).toBe(true);
    expect(
      paths.some((path) =>
        path.includes("model-requests?limit=100&workspace_id=workspace-one"),
      ),
    ).toBe(true);
  });

  it("sends protected mutation headers and does not expect a secret response", async () => {
    let received: RequestInit | undefined;
    const fetcher = vi.fn(
      async (_input: string | URL | Request, init?: RequestInit) => {
        received = init;
        return json(
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
    expect(String(received?.body)).toContain("test-secret-never-returned");
    expect(JSON.stringify(result)).not.toContain("test-secret-never-returned");
  });

  it("reports safe API, stale revision, and offline errors", async () => {
    const staleClient = createFetchAdministrationClient({
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      fetcher: vi.fn(async () =>
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
    });
    await expect(
      staleClient.putAssignment(scope, "general", {}),
    ).rejects.toMatchObject({
      code: "configuration_revision_conflict",
      staleRevision: true,
      status: 409,
    });

    const offlineClient = createFetchAdministrationClient({
      fetcher: vi.fn(async () => {
        throw new TypeError("private network detail");
      }),
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
    const fetcher = vi.fn(async () => {
      throw new DOMException("The request was aborted.", "AbortError");
    });
    const client = createFetchAdministrationClient({ fetcher });
    await expect(client.load(scope)).rejects.toMatchObject({
      name: "AbortError",
    });
  });
});
