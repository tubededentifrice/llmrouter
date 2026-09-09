import { createRoot } from "react-dom/client";
import { App } from "../../src/App.tsx";
import {
  AdministrationApiError,
  type AdministrationClient,
  type Service,
} from "../../src/api.ts";

interface ControlledOperation {
  name: string;
  args: unknown[];
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}
interface CreationFixture {
  calls: { name: string; args: unknown[] }[];
  hold: string[];
  pending: ControlledOperation[];
  services: Service[];
  finish: (
    name: string,
    result?: unknown,
    error?: {
      status: number;
      code: string;
      message: string;
      details?: { field?: string; reason?: string };
    },
  ) => void;
}
declare global {
  interface Window {
    creationFixture: CreationFixture;
    creationBoot: { hold?: string[]; services?: Service[] };
  }
}
const date = "2026-08-25T00:00:00Z";
const page = (items: unknown[]) => ({ items, page: { has_more: false } });
const initialServices: Service[] = [
  {
    api_name: "root",
    display_name: "Root service",
    parent_service_api_name: null,
    created_at: date,
  },
  {
    api_name: "parent",
    display_name: "Parent service",
    parent_service_api_name: "root",
    created_at: date,
  },
  {
    api_name: "other",
    display_name: "Other service",
    parent_service_api_name: "root",
    created_at: date,
  },
];
const fixture: CreationFixture = {
  calls: [],
  hold: [...(window.creationBoot.hold ?? ["createService"])],
  pending: [],
  services: window.creationBoot.services ?? initialServices,
  finish(name, result, error) {
    const index = fixture.pending.findIndex(
      (operation) => operation.name === name,
    );
    if (index < 0) throw Error(`No pending request: ${name}`);
    const operation = fixture.pending.splice(index, 1)[0];
    if (operation === undefined) throw Error(`No pending request: ${name}`);
    if (error)
      operation.reject(
        new AdministrationApiError(
          error.status,
          error.code,
          error.message,
          error.details,
        ),
      );
    else operation.resolve(result ?? valueFor(name, operation.args));
  },
};
function valueFor(name: string, args: unknown[]): unknown {
  if (name === "session")
    return {
      subject: "fixture-administrator",
      display_name: "Fixture administrator",
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
      csrf_token: "synthetic-csrf",
    };
  if (name === "services") return page([...fixture.services]);
  if (name === "service") {
    const service = fixture.services.find((item) => item.api_name === args[0]);
    if (service === undefined)
      throw new AdministrationApiError(
        404,
        "not_found",
        "Service was not found.",
      );
    return service;
  }
  if (name === "createService")
    return {
      ...(args[0] as object),
      created_at: date,
    };
  if (
    [
      "providers",
      "models",
      "providerModels",
      "credentials",
      "assignments",
      "workspaces",
      "keys",
      "activity",
      "activityPage",
      "requestLogs",
      "requestLogsPage",
    ].includes(name)
  )
    return page([]);
  if (name === "health")
    return { status: "healthy", checked_at: date, components: [] };
  if (name === "retention") return { duration_days: 7 };
  if (name === "statistics")
    return { from: date, to: date, group_by: [], buckets: [] };
  throw Error(`Unexpected client operation: ${name}`);
}
window.creationFixture = fixture;
const client = new Proxy({} as AdministrationClient, {
  get(_target, property) {
    return (...args: unknown[]): Promise<unknown> => {
      const name = String(property);
      fixture.calls.push({ name, args });
      if (fixture.hold.includes(name))
        return new Promise((resolve, reject) =>
          fixture.pending.push({ name, args, resolve, reject }),
        );
      try {
        return Promise.resolve(valueFor(name, args));
      } catch (error) {
        return Promise.reject(
          error instanceof Error ? error : Error(String(error)),
        );
      }
    };
  },
});
const root = document.getElementById("root");
if (root === null) throw Error("Missing fixture root.");
createRoot(root).render(<App client={client} />);
