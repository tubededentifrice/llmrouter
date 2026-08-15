import { describe, expect, it } from "vitest";
import { App } from "../src/App.js";

describe("App", () => {
  it("defines the administration prototype", () => {
    expect(App).toBeTypeOf("function");
  });
});
