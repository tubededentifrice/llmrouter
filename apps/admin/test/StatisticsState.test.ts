import { describe, expect, it, vi } from "vitest";
import {
  StatisticsController,
  exactAssignmentLabel,
  initialStatisticsDraft,
  statisticsActiveCount,
  statisticsDimensionLabel,
  statisticsGroups,
  statisticsMessage,
  validateStatistics,
} from "../src/statisticsState.ts";
import type { StatisticsResult } from "../src/api.ts";
const initial = () =>
  initialStatisticsDraft(Date.parse("2026-03-08T00:30:00Z"));
function range(from: string, through: string) {
  return validateStatistics({ ...initial(), from, through });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const result = (calls = 0): StatisticsResult => ({
  from: "2026-02-07T00:00:00Z",
  to: "2026-03-09T00:00:00Z",
  group_by: [],
  buckets: calls
    ? [
        {
          dimensions: [],
          calls,
          attempts: 0,
          units: [],
          cost: "0",
          currency: null,
        },
      ]
    : [],
});
describe("Statistics UTC dates", () => {
  it("takes both default dates from one instant near midnight", () => {
    expect(initial()).toMatchObject({
      from: "2026-02-07",
      through: "2026-03-08",
      group_by: [],
    });
  });
  it.each([
    ["2026-08-29", "2026-08-29", "2026-08-30"],
    ["2026-04-01", "2026-04-30", "2026-05-01"],
    ["2028-02-29", "2028-02-29", "2028-03-01"],
    ["2026-12-31", "2026-12-31", "2027-01-01"],
    ["2028-01-01", "2028-12-31", "2029-01-01"],
    ["0001-01-01", "0001-01-01", "0001-01-02"],
    ["0099-12-31", "0099-12-31", "0100-01-01"],
    ["2000-02-29", "2000-02-29", "2000-03-01"],
    ["9999-12-30", "9999-12-30", "9999-12-31"],
  ])(
    "maps %s through %s to exact inclusive/exclusive UTC bounds",
    (from, through, to) => {
      expect(range(from, through)).toEqual({
        errors: {},
        query: { from: `${from}T00:00:00Z`, to: `${to}T00:00:00Z` },
      });
    },
  );
  it.each([
    "",
    "2026-2-01",
    "26-02-01",
    "2026-02-01T00:00",
    " 2026-02-01",
    "2026-02-29",
    "2026-04-31",
    "1900-02-29",
    "0000-01-01",
    "10000-01-01",
    "2026-00-01",
    "2026-13-01",
    "2026-01-00",
  ])("rejects syntax/calendar date %s on its own control", (value) => {
    expect(range(value, "2026-03-01")).toMatchObject({
      query: null,
      errors: { from: "Enter a valid From date." },
    });
    expect(range("2026-01-01", value)).toMatchObject({
      query: null,
      errors: { through: "Enter a valid Through date." },
    });
  });
  it.each([
    ["2026-03-02", "2026-03-01", "Through must be the same as or after From."],
    ["2028-01-01", "2029-01-01", "Select 366 dates or fewer."],
    [
      "9999-12-30",
      "9999-12-31",
      "Through is outside the supported date range.",
    ],
  ])("assigns range error to Through", (from, through, message) => {
    expect(range(from, through)).toEqual({
      query: null,
      errors: { through: message },
    });
  });
});
describe("Statistics filter query and presentation", () => {
  it("includes all active values in API form and displayed group order", () => {
    const draft = {
      ...initial(),
      service: "root",
      workspace: "desk",
      call_actor: "administrator",
      administrator: "subject / ?",
      configuration_service: "context",
      assignment: exactAssignmentLabel,
      provider_model: "route",
      outcome: "succeeded",
      tag: "two words",
      group_by: [
        "provider_model",
        "assignment",
        "date",
        "call_actor",
        "service",
        "workspace",
        "administrator",
        "configuration_service",
      ],
    };
    expect(statisticsActiveCount(draft)).toBe(15);
    expect(validateStatistics(draft)).toEqual({
      errors: {},
      query: {
        from: "2026-02-07T00:00:00Z",
        to: "2026-03-09T00:00:00Z",
        service: "root",
        workspace: "desk",
        call_actor: "administrator",
        administrator: "subject / ?",
        configuration_service: "context",
        assignment: "(exact)",
        provider_model: "route",
        outcome: "succeeded",
        tag: "two words",
        group_by: statisticsGroups.slice(0, 8).map((group) => group.value),
      },
    });
    expect(statisticsActiveCount(initial())).toBe(0);
    expect(
      validateStatistics({ ...initial(), assignment: "named.assignment" }).query
        ?.assignment,
    ).toBe("named.assignment");
  });
  it("deduplicates groups in displayed order and rejects 9 or unknown groups", () => {
    expect(
      validateStatistics({ ...initial(), group_by: ["tag", "date", "date"] })
        .query?.group_by,
    ).toEqual(["date", "tag"]);
    for (const group_by of [
      statisticsGroups.slice(0, 9).map((group) => group.value),
      ["unknown"],
    ])
      expect(validateStatistics({ ...initial(), group_by })).toMatchObject({
        query: null,
        errors: { group_by: "Select up to 8 groups." },
      });
  });
  it.each([
    ["call_actor", "other"],
    ["administrator", "x".repeat(501)],
    ["configuration_service", "Bad Name"],
    ["assignment", "Bad Name"],
    ["provider_model", "bad_route"],
    ["outcome", "unknown"],
    ["tag", "é".repeat(65)],
    ["workspace", "bad space"],
    ["service", "BAD"],
  ] as const)("validates %s before sending", (field, value) => {
    const checked = validateStatistics({ ...initial(), [field]: value });
    expect(checked.query).toBeNull();
    expect(checked.errors[field]).toBeTruthy();
  });
  it("uses human dimension values", () => {
    expect(statisticsDimensionLabel("assignment", "(exact)")).toBe(
      exactAssignmentLabel,
    );
    expect(statisticsDimensionLabel("call_actor", "administrator")).toBe(
      "Administrator playground calls",
    );
    expect(statisticsDimensionLabel("call_actor", "service")).toBe(
      "Service calls",
    );
    expect(statisticsDimensionLabel("outcome", "failed")).toBe("Failed");
    expect(statisticsDimensionLabel("outcome", "succeeded")).toBe("Succeeded");
    expect(statisticsDimensionLabel("workspace", null)).toBe("Not applicable");
  });
});
describe("Statistics current query identity", () => {
  it.each(["success", "error"])(
    "ignores old %s after a later result and preserves draft groups",
    async (outcome) => {
      const old = deferred<StatisticsResult>(),
        latest = deferred<StatisticsResult>();
      const statistics = vi
        .fn()
        .mockReturnValueOnce(old.promise)
        .mockReturnValueOnce(latest.promise);
      const controller = new StatisticsController({ statistics });
      controller.change("group_by", ["service"]);
      const first = controller.submit();
      await controller.submit();
      expect(statistics).toHaveBeenCalledTimes(1);
      controller.change("tag", "new");
      controller.change("group_by", ["assignment"]);
      const second = controller.submit();
      expect(statistics).toHaveBeenCalledTimes(2);
      expect(statisticsMessage(controller.getSnapshot())).toBe(
        "Loading usage and cost.",
      );
      latest.resolve(result(2));
      await second;
      controller.change("group_by", ["date"]);
      const snapshot = controller.getSnapshot();
      expect(snapshot.groups).toEqual(["assignment"]);
      expect(snapshot.focus).toBeNull();
      if (outcome === "success") old.resolve(result(8));
      else old.reject(Error("obsolete"));
      await first;
      expect(controller.getSnapshot()).toBe(snapshot);
      expect(statisticsMessage(snapshot)).toBe("1 accounting group loaded.");
    },
  );
  it.each(["success", "error"])(
    "keeps validation, errors, focus and messages after stale %s",
    async (outcome) => {
      const pending = deferred<StatisticsResult>();
      const statistics = vi.fn().mockReturnValue(pending.promise);
      const controller = new StatisticsController({ statistics });
      const load = controller.submit();
      controller.change("from", "");
      controller.change("administrator", "a".repeat(501));
      await controller.submit();
      const snapshot = controller.getSnapshot();
      expect(snapshot.focus?.key).toBe("from");
      expect(snapshot.errors).toHaveProperty("administrator");
      if (outcome === "success") pending.resolve(result(4));
      else pending.reject(Error("obsolete"));
      await load;
      expect(controller.getSnapshot()).toBe(snapshot);
      controller.change("from", initial().from);
      expect(controller.getSnapshot().errors.from).toBeUndefined();
      expect(controller.getSnapshot().errors.administrator).toBeTruthy();
    },
  );
  it("clears Through order error when From changes without creating new errors", async () => {
    const statistics = vi.fn();
    const controller = new StatisticsController({ statistics });
    controller.change("from", "2026-04-02");
    controller.change("through", "2026-04-01");
    await controller.submit();
    expect(controller.getSnapshot().focus?.key).toBe("through");
    controller.change("from", "2026-04-01");
    expect(controller.getSnapshot().errors).toEqual({});
    expect(statistics).not.toHaveBeenCalled();
  });
  it("has exact empty and error states, keeps filters, and permits retry", async () => {
    const statistics = vi
      .fn()
      .mockRejectedValueOnce(Error("private server error"))
      .mockResolvedValueOnce(result());
    const controller = new StatisticsController({ statistics });
    controller.change("tag", "keep");
    await controller.submit();
    expect(statisticsMessage(controller.getSnapshot())).toBe(
      "Unable to load usage and cost. Review the filters and try again.",
    );
    expect(controller.getSnapshot().draft.tag).toBe("keep");
    await controller.submit();
    expect(statisticsMessage(controller.getSnapshot())).toBe(
      "No usage or cost matches these filters.",
    );
  });
  it("reuses the same still-pending query after a different query", async () => {
    const a = deferred<StatisticsResult>(),
      b = deferred<StatisticsResult>();
    const statistics = vi
      .fn()
      .mockReturnValueOnce(a.promise)
      .mockReturnValueOnce(b.promise);
    const controller = new StatisticsController({ statistics });
    const first = controller.submit();
    controller.change("tag", "b");
    const second = controller.submit();
    controller.change("tag", "");
    const third = controller.submit();
    expect(statistics).toHaveBeenCalledTimes(2);
    a.resolve(result(1));
    await first;
    await third;
    const snapshot = controller.getSnapshot();
    b.reject(Error("obsolete"));
    await second;
    expect(controller.getSnapshot()).toBe(snapshot);
  });
  it("makes completion after disposal inert", async () => {
    const pending = deferred<StatisticsResult>();
    const controller = new StatisticsController({
      statistics: vi.fn().mockReturnValue(pending.promise),
    });
    const load = controller.submit();
    controller.dispose();
    const snapshot = controller.getSnapshot();
    pending.resolve(result(1));
    await load;
    expect(controller.getSnapshot()).toBe(snapshot);
  });
});
