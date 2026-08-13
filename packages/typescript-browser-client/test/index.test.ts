import { describe, expect, it } from "vitest";
import { BrowserClient, type BrowserClientOptions } from "../src/index.js";

describe("BrowserClient", () => {
  it("keeps its eligible session", () => {
    const client = new BrowserClient({
      endpoint: new URL("https://router.test"),
      sessionToken: "test-only",
    });
    expect(client.options.sessionToken).toBe("test-only");
  });

  it("rejects a service bootstrap secret", () => {
    const unsafeOptions = {
      endpoint: new URL("https://router.test"),
      sessionToken: "test-only",
      // @ts-expect-error Browser code must not accept a service bootstrap secret.
      serviceBootstrapSecret: "test-only",
    } satisfies BrowserClientOptions;

    expect(unsafeOptions.serviceBootstrapSecret).toBe("test-only");
  });
});
