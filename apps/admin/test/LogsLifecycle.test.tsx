import { StrictMode, act } from "react";
import { create } from "react-test-renderer";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DataTable } from "@opendle/ui";
import { LogsPage } from "../src/LogsPage.tsx";
import { createAdministrationClient } from "../src/api.ts";
// eslint-disable-next-line @typescript-eslint/no-deprecated -- This test uses the existing repository renderer.
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
  vi.restoreAllMocks();
});
describe("Logs effect lifetime", () => {
  it("StrictMode setup-cleanup-setup finishes the current retention read", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const pending: ((value: { duration_days: number }) => void)[] = [];
    const client = createAdministrationClient(vi.fn());
    vi.spyOn(client, "retention").mockImplementation(
      () =>
        new Promise((resolve) => {
          pending.push(resolve);
        }),
    );
    const list = vi.spyOn(client, "requestLogsPage").mockResolvedValue({
      items: [],
      page: { has_more: false },
    });
    await flush(() => {
      // eslint-disable-next-line @typescript-eslint/no-deprecated -- Existing repository renderer verifies React effect replay without a DOM.
      renderer = create(
        <StrictMode>
          <LogsPage client={client} />
        </StrictMode>,
      );
    });
    expect(pending).toHaveLength(2);
    await flush(() => {
      pending[0]?.({ duration_days: 1 });
    });
    expect(list).not.toHaveBeenCalled();
    await flush(() => {
      pending[1]?.({ duration_days: 1 });
    });
    expect(list).toHaveBeenCalledTimes(1);
    expect(renderer?.root.findByType(DataTable).props.state).toMatchObject({
      kind: "empty",
    });
  });
});
