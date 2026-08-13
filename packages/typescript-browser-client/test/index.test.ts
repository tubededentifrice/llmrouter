import { describe, expect, it } from "vitest";
import {
  BrowserClient,
  ContractValidationError,
  validateContract,
  type BrowserClientOptions,
} from "../src/index.js";

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

  it("validates public contract JSON without server authority", () => {
    const workspace = {
      workspace_id: "workspace-1",
      caller_reference: "caller-1",
      display_name: "Workspace",
      state: "active",
      state_revision: "revision-1",
      operation_id: "operation-1",
    };
    expect(validateContract("Workspace", workspace)).toBe(workspace);
    expect(() =>
      validateContract("Workspace", { ...workspace, unknown: true }),
    ).toThrow(ContractValidationError);
  });
});
