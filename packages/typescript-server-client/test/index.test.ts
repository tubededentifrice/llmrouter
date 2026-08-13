import { describe, expect, it } from "vitest";
import { ServerClient } from "../src/index.js";

describe("ServerClient", () => {
  it("keeps its endpoint", () => {
    const client = new ServerClient({
      endpoint: new URL("https://router.test"),
      serviceBootstrapSecret: "test-only",
    });
    expect(client.options.endpoint.hostname).toBe("router.test");
  });
});
