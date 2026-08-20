import { describe, expect, it, vi } from "vitest";
import { ExampleHostService } from "../vite.config.js";

describe("example host server", () => {
  it("creates and revokes embed sessions only with the server token", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            session_id: "session-one",
            bootstrap_token: "b".repeat(43),
            frame_url:
              "http://127.0.0.1:5175/service-administration?session_id=session-one&host_origin=http%3A%2F%2F127.0.0.1%3A5176",
            expires_at: "2026-08-20T12:05:00Z",
            message_version: "1",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const service = new ExampleHostService(
      {
        routerOrigin: "http://127.0.0.1:5175",
        serviceId: "service-one",
        serviceToken: "server-only-token",
        workspaceIds: ["workspace-one", "workspace-two"],
      },
      { fetcher, randomId: () => "revision-one" },
    );
    const state = service.initialState();
    const created = await service.createSession(state.context);
    expect(created.session_id).toBe("session-one");
    const createCall = fetcher.mock.calls[0];
    expect(createCall?.[0]).toBe(
      "http://127.0.0.1:5175/v1/services/service-one/administration/embed-sessions",
    );
    expect(createCall?.[1]?.headers).toMatchObject({
      Authorization: "Bearer server-only-token",
    });
    const requestBody = createCall?.[1]?.body;
    expect(typeof requestBody).toBe("string");
    if (typeof requestBody !== "string")
      throw new Error("Request body missing.");
    expect(requestBody).toContain('"allowed_origin":"http://127.0.0.1:5176"');
    expect(requestBody).not.toContain("server-only-token");
    await service.revokeSession("session-one");
    expect(fetcher.mock.calls[1]?.[0]).toContain("/embed-sessions/session-one");
  });

  it("refuses session creation after membership loss", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const service = new ExampleHostService(
      {
        routerOrigin: "http://127.0.0.1:5175",
        serviceId: "service-one",
        serviceToken: "server-only-token",
        workspaceIds: ["workspace-one", "workspace-two"],
      },
      { fetcher, randomId: () => "revision-one" },
    );
    const lost = service.changeContext(
      service.initialState(),
      "lose_membership",
    );
    await expect(service.createSession(lost.context)).rejects.toThrow(
      "membership is not active",
    );
    expect(fetcher).not.toHaveBeenCalled();
  });
});
