/// <reference types="node" />

import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
// React Test Renderer supplies a reducer interaction test without a browser DOM.
import {
  act,
  create,
  type ReactTestInstance,
  type ReactTestRenderer,
} from "react-test-renderer";
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
import { recoverAfterMutationFailure } from "../src/mutationRecovery.js";
import { ServiceManagement } from "../src/ServiceManagement.js";
import {
  AdministrationApiError,
  configurationRevisionForScope,
  scheduleAdministrationSessionInspection,
  type AdministrationClient,
  type AdministrationSnapshot,
  type ScopeSelection,
  type ServiceSummary,
} from "../src/api.js";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const globalScope: ScopeSelection = {
  mode: "global",
  serviceId: "0198a080-0000-7000-8000-000000000001",
  workspaceId: "0198a080-0000-7000-8000-000000000002",
};
const serviceLevelScope: ScopeSelection = {
  ...globalScope,
  workspaceId: "",
};
const registeredService: ServiceSummary = {
  service_id: serviceLevelScope.serviceId,
  display_name: "Test service",
  state: "active",
  revision: "service-revision-one",
  bootstrap_state: "ready",
  credential_generation: 1,
  bootstrap_scope: {
    audiences: ["data_plane"],
    operations: ["model.create"],
  },
};
const catalogModels = [
  {
    stable_id: "deepseek-v4-flash",
    kind: "model" as const,
    display_name: "DeepSeek V4 Flash",
    capabilities: ["chat.complete", "chat.stream"],
    state: "active" as const,
    settings: null,
    active_revision: "catalog-revision-1",
  },
];
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
      source_layer: serviceLevelScope.serviceId,
      provider_catalog_id: "openai_compatible.v1",
      display_name: "OpenRouter",
      endpoint: "https://openrouter.ai/api/v1",
      credential_id: "credential-1",
      eligible_service_ids: [],
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
      source_layer: serviceLevelScope.serviceId,
      provider_instance_id: "provider-1",
      canonical_model_id: "deepseek-v4-flash",
      wire_model: "deepseek/deepseek-v4-flash",
      capabilities: ["chat.complete", "chat.stream"],
      eligible_service_ids: [],
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
      source_layer: globalScope.workspaceId,
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
  budget: {
    scope: "workspace",
    limit: { amount: "25", currency: "USD" },
    warning_threshold: { amount: "20", currency: "USD" },
    reserved: { amount: "1", currency: "USD" },
    used: { amount: "4", currency: "USD" },
    corrected: { amount: "-0.5", currency: "USD" },
    remaining: { amount: "20.5", currency: "USD" },
    enforcement_state: "available",
    reset_period: "monthly",
    revision: "3",
  },
  configuration_revision: null,
  failures: {},
};

const client: AdministrationClient = {
  listServices: vi.fn(),
  listCredentials: vi.fn(),
  listCatalog: vi.fn(),
  createService: vi.fn(),
  putService: vi.fn(),
  changeService: vi.fn(),
  load: vi.fn(),
  createCredential: vi.fn(),
  changeCredential: vi.fn(),
  putProvider: vi.fn(),
  putRoute: vi.fn(),
  putAssignment: vi.fn(),
  putBudget: vi.fn(),
};

function dashboard(
  section:
    | "overview"
    | "services"
    | "credentials"
    | "setup"
    | "configuration"
    | "assignments"
    | "requests"
    | "budgets"
    | "accounting",
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
      services={[registeredService]}
      catalogModels={catalogModels}
      snapshot={snapshot}
    />,
  );
}

describe("administration app states", () => {
  it("does not rotate the session proof from a temporary Strict Mode effect", () => {
    const callbacks: (() => void)[] = [];
    const inspect = vi.fn();
    const cancel = scheduleAdministrationSessionInspection(
      inspect,
      (callback) => {
        callbacks.push(callback);
      },
    );
    cancel();
    callbacks[0]?.();
    expect(inspect).not.toHaveBeenCalled();

    scheduleAdministrationSessionInspection(inspect, (callback) => {
      callbacks.push(callback);
    });
    callbacks[1]?.();
    expect(inspect).toHaveBeenCalledOnce();
  });

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
    expect(unavailable).toContain("Run LLM Router");
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

  it("shows global tasks before a service is selected", () => {
    const html = renderToStaticMarkup(<App client={client} />);
    expect(html).toContain("Run LLM Router");
    expect(html).toContain("Create your first service");
    expect(html).toContain("Service to manage");
    expect(html).not.toContain("Exact administration scope");
    expect(html).toContain('aria-current="page"');
    expect(html).toContain("Global administrator tasks");
    expect(html).toContain("Overview");
    expect(html).toContain("mobile-service-selector");
  });

  it("shows registered service names in the scope control", () => {
    const html = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        onScopeChange={vi.fn()}
        scope={{ mode: "global", serviceId: "", workspaceId: "" }}
        services={[
          {
            service_id: "service-one",
            display_name: "Xbot",
            state: "active",
            revision: "service-revision-one",
            bootstrap_state: "ready",
            credential_generation: 1,
            bootstrap_scope: { audiences: [], operations: [] },
          },
        ]}
        snapshot={null}
      />,
    );
    expect(html).toContain("No service selected");
    expect(html).toContain("Xbot · active");
    expect(html).not.toContain('placeholder="Service UUID"');
  });

  it("keeps global navigation visible while a service loads", () => {
    const props: AppProps = { client, startingScope: globalScope };
    const html = renderToStaticMarkup(<App {...props} />);
    expect(html).toContain("Global administration");
    expect(html).toContain("Services &amp; inheritance");
    expect(html).toContain("Run LLM Router");
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
    expect(staleHtml).toContain("This configuration changed");
    expect(staleHtml).toContain("review the active revision");
  });

  it("refreshes current data after an uncertain or stale mutation failure", async () => {
    const onChanged = vi.fn(() => Promise.resolve());
    const onNotice = vi.fn();
    const uncertain = new AdministrationApiError(
      "The outcome is uncertain. Refresh current data.",
      {
        code: "offline",
        requestId: null,
        status: 0,
        outcomeUncertain: true,
      },
    );
    await recoverAfterMutationFailure(uncertain, onChanged, onNotice);
    expect(onChanged).toHaveBeenCalledOnce();
    expect(onNotice).toHaveBeenCalledWith({
      tone: "error",
      message: "The outcome is uncertain. Refresh current data.",
      staleRevision: false,
    });

    onChanged.mockClear();
    onNotice.mockClear();
    await recoverAfterMutationFailure(
      new AdministrationApiError("Read the current active revision.", {
        code: "configuration_revision_conflict",
        requestId: "safe-request-1",
        status: 409,
      }),
      onChanged,
      onNotice,
    );
    expect(onChanged).toHaveBeenCalledOnce();
    expect(onNotice).toHaveBeenCalledWith({
      tone: "error",
      message: "Read the current active revision. Request safe-request-1.",
      staleRevision: true,
    });
  });

  it("does not refresh after a certain mutation rejection", async () => {
    const onChanged = vi.fn(() => Promise.resolve());
    const onNotice = vi.fn();
    await recoverAfterMutationFailure(
      new AdministrationApiError("The request is invalid.", {
        code: "invalid_request",
        requestId: null,
        status: 422,
      }),
      onChanged,
      onNotice,
    );
    expect(onChanged).not.toHaveBeenCalled();
    expect(onNotice).toHaveBeenCalledOnce();
  });

  it("keeps both safe errors when mutation recovery does not refresh", async () => {
    const onNotice = vi.fn();
    await recoverAfterMutationFailure(
      new AdministrationApiError("The outcome is uncertain.", {
        code: "offline",
        requestId: null,
        status: 0,
        outcomeUncertain: true,
      }),
      vi.fn(() =>
        Promise.reject(
          new AdministrationApiError("The administration service is offline.", {
            code: "offline",
            requestId: null,
            status: 0,
          }),
        ),
      ),
      onNotice,
    );
    expect(onNotice).toHaveBeenLastCalledWith({
      tone: "error",
      message:
        "The outcome is uncertain. Current data did not refresh. The administration service is offline.",
      staleRevision: false,
    });
  });

  it("keeps focus and phone table behavior in the app stylesheet", () => {
    expect(styles).toContain(":focus-visible");
    expect(styles).toContain("clip-path: inset(50%)");
    expect(styles).toContain("overflow-x: auto");
    expect(styles).toContain("@media (max-width: 600px)");
    expect(styles).toContain("grid-template-columns: 1fr");
    expect(styles).toMatch(/\.content\s*{[^}]*width: 100%/s);
    expect(styles).not.toContain("width: min(1240px");
    expect(styles).toMatch(/\.service-graph-page\s*{[^}]*padding: 12px/s);
    expect(styles).not.toMatch(
      /\.service-management \.od-graph-node\s*{[^}]*display: none/s,
    );
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
    expect(loadingHtml).toContain("Run LLM Router");
    expect(loadingHtml).toContain(success.message);
    expect(reloadedHtml).toContain(success.message);
    expect(dashboard("credentials")).toContain(
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
  it("shows names first and the effective service configuration", () => {
    const html = dashboard("configuration");
    expect(html).toContain("What this service will use");
    expect(html).toContain("Provider keys are managed globally");
    expect(html).not.toContain(`>${globalScope.serviceId}<`);
    expect(html).not.toContain('type="password"');
    expect(html).toContain("safe-fingerprint");
    expect(html).toContain("deepseek/deepseek-v4-flash");
    expect(html).not.toContain("Provider secret value");
  });

  it("offers direct local overrides for inherited effective values", () => {
    const inheritedSnapshot: AdministrationSnapshot = {
      ...snapshot,
      providers: snapshot.providers.map((item) => ({
        ...item,
        inherited: true,
        owner_scope: "parent-service",
      })),
      routes: snapshot.routes.map((item) => ({
        ...item,
        inherited: true,
        owner_scope: "parent-service",
      })),
      assignments: snapshot.assignments.map((item) => ({
        ...item,
        inherited: true,
        owner_scope: "parent-service",
      })),
    };
    const configurationHtml = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        initialSection="configuration"
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={serviceLevelScope}
        services={[registeredService]}
        snapshot={inheritedSnapshot}
      />,
    );
    const assignmentHtml = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        initialSection="assignments"
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={serviceLevelScope}
        services={[registeredService]}
        snapshot={inheritedSnapshot}
      />,
    );
    expect(configurationHtml.match(/Override for this service/g)).toHaveLength(
      2,
    );
    expect(assignmentHtml).toContain("Override for this service");
  });

  it("uses a named supported model and explicit prices before route publication", () => {
    const html = dashboard("configuration");
    expect(html).toContain("Supported model");
    expect(html).toContain("DeepSeek V4 Flash");
    expect(html).not.toContain("Canonical model UUID");
    expect(html).toContain("Provider model name");
    expect(html).toContain("deepseek/deepseek-v4-flash");
    expect(
      html.match(/placeholder="Explicit USD price" value=""/g),
    ).toHaveLength(2);
    expect(html).toContain(
      'disabled="" class="od-button od-button-primary" type="submit">Publish route',
    );
  });

  it("blocks model-route writes when catalog discovery fails", () => {
    const html = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        initialSection="configuration"
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={serviceLevelScope}
        services={[registeredService]}
        catalogModels={catalogModels}
        globalFailures={{ catalog: "The catalog read failed." }}
        snapshot={snapshot}
      />,
    );
    expect(html).toContain("Supported models are not available");
    expect(html).toContain("The catalog read failed.");
    expect(html).not.toContain("Publish route");
  });

  it("distinguishes loading and empty supported-model catalogs", () => {
    const loadingHtml = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        initialSection="configuration"
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={serviceLevelScope}
        services={[registeredService]}
        catalogLoading
        snapshot={snapshot}
      />,
    );
    const emptyHtml = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        initialSection="configuration"
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={serviceLevelScope}
        services={[registeredService]}
        snapshot={snapshot}
      />,
    );
    expect(loadingHtml).toContain("Supported models are loading");
    expect(loadingHtml).not.toContain("Publish route");
    expect(emptyHtml).toContain("No active supported model is available");
    expect(emptyHtml).not.toContain("Publish route");
  });

  it("uses semantic keyboard controls and a table alternative", () => {
    const html = dashboard("assignments");
    expect(html).toContain('aria-label="Administrator tasks"');
    expect(html).toContain('aria-current="page"');
    expect(html).toContain("<button");
    expect(html).toContain("<table>");
    expect(html).toContain("Complete ordered chain");
    expect(html).toContain("Primary");
  });

  it("puts effective configuration before setup for a selected service", () => {
    const html = dashboard("configuration");
    expect(html.indexOf("Effective configuration")).toBeLessThan(
      html.indexOf(">Setup<"),
    );
    expect(html).toContain("Open global tasks");
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

  it("shows all exact budget totals and the current revision", () => {
    const html = dashboard("budgets");
    expect(html).toContain("Budget summary");
    expect(html).toContain("Hard limit");
    expect(html).toContain("25 USD");
    expect(html).toContain("Warning threshold");
    expect(html).toContain("Reserved");
    expect(html).toContain("Used");
    expect(html).toContain("Corrected");
    expect(html).toContain("-0.5 USD");
    expect(html).toContain("Remaining");
    expect(html).toContain("Enforcement");
    expect(html).toContain("Reset period");
    expect(html).toContain("monthly");
    expect(html).toContain("Revision");
  });

  /* eslint-disable @typescript-eslint/no-deprecated -- The Director requires this renderer for the reducer interaction regression. */
  it("resets budget form state when the exact scope changes at one revision", () => {
    const serviceSnapshot: AdministrationSnapshot = {
      ...snapshot,
      state: {
        kind: "service",
        service_id: serviceLevelScope.serviceId,
        display_name: "Test service",
        state: "active",
        revision: "service-state-revision",
      },
      budget: {
        scope: "service",
        limit: { amount: "100", currency: "USD" },
        warning_threshold: { amount: "80", currency: "USD" },
        reserved: { amount: "1", currency: "USD" },
        used: { amount: "4", currency: "USD" },
        corrected: { amount: "0", currency: "USD" },
        remaining: { amount: "95", currency: "USD" },
        enforcement_state: "available",
        reset_period: "monthly",
        revision: "7",
      },
    };
    const workspaceSnapshot: AdministrationSnapshot = {
      ...snapshot,
      budget: {
        scope: "workspace",
        limit: { amount: "20", currency: "EUR" },
        warning_threshold: { amount: "15", currency: "EUR" },
        reserved: { amount: "2", currency: "EUR" },
        used: { amount: "3", currency: "EUR" },
        corrected: { amount: "0", currency: "EUR" },
        remaining: { amount: "15", currency: "EUR" },
        enforcement_state: "available",
        reset_period: "daily",
        revision: "7",
      },
    };
    const dashboardProps = {
      client,
      initialSection: "budgets" as const,
      notice: null,
      onNotice: vi.fn(),
      onReload: vi.fn(),
      services: [registeredService],
    };
    let renderer!: ReactTestRenderer;
    act(() => {
      renderer = create(
        <AdministrationDashboard
          {...dashboardProps}
          scope={serviceLevelScope}
          snapshot={serviceSnapshot}
        />,
      );
    });
    const labeledControl = (
      label: string,
      elementType: "input" | "select",
    ): ReactTestInstance => {
      const labelElement = renderer.root.findAllByType("label").find(
        (item) =>
          item.children.some(
            (child) => typeof child === "string" && child.includes(label),
          ),
      );
      const control = labelElement?.findByType(elementType);
      if (control === undefined) {
        throw new Error(`The ${label} control did not render.`);
      }
      return control;
    };
    const hardLimit = labeledControl("Hard limit", "input");
    if (hardLimit.props.value !== "100")
      throw new Error("The service hard limit input did not render.");
    const changeHardLimit = hardLimit.props.onChange as (event: {
      readonly currentTarget: { readonly value: string };
    }) => void;
    act(() => {
      changeHardLimit({ currentTarget: { value: "999" } });
    });
    expect(labeledControl("Hard limit", "input").props.value).toBe("999");

    act(() => {
      renderer.update(
        <AdministrationDashboard
          {...dashboardProps}
          scope={globalScope}
          snapshot={workspaceSnapshot}
        />,
      );
    });
    expect(labeledControl("Hard limit", "input").props.value).toBe("20");
    expect(labeledControl("Currency", "input").props.value).toBe("EUR");
    expect(labeledControl("Warning threshold", "input").props.value).toBe(
      "15",
    );
    expect(labeledControl("Reset period", "select").props.value).toBe("daily");
    act(() => {
      renderer.unmount();
    });
  });
  /* eslint-enable @typescript-eslint/no-deprecated */

  it("blocks a budget write when the exact scope state failed", () => {
    const failedScope = {
      ...snapshot,
      state: null,
      budget: null,
      failures: { state: "The workspace is not available." },
    };
    const html = renderToStaticMarkup(
      <AdministrationDashboard
        client={client}
        initialSection="budgets"
        notice={null}
        onNotice={vi.fn()}
        onReload={vi.fn()}
        scope={globalScope}
        services={[registeredService]}
        catalogModels={catalogModels}
        snapshot={failedScope}
      />,
    );
    expect(html).toContain("Budget scope is not available");
    expect(html).toContain("The workspace is not available");
    expect(html).not.toContain("Save budget");
  });

  it("keeps provider secret custody out of the service view", () => {
    const serviceScope: ScopeSelection = {
      ...serviceLevelScope,
      mode: "service",
    };
    const html = dashboard("configuration", serviceScope);
    expect(html).toContain("Provider keys are managed globally");
    expect(html).not.toContain("Store OpenRouter credential");
    expect(html).toContain("Eligible credential reference ID");
    expect(html).not.toContain("Exact administration scope");
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
      "No assignment is available for this service",
    );
    expect(requestHtml).toContain(
      "No request status is available for this service",
    );
  });
});

describe("service management", () => {
  it("shows root creation in a full graph workspace", () => {
    const html = renderToStaticMarkup(
      <ServiceManagement
        client={client}
        services={[]}
        selectedServiceId=""
        onSelect={vi.fn()}
        onChanged={vi.fn()}
        onContinueSetup={vi.fn()}
        pendingBootstrap={null}
        onBootstrapPending={vi.fn()}
        onSuccess={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(html).toContain("od-graph-workspace");
    expect(html).toContain("Create the first root service");
    expect(html).toContain("No parent · start a new service chain");
    expect(html).toContain('name="display_name"');
  });

  it("shows multiple roots and their inheritance edges", () => {
    const firstRoot: ServiceSummary = {
      ...registeredService,
      service_id: "root-one",
      display_name: "Platform",
      parent_service_id: null,
    };
    const secondRoot: ServiceSummary = {
      ...registeredService,
      service_id: "root-two",
      display_name: "Experiments",
      parent_service_id: null,
    };
    const child: ServiceSummary = {
      ...registeredService,
      service_id: "child-one",
      display_name: "Xbot",
      parent_service_id: firstRoot.service_id,
    };
    const html = renderToStaticMarkup(
      <ServiceManagement
        client={client}
        services={[firstRoot, secondRoot, child]}
        selectedServiceId={firstRoot.service_id}
        onSelect={vi.fn()}
        onChanged={vi.fn()}
        onContinueSetup={vi.fn()}
        pendingBootstrap={null}
        onBootstrapPending={vi.fn()}
        onSuccess={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(html).toContain("3 services · 3 active · 2 roots");
    expect(html.match(/od-graph-node-eyebrow">Root service/g)).toHaveLength(2);
    expect(html).toContain("od-graph-edge-line");
    expect(html).not.toContain("Service hierarchy list");
    expect(html).toContain('draggable="true"');
    expect(html).toContain("Create child");
  });

  it("keeps malformed cycles visible as separate roots", () => {
    const first: ServiceSummary = {
      ...registeredService,
      service_id: "cycle-one",
      display_name: "First service",
      parent_service_id: "cycle-two",
    };
    const second: ServiceSummary = {
      ...registeredService,
      service_id: "cycle-two",
      display_name: "Second service",
      parent_service_id: "cycle-one",
    };
    const html = renderToStaticMarkup(
      <ServiceManagement
        client={client}
        services={[first, second]}
        selectedServiceId=""
        onSelect={vi.fn()}
        onChanged={vi.fn()}
        onContinueSetup={vi.fn()}
        pendingBootstrap={null}
        onBootstrapPending={vi.fn()}
        onSuccess={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(html).toContain("First service");
    expect(html).toContain("Second service");
  });

  it("shows a named parent chain and keeps technical IDs secondary", () => {
    const parent: ServiceSummary = {
      ...registeredService,
      service_id: "parent-service",
      display_name: "Shared platform",
    };
    const child: ServiceSummary = {
      ...registeredService,
      service_id: "child-service",
      display_name: "Xbot",
      parent_service_id: parent.service_id,
    };
    const html = renderToStaticMarkup(
      <ServiceManagement
        client={client}
        services={[parent, child]}
        selectedServiceId={child.service_id}
        onSelect={vi.fn()}
        onChanged={vi.fn()}
        onContinueSetup={vi.fn()}
        pendingBootstrap={null}
        onBootstrapPending={vi.fn()}
        onSuccess={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(html).toContain("Shared platform → Xbot");
    expect(html).toContain(
      "inherits eligible configuration from Shared platform",
    );
    expect(html).toContain("Technical details");
  });

  it("describes the first key as model access only", () => {
    const html = renderToStaticMarkup(
      <ServiceManagement
        client={client}
        services={[]}
        selectedServiceId=""
        onSelect={vi.fn()}
        onChanged={vi.fn()}
        onContinueSetup={vi.fn()}
        pendingBootstrap={null}
        onBootstrapPending={vi.fn()}
        onSuccess={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(html).toContain("Machine access: model calls only");
    expect(html).toContain("cannot run agents, use tools");
  });

  it("keeps disabled and retired services visible", () => {
    const disabled: ServiceSummary = {
      ...registeredService,
      service_id: "disabled-service",
      display_name: "Disabled service",
      state: "disabled",
    };
    const retired: ServiceSummary = {
      ...registeredService,
      service_id: "retired-service",
      display_name: "Retired service",
      state: "retired",
    };
    const html = renderToStaticMarkup(
      <ServiceManagement
        client={client}
        services={[disabled, retired]}
        selectedServiceId={disabled.service_id}
        onSelect={vi.fn()}
        onChanged={vi.fn()}
        onContinueSetup={vi.fn()}
        pendingBootstrap={null}
        onBootstrapPending={vi.fn()}
        onSuccess={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(html).toContain("Disabled service");
    expect(html).toContain("Retired service");
  });

  it("keeps a pending one-time key visible until confirmation", () => {
    const html = renderToStaticMarkup(
      <ServiceManagement
        client={client}
        services={[registeredService]}
        selectedServiceId={registeredService.service_id}
        onSelect={vi.fn()}
        onChanged={vi.fn()}
        onContinueSetup={vi.fn()}
        pendingBootstrap={{
          service_id: registeredService.service_id,
          state: "active",
          state_revision: "1",
          bootstrap_secret: "one-time-key",
          bootstrap_secret_available: true,
          credential_generation: 1,
        }}
        onBootstrapPending={vi.fn()}
        onSuccess={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(html).toContain("Store this key now");
    expect(html).toContain("one-time-key");
    expect(html).toContain("I stored it · open setup");
  });
});
