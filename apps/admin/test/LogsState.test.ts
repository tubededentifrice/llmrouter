import { describe, expect, it, vi } from "vitest";
import {
  LogsController,
  emptyLogsFilters,
  logsRetentionBounds,
  validateLogsFilters,
} from "../src/logsState.ts";
import type {
  AdministrationClient,
  Page,
  RequestLogSummary,
} from "../src/api.ts";
const instant = Date.parse("2026-03-08T00:00:00.987Z");
const bounds = { from: "2026-03-07T00:00:00Z", to: "2026-03-08T00:00:00Z" };
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const row = (id: string): RequestLogSummary => ({
  id,
  logical_call_id: `call-${id}`,
  call_actor: "administrator",
  administrator_subject: "subject",
  provider_model_api_name: "route",
  kind: "model",
  outcome: "succeeded",
  started_at: "2026-03-07T12:00:00Z",
});
const page = (ids: string[], cursor?: string): Page<RequestLogSummary> => ({
  items: ids.map(row),
  page: {
    has_more: cursor !== undefined,
    ...(cursor === undefined ? {} : { next_cursor: cursor }),
  },
});
function setup() {
  const client = {
    retention: vi.fn(() => Promise.resolve({ duration_days: 1 })),
    requestLogsPage: vi.fn(() => Promise.resolve(page(["new"], "older"))),
    requestLog: vi.fn((id: string) =>
      Promise.resolve({
        summary: row(id),
        request_json: "<script>not executable</script>",
        attempts: [],
      }),
    ),
    requestLogMedia: vi.fn(() => Promise.resolve(new Blob(["safe"]))),
  };
  return {
    client,
    controller: new LogsController(
      client as unknown as AdministrationClient,
      () => instant,
    ),
  };
}
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
}
describe("automatic Logs UTC and validation", () => {
  for (const zone of [
    "UTC",
    "Pacific/Kiritimati",
    "Pacific/Pago_Pago",
    "America/New_York",
  ])
    for (const days of [1, 30])
      it(`${zone}: exact ${String(days)}-day query after retention with whole-second UTC`, async () => {
        const environment = process.env;
        const old = environment.TZ;
        environment.TZ = zone;
        try {
          const { client, controller } = setup();
          client.retention.mockResolvedValue({ duration_days: days });
          await controller.refresh();
          const from =
            days === 1 ? "2026-03-07T00:00:00Z" : "2026-02-06T00:00:00Z";
          expect(client.requestLogsPage).toHaveBeenCalledWith(
            from,
            "2026-03-08T00:00:00Z",
            undefined,
            expect.any(Object),
          );
          expect(logsRetentionBounds(days, instant)).toEqual({
            from,
            to: "2026-03-08T00:00:00Z",
          });
        } finally {
          if (old === undefined) delete environment.TZ;
          else environment.TZ = old;
        }
      });
  it.each([0, 31, 1.5, NaN, Infinity])(
    "rejects invalid retention %s",
    (days) => {
      expect(() => logsRetentionBounds(days, instant)).toThrow();
    },
  );
  it.each([
    "2026-02-29T01:00:00",
    "2026-04-31T01:00:00",
    "2026-03-07T24:00:00",
    "2026-03-07T01:00:60",
    "2026-03-07T01:00",
    "2026-03-07T01:00:00Z",
    "0000-03-07T01:00:00",
  ])("rejects Gregorian UTC value %s", (from) => {
    const filters = { ...emptyLogsFilters(), from };
    expect(validateLogsFilters(filters, bounds).errors.from).toBe(
      "Enter a valid From time in UTC.",
    );
  });
  it("sends all active values and omits empty values", () => {
    const filters = {
      from: "2026-03-07T00:00:00",
      to: "2026-03-07T23:59:59",
      call_actor: "administrator",
      administrator: "subject / ?",
      configuration_service: "root-child",
    };
    expect(validateLogsFilters(filters, bounds)).toEqual({
      errors: {},
      query: { ...filters, from: `${filters.from}Z`, to: `${filters.to}Z` },
    });
    expect(validateLogsFilters(emptyLogsFilters(), bounds).query).toEqual(
      bounds,
    );
  });
  it("assigns errors in control order and preserves Unicode character limits", () => {
    const filters = {
      from: "2026-03-06T00:00:00",
      to: "2026-03-09T00:00:00",
      call_actor: "wrong",
      administrator: "a".repeat(501),
      configuration_service: "Wrong_",
    };
    expect(validateLogsFilters(filters, bounds).errors).toEqual({
      from: "Select times inside the configured Logs retention window.",
      call_actor: "Select a valid call actor.",
      administrator:
        "Enter an administrator subject of 500 characters or fewer.",
      configuration_service:
        "Enter a valid assignment configuration service API name.",
    });
    expect(
      validateLogsFilters(
        { ...emptyLogsFilters(), administrator: "😀".repeat(500) },
        bounds,
      ).errors,
    ).toEqual({});
    expect(
      validateLogsFilters(
        {
          ...emptyLogsFilters(),
          from: "2026-03-07T12:00:00",
          to: "2026-03-07T12:00:00",
        },
        bounds,
      ).errors.to,
    ).toBe("From time must be before Before time.");
  });
});
describe("Logs list and selection identities", () => {
  it("does not list before retention succeeds and prevents duplicate actions", async () => {
    const { client, controller } = setup();
    const pending = deferred<{ duration_days: number }>();
    client.retention.mockReturnValue(pending.promise);
    const first = controller.refresh();
    await controller.refresh();
    expect(client.retention).toHaveBeenCalledTimes(1);
    expect(client.requestLogsPage).not.toHaveBeenCalled();
    pending.resolve({ duration_days: 1 });
    await first;
    expect(client.requestLogsPage).toHaveBeenCalledTimes(1);
  });
  it("retention failure and Retry reread retention", async () => {
    const { client, controller } = setup();
    client.retention.mockRejectedValueOnce(Error("offline"));
    await controller.refresh();
    expect(controller.getSnapshot().phase).toBe("error");
    expect(client.requestLogsPage).not.toHaveBeenCalled();
    await controller.refresh("retry");
    expect(client.retention).toHaveBeenCalledTimes(2);
    expect(controller.getSnapshot().phase).toBe("ready");
  });
  it("invalid Apply preserves detail and cursor; changed retention refresh closes detail", async () => {
    const { client, controller } = setup();
    await controller.refresh();
    await controller.inspect("new");
    const walk = controller.getSnapshot().walk;
    controller.changeFilter("from", "bad");
    await controller.apply();
    expect(controller.getSnapshot().walk).toBe(walk);
    expect(controller.getSnapshot().detail?.id).toBe("new");
    expect(client.requestLogsPage).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().focus?.target).toBe("logs-filter-from");
    await controller.refresh();
    expect(controller.getSnapshot().walk).toBe(walk);
    expect(controller.getSnapshot().detail).toBeNull();
  });
  it("applying and clearing keeps captured bounds and omits cursor", async () => {
    const { client, controller } = setup();
    await controller.refresh();
    controller.changeFilter("administrator", "alice");
    await controller.apply();
    await controller.apply(true);
    expect(client.retention).toHaveBeenCalledTimes(1);
    expect(
      client.requestLogsPage.mock.calls.map((args) => args.slice(0, 3)),
    ).toEqual(Array(3).fill([bounds.from, bounds.to, undefined]));
    expect(controller.getSnapshot().filters).toEqual(emptyLogsFilters());
  });
  it("failed more keeps safe rows and retries the same query and cursor", async () => {
    const { client, controller } = setup();
    await controller.refresh();
    client.requestLogsPage.mockRejectedValueOnce(Error("offline"));
    await controller.loadMore();
    expect(controller.getSnapshot().more).toBe("error");
    expect(controller.getSnapshot().walk?.rows.map((x) => x.id)).toEqual([
      "new",
    ]);
    client.requestLogsPage.mockResolvedValueOnce(page(["old"]));
    await controller.loadMore();
    expect(client.requestLogsPage.mock.calls[1]).toEqual(
      client.requestLogsPage.mock.calls[2],
    );
    expect(controller.getSnapshot().walk?.rows.map((x) => x.id)).toEqual([
      "new",
      "old",
    ]);
    expect(controller.getSnapshot().focus?.target).toBe("logs-ready");
  });
  it.each(["", "x".repeat(501), "older", null, undefined])(
    "stops unsafe continuation %s after keeping unique safe records",
    async (cursor) => {
      const { client, controller } = setup();
      await controller.refresh();
      client.requestLogsPage.mockResolvedValueOnce({
        items: [{ ...row("new"), logical_call_id: "replacement" }, row("old")],
        page: { has_more: true, next_cursor: cursor },
      } as Page<RequestLogSummary>);
      await controller.loadMore();
      const state = controller.getSnapshot();
      expect(state.walk?.rows.map((x) => x.id)).toEqual(["new", "old"]);
      expect(state.walk?.rows[0]?.logical_call_id).toBe("call-new");
      expect(state.walk?.stopped).toBe(true);
      expect(state.focus?.target).toBe("logs-refresh");
    },
  );
  it("ends on false has_more and stops a no-progress true cursor", async () => {
    const { client, controller } = setup();
    await controller.refresh();
    client.requestLogsPage.mockResolvedValueOnce({
      items: [row("old")],
      page: { has_more: false, next_cursor: "ignored" },
    });
    await controller.loadMore();
    expect(controller.getSnapshot().walk?.cursor).toBeNull();
    await controller.refresh();
    client.requestLogsPage.mockResolvedValueOnce(page(["new"], "different"));
    await controller.loadMore();
    expect(controller.getSnapshot().walk?.stopped).toBe(true);
  });
  it("new sequences reject old list and detail success and error without changing focus", async () => {
    const { client, controller } = setup();
    await controller.refresh();
    const more = deferred<Page<RequestLogSummary>>();
    const detail = deferred<Awaited<ReturnType<typeof client.requestLog>>>();
    client.requestLogsPage.mockReturnValueOnce(more.promise);
    client.requestLog.mockReturnValueOnce(detail.promise);
    const a = controller.loadMore();
    const b = controller.inspect("new");
    controller.changeFilter("from", "bad");
    await controller.apply();
    const state = controller.getSnapshot();
    more.resolve(page(["old"]));
    detail.reject(Error("expired"));
    await Promise.all([a, b]);
    expect(controller.getSnapshot()).toBe(state);
    expect(state.detail?.phase).toBe("error");
    expect(state.walk?.cursor).toBe("older");
  });
  it.each([true, false])(
    "current more keeps detail when detailFirst=%s",
    async (detailFirst) => {
      const { client, controller } = setup();
      await controller.refresh();
      const more = deferred<Page<RequestLogSummary>>();
      const detail = deferred<Awaited<ReturnType<typeof client.requestLog>>>();
      client.requestLogsPage.mockReturnValueOnce(more.promise);
      client.requestLog.mockReturnValueOnce(detail.promise);
      const a = controller.loadMore();
      const b = controller.inspect("new");
      const finish = () => {
        detail.resolve({
          summary: row("new"),
          request_json: "retained",
          attempts: [],
        });
      };
      if (detailFirst) {
        finish();
        await flush();
        more.resolve(page(["old"]));
      } else {
        more.resolve(page(["old"]));
        await flush();
        finish();
      }
      await Promise.all([a, b]);
      expect(controller.getSnapshot().detail?.record?.request_json).toBe(
        "retained",
      );
      expect(controller.getSnapshot().walk?.rows).toHaveLength(2);
    },
  );
  it("close and replacement selections invalidate pending detail", async () => {
    const { client, controller } = setup();
    await controller.refresh();
    const pending = deferred<Awaited<ReturnType<typeof client.requestLog>>>();
    client.requestLog.mockReturnValueOnce(pending.promise);
    const a = controller.inspect("new");
    await controller.inspect("other");
    pending.resolve({ summary: row("new"), request_json: "old", attempts: [] });
    await a;
    expect(controller.getSnapshot().detail?.id).toBe("other");
    controller.closeDetail();
    expect(controller.getSnapshot().detail).toBeNull();
  });
});

describe("Logs retained media lifetime", () => {
  it("deduplicates pending media, rejects old media after selection, and revokes retained URLs", async () => {
    const createUrl = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:owned");
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    try {
      const { client, controller } = setup();
      await controller.refresh();
      await controller.inspect("new");
      const pending = deferred<Blob>();
      client.requestLogMedia.mockReturnValueOnce(pending.promise);
      const first = controller.prepareMedia("image");
      await controller.prepareMedia("image");
      expect(client.requestLogMedia).toHaveBeenCalledTimes(1);
      await controller.inspect("other");
      pending.resolve(new Blob(["obsolete"]));
      await first;
      expect(createUrl).not.toHaveBeenCalled();
      expect(controller.getSnapshot().detail?.id).toBe("other");
      await controller.prepareMedia("image");
      expect(createUrl).toHaveBeenCalledTimes(1);
      expect(controller.getSnapshot().detail?.media?.url).toBe("blob:owned");
      await controller.refresh();
      expect(revoke).toHaveBeenCalledWith("blob:owned");
      expect(controller.getSnapshot().detail).toBeNull();
    } finally {
      createUrl.mockRestore();
      revoke.mockRestore();
    }
  });
  it("media loss keeps the same selected record and a usable list", async () => {
    const { client, controller } = setup();
    await controller.refresh();
    await controller.inspect("new");
    client.requestLogMedia.mockRejectedValueOnce(Error("expired"));
    await controller.prepareMedia("image");
    expect(controller.getSnapshot().detail).toMatchObject({
      id: "new",
      phase: "error",
      media: null,
      mediaPending: false,
    });
    expect(controller.getSnapshot().walk?.rows).toHaveLength(1);
  });
  it("a repeated ready row action focuses its heading and missing return control uses Refresh", async () => {
    const { controller } = setup();
    await controller.refresh();
    const trigger = {
      isConnected: false,
      focus: vi.fn(),
    } as unknown as HTMLElement;
    await controller.inspect("new", trigger);
    const first = controller.getSnapshot().focus?.serial;
    await controller.inspect("new", trigger);
    expect(controller.getSnapshot().focus?.target).toBe("logs-detail-heading");
    expect(controller.getSnapshot().focus?.serial).not.toBe(first);
    controller.closeDetail();
    expect(controller.getSnapshot().focus?.target).toBe("logs-refresh");
  });
});
