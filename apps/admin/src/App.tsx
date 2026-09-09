import { StatisticsPage } from "./StatisticsPage.tsx";
import { LogsPage } from "./LogsPage.tsx";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
  type SubmitEvent,
} from "react";
import {
  AccountMenu,
  ApplicationNavigation,
  ApplicationNavigationGroup,
  ApplicationShell,
  ApplicationSidebar,
  Button,
  ConfirmationDialog,
  DataTable,
  DateTime,
  Icon,
  InlineAlert,
  MobileNavigation,
  NavigationLink,
  NumberControl,
  PageHeading,
  GraphEmptyState,
  PageSurface,
  Panel,
  PanelHeader,
  SessionCard,
  SessionPage,
  SelectControl,
  ShellErrorBoundary,
  SkipLink,
  StatCard,
  StatePanel,
  StatusPill,
  Toast,
  type DataTableColumn,
  type IconName,
} from "@opendle/ui";
import {
  AdministrationApiError,
  administrationListMaximum,
  continuedPageCursor,
  createAdministrationClient,
  errorMessage,
  isoRange,
  mergeBoundedRows,
  type ActivityEvent,
  type AdministrationClient,
  type AdministratorHealth,
  type AdministratorSession,
  type Assignment,
  type Credential,
  type Model,
  type Provider,
  type ProviderModel,
  type Service,
} from "./api.ts";
import { ServiceDetails } from "./ServiceDetails.tsx";
import {
  ServiceManagement,
  type ServiceCreationEvent,
} from "./ServiceManagement.tsx";
import { ConfigurationGraph } from "./ConfigurationGraph.tsx";
import { createScopeLoadGuard } from "./accessState.ts";
import type { ConfigurationLoadPhase } from "./configurationState.ts";
import { updateRetentionDuration } from "./administrationSafety.ts";
import { scheduleSessionExpiry } from "./sessionExpiry.ts";

type Section =
  | "overview"
  | "services"
  | "configuration"
  | "logs"
  | "statistics"
  | "operations";
interface Notice {
  readonly tone: "success" | "error";
  readonly message: string;
}
interface AppData {
  readonly services: readonly Service[];
  readonly providers: readonly Provider[];
  readonly models: readonly Model[];
  readonly providerModels: readonly ProviderModel[];
  readonly credentials: readonly Credential[];
  readonly health: AdministratorHealth | null;
  readonly retentionDays: number | null;
}

function callGlobalSource<T>(source: () => Promise<T>): Promise<T> {
  return Promise.resolve().then(source);
}

// eslint-disable-next-line react-refresh/only-export-components -- A direct test uses this load helper.
export async function loadGlobalAdministrationSources(
  client: AdministrationClient,
) {
  const [
    services,
    providers,
    models,
    providerModels,
    credentials,
    health,
    retention,
  ] = await Promise.allSettled([
    callGlobalSource(() => client.services()),
    callGlobalSource(() => client.providers()),
    callGlobalSource(() => client.models()),
    callGlobalSource(() => client.providerModels()),
    callGlobalSource(() => client.credentials()),
    callGlobalSource(() => client.health()),
    callGlobalSource(() => client.retention()),
  ] as const);
  return {
    services,
    providers,
    models,
    providerModels,
    credentials,
    health,
    retention,
  };
}

const routes: readonly {
  readonly id: Section;
  readonly label: string;
  readonly icon: IconName;
  readonly group: "Manage" | "Observe";
}[] = [
  { id: "overview", label: "Overview", icon: "grid", group: "Manage" },
  { id: "services", label: "Services", icon: "layers", group: "Manage" },
  {
    id: "configuration",
    label: "LLM configuration",
    icon: "spark",
    group: "Manage",
  },
  { id: "logs", label: "Logs", icon: "list", group: "Observe" },
  {
    id: "statistics",
    label: "Usage & cost",
    icon: "activity",
    group: "Observe",
  },
  {
    id: "operations",
    label: "Activity & health",
    icon: "health",
    group: "Observe",
  },
];
const legacyConfigurationPaths = new Set([
  "providers",
  "models",
  "assignments",
  "playground",
]);

function currentSection(): Section {
  const value =
    typeof location === "undefined" ? "" : location.pathname.slice(1);
  if (value === "access" || value.startsWith("services/")) return "services";
  if (legacyConfigurationPaths.has(value)) return "configuration";
  return routes.some((route) => route.id === value)
    ? (value as Section)
    : "overview";
}
function selectedServiceFromLocation(): string {
  const search = typeof location === "undefined" ? "" : location.search;
  return new URLSearchParams(search).get("service") ?? "";
}
function safeReturnPath(): string {
  const section = currentSection();
  const detailName = detailServiceName(
    typeof location === "undefined" ? "" : location.pathname,
  );
  if (detailName !== null) return `/services/${encodeURIComponent(detailName)}`;
  const candidate = selectedServiceFromLocation();
  const service = /^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(candidate)
    ? candidate
    : "";
  return service === ""
    ? `/${section}`
    : `/${section}?service=${encodeURIComponent(service)}`;
}
function confirmDestructiveAction(message: string): boolean {
  return globalThis.confirm(message);
}
function withUnauthorizedSessionHandler(
  client: AdministrationClient,
  onUnauthorized: () => void,
): AdministrationClient {
  /* eslint-disable @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-unsafe-argument, @typescript-eslint/no-unsafe-return -- A Proxy preserves the complete AdministrationClient method interface. */
  return new Proxy(client, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver);
      if (typeof value !== "function") return value;
      return (...args: readonly unknown[]) =>
        Promise.resolve(Reflect.apply(value, target, args)).catch(
          (error: unknown) => {
            if (error instanceof AdministrationApiError && error.status === 401)
              onUnauthorized();
            throw error;
          },
        );
    },
  });
  /* eslint-enable @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-unsafe-argument, @typescript-eslint/no-unsafe-return */
}
function RouterDateTime({
  value,
}: {
  readonly value: string | null | undefined;
}) {
  return (
    <DateTime
      fallback={value == null ? "Never" : "Unavailable"}
      value={value}
    />
  );
}
function formText(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}
function tone(value: string): "green" | "amber" | "red" | "blue" {
  if (
    ["healthy", "succeeded", "enabled", "updated", "unchanged"].includes(value)
  )
    return "green";
  if (["degraded", "pending", "running", "missing"].includes(value))
    return "amber";
  if (["failed", "unavailable", "disabled"].includes(value)) return "red";
  return "blue";
}
const activityColumns: readonly DataTableColumn<ActivityEvent>[] = [
  {
    key: "time",
    header: "Time",
    width: "12rem",
    render: ({ row }) => <RouterDateTime value={row.occurred_at} />,
  },
  {
    key: "actor",
    header: "Actor",
    width: "24%",
    render: ({ row }) => row.actor_subject,
  },
  {
    key: "action",
    header: "Action",
    width: "18%",
    render: ({ row }) => row.action,
  },
  {
    key: "target",
    header: "Target",
    width: "30%",
    render: ({ row }) =>
      `${row.resource_type} · ${row.resource_api_name ?? row.resource_id ?? "Unavailable"}`,
  },
  {
    key: "result",
    header: "Result",
    width: "8rem",
    render: ({ row }) => (
      <StatusPill tone={tone(row.result)}>{row.result}</StatusPill>
    ),
  },
];
function LoadingPage({
  title = "Loading administration data",
}: {
  readonly title?: string;
}) {
  return (
    <StatePanel kind="loading" title={title}>
      Wait while the Router reads current state.
    </StatePanel>
  );
}
function SignIn({
  client,
  expired,
}: {
  readonly client: AdministrationClient;
  readonly expired: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  async function signIn() {
    setBusy(true);
    setFailure(null);
    try {
      globalThis.location.assign(await client.startSession(safeReturnPath()));
    } catch (error) {
      setFailure(errorMessage(error));
      setBusy(false);
    }
  }
  return (
    <SessionPage>
      <SessionCard
        actions={
          <Button disabled={busy} onClick={() => void signIn()}>
            {busy ? "Opening Pocket ID…" : "Continue with Pocket ID"}
          </Button>
        }
        description={
          expired
            ? "Your local administrator session expired. Sign in again."
            : "Use an allowlisted Pocket ID identity."
        }
        eyebrow="LLM Router administration"
        footer="A Pocket ID account does not give Router access. The subject must be on the deployment allowlist."
        icon={<Icon name="shield" size={25} />}
        title={expired ? "Your session expired" : "Administrator sign-in"}
        feedback={failure === null ? null : <p role="alert">{failure}</p>}
      />
    </SessionPage>
  );
}

function Overview({ resource }: { readonly resource: RouteSources }) {
  const { data, confirmed, failures, pending } = resource;
  const cooldowns = data.providerModels.filter(
    (item) =>
      item.cooldown != null &&
      Date.parse(item.cooldown.until) > resource.checkedAt,
  );
  const anyConfirmed = Object.values(confirmed).some(Boolean);
  return (
    <PageSurface className="administration-page">
      <PageHeading
        eyebrow="Global administration"
        title="Overview"
        actions={
          <RefreshAction
            label="Refresh overview"
            pending={pending}
            onRefresh={resource.load}
          />
        }
      />
      {pending && !anyConfirmed ? (
        <LoadingPage title="Loading Overview." />
      ) : null}
      {!pending && !anyConfirmed ? (
        <StatePanel kind="error" title="Overview is unavailable.">
          <RefreshAction
            label="Retry Overview"
            pending={pending}
            onRefresh={resource.load}
          />
        </StatePanel>
      ) : null}
      <SourceFailures resource={resource} retryLabel="Retry Overview" />
      {Object.values(resource.phases).includes("partial") ? (
        <StatePanel kind="empty" title="Overview is partial.">
          Some resource lists are incomplete. Refresh overview to try again.
        </StatePanel>
      ) : null}
      {anyConfirmed ? (
        <section className="resource-totals" aria-label="Resource totals">
          <StatCard
            icon={<Icon name="server" />}
            label="Services"
            value={
              confirmed.services ? String(data.services.length) : "Unavailable"
            }
          />
          <StatCard
            icon={<Icon name="cloud" />}
            label="Provider connections"
            value={
              confirmed.providers
                ? String(data.providers.length)
                : "Unavailable"
            }
          />
          <StatCard
            icon={<Icon name="spark" />}
            label="Provider-models"
            value={
              confirmed.providerModels
                ? String(data.providerModels.length)
                : "Unavailable"
            }
          />
          <StatCard
            icon={<Icon name="warning" />}
            label="Current cooldowns"
            value={
              confirmed.providerModels
                ? String(cooldowns.length)
                : "Unavailable"
            }
          />
        </section>
      ) : null}
      {data.health === null ? null : (
        <Panel>
          <PanelHeader
            description={
              <>
                Checked <RouterDateTime value={data.health.checked_at} />
              </>
            }
            title="Small health summary"
          />
          <ul className="health-list">
            {data.health.components.map((item) => (
              <li key={item.name}>
                <span>
                  <strong>{item.name.replaceAll("_", " ")}</strong>
                  {item.message == null ? null : <small>{item.message}</small>}
                </span>
                <StatusPill tone={tone(item.status)}>{item.status}</StatusPill>
              </li>
            ))}
          </ul>
        </Panel>
      )}
      {Object.keys(failures).length > 0 && anyConfirmed ? (
        <p role="status">
          Overview is partial or stale. Retry the failed summaries.
        </p>
      ) : null}
    </PageSurface>
  );
}

function RetentionDaysControl({ value }: { readonly value: number }) {
  const [input, setInput] = useReducer(
    (_current: number | "", next: number | "") => next,
    value,
  );
  return (
    <NumberControl
      label="Duration in whole days"
      max={30}
      min={1}
      name="days"
      onChange={(event) => {
        setInput(
          event.currentTarget.value === ""
            ? ""
            : event.currentTarget.valueAsNumber,
        );
      }}
      value={input}
    />
  );
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- This retained page coordinates health, retention writes, cooldowns, and one incremental activity walk; each shared panel and table owns its render behavior.
function OperationsPage({
  client,
  csrf,
  health,
  onNotice,
  onRefresh,
  providerModels,
  retentionDays,
  refreshPending,
  initialPhases,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly health: AdministratorHealth | null;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
  readonly providerModels: readonly ProviderModel[];
  readonly retentionDays: number | null;
  readonly refreshPending: boolean;
  readonly initialPhases: Readonly<
    Record<
      "health" | "retentionDays" | "providerModels",
      ConfigurationLoadPhase
    >
  >;
}) {
  const range = useMemo(() => isoRange(7), []);
  const [activityState, updateActivity] = useReducer(
    (
      current: {
        readonly confirmed: boolean;
        readonly items: readonly ActivityEvent[];
        readonly loadMoreFailure: string | null;
        readonly loadMorePending: boolean;
        readonly nextCursor: string | null;
        readonly phase: "loading" | "ready" | "error";
      },
      patch: Partial<typeof current>,
    ) => ({ ...current, ...patch }),
    {
      confirmed: false,
      items: [],
      loadMoreFailure: null,
      loadMorePending: false,
      nextCursor: null,
      phase: "loading",
    },
  );
  const {
    items: activity,
    loadMoreFailure: activityLoadMoreFailure,
    loadMorePending: activityLoadMorePending,
    nextCursor: activityNextCursor,
    phase: activityPhase,
  } = activityState;
  const activityLoadedPages = useRef(0);
  const activityLoadGuard = useRef(createScopeLoadGuard());
  const activityCursors = useRef(new Set<string>());
  const loadActivity = useCallback(() => {
    const generation = activityLoadGuard.current.begin();
    activityCursors.current.clear();
    activityLoadedPages.current = 0;
    updateActivity({
      loadMoreFailure: null,
      loadMorePending: false,
      nextCursor: null,
      phase: "loading",
    });
    return client
      .activityPage(range.from, range.to)
      .then((page) => {
        if (!activityLoadGuard.current.isCurrent(generation)) return;
        const items = mergeBoundedRows(
          [],
          page.items,
          (item) => item.id,
          "The activity list",
        );
        updateActivity({
          confirmed: true,
          items,
          nextCursor: continuedPageCursor(
            page,
            1,
            activityCursors.current,
            "The activity list",
          ),
          phase: "ready",
        });
        activityLoadedPages.current = 1;
      })
      .catch((error: unknown) => {
        if (!activityLoadGuard.current.isCurrent(generation)) return;
        updateActivity({ phase: "error" });
        onNotice(
          "error",
          error instanceof AdministrationApiError
            ? errorMessage(error)
            : error instanceof Error
              ? error.message
              : "The activity query is inconsistent.",
        );
      });
  }, [client, onNotice, range.from, range.to]);
  function loadMoreActivity(): Promise<void> {
    const cursor = activityNextCursor;
    if (cursor === null || activityLoadMorePending) return Promise.resolve();
    const generation = activityLoadGuard.current.begin();
    activityCursors.current.add(cursor);
    updateActivity({ loadMoreFailure: null, loadMorePending: true });
    return client
      .activityPage(range.from, range.to, cursor)
      .then((page) => {
        if (!activityLoadGuard.current.isCurrent(generation)) return;
        const items = mergeBoundedRows(
          activity,
          page.items,
          (item) => item.id,
          "The activity list",
        );
        updateActivity({
          items,
          loadMorePending: false,
          nextCursor: continuedPageCursor(
            page,
            activityLoadedPages.current + 1,
            activityCursors.current,
            "The activity list",
          ),
        });
        activityLoadedPages.current += 1;
      })
      .catch((error: unknown) => {
        if (!activityLoadGuard.current.isCurrent(generation)) return;
        updateActivity({
          loadMoreFailure:
            error instanceof AdministrationApiError
              ? errorMessage(error)
              : error instanceof Error
                ? error.message
                : "The activity page is inconsistent.",
          loadMorePending: false,
        });
      });
  }
  useEffect(() => {
    const loadGuard = activityLoadGuard.current;
    const timer = globalThis.setTimeout(() => {
      void loadActivity();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
      loadGuard.invalidate();
    };
  }, [loadActivity]);
  const cooldowns = providerModels.filter((item) => item.cooldown != null);
  async function saveRetention(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (retentionDays === null) {
      onNotice("error", "Retention is unavailable. Refresh and try again.");
      return;
    }
    const form = event.currentTarget;
    const days = Number(formText(new FormData(form), "days"));
    try {
      if (
        await updateRetentionDuration(
          retentionDays,
          days,
          confirmDestructiveAction,
          (value) => client.putRetention(value, csrf),
        )
      )
        await onRefresh();
    } catch (error) {
      onNotice("error", errorMessage(error));
    }
  }
  return (
    <PageSurface className="administration-page">
      <PageHeading
        eyebrow="Operations"
        title="Activity & health"
        actions={
          <RefreshAction
            label="Refresh operations"
            pending={refreshPending || activityPhase === "loading"}
            onRefresh={() =>
              Promise.all([onRefresh(), loadActivity()]).then(() => undefined)
            }
          />
        }
      />
      <div className="administration-sections">
        {health === null && initialPhases.health === "loading" ? (
          <LoadingPage title="Loading health." />
        ) : health === null ? (
          <StatePanel kind="error" title="Health unavailable">
            Refresh the administration data to try the health read again.
          </StatePanel>
        ) : (
          <Panel>
            <PanelHeader
              description={<RouterDateTime value={health.checked_at} />}
              title="Health components"
            />
            <ul className="health-list">
              {health.components.map((item) => (
                <li key={item.name}>
                  <span>
                    <strong>{item.name.replaceAll("_", " ")}</strong>
                    {item.message == null ? null : (
                      <small>{item.message}</small>
                    )}
                  </span>
                  <StatusPill tone={tone(item.status)}>
                    {item.status}
                  </StatusPill>
                </li>
              ))}
            </ul>
          </Panel>
        )}
        {retentionDays === null && initialPhases.retentionDays === "loading" ? (
          <LoadingPage title="Loading retention." />
        ) : retentionDays === null ? (
          <StatePanel kind="error" title="Retention unavailable">
            Refresh the administration data to try the retention read again.
          </StatePanel>
        ) : (
          <Panel>
            <PanelHeader
              description="The duration applies to detailed logs, activity, uploaded images, and retained generated media."
              title="Global retention"
            />
            <form
              className="administration-form retention-form"
              onSubmit={(event) => {
                void saveRetention(event);
              }}
            >
              <RetentionDaysControl key={retentionDays} value={retentionDays} />
              <Button type="submit">Save retention</Button>
            </form>
          </Panel>
        )}
      </div>
      <Panel>
        <PanelHeader
          description="Cooldowns are process-local best-effort state and can clear after a restart."
          title="Current provider-model cooldowns"
        />
        <ul className="record-list">
          {initialPhases.providerModels === "loading" ? (
            <li>Loading current cooldowns.</li>
          ) : initialPhases.providerModels === "error" ? (
            <li>Current cooldowns are unavailable.</li>
          ) : cooldowns.length === 0 ? (
            <li>No current cooldowns</li>
          ) : (
            cooldowns.map((item) => (
              <li key={item.api_name}>
                <span>
                  <strong>{item.api_name}</strong>
                  <small>
                    {item.cooldown?.reason} · until{" "}
                    <RouterDateTime value={item.cooldown?.until} />
                  </small>
                </span>
                <StatusPill tone="amber">cooldown</StatusPill>
              </li>
            ))
          )}
        </ul>
      </Panel>
      <Panel>
        <PanelHeader
          actions={
            <Button
              disabled={activityPhase === "loading"}
              onClick={() => void loadActivity()}
              variant="secondary"
            >
              Refresh activity
            </Button>
          }
          title="Configuration activity, last 7 days"
        />
        <DataTable
          ariaLabel="Configuration activity"
          className="administration-panel-table"
          columns={activityColumns}
          getRowId={(item) => item.id}
          getRowLabel={(item) =>
            `${item.action} for ${item.resource_api_name ?? item.resource_id ?? item.resource_type}`
          }
          liveMessage={`${String(activity.length)} activity records loaded.`}
          {...(activityPhase !== "ready" || activity.length === 0
            ? {}
            : {
                loadMore: {
                  completeLabel: "The retained activity range is complete",
                  ...(activityLoadMoreFailure === null
                    ? {}
                    : { error: activityLoadMoreFailure }),
                  hasMore: activityNextCursor !== null,
                  loadedLabel: `${String(activity.length)} activity records loaded`,
                  loading: activityLoadMorePending,
                  onLoadMore: loadMoreActivity,
                  onRetry: loadMoreActivity,
                },
              })}
          maxRows={administrationListMaximum}
          minimumWidth="56rem"
          rows={activity}
          state={
            activityPhase === "loading"
              ? { kind: "loading", message: "Loading retained activity" }
              : activityPhase === "error"
                ? {
                    kind: "error",
                    message: activityState.confirmed
                      ? "Retained activity is stale. Refresh activity to try again."
                      : "Retained activity is unavailable.",
                    onRetry: loadActivity,
                    retryLabel: "Try loading activity again",
                  }
                : activity.length === 0
                  ? {
                      kind: "empty",
                      message: "No retained activity is available.",
                    }
                  : {
                      kind: "ready",
                      message:
                        activityNextCursor === null
                          ? "The activity query is complete."
                          : "More retained activity is available.",
                    }
          }
        />
      </Panel>
    </PageSurface>
  );
}

export interface AppProps {
  readonly client?: AdministrationClient;
}
const defaultAdministrationClient = createAdministrationClient();

type Source = keyof AppData;
const overviewSources: readonly Source[] = [
  "services",
  "providers",
  "providerModels",
  "health",
];
const configurationSources: readonly Source[] = [
  "services",
  "providers",
  "models",
  "providerModels",
  "credentials",
];
const serviceSources: readonly Source[] = ["services"];
const operationSources: readonly Source[] = [
  "health",
  "retentionDays",
  "providerModels",
];
const sourceLabels: Record<Source, string> = {
  services: "Services",
  providers: "Provider connections",
  models: "Canonical models",
  providerModels: "Provider-models and current cooldowns",
  credentials: "Credentials",
  health: "Small health summary",
  retentionDays: "Retention",
};
const emptyData: AppData = {
  services: [],
  providers: [],
  models: [],
  providerModels: [],
  credentials: [],
  health: null,
  retentionDays: null,
};
interface RouteSourceState {
  readonly servicesReadVersion: number;
  readonly checkedAt: number;
  readonly data: AppData;
  readonly confirmed: Partial<Record<Source, boolean>>;
  readonly phases: Partial<Record<Source, ConfigurationLoadPhase>>;
  readonly failures: Partial<Record<Source, string>>;
  readonly pending: boolean;
}

/** Each mounted route owns its reads, confirmed values, and response generation. */
function useRouteSources(
  client: AdministrationClient,
  sources: readonly Source[],
  requireServiceRoot = false,
) {
  const [state, update] = useReducer(
    (
      current: RouteSourceState,
      patch: Partial<RouteSourceState>,
    ): RouteSourceState => ({ ...current, ...patch }),
    {
      servicesReadVersion: 0,
      checkedAt: 0,
      data: emptyData,
      confirmed: {},
      phases: {},
      failures: {},
      pending: true,
    },
  );
  const stateRef = useRef(state);
  useLayoutEffect(() => {
    stateRef.current = state;
  }, [state]);
  const guard = useRef(createScopeLoadGuard());
  const pending = useRef<Promise<void> | null>(null);
  const serviceCreation = useRef<{
    parent: Service;
    pending: boolean;
    deferred: RouteSourceState | null;
  } | null>(null);
  const onCreationEvent = useCallback((event: ServiceCreationEvent) => {
    const creation = serviceCreation.current;
    if (event.type === "opened") {
      // A pre-open list request cannot change this attempt or its eventual result.
      guard.current.invalidate();
      pending.current = null;
      serviceCreation.current = {
        parent: event.parent,
        pending: false,
        deferred: null,
      };
      const next = { ...stateRef.current, pending: false };
      stateRef.current = next;
      update(next);
      return;
    }
    if (event.type === "closed") {
      serviceCreation.current = null;
      return;
    }
    if (creation === null) return;
    if (event.type === "started") {
      creation.pending = true;
      return;
    }
    if (event.type === "succeeded") {
      guard.current.invalidate();
      pending.current = null;
      const previous = stateRef.current;
      const failures = { ...previous.failures };
      delete failures.services;
      const next: RouteSourceState = {
        ...previous,
        servicesReadVersion: previous.servicesReadVersion + 1,
        data: {
          ...previous.data,
          services: [
            ...previous.data.services.filter(
              (service) => service.api_name !== event.service.api_name,
            ),
            event.service,
          ],
        },
        confirmed: { ...previous.confirmed, services: true },
        phases: { ...previous.phases, services: "ready" },
        failures,
        pending: false,
      };
      serviceCreation.current = null;
      stateRef.current = next;
      update(next);
      return;
    }
    creation.pending = false;
    let next = creation.deferred ?? stateRef.current;
    creation.deferred = null;
    if (event.parentUnavailable) {
      guard.current.invalidate();
      pending.current = null;
      const removed = new Set([creation.parent.api_name]);
      for (let count = -1; count !== removed.size;) {
        count = removed.size;
        for (const service of next.data.services)
          if (removed.has(service.parent_service_api_name ?? ""))
            removed.add(service.api_name);
      }
      const services = next.data.services.filter(
        (service) => !removed.has(service.api_name),
      );
      next = {
        ...next,
        servicesReadVersion: next.servicesReadVersion + 1,
        data: { ...next.data, services },
        pending: false,
      };
    }
    stateRef.current = next;
    update(next);
  }, []);
  const load = useCallback((): Promise<void> => {
    if (pending.current !== null) return pending.current;
    const generation = guard.current.begin();
    update({ pending: true });
    async function read(source: Source) {
      if (source === "health")
        return { value: await client.health(), partial: false };
      if (source === "retentionDays")
        return {
          value: (await client.retention()).duration_days,
          partial: false,
        };
      if (source === "services" && requireServiceRoot) {
        const page = await client.services();
        if (
          !page.items.some(
            (service) =>
              service.api_name === "root" &&
              service.parent_service_api_name === null,
          )
        ) {
          throw new Error(
            "The permanent root service is unavailable. Retry services to continue.",
          );
        }
        return {
          value: page.items,
          partial: page.page.has_more || page.retrieval?.complete === false,
        };
      }
      const page = await client[source]();
      return {
        value: page.items,
        partial: page.page.has_more || page.retrieval?.complete === false,
      };
    }
    const request = Promise.allSettled(sources.map(read))
      .then((results) => {
        if (!guard.current.isCurrent(generation)) return;
        const previous = stateRef.current;
        let values = Object.fromEntries(
          results.flatMap((result, index) =>
            result.status === "fulfilled"
              ? [[sources[index], result.value.value]]
              : [],
          ),
        ) as Partial<AppData>;
        const confirmed = { ...previous.confirmed };
        const phases = { ...previous.phases };
        const failures: Partial<Record<Source, string>> = {};
        results.forEach((result, index) => {
          const source = sources[index];
          if (source === undefined) return;
          if (result.status === "fulfilled") {
            confirmed[source] = true;
            phases[source] = result.value.partial ? "partial" : "ready";
          } else {
            phases[source] = "error";
            const reason: unknown = result.reason;
            failures[source] = errorMessage(reason);
          }
        });
        // A partial list cannot establish that an existing service is absent.
        const servicesIndex = sources.indexOf("services");
        const serviceResult = results[servicesIndex];
        const completeServices =
          serviceResult?.status === "fulfilled" && !serviceResult.value.partial;
        if (
          requireServiceRoot &&
          values.services !== undefined &&
          !completeServices
        ) {
          values = {
            ...values,
            services: [
              ...previous.data.services.filter(
                (old) =>
                  !values.services?.some(
                    (service) => service.api_name === old.api_name,
                  ),
              ),
              ...values.services,
            ],
          };
        }
        const next: RouteSourceState = {
          servicesReadVersion:
            previous.servicesReadVersion + (completeServices ? 1 : 0),
          checkedAt: Date.now(),
          data: { ...previous.data, ...values },
          confirmed,
          phases,
          failures,
          pending: false,
        };
        const creation = serviceCreation.current;
        if (
          creation?.pending &&
          completeServices &&
          !next.data.services.some(
            (service) => service.api_name === creation.parent.api_name,
          )
        ) {
          creation.deferred = next;
          update({ pending: false });
          return;
        }
        if (creation !== null && completeServices) creation.deferred = null;
        stateRef.current = next;
        update(next);
      })
      .finally(() => {
        if (guard.current.isCurrent(generation)) pending.current = null;
      });
    pending.current = request;
    return request;
  }, [client, sources, requireServiceRoot]);
  useEffect(() => {
    const loadGuard = guard.current;
    const timer = globalThis.setTimeout(() => {
      void load();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
      loadGuard.invalidate();
      pending.current = null;
    };
  }, [load]);
  return { ...state, load, onCreationEvent };
}

type RouteSources = ReturnType<typeof useRouteSources>;
function RefreshAction({
  label,
  pending,
  onRefresh,
}: {
  readonly label: string;
  readonly pending: boolean;
  readonly onRefresh: () => Promise<void>;
}) {
  return (
    <Button
      aria-busy={pending}
      aria-disabled={pending}
      onClick={() => {
        if (!pending) void onRefresh();
      }}
      variant="secondary"
    >
      <Icon name="refresh" size={16} />
      {label}
    </Button>
  );
}
function SourceFailures({
  resource,
  retryLabel,
  graph = false,
}: {
  readonly resource: RouteSources;
  readonly retryLabel: string;
  readonly graph?: boolean;
}) {
  return (
    <>
      {Object.entries(resource.failures).map(([name, message]) => {
        const source = name as Source;
        if (graph)
          return (
            <InlineAlert
              key={source}
              tone="error"
              title={`${sourceLabels[source]} ${resource.confirmed[source] ? "is stale." : "is unavailable."}`}
              actions={
                <RefreshAction
                  label={retryLabel}
                  pending={resource.pending}
                  onRefresh={resource.load}
                />
              }
            >
              {message}
            </InlineAlert>
          );
        return (
          <StatePanel
            key={source}
            kind="error"
            title={`${sourceLabels[source]} ${resource.confirmed[source] ? "is stale." : "is unavailable."}`}
          >
            {message}{" "}
            <RefreshAction
              label={retryLabel}
              pending={resource.pending}
              onRefresh={resource.load}
            />
          </StatePanel>
        );
      })}
    </>
  );
}
function resourcePhase(
  resource: RouteSources,
  sources: readonly Source[],
): ConfigurationLoadPhase {
  if (sources.some((source) => resource.phases[source] === "error"))
    return "error";
  if (sources.some((source) => !resource.confirmed[source])) return "loading";
  if (sources.some((source) => resource.phases[source] === "partial"))
    return "partial";
  return "ready";
}

interface TreeRestore {
  readonly mode: "history" | "fallback" | "deleted";
  readonly left: number;
  readonly top: number;
}
interface NavigationState {
  readonly treeRestore?: TreeRestore;
  readonly sourceTree?: string;
}
function detailServiceName(path: string): string | null {
  const match = /^\/services\/([^/]+)$/.exec(path);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1] ?? "");
  } catch {
    return match[1] ?? "";
  }
}
interface RouteLocation extends NavigationState {
  readonly entry?: number;
  readonly pathname: string;
  readonly search: string;
}
function readLocation(): RouteLocation {
  return typeof location === "undefined"
    ? { entry: 0, pathname: "/overview", search: "" }
    : {
        entry: 0,
        pathname: location.pathname,
        search: location.search,
        ...readNavigationState(),
      };
}
function readNavigationState(): NavigationState {
  const value: unknown = globalThis.history.state;
  if (value === null || typeof value !== "object") return {};
  const result: { treeRestore?: TreeRestore; sourceTree?: string } = {};
  if (
    "sourceTree" in value &&
    typeof value.sourceTree === "string" &&
    /^\/services(?:\?[^#]*)?$/.test(value.sourceTree)
  )
    result.sourceTree = value.sourceTree;
  if (
    "treeRestore" in value &&
    value.treeRestore !== null &&
    typeof value.treeRestore === "object"
  ) {
    const tree = value.treeRestore;
    if (
      "mode" in tree &&
      (tree.mode === "history" ||
        tree.mode === "fallback" ||
        tree.mode === "deleted") &&
      "left" in tree &&
      typeof tree.left === "number" &&
      Number.isFinite(tree.left) &&
      "top" in tree &&
      typeof tree.top === "number" &&
      Number.isFinite(tree.top)
    )
      result.treeRestore = {
        mode: tree.mode,
        left: Math.max(0, tree.left),
        top: Math.max(0, tree.top),
      };
  }
  return result;
}
function normalizedLocation(): RouteLocation {
  const current = readLocation();
  const pathname =
    current.pathname === "/"
      ? "/overview"
      : current.pathname === "/access"
        ? "/services"
        : legacyConfigurationPaths.has(current.pathname.slice(1))
          ? "/configuration"
          : current.pathname;
  const query = new URLSearchParams(current.search);
  if (pathname !== "/services" && pathname !== "/configuration")
    query.delete("service");
  const search = query.size === 0 ? "" : `?${query.toString()}`;
  if (
    typeof location !== "undefined" &&
    (pathname !== current.pathname || search !== current.search)
  )
    globalThis.history.replaceState(
      globalThis.history.state,
      "",
      `${pathname}${search}`,
    );
  return { ...current, pathname, search };
}
function sectionForPath(pathname: string): Section {
  if (detailServiceName(pathname) !== null) return "services";
  const value = pathname.slice(1);
  return routes.find((route) => route.id === value)?.id ?? "overview";
}
function destinationPath(id: Section, service: string): string {
  return `/${id}${service !== "" && (id === "services" || id === "configuration") ? `?service=${encodeURIComponent(service)}` : ""}`;
}
interface RouteProps {
  readonly client: AdministrationClient;
  readonly session: AdministratorSession;
  readonly location: RouteLocation;
  readonly navigate: (
    path: string,
    replace?: boolean,
    state?: NavigationState,
  ) => void;
  readonly canNavigate: () => boolean;
  readonly registerNavigationGuard: (
    guard: (intent?: "unload") => boolean,
  ) => () => void;
}
function useRouteNotice() {
  const [notice, setNotice] = useState<Notice | null>(null);
  const notify = useCallback((tone: "success" | "error", message: string) => {
    setNotice({ tone, message });
  }, []);
  return {
    notice,
    notify,
    dismiss: () => {
      setNotice(null);
    },
  };
}
function AuthenticatedAdministration({
  children,
  location: routeLocation,
  session,
  client,
  navigate,
  canNavigate,
  destinations = routes.map((route) => ({ ...route, href: `/${route.id}` })),
  notice,
  onDismissNotice,
  notify,
}: {
  readonly children: ReactNode;
  readonly location: RouteLocation;
  readonly session: AdministratorSession;
  readonly client: AdministrationClient;
  readonly navigate: (path: string) => void;
  readonly canNavigate: () => boolean;
  readonly destinations?: readonly ((typeof routes)[number] & {
    readonly href: string;
  })[];
  readonly notice?: Notice | null;
  readonly onDismissNotice?: () => void;
  readonly notify: (tone: "success" | "error", message: string) => void;
}) {
  const section = sectionForPath(routeLocation.pathname);
  const graphPage =
    routeLocation.pathname === "/services" ||
    routeLocation.pathname === "/configuration";
  useLayoutEffect(() => {
    const heading = document.querySelector<HTMLElement>("#main-content h1");
    if (heading) {
      heading.tabIndex = -1;
      document.title = `${heading.textContent} · LLM Router`;
      heading.focus({ preventScroll: true });
    }
  }, [routeLocation.pathname, routeLocation.entry]);
  const accountActions = (
    <Button
      onClick={() => {
        if (!canNavigate()) return;
        void client
          .logout(session.csrf_token)
          .then(() => {
            globalThis.location.reload();
          })
          .catch((error: unknown) => {
            notify("error", errorMessage(error));
          });
      }}
      variant="quiet"
    >
      <Icon name="logout" size={16} /> Sign out
    </Button>
  );
  const items = destinations.map((route) => ({
    ...route,
    icon: <Icon name={route.icon} />,
    active: route.id === section,
  }));
  return (
    <>
      <SkipLink
        href="#main-content"
        label="Skip to content"
        onClick={(event) => {
          if (
            event.button !== 0 ||
            event.metaKey ||
            event.ctrlKey ||
            event.shiftKey ||
            event.altKey
          )
            return;
          event.preventDefault();
          document.querySelector<HTMLElement>("#main-content h1")?.focus();
        }}
      />
      <ApplicationShell
        mainProps={{
          id: "main-content",
          tabIndex: -1,
          ...(graphPage
            ? {
                className: "administration-graph-main",
                "aria-labelledby": "graph-page-heading",
              }
            : {}),
        }}
        sidebar={
          <ApplicationSidebar
            brand={
              <div className="application-brand">
                <span>
                  <Icon name="spark" size={19} />
                </span>
                <strong>LLM Router</strong>
              </div>
            }
            context={<p>Global administrator</p>}
            footer={
              <>
                <AccountMenu
                  avatar={session.display_name.slice(0, 2).toUpperCase()}
                  name={session.display_name}
                  detail={
                    <>
                      Expires <RouterDateTime value={session.expires_at} />
                    </>
                  }
                />
                {accountActions}
              </>
            }
            navigation={
              <ApplicationNavigation aria-label="Administration navigation">
                {(["Manage", "Observe"] as const).map((group) => (
                  <ApplicationNavigationGroup key={group} label={group}>
                    {destinations.map((route) =>
                      route.group === group ? (
                        <NavigationLink
                          active={route.id === section}
                          href={route.href}
                          icon={<Icon name={route.icon} size={17} />}
                          key={route.id}
                          label={route.label}
                          onClick={(event) => {
                            if (
                              event.button !== 0 ||
                              event.metaKey ||
                              event.ctrlKey ||
                              event.shiftKey ||
                              event.altKey
                            )
                              return;
                            event.preventDefault();
                            navigate(route.href);
                          }}
                        />
                      ) : null,
                    )}
                  </ApplicationNavigationGroup>
                ))}
              </ApplicationNavigation>
            }
          />
        }
        mobileNavigation={
          <MobileNavigation
            aria-label="Mobile administration navigation"
            items={items.slice(0, 1)}
            onNavigate={(item, event) => {
              event.preventDefault();
              navigate(item.href);
            }}
            surface={{
              label: "Navigation",
              icon: <Icon name="menu" />,
              applicationName: "LLM Router",
              context: { label: "Administrator", value: session.display_name },
              accountActions,
              closeLabel: "Close navigation",
              items,
            }}
          />
        }
      >
        <div className="administration-content">
          <ShellErrorBoundary
            fallbackMessage="Reload this route to try again."
            fallbackTitle="The administration interface stopped"
            resetKey={routeLocation.pathname}
          >
            {children}
          </ShellErrorBoundary>
        </div>
        {notice == null ? null : (
          <Toast
            className={`notice-${notice.tone}`}
            role={notice.tone === "error" ? "alert" : "status"}
            {...(onDismissNotice === undefined
              ? {}
              : { onDismiss: onDismissNotice })}
          >
            {notice.message}
          </Toast>
        )}
      </ApplicationShell>
    </>
  );
}
function OverviewRoute(props: RouteProps) {
  const resource = useRouteSources(props.client, overviewSources);
  const notices = useRouteNotice();
  return (
    <AuthenticatedAdministration
      {...props}
      {...notices}
      onDismissNotice={notices.dismiss}
    >
      <Overview resource={resource} />
    </AuthenticatedAdministration>
  );
}
function useServiceContext(props: RouteProps, resource: RouteSources) {
  const { location, navigate } = props;
  const query = new URLSearchParams(location.search);
  const serviceCount = query.getAll("service").length;
  const received = query.get("service") ?? "";
  const selectedService = resource.data.services.some(
    (service) => service.api_name === received,
  )
    ? received
    : location.pathname === "/services"
      ? (resource.data.services.find((service) => service.api_name === "root")
          ?.api_name ??
        resource.data.services[0]?.api_name ??
        "")
      : "";
  const replaceService = useCallback(
    (value: string) => {
      const next = new URLSearchParams(location.search);
      next.delete("service");
      if (value !== "") next.set("service", value);
      navigate(
        `${location.pathname}${next.size === 0 ? "" : `?${next.toString()}`}`,
        true,
      );
    },
    [location.pathname, location.search, navigate],
  );
  useEffect(() => {
    if (
      resource.confirmed.services &&
      (received !== selectedService || serviceCount > 1)
    )
      replaceService(selectedService);
  }, [
    received,
    replaceService,
    resource.confirmed.services,
    selectedService,
    serviceCount,
  ]);
  return {
    selectedService,
    replaceService,
    destinations: routes.map((route) => ({
      ...route,
      href: destinationPath(route.id, selectedService),
    })),
  };
}
function ServicesRoute(props: RouteProps) {
  const resource = useRouteSources(props.client, serviceSources, true);
  const context = useServiceContext(props, resource);
  const notices = useRouteNotice();
  const treeScroll = useRef({ left: 0, top: 0 });
  return (
    <AuthenticatedAdministration
      {...props}
      {...notices}
      destinations={context.destinations}
      onDismissNotice={notices.dismiss}
    >
      <h1 className="od-visually-hidden" id="graph-page-heading" tabIndex={-1}>
        Services
      </h1>
      <ServiceManagement
        initialSelectionRequested={resource.data.services.some(
          (service) =>
            service.api_name ===
            new URLSearchParams(props.location.search).get("service"),
        )}
        onCreationEvent={resource.onCreationEvent}
        servicesReadVersion={resource.servicesReadVersion}
        {...(props.location.treeRestore === undefined
          ? {}
          : { restoreTree: props.location.treeRestore })}
        onTreeScroll={(position) => {
          treeScroll.current = position;
        }}
        onOpenDetails={(name) => {
          if (!props.canNavigate()) return;
          const source = `/services?service=${encodeURIComponent(name)}`;
          props.navigate(source, true, {
            treeRestore: { mode: "history", ...treeScroll.current },
          });
          props.navigate(`/services/${encodeURIComponent(name)}`, false, {
            sourceTree: source,
          });
        }}
        available={resource.confirmed.services === true}
        stateContent={
          resource.confirmed.services &&
          Object.keys(resource.failures).length > 0 ? (
            <SourceFailures
              graph
              resource={resource}
              retryLabel="Retry services"
            />
          ) : undefined
        }
        initialState={
          <GraphEmptyState
            icon={<Icon name={resource.pending ? "layers" : "warning"} />}
            title={
              resource.pending
                ? "Loading Services."
                : "Services are unavailable."
            }
            description={
              resource.pending
                ? "Wait while the Router reads the service tree."
                : (resource.failures.services ??
                  "The permanent root service is unavailable. Retry services to continue.")
            }
            role={resource.pending ? "status" : "alert"}
            actions={
              resource.pending &&
              resource.failures.services === undefined ? undefined : (
                <Button
                  aria-busy={resource.pending}
                  aria-disabled={resource.pending}
                  onClick={() => {
                    if (!resource.pending) void resource.load();
                  }}
                >
                  Retry services
                </Button>
              )
            }
          />
        }
        registerNavigationGuard={props.registerNavigationGuard}
        client={props.client}
        csrf={props.session.csrf_token}
        onNotice={notices.notify}
        onRefresh={resource.load}
        onSelect={context.replaceService}
        selectedService={context.selectedService}
        services={resource.data.services}
        graphActions={
          <RefreshAction
            label="Refresh services"
            pending={resource.pending}
            onRefresh={resource.load}
          />
        }
      />
    </AuthenticatedAdministration>
  );
}
function ServiceDetailsRoute(
  props: RouteProps & { readonly serviceApiName: string },
) {
  const notices = useRouteNotice();
  const [context, setContext] = useState("");
  return (
    <AuthenticatedAdministration
      {...props}
      {...notices}
      onDismissNotice={notices.dismiss}
      destinations={routes.map((route) => ({
        ...route,
        href: destinationPath(route.id, context),
      }))}
    >
      <ServiceDetails
        client={props.client}
        csrf={props.session.csrf_token}
        serviceApiName={props.serviceApiName}
        onContext={setContext}
        onNotice={notices.notify}
        registerNavigationGuard={props.registerNavigationGuard}
        onBack={(unavailable) => {
          if (!unavailable && props.location.sourceTree !== undefined) {
            globalThis.history.back();
            return;
          }
          if (!props.canNavigate()) return;
          props.navigate(
            unavailable
              ? "/services"
              : `/services?service=${encodeURIComponent(props.serviceApiName)}`,
            true,
            {
              treeRestore: {
                mode: unavailable ? "deleted" : "fallback",
                left: 0,
                top: 0,
              },
            },
          );
        }}
        onDeleted={() => {
          props.navigate("/services", true, {
            treeRestore: { mode: "deleted", left: 0, top: 0 },
          });
        }}
      />
    </AuthenticatedAdministration>
  );
}
function useAssignments(client: AdministrationClient, service: string) {
  const [state, update] = useReducer(
    (
      current: {
        readonly service: string;
        readonly items: readonly Assignment[];
        readonly phase: ConfigurationLoadPhase;
        readonly pending: boolean;
        readonly failure: string | null;
      },
      patch: Partial<typeof current>,
    ) => ({
      ...current,
      ...(patch.service !== undefined && patch.service !== current.service
        ? { items: [], phase: "loading" as const }
        : {}),
      ...patch,
    }),
    {
      service,
      items: [],
      phase: service === "" ? "ready" : "loading",
      pending: false,
      failure: null,
    },
  );
  const guard = useRef(createScopeLoadGuard());
  const pending = useRef<Promise<void> | null>(null);
  const load = useCallback((): Promise<void> => {
    if (pending.current) return pending.current;
    const generation = guard.current.begin();
    if (service === "") {
      update({
        service,
        items: [],
        phase: "ready",
        pending: false,
        failure: null,
      });
      return Promise.resolve();
    }
    update({ service, pending: true, failure: null });
    const request = client
      .assignments(service)
      .then((page) => {
        if (guard.current.isCurrent(generation))
          update({
            service,
            items: page.items,
            phase:
              page.page.has_more || page.retrieval?.complete === false
                ? "partial"
                : "ready",
            pending: false,
          });
      })
      .catch((error: unknown) => {
        if (guard.current.isCurrent(generation))
          update({
            service,
            phase: "error",
            pending: false,
            failure: errorMessage(error),
          });
      })
      .finally(() => {
        if (guard.current.isCurrent(generation)) pending.current = null;
      });
    pending.current = request;
    return request;
  }, [client, service]);
  useLayoutEffect(() => {
    const loadGuard = guard.current;
    const timer = globalThis.setTimeout(() => {
      void load();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
      loadGuard.invalidate();
      pending.current = null;
    };
  }, [load]);
  return {
    items: state.service === service ? state.items : [],
    phase:
      state.service === service
        ? state.phase
        : service === ""
          ? ("ready" as const)
          : ("loading" as const),
    pending: state.service !== service || state.pending,
    failure: state.service === service ? state.failure : null,
    load,
  };
}
function ConfigurationRoute(props: RouteProps) {
  const resource = useRouteSources(props.client, configurationSources);
  const { selectedService, replaceService, destinations } = useServiceContext(
    props,
    resource,
  );
  const assignments = useAssignments(props.client, selectedService);
  const notices = useRouteNotice();
  const assignmentDirty = useRef(false);
  const [assignmentPending, setAssignmentPending] = useState(false);
  const [pendingService, setPendingService] = useState<string | null>(null);
  const serviceChangeReturnFocusRef = useRef<HTMLElement | null>(null);
  function selectService(value: string, trigger: HTMLElement) {
    if (value === selectedService) return;
    if (assignmentPending) {
      notices.notify(
        "error",
        "Wait for the selected service assignment write to finish.",
      );
      return;
    }
    if (assignmentDirty.current) {
      serviceChangeReturnFocusRef.current = trigger;
      setPendingService(value);
      return;
    }
    replaceService(value);
  }
  const { registerNavigationGuard } = props;
  const { notify } = notices;
  useLayoutEffect(
    () =>
      registerNavigationGuard(() => {
        if (!assignmentPending && !assignmentDirty.current) return true;
        notify(
          "error",
          assignmentPending
            ? "Wait for the selected service assignment write to finish."
            : "Close the assignment form and confirm that you want to discard its changes before you leave this page.",
        );
        return false;
      }),
    [assignmentPending, notify, registerNavigationGuard],
  );
  const refresh = () =>
    Promise.all([resource.load(), assignments.load()]).then(() => undefined);
  return (
    <AuthenticatedAdministration
      {...props}
      {...notices}
      destinations={destinations}
      onDismissNotice={notices.dismiss}
    >
      <h1 className="od-visually-hidden" id="graph-page-heading" tabIndex={-1}>
        LLM configuration
      </h1>
      <PageSurface edgeToEdge>
        <ConfigurationGraph
          stateContent={
            Object.keys(resource.failures).length > 0 ||
            assignments.failure !== null ? (
              <>
                <SourceFailures
                  resource={resource}
                  graph
                  retryLabel="Retry configuration"
                />
                {assignments.failure === null ? null : (
                  <InlineAlert
                    tone="error"
                    title="Assignments are stale or unavailable."
                  >
                    {assignments.failure}
                  </InlineAlert>
                )}
              </>
            ) : undefined
          }
          assignmentPhase={assignments.phase}
          assignments={assignments.items}
          catalogPhase={resourcePhase(resource, ["models", "providerModels"])}
          client={props.client}
          credentials={resource.data.credentials}
          csrf={props.session.csrf_token}
          globalPhase={resourcePhase(resource, configurationSources)}
          models={resource.data.models}
          onAssignmentDirtyChange={(dirty) => {
            assignmentDirty.current = dirty;
          }}
          onAssignmentPendingChange={setAssignmentPending}
          onNotice={notices.notify}
          onRefreshAssignments={assignments.load}
          onRefreshGlobal={resource.load}
          providerModels={resource.data.providerModels}
          providerPhase={resourcePhase(resource, ["providers", "credentials"])}
          providers={resource.data.providers}
          selectedService={selectedService}
          services={resource.data.services}
          toolbar={{
            leading: (
              <SelectControl
                aria-label="Service context"
                label="Service context"
                disabled={assignmentPending}
                onChange={(event) => {
                  selectService(event.currentTarget.value, event.currentTarget);
                }}
                value={selectedService}
              >
                <option value="">All services</option>
                {resource.data.services.map((service) => (
                  <option key={service.api_name} value={service.api_name}>
                    {service.display_name}
                  </option>
                ))}
              </SelectControl>
            ),
            actions: (
              <RefreshAction
                label="Refresh configuration"
                pending={
                  resource.pending || assignments.pending || assignmentPending
                }
                onRefresh={refresh}
              />
            ),
          }}
        />
      </PageSurface>
      <ConfirmationDialog
        confirmLabel="Discard and change service"
        description="The open assignment form has unsaved values. The service change closes that form and replaces only the assignment column."
        impactStatement={`discard assignment changes for ${selectedService || "the selected service"}`}
        onCancel={() => {
          setPendingService(null);
        }}
        onConfirm={() => {
          if (pendingService !== null) replaceService(pendingService);
          setPendingService(null);
          assignmentDirty.current = false;
        }}
        open={pendingService !== null}
        pending={assignmentPending}
        returnFocusRef={serviceChangeReturnFocusRef}
        title="Discard assignment changes?"
      />
    </AuthenticatedAdministration>
  );
}
function LogsRoute(props: RouteProps) {
  const notices = useRouteNotice();
  return (
    <AuthenticatedAdministration
      {...props}
      {...notices}
      onDismissNotice={notices.dismiss}
    >
      <LogsPage client={props.client} />
    </AuthenticatedAdministration>
  );
}
function StatisticsRoute(props: RouteProps) {
  const resource = useRouteSources(props.client, serviceSources);
  const notices = useRouteNotice();
  return (
    <AuthenticatedAdministration
      {...props}
      {...notices}
      onDismissNotice={notices.dismiss}
    >
      <StatisticsPage client={props.client} services={resource.data.services} />
      <SourceFailures resource={resource} retryLabel="Retry service filters" />
    </AuthenticatedAdministration>
  );
}
function OperationsRoute(props: RouteProps) {
  const resource = useRouteSources(props.client, operationSources);
  const notices = useRouteNotice();
  return (
    <AuthenticatedAdministration
      {...props}
      {...notices}
      onDismissNotice={notices.dismiss}
    >
      <OperationsPage
        client={props.client}
        csrf={props.session.csrf_token}
        health={resource.data.health}
        onNotice={notices.notify}
        onRefresh={resource.load}
        providerModels={resource.data.providerModels}
        retentionDays={resource.data.retentionDays}
        refreshPending={resource.pending}
        initialPhases={{
          health: resource.confirmed.health
            ? "ready"
            : resourcePhase(resource, ["health"]),
          retentionDays: resource.confirmed.retentionDays
            ? "ready"
            : resourcePhase(resource, ["retentionDays"]),
          providerModels: resource.confirmed.providerModels
            ? "ready"
            : resourcePhase(resource, ["providerModels"]),
        }}
      />
      <SourceFailures resource={resource} retryLabel="Retry operations" />
    </AuthenticatedAdministration>
  );
}

interface SessionState {
  readonly status:
    "loading" | "active" | "signed-out" | "expired" | "denied" | "failed";
  readonly session?: AdministratorSession;
  readonly message?: string;
}
export function App({ client = defaultAdministrationClient }: AppProps) {
  const [sessionState, setSessionState] = useState<SessionState>({
    status: "loading",
  });
  const [location, setLocation] = useState(readLocation);
  const navigationGuard = useRef<((intent?: "unload") => boolean) | null>(null);
  const historyIndex = useRef(0);
  const restoringHistory = useRef(false);
  const registerNavigationGuard = useCallback(
    (guard: (intent?: "unload") => boolean) => {
      navigationGuard.current = guard;
      return () => {
        if (navigationGuard.current === guard) navigationGuard.current = null;
      };
    },
    [],
  );
  const canNavigate = useCallback(() => {
    if (restoringHistory.current) return false;
    if (navigationGuard.current?.() !== false) return true;
    document
      .querySelector<HTMLElement>("#main-content h1")
      ?.focus({ preventScroll: true });
    return false;
  }, []);
  const expireAdministratorSession = useCallback(() => {
    setSessionState({ status: "expired" });
  }, []);
  const authenticatedClient = useMemo(
    () => withUnauthorizedSessionHandler(client, expireAdministratorSession),
    [client, expireAdministratorSession],
  );
  const inspectSession = useCallback(async () => {
    try {
      const session = await client.session();
      const current = normalizedLocation();
      const state: unknown = globalThis.history.state;
      historyIndex.current =
        state !== null &&
        typeof state === "object" &&
        "routerIndex" in state &&
        typeof state.routerIndex === "number"
          ? state.routerIndex
          : 0;
      globalThis.history.replaceState(
        { routerIndex: historyIndex.current, ...readNavigationState() },
        "",
        `${current.pathname}${current.search}`,
      );
      setLocation(current);
      setSessionState({ status: "active", session });
    } catch (error) {
      setSessionState({
        status:
          error instanceof AdministrationApiError && error.status === 403
            ? "denied"
            : error instanceof AdministrationApiError && error.status === 401
              ? "signed-out"
              : "failed",
        message: errorMessage(error),
      });
    }
  }, [client]);
  useEffect(() => {
    const timer = globalThis.setTimeout(() => {
      void inspectSession();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
    };
  }, [inspectSession]);
  const expiry = sessionState.session?.expires_at;
  useEffect(() => {
    if (sessionState.status === "active" && expiry !== undefined)
      return scheduleSessionExpiry(expiry, expireAdministratorSession);
    return undefined;
  }, [expiry, expireAdministratorSession, sessionState.status]);
  useEffect(() => {
    const restore = (event: PopStateEvent) => {
      const state: unknown = event.state;
      const targetIndex =
        state !== null &&
        typeof state === "object" &&
        "routerIndex" in state &&
        typeof state.routerIndex === "number"
          ? state.routerIndex
          : null;
      if (restoringHistory.current) {
        restoringHistory.current = false;
        return;
      }
      if (!canNavigate() && targetIndex !== null) {
        const delta = historyIndex.current - targetIndex;
        if (delta !== 0) {
          restoringHistory.current = true;
          globalThis.history.go(delta);
        }
        return;
      }
      historyIndex.current = targetIndex ?? 0;
      const next = normalizedLocation();
      setLocation((current) => ({ ...next, entry: (current.entry ?? 0) + 1 }));
    };
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (navigationGuard.current?.("unload") === false) {
        event.preventDefault();
      }
    };
    globalThis.addEventListener("popstate", restore);
    globalThis.addEventListener("beforeunload", beforeUnload);
    return () => {
      globalThis.removeEventListener("popstate", restore);
      globalThis.removeEventListener("beforeunload", beforeUnload);
    };
  }, [canNavigate]);
  const navigate = useCallback(
    (path: string, replace = false, state: NavigationState = {}) => {
      if (!replace && !canNavigate()) return;
      if (!replace) historyIndex.current += 1;
      globalThis.history[replace ? "replaceState" : "pushState"](
        { routerIndex: historyIndex.current, ...state },
        "",
        path,
      );
      const next = normalizedLocation();
      setLocation((current) => ({
        ...next,
        entry: (current.entry ?? 0) + (replace ? 0 : 1),
      }));
      if (!replace) globalThis.scrollTo({ behavior: "auto", left: 0, top: 0 });
    },
    [canNavigate],
  );
  if (sessionState.status === "loading")
    return (
      <SessionPage>
        <LoadingPage title="Checking the administrator session" />
      </SessionPage>
    );
  if (sessionState.status === "signed-out" || sessionState.status === "expired")
    return (
      <SignIn client={client} expired={sessionState.status === "expired"} />
    );
  if (sessionState.status === "failed")
    return (
      <SessionPage>
        <SessionCard
          actions={
            <Button
              onClick={() => {
                setSessionState({ status: "loading" });
                void inspectSession();
              }}
            >
              Try again
            </Button>
          }
          description={
            sessionState.message ??
            "The Router could not check the administrator session."
          }
          eyebrow="Session check"
          icon={<Icon name="warning" size={25} />}
          title="The session status is unavailable"
        />
      </SessionPage>
    );
  if (sessionState.status === "denied")
    return (
      <SessionPage>
        <SessionCard
          actions={
            <Button
              onClick={() => {
                setSessionState({ status: "signed-out" });
              }}
            >
              Return to sign-in
            </Button>
          }
          description={
            sessionState.message ??
            "This Pocket ID subject is not allowed to administer the Router."
          }
          eyebrow="Access denied"
          icon={<Icon name="lock" size={25} />}
          title="Administrator access is denied"
        />
      </SessionPage>
    );
  if (sessionState.session === undefined) return null;
  const props: RouteProps = {
    client: authenticatedClient,
    session: sessionState.session,
    location,
    navigate,
    canNavigate,
    registerNavigationGuard,
  };
  const section = sectionForPath(location.pathname);
  const serviceApiName = detailServiceName(location.pathname);
  if (serviceApiName !== null)
    return (
      <ServiceDetailsRoute
        key={serviceApiName}
        {...props}
        serviceApiName={serviceApiName}
      />
    );
  if (section === "services") return <ServicesRoute {...props} />;
  if (section === "configuration") return <ConfigurationRoute {...props} />;
  if (section === "logs") return <LogsRoute {...props} />;
  if (section === "statistics") return <StatisticsRoute {...props} />;
  if (section === "operations") return <OperationsRoute {...props} />;
  return <OverviewRoute {...props} />;
}
