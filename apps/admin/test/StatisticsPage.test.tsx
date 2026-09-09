import { StrictMode, act } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { create } from "react-test-renderer";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CompactCheckboxGroup,
  DataTable,
  type DataTableProps,
} from "@opendle/ui";
import { StatisticsPage } from "../src/StatisticsPage.tsx";
import {
  createAdministrationClient,
  type StatisticsBucket,
  type StatisticsFilters,
  type StatisticsResult,
} from "../src/api.ts";
// eslint-disable-next-line @typescript-eslint/no-deprecated -- Use the repository renderer to check effect lifetime.
let renderer: ReturnType<typeof create> | undefined;
async function flush(action: () => void | Promise<void>): Promise<void> {
  await act(async () => {
    await action();
  });
}
afterEach(async () => {
  await flush(() => {
    renderer?.unmount();
  });
  vi.unstubAllGlobals();
});
describe("Statistics page rendering", () => {
  it("renders UTC date controls, no groups, and both closed disclosures on the server", () => {
    const markup = renderToStaticMarkup(
      <StatisticsPage
        client={createAdministrationClient(vi.fn())}
        services={[]}
      />,
    );
    expect(markup).toContain(
      "UTC dates. From and Through include the selected dates.",
    );
    expect(markup.match(/type="date"/g)).toHaveLength(2);
    expect(markup).not.toContain('type="datetime-local"');
    expect(markup).toContain("Group results (0 selected)");
    expect(markup).not.toMatch(/<details[^>]*\sopen/);
    expect(markup).toContain("All call actors");
    expect(markup).toContain("All services");
    expect(markup).toContain("All outcomes");
  });
  it("keeps result dimensions tied to submitted groups after draft edits and effect replay", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const client = createAdministrationClient(vi.fn());
    const statistics = vi
      .spyOn(client, "statistics")
      .mockImplementation((query: StatisticsFilters) =>
        Promise.resolve({
          from: query.from,
          to: query.to,
          group_by: query.group_by ?? [],
          buckets: [
            {
              dimensions: ["(exact)"],
              calls: 1,
              attempts: 1,
              units: [{ unit: "input_token", quantity: "2" }],
              cost: "0.12",
              currency: "USD",
            },
          ],
        } satisfies StatisticsResult),
      );
    await flush(() => {
      // eslint-disable-next-line @typescript-eslint/no-deprecated -- Use the existing renderer to check StrictMode without a DOM.
      renderer = create(
        <StrictMode>
          <StatisticsPage client={client} services={[]} />
        </StrictMode>,
      );
    });
    await flush(() => {
      (
        renderer?.root.findByType(CompactCheckboxGroup).props.onChange as (
          value: string[],
        ) => void
      )(["assignment"]);
    });
    await flush(async () => {
      (
        renderer?.root.findByType("form").props.onSubmit as (event: {
          preventDefault: () => void;
        }) => void
      )({ preventDefault: vi.fn() });
      await Promise.resolve();
    });
    expect(statistics).toHaveBeenCalledTimes(1);
    const before = renderToStaticMarkup(
      <DataTable
        {...(renderer?.root.findByType(DataTable)
          .props as DataTableProps<StatisticsBucket>)}
      />,
    );
    expect(before).toContain("Assignment: Exact provider route calls");
    await flush(() => {
      (
        renderer?.root.findByType(CompactCheckboxGroup).props.onChange as (
          value: string[],
        ) => void
      )(["date"]);
    });
    const after = renderToStaticMarkup(
      <DataTable
        {...(renderer?.root.findByType(DataTable)
          .props as DataTableProps<StatisticsBucket>)}
      />,
    );
    expect(after).toContain("Assignment: Exact provider route calls");
    expect(after).not.toContain("Date: (exact)");
  });
});
