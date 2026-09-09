import { describe, expect, it, vi } from "vitest";
import { createAdministrationClient } from "../src/api.ts";
import {
  exactAssignmentLabel,
  initialStatisticsDraft,
  validateStatistics,
} from "../src/statisticsState.ts";
describe("Statistics native HTTP query", () => {
  it("sends exact timestamps, each active filter and eight ordered unique groups, then omits inactive filters", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            from: "2028-01-01T00:00:00Z",
            to: "2029-01-01T00:00:00Z",
            group_by: [],
            buckets: [],
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const client = createAdministrationClient(fetcher);
    const draft = {
      ...initialStatisticsDraft(),
      from: "2028-01-01",
      through: "2028-12-31",
      service: "root",
      workspace: "desk",
      call_actor: "administrator",
      administrator: "a / ?",
      configuration_service: "context",
      assignment: exactAssignmentLabel,
      provider_model: "route",
      outcome: "failed",
      tag: "a & b",
      group_by: [
        "tag",
        "outcome",
        "assignment",
        "configuration_service",
        "administrator",
        "workspace",
        "service",
        "date",
        "date",
      ],
    };
    const query = validateStatistics(draft).query;
    if (!query) throw Error("Expected valid query.");
    await client.statistics(query);
    const input = fetcher.mock.calls[0]?.[0];
    const url = new URL(
      input instanceof Request ? input.url : String(input),
      "http://127.0.0.1:5174",
    );
    expect(url.pathname).toBe("/v1/admin/statistics");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      from: "2028-01-01T00:00:00Z",
      to: "2029-01-01T00:00:00Z",
      service: "root",
      workspace: "desk",
      call_actor: "administrator",
      administrator: "a / ?",
      configuration_service: "context",
      assignment: "(exact)",
      provider_model: "route",
      outcome: "failed",
      tag: "a & b",
      group_by: "tag",
    });
    expect(url.searchParams.getAll("group_by")).toEqual([
      "date",
      "service",
      "workspace",
      "administrator",
      "configuration_service",
      "assignment",
      "outcome",
      "tag",
    ]);
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({
      credentials: "same-origin",
      cache: "no-store",
    });
    await client.statistics({ from: query.from, to: query.to });
    const next = fetcher.mock.calls[1]?.[0];
    expect(
      Object.fromEntries(
        new URL(
          next instanceof Request ? next.url : String(next),
          "http://127.0.0.1:5174",
        ).searchParams,
      ),
    ).toEqual({ from: query.from, to: query.to });
  });
});
