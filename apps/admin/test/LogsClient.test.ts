import { describe, expect, it, vi } from "vitest";
import { createAdministrationClient } from "../src/api.ts";
const row = {
  id: "record",
  logical_call_id: "call",
  call_actor: "administrator",
  administrator_subject: "admin",
  provider_model_api_name: "route",
  kind: "model",
  outcome: "succeeded",
  started_at: "2026-09-08T00:00:00Z",
};
const respond = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
function requestPath(input: string | URL | Request | undefined): string {
  if (input === undefined) throw Error("Missing request.");
  return input instanceof Request ? input.url : input.toString();
}
describe("Logs page client contract", () => {
  it("sends exact100 and only active non-time filters with unchanged cursor", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockImplementation(() =>
        Promise.resolve(respond({ items: [], page: { has_more: false } })),
      );
    const client = createAdministrationClient(fetcher);
    const from = "2026-09-01T12:00:00Z",
      to = "2026-09-08T12:00:00Z";
    await client.requestLogsPage(from, to, undefined, {
      call_actor: "administrator",
      administrator: "a / ?",
      configuration_service: "root",
    });
    let request = new URL(
      requestPath(fetcher.mock.calls[0]?.[0]),
      "http://127.0.0.1:5174",
    );
    expect(Object.fromEntries(request.searchParams)).toEqual({
      from,
      to,
      limit: "100",
      call_actor: "administrator",
      administrator: "a / ?",
      configuration_service: "root",
    });
    await client.requestLogsPage(from, to, "opaque / cursor");
    request = new URL(
      requestPath(fetcher.mock.calls[1]?.[0]),
      "http://127.0.0.1:5174",
    );
    expect(Object.fromEntries(request.searchParams)).toEqual({
      from,
      to,
      limit: "100",
      cursor: "opaque / cursor",
    });
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({
      credentials: "same-origin",
      cache: "no-store",
    });
  });
  it.each([undefined, null, "", "c".repeat(501), 42])(
    "retains valid records for stopped continuation %s",
    async (cursor) => {
      const value = {
        items: [row],
        page: {
          has_more: true,
          ...(cursor === undefined ? {} : { next_cursor: cursor }),
        },
      };
      const client = createAdministrationClient(
        vi.fn<typeof fetch>().mockResolvedValue(respond(value)),
      );
      const result = await client.requestLogsPage("from", "to");
      expect(result.items).toEqual([row]);
      expect(result.page.has_more).toBe(true);
    },
  );
  it.each([
    { items: [{ ...row, secret: "bad" }], page: { has_more: true } },
    { items: [{ ...row, call_actor: "wrong" }], page: { has_more: true } },
    { items: [row], page: { has_more: "true" } },
    { items: [row], page: { has_more: true, extra: "bad" } },
    { items: [row], page: { has_more: true }, extra: "bad" },
  ])(
    "does not accept invalid records or unrelated page shape",
    async (value) => {
      const client = createAdministrationClient(
        vi.fn<typeof fetch>().mockResolvedValue(respond(value)),
      );
      await expect(client.requestLogsPage("from", "to")).rejects.toMatchObject({
        code: "invalid_response",
      });
    },
  );
  it("rejects101 records and reports requested100", async () => {
    const client = createAdministrationClient(
      vi.fn<typeof fetch>().mockResolvedValue(
        respond({
          items: Array.from({ length: 101 }, () => row),
          page: { has_more: false },
        }),
      ),
    );
    await expect(client.requestLogsPage("from", "to")).rejects.toMatchObject({
      details: {
        reason: "The list page exceeds the requested 100 item limit.",
      },
    });
  });
  it("reads selected detail and media only through authenticated Router paths", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        respond({ summary: row, request_json: "retained", attempts: [] }),
      )
      .mockResolvedValueOnce(new Response("bytes"));
    const client = createAdministrationClient(fetcher);
    await client.requestLog("record");
    await client.requestLogMedia("record", "image / ?");
    expect(fetcher.mock.calls.map((args) => args[0])).toEqual([
      "/v1/admin/request-logs/record",
      "/v1/admin/request-logs/record/media/image%20%2F%20%3F/content",
    ]);
    for (const args of fetcher.mock.calls)
      expect(args[1]).toMatchObject({
        credentials: "same-origin",
        cache: "no-store",
      });
  });
});
