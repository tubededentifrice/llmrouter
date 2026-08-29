import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, loadGlobalAdministrationSources } from "../src/App.js";
import {
  expireAdministratorSessionLoads,
  invalidateRetainedMediaLoad,
  updateRetentionDuration,
} from "../src/administrationSafety.js";
import {
  MissingProtectedKeyInspector,
  ServiceManagement,
} from "../src/ServiceManagement.js";
import type {
  AdministrationClient,
  RequestLogSummary,
  Service,
} from "../src/api.js";
import {
  administrationListMaximum,
  administrationListPageMaximum,
  continuedPageCursor,
  mergeBoundedRows,
} from "../src/api.js";
import {
  createScopeLoadGuard,
  protectedServiceApiName,
  reduceKeyCreationLifecycle,
  serviceInteractionLocked,
  uniqueDraftRowId,
  type KeyCreationLifecycle,
} from "../src/accessState.js";
import {
  createInputImageSelectionQueue,
  credentialFormValue,
  parseManualPrice,
  validateInputImageSelection,
} from "../src/formContracts.js";
import { scheduleSessionExpiry } from "../src/sessionExpiry.js";
import {
  requestLogActorLabel,
  requestLogRouteLabel,
  requestLogScopeLabel,
} from "../src/logPresentation.js";

const emptyPage = { items: [], page: { has_more: false, next_cursor: null } };

function client(): AdministrationClient {
  return {
    session: vi.fn().mockResolvedValue({
      subject: "subject",
      display_name: "Admin",
      expires_at: "2026-08-25T00:00:00Z",
      csrf_token: "csrf",
    }),
    startSession: vi.fn(),
    logout: vi.fn(),
    services: vi.fn().mockResolvedValue(emptyPage),
    createService: vi.fn(),
    updateService: vi.fn(),
    deleteService: vi.fn(),
    workspaces: vi.fn().mockResolvedValue(emptyPage),
    createWorkspace: vi.fn(),
    deleteWorkspace: vi.fn(),
    keys: vi.fn().mockResolvedValue(emptyPage),
    createKey: vi.fn(),
    revokeKey: vi.fn(),
    assignments: vi.fn().mockResolvedValue(emptyPage),
    putAssignment: vi.fn(),
    deleteAssignment: vi.fn(),
    removeRequirement: vi.fn(),
    providers: vi.fn().mockResolvedValue(emptyPage),
    createProvider: vi.fn(),
    putProvider: vi.fn(),
    deleteProvider: vi.fn(),
    models: vi.fn().mockResolvedValue(emptyPage),
    createModel: vi.fn(),
    putModel: vi.fn(),
    deleteModel: vi.fn(),
    providerModels: vi.fn().mockResolvedValue(emptyPage),
    createProviderModel: vi.fn(),
    putProviderModel: vi.fn(),
    deleteProviderModel: vi.fn(),
    credentials: vi.fn().mockResolvedValue(emptyPage),
    createCredential: vi.fn(),
    replaceCredential: vi.fn(),
    deleteCredential: vi.fn(),
    previewImport: vi.fn(),
    importModels: vi.fn(),
    previewOpenRouterModel: vi.fn(),
    importOpenRouterModel: vi.fn(),
    synchronizePrices: vi.fn(),
    activity: vi.fn().mockResolvedValue(emptyPage),
    activityPage: vi.fn().mockResolvedValue(emptyPage),
    statistics: vi.fn(),
    requestLogs: vi.fn().mockResolvedValue(emptyPage),
    requestLogsPage: vi.fn().mockResolvedValue(emptyPage),
    requestLog: vi.fn(),
    requestLogMedia: vi.fn(),
    retention: vi.fn().mockResolvedValue({ duration_days: 7 }),
    putRetention: vi.fn(),
    health: vi.fn().mockResolvedValue({
      status: "healthy",
      checked_at: "2026-08-24T00:00:00Z",
      components: [],
    }),
  };
}

const services: readonly Service[] = [
  {
    api_name: "root",
    display_name: "Root",
    parent_service_api_name: null,
    created_at: "2026-08-24T00:00:00Z",
  },
  {
    api_name: "child",
    display_name: "Child",
    parent_service_api_name: "root",
    created_at: "2026-08-24T00:00:00Z",
  },
];

describe("accepted administration composition", () => {
  it("labels service and administrator log rows from their immutable actor", () => {
    const common = {
      id: "log-1",
      logical_call_id: "call-1",
      kind: "model" as const,
      outcome: "succeeded" as const,
      started_at: "2026-08-25T00:00:00Z",
    };
    const cases: readonly {
      readonly summary: RequestLogSummary;
      readonly actor: string;
      readonly scope: string;
      readonly route: string;
    }[] = [
      {
        summary: {
          ...common,
          call_actor: "service",
          service_api_name: "billing",
          workspace_api_name: "production",
          assignment_api_name: "summarize",
        },
        actor: "Service",
        scope: "billing / production",
        route: "summarize",
      },
      {
        summary: {
          ...common,
          call_actor: "administrator",
          administrator_subject: "pocket-id-subject",
          provider_model_api_name: "primary-text",
        },
        actor: "Administrator",
        scope: "pocket-id-subject / global exact route",
        route: "primary-text",
      },
      {
        summary: {
          ...common,
          call_actor: "administrator",
          administrator_subject: "pocket-id-subject",
          assignment_api_name: "summarize",
          configuration_service_api_name: "billing",
          provider_model_api_name: "fallback-text",
        },
        actor: "Administrator",
        scope: "pocket-id-subject / configuration billing",
        route: "summarize",
      },
    ];

    for (const item of cases) {
      expect(requestLogActorLabel(item.summary)).toBe(item.actor);
      expect(requestLogScopeLabel(item.summary)).toBe(item.scope);
      expect(requestLogRouteLabel(item.summary)).toBe(item.route);
    }
  });

  it("uses one configuration graph as the only LLM configuration entry", () => {
    const application = readFileSync(
      new URL("../src/App.tsx", import.meta.url),
      "utf8",
    );
    const configuration = readFileSync(
      new URL("../src/ConfigurationGraph.tsx", import.meta.url),
      "utf8",
    );
    expect(application).toContain('id: "configuration"');
    expect(application).not.toContain('label: "Providers"');
    expect(application).not.toContain('label: "Models & prices"');
    expect(application).not.toContain('label: "Assignments"');
    expect(application).not.toContain('label: "Playground"');
    expect(application).not.toContain("function ProvidersPage");
    expect(application).not.toContain("function ModelsPage");
    expect(application).not.toContain("function AssignmentsPage");
    expect(application).not.toContain("function PlaygroundPage");
    expect(configuration).toContain("RelationshipGraph");
    expect(configuration).toContain('label: "Providers"');
    expect(configuration).toContain('label: "Canonical models"');
    expect(configuration).toContain('label: "Assignments"');
    expect(configuration).toContain('rowsLabel: "Provider routes"');
    expect(configuration).toContain('label: "Provides"');
    expect(configuration).toContain(
      'aria-label="LLM configuration relationships"',
    );
    expect(configuration).toContain('searchLabel="Search configuration"');
    expect(application).not.toContain("key={selectedService}");
    expect(application).toContain("assignmentPending");
    expect(configuration).toContain("Play exact route");
    expect(configuration).toContain("Play assignment");
    expect(configuration).toContain("<PlaygroundModal");
    expect(configuration).toContain(
      "playgroundTarget.serviceContext === selectedService",
    );
    expect(configuration).toContain(".od-relationship-graph-empty button");
    expect(configuration).not.toContain("setPlaygroundTarget(null);\n\n  if (");
    expect(configuration).not.toContain(
      "Playground available after configuration delivery",
    );
  });

  it("uses the shared page and table contracts for each retained page", () => {
    const application = readFileSync(
      new URL("../src/App.tsx", import.meta.url),
      "utf8",
    );
    const styles = readFileSync(
      new URL("../src/styles.css", import.meta.url),
      "utf8",
    );

    expect(
      application.match(/<PageSurface className="administration-page">/g),
    ).toHaveLength(7);
    expect(application.match(/<DataTable/g)).toHaveLength(3);
    expect(application).not.toContain("<table");
    expect(application).not.toContain("EmptyTable");
    expect(application).toContain('ariaLabel="Detailed request logs"');
    expect(application).toContain('ariaLabel="Usage and cost statistics"');
    expect(application).toContain('ariaLabel="Configuration activity"');
    expect(administrationListMaximum).toBe(20_000);
    expect(administrationListPageMaximum).toBe(100);
    expect(application).toContain("STATISTICS_GROUP_MAXIMUM = 1_000");
    expect(application).toContain("requestLogsPage(");
    expect(application).toContain("activityPage(");
    expect(application.match(/loadMore: \{/g)).toHaveLength(2);
    expect(styles).not.toContain(".administration-table-region");
    expect(styles).toContain(".administration-data-table");
    expect(styles).toContain("padding-block: 32px 76px");
  });

  it("keeps an unavailable selected log explicit and recoverable", () => {
    const application = readFileSync(
      new URL("../src/App.tsx", import.meta.url),
      "utf8",
    );
    expect(application).toContain("detailFailure");
    expect(application).toContain("The selected request log is unavailable.");
    expect(application).toContain("detailReturnFocus.current");
    expect(application).toContain('id="request-log-close"');
    expect(application).toContain(
      "[logs.detail, logs.detailFailure, logs.detailId]",
    );
    expect(
      application.match(
        /detailReturnFocus\.current = context\?\.trigger \?\? null;/g,
      ),
    ).toHaveLength(1);
    expect(application).toContain("Usage unavailable");
    expect(application).toContain("detailLoadGuard.current.invalidate()");
    expect(application).toContain("detailReturnFocus.current = null");
    expect(application).toContain(
      'getElementById("request-log-load")?.focus()',
    );
    expect(application).toContain("setResult(null)");
  });

  it("rejects inconsistent incremental pages without changing loaded rows", () => {
    const current = [{ id: "one" }];
    expect(() =>
      mergeBoundedRows(current, [{ id: "one" }], (item) => item.id, "Records"),
    ).toThrow("repeated record one");
    expect(current).toEqual([{ id: "one" }]);

    const progressingCursors = new Set(
      Array.from({ length: 98 }, (_, index) => `page-${String(index + 1)}`),
    );
    expect(
      continuedPageCursor(
        { items: [], page: { has_more: true, next_cursor: "page-100" } },
        99,
        progressingCursors,
        "Records",
      ),
    ).toBe("page-100");
    progressingCursors.add("page-100");
    expect(() =>
      continuedPageCursor(
        { items: [], page: { has_more: true, next_cursor: "page-101" } },
        100,
        progressingCursors,
        "Records",
      ),
    ).toThrow("100 page safety limit");
    expect(() =>
      continuedPageCursor(
        { items: [], page: { has_more: true, next_cursor: "repeat" } },
        2,
        new Set(["repeat"]),
        "Records",
      ),
    ).toThrow("repeated a continuation cursor");
  });

  it("does not restore removed product surfaces", () => {
    const source = readFileSync(
      new URL("../src/App.tsx", import.meta.url),
      "utf8",
    );
    for (const removed of [
      "Budgets",
      "Trusted grant",
      "Recent authentication",
      "Configuration revision",
      "ExportView",
      "Recovery",
    ])
      expect(source).not.toContain(removed);
  });

  it("uses the service graph as the only service record surface", () => {
    const markup = renderToStaticMarkup(
      <ServiceManagement
        client={client()}
        csrf="csrf"
        onNotice={vi.fn()}
        onRefresh={vi.fn()}
        onSelect={vi.fn()}
        selectedService="root"
        services={services}
      />,
    );
    expect(markup).toContain("Service tree canvas");
    expect(markup).not.toContain("Accessible service list");
    expect(markup).not.toContain("<strong>Service tree</strong>");
    expect(markup).toContain('data-canvas-alignment="center"');
    expect(markup.match(/tabindex="0"/g)).toHaveLength(1);
    expect(markup).toContain("Root");
    expect(markup).toContain("Child");
    expect(markup).toContain("tree level 1");
    expect(markup).toContain("tree level 2");
    expect(markup).toContain("Workspaces");
    expect(markup).toContain("Service API keys");
    expect(markup).toContain("Loading workspaces");
    expect(markup).toContain("Loading service API keys");
    expect(markup).toContain("Move or delete each child");
    expect(markup).toContain('aria-expanded="false"');
    expect(markup).toContain("od-form-section");
    expect(markup).toContain("od-form-actions");
    expect(markup).toContain('name="parent"');
    expect(markup).toContain('dateTime="2026-08-24T00:00:00.000Z"');
  });

  it("keeps the exact service timestamp fallback without an invalid attribute", () => {
    const markup = renderToStaticMarkup(
      <ServiceManagement
        client={client()}
        csrf="csrf"
        onNotice={vi.fn()}
        onRefresh={vi.fn()}
        onSelect={vi.fn()}
        selectedService="invalid-time"
        services={[
          {
            api_name: "invalid-time",
            display_name: "Invalid time",
            parent_service_api_name: null,
            created_at: "not-a-timestamp",
          },
        ]}
      />,
    );

    expect(markup).toContain("od-date-time-invalid");
    expect(markup).toContain("not-a-timestamp");
    expect(markup).not.toContain('dateTime="not-a-timestamp"');
  });

  it("renders the empty service tree without resetting an empty focus target", () => {
    const markup = renderToStaticMarkup(
      <ServiceManagement
        client={client()}
        csrf="csrf"
        onNotice={vi.fn()}
        onRefresh={vi.fn()}
        onSelect={vi.fn()}
        selectedService=""
        services={[]}
      />,
    );

    expect(markup).toContain("No services");
    expect(markup).toContain("Create a root service");
  });

  it("keeps workspaces and keys in the service inspector", () => {
    const applicationSource = readFileSync(
      new URL("../src/App.tsx", import.meta.url),
      "utf8",
    );
    const serviceSource = readFileSync(
      new URL("../src/ServiceManagement.tsx", import.meta.url),
      "utf8",
    );
    expect(applicationSource).not.toContain('label: "Workspaces & keys"');
    expect(applicationSource).not.toContain("function AccessPage");
    expect(serviceSource).toContain("EditableTable");
    expect(serviceSource).toContain("ConfirmationDialog");
    expect(serviceSource).toContain("SecretRevealPanel");
    expect(serviceSource).toContain("SearchableSelect");
    expect(serviceSource).toContain("FormField");
    expect(serviceSource).toContain("FormActions");
    expect(serviceSource).toContain("FormSection");
    expect(serviceSource).toContain("InlineAlert");
    expect(serviceSource).toContain("<DateTime");
    expect(serviceSource).not.toContain("globalThis.confirm");
    expect(serviceSource).toContain("Copy this key now");
    expect(serviceSource).toContain(
      "keyLifecycleActive || accessPending || busy ? {} : { onClose }",
    );
    expect(serviceSource).toContain("serviceInteractionLocked");
    expect(serviceSource).toContain("MissingProtectedKeyInspector");
    expect(serviceSource).toContain("busyRef.current");
    expect(serviceSource).toContain(
      'phase === "loading" || keyLifecycleActive',
    );
    expect(serviceSource).not.toContain('target.closest("dialog:modal")');
    expect(serviceSource).not.toContain("localStorage");
    expect(serviceSource).not.toContain("sessionStorage");
    expect(serviceSource).not.toContain("Use in playground");
    expect(applicationSource).toContain("<DateTime");
  });

  it("uses a create-row identity that no workspace API name can use", () => {
    const serviceSource = readFileSync(
      new URL("../src/ServiceManagement.tsx", import.meta.url),
      "utf8",
    );
    expect(serviceSource).toContain(
      'const WORKSPACE_CREATE_ROW_ID = "__new_workspace__"',
    );
    expect(serviceSource).not.toContain('id: "new-workspace"');
  });

  it("allocates a key create-row identity outside the current opaque IDs", () => {
    expect(uniqueDraftRowId([], "__new_service_key__")).toBe(
      "__new_service_key__",
    );
    expect(
      uniqueDraftRowId(
        ["__new_service_key__", "__new_service_key___"],
        "__new_service_key__",
      ),
    ).toBe("__new_service_key____");
  });

  it("locks graph actions synchronously for each pending host operation", () => {
    expect(serviceInteractionLocked(false, 0, null)).toBe(false);
    expect(serviceInteractionLocked(true, 0, null)).toBe(true);
    expect(serviceInteractionLocked(false, 1, null)).toBe(true);
    expect(
      serviceInteractionLocked(false, 0, {
        phase: "pending",
        serviceApiName: "root",
      }),
    ).toBe(true);
  });

  it("keeps a key request and one-time secret bound to its service", () => {
    let lifecycle: KeyCreationLifecycle | null = null;
    lifecycle = reduceKeyCreationLifecycle(lifecycle, {
      type: "begin",
      serviceApiName: "root",
    });
    expect(lifecycle).toEqual({ phase: "pending", serviceApiName: "root" });
    expect(reduceKeyCreationLifecycle(lifecycle, { type: "clear" })).toBe(
      lifecycle,
    );
    expect(
      reduceKeyCreationLifecycle(lifecycle, {
        type: "failed",
        serviceApiName: "child",
      }),
    ).toBe(lifecycle);

    lifecycle = reduceKeyCreationLifecycle(lifecycle, {
      type: "created",
      secret: "one-time-secret",
      serviceApiName: "root",
    });
    expect(lifecycle).toEqual({
      phase: "shown",
      secret: "one-time-secret",
      serviceApiName: "root",
    });
    expect(
      reduceKeyCreationLifecycle(lifecycle, {
        type: "begin",
        serviceApiName: "child",
      }),
    ).toBe(lifecycle);
    expect(reduceKeyCreationLifecycle(lifecycle, { type: "clear" })).toBeNull();
    expect(
      reduceKeyCreationLifecycle(null, {
        type: "created",
        secret: "late-secret",
        serviceApiName: "root",
      }),
    ).toBeNull();
    const failed = reduceKeyCreationLifecycle(
      { phase: "pending", serviceApiName: "root" },
      { type: "failed", serviceApiName: "root" },
    );
    expect(failed).toBeNull();
  });

  it("keeps a missing service key lifecycle reachable", () => {
    const pending: KeyCreationLifecycle = {
      phase: "pending",
      serviceApiName: "removed-service",
    };
    expect(protectedServiceApiName("child", pending)).toBe("removed-service");
    expect(
      services.find(
        (service) =>
          service.api_name === protectedServiceApiName("child", pending),
      ),
    ).toBeUndefined();
    const pendingMarkup = renderToStaticMarkup(
      <MissingProtectedKeyInspector
        keyLifecycle={pending}
        onClearKey={vi.fn()}
        onNotice={vi.fn()}
      />,
    );
    expect(pendingMarkup).toContain("Creating the service API key");
    expect(pendingMarkup).toContain("The service record is unavailable");

    const shownMarkup = renderToStaticMarkup(
      <MissingProtectedKeyInspector
        keyLifecycle={{
          phase: "shown",
          secret: "one-time-secret",
          serviceApiName: "removed-service",
        }}
        onClearKey={vi.fn()}
        onNotice={vi.fn()}
      />,
    );
    expect(shownMarkup).toContain("one-time-secret");
    expect(shownMarkup).toContain("Clear key");
    expect(shownMarkup).toContain("od-secret-reveal-panel");
    expect(shownMarkup).toContain("Service API key");
  });

  it("starts in a session loading state", () => {
    const administration = client();
    const markup = renderToStaticMarkup(<App client={administration} />);
    expect(markup).toContain("Checking the administrator session");
  });

  it("uses semantic CSS class names", () => {
    const styles = readFileSync(
      new URL("../src/styles.css", import.meta.url),
      "utf8",
    );
    expect(styles).toContain(".administration-form");
    expect(styles).toContain(".health-list");
    expect(styles).not.toContain(".secret-panel");
    expect(styles).not.toMatch(/\.(?:mt|mb|px|py|flex|grid)-\d/);
    for (const layoutHelper of [
      ".page-stack",
      ".two-column",
      ".compact-form",
      ".range-form",
      ".stack-form",
      ".table-scroll",
      ".inline-actions",
    ])
      expect(styles).not.toContain(layoutHelper);
  });
});

describe("load isolation", () => {
  it("keeps successful global source results when one source fails", async () => {
    const administration = {
      ...client(),
      providers: vi.fn(() => {
        throw new Error("The provider source failed.");
      }),
    };

    const results = await loadGlobalAdministrationSources(administration);

    expect(results.providers).toMatchObject({
      status: "rejected",
      reason: new Error("The provider source failed."),
    });
    for (const result of [
      results.services,
      results.models,
      results.providerModels,
      results.credentials,
      results.health,
      results.retention,
    ])
      expect(result.status).toBe("fulfilled");
  });

  it("rejects a late completion after the selected scope changes", () => {
    const guard = createScopeLoadGuard();
    const first = guard.begin();
    expect(guard.isCurrent(first)).toBe(true);
    guard.invalidate();
    expect(guard.isCurrent(first)).toBe(false);
    const second = guard.begin();
    expect(guard.isCurrent(second)).toBe(true);
  });
});

describe("playground image boundaries", () => {
  const image = (size: number, type = "image/png") => ({ size, type });

  it("accepts the exact file-count and byte boundaries", () => {
    expect(() => {
      validateInputImageSelection(
        Array.from({ length: 7 }, () => ({ sizeBytes: 1 })),
        [image(1)],
      );
    }).not.toThrow();
    expect(() => {
      validateInputImageSelection(
        [{ sizeBytes: 31_457_280 }],
        [image(20_971_520)],
      );
    }).not.toThrow();
  });

  it("rejects too many, empty, oversized, and unsupported images", () => {
    expect(() => {
      validateInputImageSelection(
        Array.from({ length: 8 }, () => ({ sizeBytes: 1 })),
        [image(1)],
      );
    }).toThrow("no more than 8");
    expect(() => {
      validateInputImageSelection([], [image(0)]);
    }).toThrow("at least 1 byte");
    expect(() => {
      validateInputImageSelection([], [image(20_971_521)]);
    }).toThrow("20,971,520 bytes or smaller");
    expect(() => {
      validateInputImageSelection([], [image(1, "image/gif")]);
    }).toThrow("JPEG, PNG, or WebP");
  });

  it("rejects a combined byte count above the exact boundary", () => {
    expect(() => {
      validateInputImageSelection(
        [{ sizeBytes: 31_457_281 }],
        [image(20_971_520)],
      );
    }).toThrow("52,428,800 bytes or less");
  });

  it("serializes concurrent selections without losing an accepted batch", async () => {
    interface Item {
      readonly id: string;
      readonly sizeBytes: number;
    }
    const changes: (readonly Item[])[] = [];
    let releaseFirst: (() => void) | undefined;
    const firstRead = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const queue = createInputImageSelectionQueue<Item>([], (items) => {
      changes.push(items);
    });
    const first = queue.add([image(1)], async () => {
      await firstRead;
      return { id: "first", sizeBytes: 1 };
    });
    const second = queue.add([image(1)], () =>
      Promise.resolve({ id: "second", sizeBytes: 1 }),
    );
    await Promise.resolve();
    expect(changes).toEqual([]);
    releaseFirst?.();
    await expect(first).resolves.toHaveLength(1);
    await expect(second).resolves.toHaveLength(2);
    expect(changes.at(-1)?.map((item) => item.id)).toEqual(["first", "second"]);
  });

  it("applies count and total-byte limits after each queued batch", async () => {
    const queue = createInputImageSelectionQueue(
      Array.from({ length: 6 }, (_, index) => ({
        id: `existing-${String(index)}`,
        sizeBytes: 1,
      })),
      vi.fn(),
    );
    let releaseFirst: (() => void) | undefined;
    const firstRead = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const first = queue.add([image(20_971_520)], async () => {
      await firstRead;
      return { id: "first", sizeBytes: 20_971_520 };
    });
    const second = queue.add([image(20_971_520), image(20_971_520)], () =>
      Promise.resolve({ id: "second", sizeBytes: 20_971_520 }),
    );
    releaseFirst?.();
    await expect(first).resolves.toHaveLength(7);
    await expect(second).rejects.toThrow("no more than 8");

    const totalQueue = createInputImageSelectionQueue(
      [{ id: "existing", sizeBytes: 10_485_761 }],
      vi.fn(),
    );
    const accepted = totalQueue.add([image(20_971_520)], () =>
      Promise.resolve({ id: "accepted", sizeBytes: 20_971_520 }),
    );
    const rejected = totalQueue.add([image(20_971_521 - 1)], () =>
      Promise.resolve({ id: "rejected", sizeBytes: 20_971_520 }),
    );
    await expect(accepted).resolves.toHaveLength(2);
    await expect(rejected).rejects.toThrow("52,428,800 bytes or less");
  });

  it("orders removal after an active selection batch", async () => {
    interface Item {
      readonly id: string;
      readonly sizeBytes: number;
    }
    const changes: (readonly Item[])[] = [];
    let release: (() => void) | undefined;
    const reading = new Promise<void>((resolve) => {
      release = resolve;
    });
    const queue = createInputImageSelectionQueue<Item>(
      [
        { id: "keep", sizeBytes: 1 },
        { id: "remove", sizeBytes: 1 },
      ],
      (items) => {
        changes.push(items);
      },
    );
    const adding = queue.add([image(1)], async () => {
      await reading;
      return { id: "added", sizeBytes: 1 };
    });
    const removing = queue.remove((item) => item.id === "remove");
    release?.();
    await expect(adding).resolves.toHaveLength(3);
    await expect(removing).resolves.toEqual([
      { id: "keep", sizeBytes: 1 },
      { id: "added", sizeBytes: 1 },
    ]);
    expect(changes.at(-1)?.map((item) => item.id)).toEqual(["keep", "added"]);
  });

  it("drops a late file read after queue disposal", async () => {
    interface Item {
      readonly id: string;
      readonly sizeBytes: number;
    }
    const changes = vi.fn();
    let release: (() => void) | undefined;
    const reading = new Promise<void>((resolve) => {
      release = resolve;
    });
    const queue = createInputImageSelectionQueue<Item>([], changes);
    const adding = queue.add([image(1)], async () => {
      await reading;
      return { id: "late", sizeBytes: 1 };
    });
    await Promise.resolve();
    queue.dispose();
    release?.();
    await expect(adding).resolves.toEqual([]);
    expect(changes).not.toHaveBeenCalled();
    await expect(queue.remove(() => true)).resolves.toEqual([]);
  });

  it("drops active and queued selections after reset", async () => {
    interface Item {
      readonly id: string;
      readonly sizeBytes: number;
    }
    let release: (() => void) | undefined;
    const reading = new Promise<void>((resolve) => {
      release = resolve;
    });
    const changes: (readonly Item[])[] = [];
    const secondRead = vi.fn(() =>
      Promise.resolve({ id: "queued", sizeBytes: 1 }),
    );
    const queue = createInputImageSelectionQueue<Item>([], (items) => {
      changes.push(items);
    });
    const active = queue.add([image(1)], async () => {
      await reading;
      return { id: "active", sizeBytes: 1 };
    });
    const queued = queue.add([image(1)], secondRead);
    await Promise.resolve();
    expect(queue.clear()).toEqual([]);
    release?.();
    await expect(active).resolves.toEqual([]);
    await expect(queued).resolves.toEqual([]);
    expect(secondRead).not.toHaveBeenCalled();
    expect(changes).toEqual([[]]);
  });
});

describe("manual typed prices", () => {
  it("builds one exact price with the complete current unit set", () => {
    expect(
      parseManualPrice(
        "usd",
        "input_token=0.001, output_token=0.002, cached_input_token=0.0005, image=1, video_second=0.1, audio_second=0.05, request=0.01, provider_unit=2",
      ),
    ).toEqual({
      currency: "USD",
      unit_prices: [
        { unit: "input_token", amount: "0.001" },
        { unit: "output_token", amount: "0.002" },
        { unit: "cached_input_token", amount: "0.0005" },
        { unit: "image", amount: "1" },
        { unit: "video_second", amount: "0.1" },
        { unit: "audio_second", amount: "0.05" },
        { unit: "request", amount: "0.01" },
        { unit: "provider_unit", amount: "2" },
      ],
    });
  });

  it("uses both empty fields only to clear a manual price", () => {
    expect(parseManualPrice("", "")).toBeNull();
    expect(() => parseManualPrice("", "request=1")).toThrow(
      "three-letter currency",
    );
    expect(() => parseManualPrice("USD", "")).toThrow(
      "at least one typed unit amount",
    );
    expect(() => parseManualPrice("USD", "request=")).toThrow(
      "fixed-decimal amount",
    );
  });
});

describe("write-only credential form", () => {
  it("preserves every opaque secret byte while names stay separate", () => {
    const form = new FormData();
    form.set("credential_api_name", "  credential-name  ");
    form.set("secret", "  exact secret with space and newline\n ");
    expect(credentialFormValue(form)).toEqual({
      apiName: "credential-name",
      secret: "  exact secret with space and newline\n ",
    });
  });
});

describe("absolute session expiry", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("expires at the exact local deadline without a request", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-24T12:00:00.000Z"));
    const expired = vi.fn();
    const cancel = scheduleSessionExpiry("2026-08-24T12:00:01.000Z", expired);
    await vi.advanceTimersByTimeAsync(999);
    expect(expired).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(expired).toHaveBeenCalledOnce();
    cancel();
  });

  it("cancels the local expiry transition safely", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-24T12:00:00.000Z"));
    const expired = vi.fn();
    const cancel = scheduleSessionExpiry("2026-08-24T12:00:01.000Z", expired);
    cancel();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(expired).not.toHaveBeenCalled();
  });

  it("makes every pending administrator load stale before session clear", () => {
    const globalLoad = createScopeLoadGuard();
    const scopeLoad = createScopeLoadGuard();
    const globalGeneration = globalLoad.begin();
    const scopeGeneration = scopeLoad.begin();
    const clearSession = vi.fn(() => {
      expect(globalLoad.isCurrent(globalGeneration)).toBe(false);
      expect(scopeLoad.isCurrent(scopeGeneration)).toBe(false);
    });

    expireAdministratorSessionLoads(globalLoad, scopeLoad, clearSession);

    expect(clearSession).toHaveBeenCalledOnce();
    expect(globalLoad.isCurrent(globalGeneration)).toBe(false);
    expect(scopeLoad.isCurrent(scopeGeneration)).toBe(false);
  });
});

describe("destructive retention and retained-media races", () => {
  it("does not write when an administrator cancels a retention decrease", async () => {
    const confirm = vi.fn((message: string) => {
      void message;
      return false;
    });
    const write = vi.fn().mockResolvedValue({ duration_days: 7 });

    await expect(updateRetentionDuration(30, 7, confirm, write)).resolves.toBe(
      false,
    );

    expect(confirm).toHaveBeenCalledOnce();
    expect(confirm.mock.calls[0]?.[0]).toContain(
      "Detailed logs, activity, uploaded images, and retained generated media",
    );
    expect(write).not.toHaveBeenCalled();
  });

  it("writes a retention increase without a destructive confirmation", async () => {
    const confirm = vi.fn((message: string) => {
      void message;
      return true;
    });
    const write = vi.fn().mockResolvedValue({ duration_days: 30 });

    await expect(updateRetentionDuration(7, 30, confirm, write)).resolves.toBe(
      true,
    );

    expect(confirm).not.toHaveBeenCalled();
    expect(write).toHaveBeenCalledExactlyOnceWith(30);
  });

  it("ignores retained media that completes after a new detail starts", async () => {
    const mediaLoad = createScopeLoadGuard();
    const staleGeneration = mediaLoad.begin();
    const revoke = vi.fn();
    const storeBlobUrl = vi.fn();

    expect(
      invalidateRetainedMediaLoad(mediaLoad, "blob:prior", revoke),
    ).toBeNull();
    await Promise.resolve("blob:stale").then((url) => {
      if (mediaLoad.isCurrent(staleGeneration)) storeBlobUrl(url);
    });

    expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:prior");
    expect(storeBlobUrl).not.toHaveBeenCalled();
  });
});
