import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  ContractValidationError,
  contractSchemas,
  validateContract,
  type ContractSchemaName,
} from "../src/index.js";

const repositoryRoot = fileURLToPath(new URL("../../..", import.meta.url));
const fixtures = {
  ContractManifest: "contract-manifest.json",
  ServiceToken: "service-token.json",
  Workspace: "workspace.json",
  Attachment: "attachment.json",
  ModelRequest: "model-request.json",
  EffectiveConfiguration: "effective-configuration.json",
  AdministratorGrant: "administration-grant.json",
  Health: "health.json",
  BusinessToolCall: "business-tool-call.json",
} as const satisfies Partial<Record<ContractSchemaName, string>>;

function loadFixture(filename: string): Record<string, unknown> {
  return JSON.parse(
    readFileSync(`${repositoryRoot}/docs/api/fixtures/${filename}`, "utf8"),
  ) as Record<string, unknown>;
}

describe("generated contract models", () => {
  it("contains every accepted component schema", () => {
    expect(Object.keys(contractSchemas)).toHaveLength(120);
  });
  for (const [schemaName, filename] of Object.entries(fixtures)) {
    it(`round trips the valid ${schemaName} fixture`, () => {
      const fixture = loadFixture(filename);
      const validated = validateContract(
        schemaName as ContractSchemaName,
        fixture,
      );
      expect(JSON.parse(JSON.stringify(validated))).toEqual(fixture);
    });

    it(`rejects an unknown ${schemaName} field`, () => {
      const fixture = loadFixture(filename);
      fixture.unknown_contract_field = true;
      expect(() =>
        validateContract(schemaName as ContractSchemaName, fixture),
      ).toThrow(ContractValidationError);
    });
  }

  it("enforces numeric, pattern, and format limits", () => {
    const request = loadFixture("model-request.json");
    const limits = request.limits as Record<string, unknown>;
    limits.attempt_timeout_ms = 120_001;
    expect(() => validateContract("ModelRequest", request)).toThrow("maximum");

    const attachment = loadFixture("attachment.json");
    attachment.sha256 = "short";
    expect(() => validateContract("Attachment", attachment)).toThrow("pattern");

    const workspace = loadFixture("workspace.json");
    workspace.operation_id = "";
    expect(() => validateContract("Workspace", workspace)).toThrow("minLength");

    const toolCall = loadFixture("business-tool-call.json");
    toolCall.deadline = "not-a-time";
    expect(() => validateContract("BusinessToolCall", toolCall)).toThrow(
      "format",
    );
  });

  it("enforces composition, unique items, and nested closed objects", () => {
    const request = loadFixture("model-request.json");
    request.tool_allow_list = ["same", "same"];
    expect(() => validateContract("ModelRequest", request)).toThrow(
      "duplicate items",
    );

    const nestedRequest = loadFixture("model-request.json");
    const message = (nestedRequest.messages as Record<string, unknown>[])[0];
    if (message === undefined) {
      throw new Error("The fixture must contain a message.");
    }
    message.content = { unsupported: true };
    expect(() => validateContract("ModelRequest", nestedRequest)).toThrow();
  });

  it("rejects missing required fields, enum values, and UUIDv7 values", () => {
    const workspace = loadFixture("workspace.json");
    delete workspace.workspace_id;
    expect(() => validateContract("Workspace", workspace)).toThrow("required");

    const changedWorkspace = loadFixture("workspace.json");
    changedWorkspace.state = "unknown";
    expect(() => validateContract("Workspace", changedWorkspace)).toThrow(
      "enum",
    );

    expect(() =>
      validateContract("UuidV7", "0198a5b0-1234-6abc-8def-0123456789ab"),
    ).toThrow("pattern");
  });
});
