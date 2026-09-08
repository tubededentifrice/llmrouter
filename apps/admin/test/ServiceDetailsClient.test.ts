import { expect, it, vi } from "vitest";
import { createAdministrationClient } from "../src/api.ts";

it("reads the exact encoded service without a service list request", async () => {
  const service = {
    api_name: "child",
    display_name: "Child",
    parent_service_api_name: "root",
    is_root: false,
    created_at: "2026-08-25T00:00:00Z",
  };
  const fetcher = vi.fn<typeof fetch>(() =>
    Promise.resolve(
      new Response(JSON.stringify(service), {
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        },
      }),
    ),
  );
  await expect(
    createAdministrationClient(fetcher).service("child/other"),
  ).resolves.toEqual(service);
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0]?.[0]).toBe("/v1/admin/services/child%2Fother");
});
