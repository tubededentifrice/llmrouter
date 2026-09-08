import { createRoot } from "react-dom/client";
import { App } from "../../src/App.tsx";
import {
  AdministrationApiError,
  type AdministrationClient,
} from "../../src/api.ts";

export interface ShellFixture {
  calls: { name: string; args: unknown[] }[];
  hold: string[];
  fail: string[];
  values: Record<string, unknown>;
  pending: {
    name: string;
    args: unknown[];
    resolve: (value: unknown) => void;
    reject: (error: Error) => void;
  }[];
  finish: (name: string, fail?: boolean, service?: string) => void;
}

declare global {
  interface Window {
    shellFixture: ShellFixture;
    shellBoot: { hold: string[]; fail: string[]; signedOut: boolean };
  }
}

const date = "2026-08-25T00:00:00Z";
const page = (items: unknown[]) => ({
  items,
  page: { has_more: false },
});
const services = [
  {
    api_name: "root",
    is_root: true,
    display_name: "Root service",
    parent_service_api_name: null,
    created_at: date,
  },
  {
    api_name: "child",
    is_root: false,
    display_name: "Child service",
    parent_service_api_name: "root",
    created_at: date,
  },
];
const provider = {
  api_name: "provider",
  display_name: "Fixture provider",
  adapter: "openrouter",
  credential_api_name: "credential",
  enabled: true,
  created_at: date,
};
const model = {
  api_name: "model",
  display_name: "Fixture model",
  input_modalities: ["text"],
  output_modalities: ["text"],
  capabilities: ["streaming"],
  constraints: {},
  price_source: "manual",
  created_at: date,
};
const mapping = {
  api_name: "route",
  provider_api_name: "provider",
  model_api_name: "model",
  provider_model_name: "fixture/model",
  enabled: true,
  input_modalities: ["text"],
  output_modalities: ["text"],
  capabilities: ["streaming"],
  reasoning_mappings: [],
  cooldown: { until: "2099-01-01T00:00:00Z", reason: "rate_limit" },
  created_at: date,
};
const fixture: ShellFixture = {
  calls: [],
  hold: [...window.shellBoot.hold],
  fail: [...window.shellBoot.fail],
  pending: [],
  values: {
    session: {
      subject: "fixture-administrator",
      display_name: "Fixture administrator",
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
      csrf_token: "synthetic-csrf",
    },
    services: page(services),
    providers: page([provider]),
    models: page([model]),
    providerModels: page([mapping]),
    credentials: page([
      {
        api_name: "credential",
        fingerprint: "sha256:fixture",
        created_at: date,
        updated_at: date,
      },
    ]),
    assignments: page([]),
    createKey: {
      key: {
        id: "fixture-key",
        name: "Fixture key",
        created_at: date,
        last_used_at: null,
      },
      secret: "synthetic-one-time-fixture-key",
    },
    putAssignment: {},
    health: {
      status: "healthy",
      checked_at: date,
      components: [{ name: "storage", status: "healthy" }],
    },
    retention: { duration_days: 7 },
    activity: page([]),
    activityPage: page([]),
    requestLogs: page([]),
    requestLogsPage: page([]),
    statistics: { from: date, to: date, group_by: [], buckets: [] },
    workspaces: page([]),
    keys: page([]),
    logout: undefined,
  },
  finish(name, fail = false, service) {
    const index = fixture.pending.findIndex(
      (operation) =>
        operation.name === name &&
        (service === undefined || operation.args[0] === service),
    );
    if (index < 0) throw Error(`No pending request: ${name}`);
    const operation = fixture.pending.splice(index, 1)[0];
    if (operation === undefined) throw Error(`No pending request: ${name}`);
    if (fail) operation.reject(Error(`Controlled ${name} failure.`));
    else
      operation.resolve(
        fixture.values[`${name}:${String(operation.args[0])}`] ??
          fixture.values[name],
      );
  },
};
window.shellFixture = fixture;
const client = new Proxy({} as AdministrationClient, {
  get(_target, property) {
    return (...args: unknown[]): Promise<unknown> => {
      const name = String(property);
      fixture.calls.push({ name, args });
      if (name === "startSession") {
        sessionStorage.setItem("shell-sign-in-target", String(args[0]));
        sessionStorage.setItem("shell-signed-in", "true");
        return Promise.resolve(args[0]);
      }
      if (
        name === "session" &&
        window.shellBoot.signedOut &&
        sessionStorage.getItem("shell-signed-in") !== "true"
      )
        return Promise.reject(
          new AdministrationApiError(401, "unauthorized", "Sign in."),
        );
      if (!(name in fixture.values))
        return Promise.reject(Error(`Unexpected client operation: ${name}`));
      if (fixture.hold.includes(name))
        return new Promise((resolve, reject) =>
          fixture.pending.push({ name, args, resolve, reject }),
        );
      if (fixture.fail.includes(name))
        return Promise.reject(Error(`Controlled ${name} failure.`));
      return Promise.resolve(
        fixture.values[`${name}:${String(args[0])}`] ?? fixture.values[name],
      );
    };
  },
});
const root = document.getElementById("root");
if (root === null) throw Error("Missing fixture root.");
createRoot(root).render(<App client={client} />);
