/// <reference types="node" />

import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  AdministrationDashboard,
  AdministrationStateView,
  App,
  LocalAdministrationGateView,
  LocalAdministratorActivation,
  StaleRevisionBanner,
  StateMessage,
  type AppProps,
} from "../src/App.js";
import { scheduleFrameStart } from "../src/embedProtocol.js";
import {
  configurationRevisionForScope,
  type AdministrationClient,
  type AdministrationSnapshot,
  type ScopeSelection,
} from "../src/api.js";

const globalScope: ScopeSelection = {
  mode: "global",
  serviceId: "0198a080-0000-7000-8000-000000000001",
  workspaceId: "0198a080-0000-7000-8000-000000000002",
};
const serviceLevelScope: ScopeSelection = {
  ...globalScope,
  workspaceId: "",
};
const styles = readFileSync(
  new URL("../src/styles.css", import.meta.url),
  "utf8",
);

const snapshot: AdministrationSnapshot = {
  state: {
    kind: "workspace",
    service_id: globalScope.serviceId,
    workspace_id: globalScope.workspaceId,
    display_name: "Test workspace",
    state: "active",
    revision: "0198a080-0000-7000-8000-000000000003",
  },
  credentials: [
    {
      credential_id: "credential-1",
      owner_scope: globalScope.serviceId,
      provider_catalog_id: "openai_compatible.v1",
      state: "active",
      revision: "credential-revision-1",
      created_at: "2026-08-20T00:00:00Z",
      fingerprint: "safe-fingerprint",
    },
  ],
  providers: [
    {
      provider_instance_id: "provider-1",
      owner_scope: globalScope.serviceId,
      source_layer: "service",
      provider_catalog_id: "openai_compatible.v1",
      display_name: "OpenRouter",
      endpoint: "https://openrouter.ai/api/v1",
      credential_id: "credential-1",
      state: "active",
      active_revision: "provider-revision-1",
      inherited: false,
      settings: {
        schema_name: "adapter.openai_compatible.settings",
        major_version: 1,
        document: {
          profile: "openrouter",
          supported_operations: ["chat.complete", "chat.stream"],
        },
      },
    },
  ],
  routes: [
    {
      provider_model_route_id: "route-1",
      owner_scope: globalScope.serviceId,
      source_layer: "service",
      provider_instance_id: "provider-1",
      canonical_model_id: "deepseek-v4-flash",
      wire_model: "deepseek/deepseek-v4-flash",
      capabilities: ["chat.complete", "chat.stream"],
      settings: {
        schema_name: "adapter.openai_compatible.route",
        major_version: 1,
        document: {},
      },
      price_authority: {
        mode: "manual",
        source_name: null,
        lookup_identifier: null,
      },
      prices: [],
      synchronization_schedule: "0 0 * * 0",
      stale_after_seconds: 1209600,
      state: "active",
      active_revision: "route-revision-1",
      inherited: false,
    },
  ],
  assignments: [
    {
      name: "general",
      owner_scope: globalScope.serviceId,
      source_layer: "workspace",
      state: "active",
      inherited: false,
      active_revision: "assignment-revision-1",
      candidates: [
        { provider_model_route_id: "route-1", attempt_timeout_ms: 30000 },
      ],
      required_capabilities: ["chat.complete", "chat.stream"],
    },
  ],
  requests: [
    {
      request_id: "request-1",
      workspace_id: globalScope.workspaceId,
      assignment: "general",
      state: "succeeded",
      state_revision: 4,
    },
  ],
  accounting: {
    from: "2026-08-13T00:00:00Z",
    to: "2026-08-20T00:00:00Z",
    currency: "USD",
    logical_requests: 1,
    attempts: 1,
    usage: [{ unit: "input_token", quantity: "4" }],
    cost: "0.0001",
    corrections: "0",
  },
};

const client: AdministrationClient = {
  load: vi.fn(),
  createCredential: vi.fn(),
  putProvider: vi.fn(),
  putRoute: vi.fn(),
  putAssignment: vi.fn(),
};

function dashboard(
  section: "configuration" | "assignments" | "requests" | "accounting",
  scope = serviceLevelScope,
): string {
  return renderToStaticMarkup(
    <AdministrationDashboard
      client={client}
      initialSection={section}
      notice={null}
      onNotice={vi.fn()}
      onReload={vi.fn()}
      scope={scope}
      snapshot={snapshot}
    />,
  );
}

describe("administration app states", () => {
  it("does not consume an embed token from a temporary Strict Mode effect", () => {
    const callbacks: (() => void)[] = [];
    const start = vi.fn();
    const cancel = scheduleFrameStart(start, (callback) => {
      callbacks.push(callback);
    });
    cancel();
    callbacks[0]?.();
    expect(start).not.toHaveBeenCalled();

    scheduleFrameStart(start, (callback) => {
      callbacks.push(callback);
    });
    callbacks[1]?.();
    expect(start).toHaveBeenCalledOnce();
  });

  it("uses one write-only local administrator control", () => {
    const html = renderToStaticMarkup(
      <LocalAdministratorActivation onActivate={vi.fn()} />,
    );
    expect(html).toContain("Activate administrator session");
    expect(html).toContain('type="password"');
    expect(html).toContain('autoComplete="off"');
    expect(html).toContain('value=""');
    expect(html).not.toContain("localStorage");
  });

  it("keeps the normal app when local activation capability is absent", () => {
    const unavailable = renderToStaticMarkup(
      <LocalAdministrationGateView
        session={{ state: "unavailable" }}
        onActivate={vi.fn()}
      />,
    );
    const required = renderToStaticMarkup(
      <LocalAdministrationGateView
        session={{ state: "required" }}
        onActivate={vi.fn()}
      />,
    );
    expect(unavailable).toContain("Select an exact scope");
    expect(unavailable).not.toContain("Activate administrator session");
    expect(required).toContain("Activate administrator session");
  });

  it("shows bounded Pocket ID action progress and retry errors", () => {
    const pending = renderToStaticMarkup(
      <LocalAdministrationGateView
        session={{ state: "oidc_required" }}
        sessionAction="sign_in_pending"
        onActivate={vi.fn()}
      />,
    );
    const failed = renderToStaticMarkup(
      <LocalAdministrationGateView
        session={{ state: "oidc_required" }}
        sessionAction="error"
        onActivate={vi.fn()}
      />,
    );
    expect(pending).toContain("Opening Pocket ID…");
    expect(pending).toContain("disabled");
    expect(failed).toContain("Pocket ID sign-in did not start. Try again.");
  });

  it("shows an empty scope state before it makes a request", () => {
    const html = renderToStaticMarkup(<App client={client} />);
    expect(html).toContain("Select an exact scope");
    expect(html).toContain("Service ID");
  });

  it("shows a loading state for a supplied exact scope", () => {
    const props: AppProps = { client, startingScope: globalScope };
    const html = renderToStaticMarkup(<App {...props} />);
    expect(html).toContain("Loading protected state");
    expect(html).toContain("Protected service and workspace state is loading");
  });

  it("shows safe error and stale revision recovery states", () => {
    const errorHtml = renderToStaticMarkup(
      <StateMessage kind="error">
        The administration service is offline. No change was sent.
      </StateMessage>,
    );
    const staleHtml = renderToStaticMarkup(<StaleRevisionBanner />);
    expect(errorHtml).toContain('role="alert"');
    expect(errorHtml).toContain("No change was sent");
    expect(staleHtml).toContain("Stale configuration revision");
    expect(staleHtml).toContain("review the active revision");
  });

  it("keeps focus and phone table behavior in the app stylesheet", () => {
    expect(styles).toContain(":focus-visible");
    expect(styles).toContain("clip-path: inset(50%)");
    expect(styles).toContain("overflow-x: auto");
    expect(styles).toContain("@media (max-width: 600px)");
    expect(styles).toContain("grid-template-columns: 1fr");
  });

  it("keeps a credential success notice through reload and remounts an empty secret", () => {
    const success = {
      tone: "success" as const,
      message: "The write-only OpenRouter credential was stored.",
    };
    const loadingHtml = renderToStaticMarkup(
      <AdministrationStateView
        client={client}
        failure={null}
        loading
        notice={success}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={serviceLevelScope}
        snapshot={snapshot}
      />,
    );
    const reloadedHtml = renderToStaticMarkup(
      <AdministrationStateView
        client={client}
        failure={null}
        loading={false}
        notice={success}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={serviceLevelScope}
        snapshot={snapshot}
      />,
    );
    expect(loadingHtml).toContain("Loading protected state");
    expect(loadingHtml).toContain(success.message);
    expect(reloadedHtml).toContain(success.message);
    expect(reloadedHtml).toContain(
      'type="password" autoComplete="new-password" spellCheck="false" value=""',
    );
  });

  it("selects the active revision from the exact configuration layer", () => {
    expect(configurationRevisionForScope(snapshot, serviceLevelScope)).toBe(
      "provider-revision-1",
    );
    expect(configurationRevisionForScope(snapshot, globalScope)).toBe(
      "assignment-revision-1",
    );
  });
});

describe("protected administration dashboard", () => {
  it("shows exact scope, write-only secret controls, and effective configuration", () => {
    const html = dashboard("configuration");
    expect(html).toContain(globalScope.serviceId);
    expect(html).toContain("Service level");
    expect(html).toContain('type="password"');
    expect(html).toContain('autoComplete="new-password"');
    expect(html).toContain("safe-fingerprint");
    expect(html).toContain("deepseek/deepseek-v4-flash");
    expect(html).not.toContain("Provider secret value");
  });

  it("requires a canonical UUID and explicit prices before route publication", () => {
    const html = dashboard("configuration");
    expect(html).toContain("Canonical model UUID");
    expect(html).toContain(
      'pattern="[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"',
    );
    expect(html).toContain('value="deepseek/deepseek-v4-flash"');
    expect(html).not.toContain('value="deepseek-v4-flash"');
    expect(
      html.match(/placeholder="Explicit USD price" value=""/g),
    ).toHaveLength(2);
    expect(html).toContain(
      'disabled="" class="od-button od-button-primary" type="submit">Publish route',
    );
  });

  it("uses semantic keyboard controls and a table alternative", () => {
    const html = dashboard("assignments");
    expect(html).toContain('aria-label="Administration areas"');
    expect(html).toContain('aria-current="page"');
    expect(html).toContain("<button");
    expect(html).toContain("<table>");
    expect(html).toContain("Complete ordered chain");
    expect(html).toContain("Primary");
  });

  it("shows content-free request status and bounded accounting", () => {
    const requestHtml = dashboard("requests");
    const accountingHtml = dashboard("accounting");
    expect(requestHtml).toContain("Content-free status");
    expect(requestHtml).toContain("request-1");
    expect(requestHtml).not.toContain("model output value");
    expect(accountingHtml).toContain("Bounded accounting");
    expect(accountingHtml).toContain("0.0001 USD");
    expect(accountingHtml).toContain("input_token");
  });

  it("keeps provider secret custody out of the service view", () => {
    const serviceScope: ScopeSelection = {
      ...serviceLevelScope,
      mode: "service",
    };
    const html = dashboard("configuration", serviceScope);
    expect(html).toContain("Secret custody stays global");
    expect(html).not.toContain("Store OpenRouter credential");
    expect(html).toContain("Eligible credential reference ID");
    expect(html).toContain("Exact administration scope");
  });

  it("keeps service-owned provider changes out of a workspace view", () => {
    const html = dashboard("configuration", globalScope);
    expect(html).toContain("Provider configuration stays at service level");
    expect(html).not.toContain("Add OpenRouter instance");
    expect(html).not.toContain("Add provider-model route");
    expect(html).toContain("Read only");
  });

  it("shows clear empty table states", () => {
    const empty = { ...snapshot, assignments: [], requests: [] };
    const assignmentHtml = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        initialSection="assignments"
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={globalScope}
        snapshot={empty}
      />,
    );
    const requestHtml = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        initialSection="requests"
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={globalScope}
        snapshot={empty}
      />,
    );
    expect(assignmentHtml).toContain(
      "No assignment is effective in this scope",
    );
    expect(requestHtml).toContain("No request status is in this bounded scope");
  });
});
