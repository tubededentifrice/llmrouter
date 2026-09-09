import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "../../src/App.tsx";
import {
  AdministrationApiError,
  type AdministrationClient,
  type RequestLog,
  type RequestLogSummary,
} from "../../src/api.ts";

interface LogsFixture {
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
  finish: (
    name: string,
    value?: unknown,
    fail?: boolean,
    index?: number,
  ) => void;
  summary: (id: string) => RequestLogSummary;
  detail: (id: string) => RequestLog;
}

declare global {
  interface Window {
    logsFixture: LogsFixture;
    logsBoot: { hold: string[]; fail: string[]; strict?: boolean };
  }
}

const date = "2026-09-09T00:00:00Z";
const summary = (id: string): RequestLogSummary => ({
  id,
  logical_call_id: `call-${id}`,
  call_actor: "administrator",
  administrator_subject: "fixture-administrator",
  assignment_api_name: "fixture-assignment",
  configuration_service_api_name: "fixture-service",
  provider_model_api_name: "fixture-route",
  kind: "model",
  outcome: "succeeded",
  started_at: date,
  tags: ["synthetic-browser-fixture"],
});
const detail = (id: string): RequestLog => ({
  summary: summary(id),
  request_json: JSON.stringify({
    prompt:
      '<script>window.logMarkupExecuted=true</script><a href="https://example.invalid">Untrusted link</a>',
    long_text: "Synthetic retained content ".repeat(500),
    unbroken: "W".repeat(1500),
  }),
  response_json: JSON.stringify({ content: `Complete detail ${id}` }),
  attempts: [
    {
      provider_model_api_name: "fixture-route",
      outcome: "failed",
      started_at: date,
      completed_at: date,
      applied_prices: {
        currency: "USD",
        unit_prices: [{ unit: "input_token", amount: "0.001" }],
      },
      error: { code: "provider_error", message: "Synthetic error ".repeat(30) },
      response_json: JSON.stringify({
        tool_result: "Synthetic tool data ".repeat(100),
      }),
    },
  ],
  media: [
    {
      id: "media-fixture",
      role: "input",
      media_type: "image/png",
      size_bytes: 4,
    },
  ],
});

const fixture: LogsFixture = {
  calls: [],
  hold: [...window.logsBoot.hold],
  fail: [...window.logsBoot.fail],
  pending: [],
  summary,
  detail,
  values: {
    session: {
      subject: "fixture-administrator",
      display_name: "Fixture administrator",
      expires_at: "2099-01-01T00:00:00Z",
      csrf_token: "synthetic-csrf",
    },
    services: { items: [], page: { has_more: false } },
    retention: { duration_days: 30 },
    requestLogsPage: {
      items: [summary("log-c"), summary("log-b")],
      page: { has_more: true, next_cursor: "cursor-1" },
    },
    requestLogMedia: new Blob(["test"], { type: "image/png" }),
  },
  finish(name, value, fail = false, index = 0) {
    const matches = fixture.pending.filter(
      (operation) => operation.name === name,
    );
    const operation = matches[index];
    if (operation === undefined) throw Error(`No pending request: ${name}`);
    fixture.pending.splice(fixture.pending.indexOf(operation), 1);
    if (fail)
      operation.reject(
        new AdministrationApiError(
          404,
          "not_found",
          "Synthetic record is unavailable.",
        ),
      );
    else
      operation.resolve(
        value ??
          (name === "requestLog"
            ? detail(String(operation.args[0]))
            : fixture.values[name]),
      );
  },
};
window.logsFixture = fixture;
const client = new Proxy({} as AdministrationClient, {
  get(_target, property) {
    return (...args: unknown[]): Promise<unknown> => {
      const name = String(property);
      fixture.calls.push({ name, args });
      if (fixture.hold.includes(name))
        return new Promise((resolve, reject) =>
          fixture.pending.push({ name, args, resolve, reject }),
        );
      if (fixture.fail.includes(name))
        return Promise.reject(
          new AdministrationApiError(
            404,
            "not_found",
            "Synthetic record is unavailable.",
          ),
        );
      if (name === "requestLog")
        return Promise.resolve(detail(String(args[0])));
      if (!(name in fixture.values))
        return Promise.reject(Error(`Unexpected client operation: ${name}`));
      return Promise.resolve(fixture.values[name]);
    };
  },
});
const root = document.getElementById("root");
if (root === null) throw Error("Missing fixture root.");
createRoot(root).render(
  window.logsBoot.strict ? (
    <StrictMode>
      <App client={client} />
    </StrictMode>
  ) : (
    <App client={client} />
  ),
);
