import {
  useCallback,
  useEffect,
  useEffectEvent,
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
  ApplicationTopbar,
  Button,
  ConfirmationDialog,
  DataTable,
  Icon,
  MobileNavigation,
  NavigationItem,
  PageHeading,
  PageSurface,
  Panel,
  PanelHeader,
  SessionCard,
  SessionPage,
  ShellErrorBoundary,
  StatCard,
  StatePanel,
  StatusPill,
  Toast,
  WorkspaceSelector,
  type DataTableAction,
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
  type LogMedia,
  type Provider,
  type ProviderModel,
  type RequestLog,
  type RequestLogSummary,
  type Service,
  type StatisticsBucket,
  type StatisticsResult,
} from "./api.js";
import { ServiceManagement } from "./ServiceManagement.js";
import { ConfigurationGraph } from "./ConfigurationGraph.js";
import { createScopeLoadGuard } from "./accessState.js";
import {
  expireAdministratorSessionLoads,
  invalidateRetainedMediaLoad,
  updateRetentionDuration,
} from "./administrationSafety.js";
import { scheduleSessionExpiry } from "./sessionExpiry.js";
import {
  requestLogActorLabel,
  requestLogRouteLabel,
  requestLogScopeLabel,
} from "./logPresentation.js";

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
  readonly health: AdministratorHealth;
  readonly retentionDays: number;
  readonly configurationPhase: "ready" | "partial";
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
  { id: "logs", label: "Detailed logs", icon: "list", group: "Observe" },
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
  if (value === "access") return "services";
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
function displayTime(value: string | null | undefined): string {
  if (value == null) return "Never";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "Unavailable"
    : parsed.toLocaleString();
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
const STATISTICS_GROUP_MAXIMUM = 1_000;

function usageLabel(item: StatisticsBucket): string {
  return (
    item.units.map((unit) => `${unit.unit} ${unit.quantity}`).join(", ") ||
    "None"
  );
}

const requestLogColumns: readonly DataTableColumn<RequestLogSummary>[] = [
  {
    key: "started",
    header: "Started",
    width: "12rem",
    render: ({ row }) => displayTime(row.started_at),
  },
  {
    key: "actor",
    header: "Actor",
    width: "8rem",
    render: ({ row }) => requestLogActorLabel(row),
  },
  {
    key: "scope",
    header: "Scope",
    width: "18%",
    render: ({ row }) => requestLogScopeLabel(row),
  },
  {
    key: "kind",
    header: "Kind",
    width: "7rem",
    render: ({ row }) => row.kind,
  },
  {
    key: "route",
    header: "Route",
    width: "20%",
    render: ({ row }) => requestLogRouteLabel(row),
  },
  {
    key: "tags",
    header: "Tags",
    width: "20%",
    render: ({ row }) =>
      row.tags == null || row.tags.length === 0 ? "None" : row.tags.join(", "),
  },
  {
    key: "outcome",
    header: "Outcome",
    width: "8rem",
    render: ({ row }) => (
      <StatusPill tone={tone(row.outcome)}>
        {row.outcome}
      </StatusPill>
    ),
  },
];

const statisticsColumns: readonly DataTableColumn<StatisticsBucket>[] = [
  {
    key: "dimensions",
    header: "Dimensions",
    width: "32%",
    render: ({ row }) =>
      row.dimensions.length === 0
        ? "Total"
        : row.dimensions
            .map((dimension) => dimension ?? "Not applicable")
            .join(" / "),
  },
  {
    align: "end",
    key: "calls",
    header: "Calls",
    width: "7rem",
    render: ({ row }) => row.calls,
  },
  {
    align: "end",
    key: "attempts",
    header: "Attempts",
    width: "7rem",
    render: ({ row }) => row.attempts,
  },
  {
    key: "usage",
    header: "Typed usage",
    width: "32%",
    render: ({ row }) => usageLabel(row),
  },
  {
    align: "end",
    key: "cost",
    header: "Cost",
    width: "10rem",
    render: ({ row }) =>
      row.cost === null
        ? "Unavailable"
        : row.currency === null
          ? `${row.cost} (no currency)`
          : `${row.currency} ${row.cost}`,
  },
];

const activityColumns: readonly DataTableColumn<ActivityEvent>[] = [
  {
    key: "time",
    header: "Time",
    width: "12rem",
    render: ({ row }) => displayTime(row.occurred_at),
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
      <StatusPill tone={tone(row.result)}>
        {row.result}
      </StatusPill>
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
function FailurePage({
  message,
  onRetry,
}: {
  readonly message: string;
  readonly onRetry: () => void;
}) {
  return (
    <StatePanel
      kind="error"
      onRetry={onRetry}
      title="The administration data is not available"
    >
      {message}
    </StatePanel>
  );
}

function AdministrationStatePage({
  children,
}: {
  readonly children: ReactNode;
}) {
  return <PageSurface className="administration-page">{children}</PageSurface>;
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

function Overview({ data }: { readonly data: AppData }) {
  const cooldowns = data.providerModels.filter((item) => item.cooldown != null);
  return (
    <PageSurface className="administration-page">
      <PageHeading
        description="Inspect the current global calling service."
        eyebrow="Global administration"
        title="Router overview"
      />
      <section className="resource-totals" aria-label="Resource totals">
        <StatCard
          icon={<Icon name="server" />}
          label="Services"
          value={String(data.services.length)}
        />
        <StatCard
          icon={<Icon name="cloud" />}
          label="Provider connections"
          value={String(data.providers.length)}
        />
        <StatCard
          icon={<Icon name="spark" />}
          label="Provider-models"
          value={String(data.providerModels.length)}
        />
        <StatCard
          icon={<Icon name="warning" />}
          label="Current cooldowns"
          value={String(cooldowns.length)}
        />
      </section>
      <Panel>
        <PanelHeader
          description={`Checked ${displayTime(data.health.checked_at)}`}
          title="Small health summary"
        />
        <ul className="health-list">
          {data.health.components.map((item) => (
            <li key={item.name}>
              <span>
                <strong>{item.name.replaceAll("_", " ")}</strong>
                {item.message == null ? null : <small>{item.message}</small>}
              </span>
              <StatusPill tone={tone(item.status)}>
                {item.status}
              </StatusPill>
            </li>
          ))}
        </ul>
      </Panel>
    </PageSurface>
  );
}

interface LogsPageState {
  readonly from: string;
  readonly to: string;
  readonly items: readonly RequestLogSummary[];
  readonly detailId: string | null;
  readonly detail: RequestLog | null;
  readonly detailFailure: string | null;
  readonly loadMoreFailure: string | null;
  readonly loadMorePending: boolean;
  readonly loadedFrom: string | null;
  readonly loadedPages: number;
  readonly loadedTo: string | null;
  readonly nextCursor: string | null;
  readonly phase: "unqueried" | "loading" | "ready" | "error";
}

function RequestLogDetail({
  detail,
  mediaLink,
  onClose,
  onPrepareMedia,
}: {
  readonly detail: RequestLog;
  readonly mediaLink: { readonly id: string; readonly url: string } | null;
  readonly onClose: () => void;
  readonly onPrepareMedia: (item: LogMedia) => void;
}) {
  return (
    <Panel
      aria-live="polite"
      onKeyDown={(event) => {
        if (event.defaultPrevented || event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
    >
      <PanelHeader
        actions={
          <Button id="request-log-close" onClick={onClose} variant="quiet">
            Close
          </Button>
        }
        description={`${requestLogActorLabel(detail.summary)} · ${requestLogScopeLabel(detail.summary)}`}
        title={`Request ${detail.summary.id}`}
      />
      <div className="log-detail">
        <section>
          <h3>Request facts</h3>
          <dl className="log-detail-facts">
            <div>
              <dt>Logical call</dt>
              <dd>{detail.summary.logical_call_id}</dd>
            </div>
            <div>
              <dt>Actor and scope</dt>
              <dd>
                {requestLogActorLabel(detail.summary)} ·{" "}
                {requestLogScopeLabel(detail.summary)}
              </dd>
            </div>
            <div>
              <dt>Route</dt>
              <dd>{requestLogRouteLabel(detail.summary)}</dd>
            </div>
            <div>
              <dt>Kind and outcome</dt>
              <dd>
                {detail.summary.kind} · {detail.summary.outcome}
              </dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd>{displayTime(detail.summary.started_at)}</dd>
            </div>
            <div>
              <dt>Tags</dt>
              <dd>
                {detail.summary.tags === undefined ||
                detail.summary.tags.length === 0
                  ? "None"
                  : detail.summary.tags.join(", ")}
              </dd>
            </div>
          </dl>
        </section>
        <section>
          <h3>Request content</h3>
          <pre>{detail.request_json}</pre>
        </section>
        <section>
          <h3>Response content</h3>
          <pre>
            {detail.response_json ?? "Response content is unavailable."}
          </pre>
        </section>
        <section>
          <h3>Attempts</h3>
          <ol className="log-attempt-list">
            {detail.attempts.map((item, index) => (
              <li key={`${item.provider_model_api_name}-${String(index)}`}>
                <strong>
                  {item.provider_model_api_name} · {item.outcome}
                </strong>
                <span>
                  {displayTime(item.started_at)} through{" "}
                  {displayTime(item.completed_at)}
                </span>
                <span>
                  {item.usage === undefined
                    ? "Usage unavailable"
                    : `${item.usage.currency} ${item.usage.cost} · ${item.usage.units
                        .map((unit) => `${unit.unit} ${unit.quantity}`)
                        .join(", ")}`}
                </span>
                <span>
                  Applied prices: {item.applied_prices.currency} ·{" "}
                  {item.applied_prices.unit_prices
                    .map((price) => `${price.unit} ${price.amount}`)
                    .join(", ")}
                  {typeof item.applied_prices.source === "string"
                    ? ` · source ${item.applied_prices.source}`
                    : ""}
                  {item.applied_prices.synchronized_at === undefined
                    ? ""
                    : ` · synchronized ${displayTime(item.applied_prices.synchronized_at)}`}
                </span>
                {item.error == null ? null : (
                  <span>
                    {item.error.code}: {item.error.message}
                    {item.error.details?.field === undefined
                      ? ""
                      : ` · field ${item.error.details.field}`}
                    {item.error.details?.reason === undefined
                      ? ""
                      : ` · ${item.error.details.reason}`}
                  </span>
                )}
                {item.response_json === undefined ? null : (
                  <pre>{item.response_json}</pre>
                )}
              </li>
            ))}
          </ol>
        </section>
        <section>
          <h3>Retained media</h3>
          {detail.media == null || detail.media.length === 0 ? (
            <p>No retained media</p>
          ) : (
            <ul>
              {detail.media.map((item) => (
                <li key={item.id}>
                  <span>
                    {item.role} · {item.media_type} · {String(item.size_bytes)}{" "}
                    bytes
                  </span>
                  <Button
                    onClick={() => {
                      onPrepareMedia(item);
                    }}
                    variant="quiet"
                  >
                    Prepare retained media download
                  </Button>
                  {mediaLink?.id === item.id ? (
                    <a
                      download={`request-log-media-${item.id}`}
                      href={mediaLink.url}
                    >
                      Download retained media
                    </a>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Panel>
  );
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- This page coordinates one cursor walk, selected-detail focus, stale-load rejection, and retained-media URL revocation; its render-only detail is already separate.
function LogsPage({
  client,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
}) {
  const [logs, updateLogs] = useReducer(
    (state: LogsPageState, patch: Partial<LogsPageState>) => ({
      ...state,
      ...patch,
    }),
    undefined,
    (): LogsPageState => {
      const initial = isoRange();
      return {
        from: initial.from.slice(0, 16),
        to: initial.to.slice(0, 16),
        items: [],
        detailId: null,
        detail: null,
        detailFailure: null,
        loadMoreFailure: null,
        loadMorePending: false,
        loadedFrom: null,
        loadedPages: 0,
        loadedTo: null,
        nextCursor: null,
        phase: "unqueried",
      };
    },
  );
  const mediaUrl = useRef<string | null>(null);
  const mediaLoadGuard = useRef(createScopeLoadGuard());
  const listLoadGuard = useRef(createScopeLoadGuard());
  const listCursors = useRef(new Set<string>());
  const detailLoadGuard = useRef(createScopeLoadGuard());
  const detailReturnFocus = useRef<HTMLButtonElement | null>(null);
  const [mediaLink, setMediaLink] = useState<{
    readonly id: string;
    readonly url: string;
  } | null>(null);
  useEffect(
    () => () => {
      listLoadGuard.current.invalidate();
      detailLoadGuard.current.invalidate();
      mediaUrl.current = invalidateRetainedMediaLoad(
        mediaLoadGuard.current,
        mediaUrl.current,
        (url) => {
          URL.revokeObjectURL(url);
        },
      );
    },
    [],
  );
  useEffect(() => {
    if (logs.detailId !== null)
      globalThis.document.getElementById("request-log-close")?.focus();
  }, [logs.detail, logs.detailFailure, logs.detailId]);
  function closeDetail(): void {
    detailLoadGuard.current.invalidate();
    updateLogs({ detail: null, detailFailure: null, detailId: null });
    setMediaLink(null);
    mediaUrl.current = invalidateRetainedMediaLoad(
      mediaLoadGuard.current,
      mediaUrl.current,
      (url) => {
        URL.revokeObjectURL(url);
      },
    );
    const target = detailReturnFocus.current;
    if (target?.isConnected) target.focus();
    detailReturnFocus.current = null;
  }
  function load(): Promise<void> {
    const generation = listLoadGuard.current.begin();
    globalThis.document.getElementById("request-log-load")?.focus();
    listCursors.current.clear();
    detailLoadGuard.current.invalidate();
    detailReturnFocus.current = null;
    setMediaLink(null);
    mediaUrl.current = invalidateRetainedMediaLoad(
      mediaLoadGuard.current,
      mediaUrl.current,
      (url) => {
        URL.revokeObjectURL(url);
      },
    );
    updateLogs({
      detail: null,
      detailFailure: null,
      detailId: null,
      items: [],
      loadMoreFailure: null,
      loadMorePending: false,
      loadedFrom: null,
      loadedPages: 0,
      loadedTo: null,
      nextCursor: null,
      phase: "loading",
    });
    return Promise.resolve()
      .then(async () => {
        const loadedFrom = new Date(logs.from).toISOString();
        const loadedTo = new Date(logs.to).toISOString();
        return {
          loadedFrom,
          loadedTo,
          page: await client.requestLogsPage(loadedFrom, loadedTo),
        };
      })
      .then(({ loadedFrom, loadedTo, page }) => {
        if (!listLoadGuard.current.isCurrent(generation)) return;
        const items = mergeBoundedRows(
          [],
          page.items,
          (item) => item.id,
          "The request-log list",
        );
        const nextCursor = continuedPageCursor(
          page,
          1,
          listCursors.current,
          "The request-log list",
        );
        updateLogs({
          items,
          loadedFrom,
          loadedPages: 1,
          loadedTo,
          nextCursor,
          phase: "ready",
        });
      })
      .catch((error: unknown) => {
        if (!listLoadGuard.current.isCurrent(generation)) return;
        updateLogs({ phase: "error" });
        onNotice(
          "error",
          error instanceof AdministrationApiError
            ? errorMessage(error)
            : error instanceof Error
              ? error.message
              : "The request-log query is inconsistent.",
        );
      });
  }
  function loadMore(): Promise<void> {
    const cursor = logs.nextCursor;
    if (
      cursor === null ||
      logs.loadedFrom === null ||
      logs.loadedTo === null ||
      logs.loadMorePending
    )
      return Promise.resolve();
    const generation = listLoadGuard.current.begin();
    listCursors.current.add(cursor);
    updateLogs({ loadMoreFailure: null, loadMorePending: true });
    return client
      .requestLogsPage(logs.loadedFrom, logs.loadedTo, cursor)
      .then(
        (page) => {
          if (!listLoadGuard.current.isCurrent(generation)) return;
          const items = mergeBoundedRows(
            logs.items,
            page.items,
            (item) => item.id,
            "The request-log list",
          );
          const nextCursor = continuedPageCursor(
            page,
            logs.loadedPages + 1,
            listCursors.current,
            "The request-log list",
          );
          const detailUnavailable =
            nextCursor === null &&
            logs.detailId !== null &&
            !items.some((item) => item.id === logs.detailId);
          if (detailUnavailable) {
            detailLoadGuard.current.invalidate();
            setMediaLink(null);
            mediaUrl.current = invalidateRetainedMediaLoad(
              mediaLoadGuard.current,
              mediaUrl.current,
              (url) => {
                URL.revokeObjectURL(url);
              },
            );
          }
          updateLogs({
            ...(detailUnavailable
              ? {
                  detail: null,
                  detailFailure:
                    "The selected request log is not available in the loaded range.",
                }
              : {}),
            items,
            loadMoreFailure: null,
            loadMorePending: false,
            loadedPages: logs.loadedPages + 1,
            nextCursor,
          });
        },
        (error: unknown) => {
          if (!listLoadGuard.current.isCurrent(generation)) return;
          updateLogs({
            loadMoreFailure: errorMessage(error),
            loadMorePending: false,
          });
        },
      )
      .catch((error: unknown) => {
        if (!listLoadGuard.current.isCurrent(generation)) return;
        updateLogs({
          loadMoreFailure:
            error instanceof Error
              ? error.message
              : "The request-log page is inconsistent.",
          loadMorePending: false,
        });
      });
  }
  const inspectActions: readonly DataTableAction<RequestLogSummary>[] = [
    {
      key: "inspect",
      label: () => "Inspect",
      onAction: (item, _index, context) => {
        const generation = detailLoadGuard.current.begin();
        detailReturnFocus.current = context?.trigger ?? null;
        mediaUrl.current = invalidateRetainedMediaLoad(
          mediaLoadGuard.current,
          mediaUrl.current,
          (url) => {
            URL.revokeObjectURL(url);
          },
        );
        setMediaLink(null);
        updateLogs({
          detail: null,
          detailFailure: null,
          detailId: item.id,
        });
        return client.requestLog(item.id).then(
          (value) => {
            if (!detailLoadGuard.current.isCurrent(generation)) return;
            updateLogs({ detail: value });
          },
          (error: unknown) => {
            if (!detailLoadGuard.current.isCurrent(generation)) return;
            updateLogs({
              detailFailure: `The selected request log is unavailable. ${errorMessage(error)}`,
            });
          },
        );
      },
    },
  ];
  const detail = logs.detail;
  return (
    <PageSurface className="administration-page">
      <PageHeading
        description="Only global administrators can read complete retained model content and media."
        eyebrow="Best-effort diagnostics"
        title="Detailed request logs"
      />
      <DataTable
        actions={inspectActions}
        actionsLabel="Request actions"
        ariaLabel="Detailed request logs"
        className="administration-data-table"
        columns={requestLogColumns}
        filters={
          <form
            className="administration-form request-log-filter-form"
            onSubmit={(event) => {
              event.preventDefault();
              void load();
            }}
          >
            <label>
              From
              <input
                onChange={(event) => {
                  updateLogs({ from: event.currentTarget.value });
                }}
                type="datetime-local"
                value={logs.from}
              />
            </label>
            <label>
              To
              <input
                onChange={(event) => {
                  updateLogs({ to: event.currentTarget.value });
                }}
                type="datetime-local"
                value={logs.to}
              />
            </label>
            <Button id="request-log-load" type="submit">
              Load logs
            </Button>
          </form>
        }
        getRowId={(item) => item.id}
        getRowLabel={(item) => `Request ${item.id}`}
        liveMessage={`${String(logs.items.length)} detailed logs loaded.`}
        {...(logs.phase !== "ready" || logs.items.length === 0
          ? {}
          : {
              loadMore: {
                ...(logs.loadMoreFailure === null
                  ? {}
                  : { error: logs.loadMoreFailure }),
                hasMore: logs.nextCursor !== null,
                loadedLabel: `${String(logs.items.length)} detailed logs loaded`,
                loading: logs.loadMorePending,
                onLoadMore: loadMore,
                onRetry: loadMore,
                completeLabel: "The loaded date range is complete",
              },
            })}
        maxRows={administrationListMaximum}
        minimumWidth="74rem"
        rows={logs.items}
        state={
          logs.phase === "loading"
            ? { kind: "loading", message: "Loading detailed logs" }
            : logs.phase === "error"
              ? {
                  kind: "error",
                  message: "Detailed logs are unavailable.",
                  onRetry: load,
                  retryLabel: "Try loading logs again",
                }
              : logs.items.length === 0
                ? {
                    kind: "empty",
                    message:
                      logs.phase === "unqueried"
                        ? "Choose a date range and load detailed logs."
                        : "No logs are in this range.",
                  }
                : {
                    kind: "ready",
                    message:
                      logs.nextCursor === null
                        ? "The detailed-log query is complete."
                        : "More detailed logs are available.",
                  }
        }
        toolbarLabel="Detailed request log filters"
      />
      {logs.detailId === null ? null : detail === null ? (
        <StatePanel
          actions={
            <Button
              id="request-log-close"
              onClick={closeDetail}
              variant="quiet"
            >
              Close
            </Button>
          }
          kind={logs.detailFailure === null ? "loading" : "error"}
          title={
            logs.detailFailure === null
              ? `Loading request ${logs.detailId}`
              : `Request ${logs.detailId} is unavailable`
          }
        >
          {logs.detailFailure ?? "Wait while the Router reads retained detail."}
        </StatePanel>
      ) : (
        <RequestLogDetail
          detail={detail}
          mediaLink={mediaLink}
          onClose={closeDetail}
          onPrepareMedia={(item) => {
            const generation = mediaLoadGuard.current.begin();
            void client
              .requestLogMedia(detail.summary.id, item.id)
              .then((blob) => {
                if (!mediaLoadGuard.current.isCurrent(generation)) return;
                if (mediaUrl.current !== null)
                  URL.revokeObjectURL(mediaUrl.current);
                mediaUrl.current = URL.createObjectURL(
                  new Blob([blob], { type: "application/octet-stream" }),
                );
                setMediaLink({ id: item.id, url: mediaUrl.current });
              })
              .catch((error: unknown) => {
                if (!mediaLoadGuard.current.isCurrent(generation)) return;
                onNotice(
                  "error",
                  `The retained media is unavailable. ${errorMessage(error)}`,
                );
              });
          }}
        />
      )}
    </PageSurface>
  );
}

function StatisticsPage({
  client,
  onNotice,
  services,
}: {
  readonly client: AdministrationClient;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly services: readonly Service[];
}) {
  const initial = useMemo(() => isoRange(30), []);
  const [result, setResult] = useState<StatisticsResult | null>(null);
  const [phase, setPhase] = useState<
    "unqueried" | "loading" | "ready" | "error"
  >("unqueried");
  const loadGuard = useRef(createScopeLoadGuard());
  useEffect(
    () => () => {
      loadGuard.current.invalidate();
    },
    [],
  );
  async function load(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const generation = loadGuard.current.begin();
    const form = new FormData(event.currentTarget);
    const outcome = formText(form, "outcome");
    setResult(null);
    setPhase("loading");
    try {
      const nextResult = await client.statistics({
        from: new Date(formText(form, "from")).toISOString(),
        to: new Date(formText(form, "to")).toISOString(),
        ...(formText(form, "service") === ""
          ? {}
          : { service: formText(form, "service") }),
        ...(formText(form, "workspace") === ""
          ? {}
          : { workspace: formText(form, "workspace") }),
        ...(formText(form, "assignment") === ""
          ? {}
          : { assignment: formText(form, "assignment") }),
        ...(formText(form, "provider_model") === ""
          ? {}
          : { provider_model: formText(form, "provider_model") }),
        ...(outcome === "succeeded" || outcome === "failed" ? { outcome } : {}),
        ...(formText(form, "tag") === "" ? {} : { tag: formText(form, "tag") }),
        group_by: form.getAll("group_by").map(String),
      });
      if (loadGuard.current.isCurrent(generation)) {
        setResult(nextResult);
        setPhase("ready");
      }
    } catch (error) {
      if (!loadGuard.current.isCurrent(generation)) return;
      setPhase("error");
      onNotice("error", errorMessage(error));
    }
  }
  const buckets = result?.buckets ?? [];
  return (
    <PageSurface className="administration-page">
      <PageHeading
        description="Group calls, attempts, typed units, and fixed-decimal cost across at most 366 days."
        eyebrow="Durable accounting"
        title="Usage and cost statistics"
      />
      <DataTable
        ariaLabel="Usage and cost statistics"
        className="administration-data-table"
        columns={statisticsColumns}
        filters={
          <form
            className="administration-form statistics-form"
            onSubmit={(event) => void load(event)}
          >
            <label>
              From
              <input
                defaultValue={initial.from.slice(0, 16)}
                name="from"
                type="datetime-local"
              />
            </label>
            <label>
              To
              <input
                defaultValue={initial.to.slice(0, 16)}
                name="to"
                type="datetime-local"
              />
            </label>
            <label>
              Service
              <select name="service">
                <option value="">All services</option>
                {services.map((item) => (
                  <option key={item.api_name}>{item.api_name}</option>
                ))}
              </select>
            </label>
            <label>
              Workspace
              <input name="workspace" />
            </label>
            <label>
              Assignment
              <input name="assignment" placeholder="Name or (exact)" />
            </label>
            <label>
              Provider-model
              <input name="provider_model" />
            </label>
            <label>
              Outcome
              <select name="outcome">
                <option value="">All outcomes</option>
                <option>succeeded</option>
                <option>failed</option>
              </select>
            </label>
            <label>
              Tag
              <input name="tag" />
            </label>
            <fieldset>
              <legend>Group by</legend>
              {[
                "date",
                "service",
                "workspace",
                "assignment",
                "provider_model",
                "outcome",
                "tag",
              ].map((item) => (
                <label className="checkbox-field" key={item}>
                  <input name="group_by" type="checkbox" value={item} /> {item}
                </label>
              ))}
            </fieldset>
            <Button type="submit">Run statistics</Button>
          </form>
        }
        getRowId={(item, index) =>
          `${String(index)}:${JSON.stringify([item.dimensions, item.currency])}`
        }
        getRowLabel={(item) =>
          `Statistics group ${
            item.dimensions.length === 0
              ? "Total"
              : item.dimensions
                  .map((dimension) => dimension ?? "Not applicable")
                  .join(" / ")
          }`
        }
        liveMessage={
          result === null
            ? undefined
            : `${String(buckets.length)} accounting groups loaded for ${displayTime(result.from)} through ${displayTime(result.to)}.`
        }
        maxRows={STATISTICS_GROUP_MAXIMUM}
        minimumWidth="56rem"
        rows={buckets}
        state={
          phase === "loading"
            ? { kind: "loading", message: "Loading accounting groups" }
            : phase === "error"
              ? {
                  kind: "error",
                  message:
                    "The statistics query failed. Review the filters and run it again.",
                }
              : buckets.length === 0
                ? {
                    kind: "empty",
                    message:
                      phase === "unqueried"
                        ? "Choose filters and run the statistics query."
                        : "No accounting groups match these filters.",
                  }
                : {
                    kind: "ready",
                    message: `${String(buckets.length)} accounting groups loaded.`,
                  }
        }
        toolbarLabel="Usage and cost filters"
      />
    </PageSurface>
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
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly health: AdministratorHealth;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
  readonly providerModels: readonly ProviderModel[];
  readonly retentionDays: number;
}) {
  const range = useMemo(() => isoRange(7), []);
  const [activityState, updateActivity] = useReducer(
    (
      current: {
        readonly items: readonly ActivityEvent[];
        readonly loadMoreFailure: string | null;
        readonly loadMorePending: boolean;
        readonly nextCursor: string | null;
        readonly phase: "loading" | "ready" | "error";
      },
      patch: Partial<typeof current>,
    ) => ({ ...current, ...patch }),
    {
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
        updateActivity({ items: [], phase: "error" });
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
        description="Inspect current health, best-effort cooldowns, retention, and basic configuration activity."
        eyebrow="Operations"
        title="Activity and health"
      />
      <div className="administration-sections">
        <Panel>
          <PanelHeader
            description={displayTime(health.checked_at)}
            title="Health components"
          />
          <ul className="health-list">
            {health.components.map((item) => (
              <li key={item.name}>
                <span>
                  <strong>{item.name.replaceAll("_", " ")}</strong>
                  <small>{item.message ?? "No corrective message"}</small>
                </span>
                <StatusPill tone={tone(item.status)}>
                  {item.status}
                </StatusPill>
              </li>
            ))}
          </ul>
        </Panel>
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
            <label>
              Duration in whole days
              <input
                defaultValue={retentionDays}
                max={30}
                min={1}
                name="days"
                type="number"
              />
            </label>
            <Button type="submit">Save retention</Button>
          </form>
        </Panel>
      </div>
      <Panel>
        <PanelHeader
          description="Cooldowns are process-local best-effort state and can clear after a restart."
          title="Current provider-model cooldowns"
        />
        <ul className="record-list">
          {cooldowns.length === 0 ? (
            <li>No current cooldowns</li>
          ) : (
            cooldowns.map((item) => (
              <li key={item.api_name}>
                <span>
                  <strong>{item.api_name}</strong>
                  <small>
                    {item.cooldown?.reason} · until{" "}
                    {displayTime(item.cooldown?.until)}
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
          description="This is a basic activity record. It is not immutable configuration history."
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
                    message: "Retained activity is unavailable.",
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

interface MainState {
  readonly assignments: readonly Assignment[];
  readonly assignmentPending: boolean;
  readonly data: AppData | null;
  readonly failure: string | null;
  readonly notice: Notice | null;
  readonly assignmentDirty: boolean;
  readonly pendingService: string | null;
  readonly section: Section;
  readonly selectedService: string;
  readonly sessionState: {
    readonly status:
      "loading" | "active" | "signed-out" | "expired" | "denied" | "failed";
    readonly session?: AdministratorSession;
    readonly message?: string;
  };
}

function initialMainState(): MainState {
  return {
    assignments: [],
    assignmentPending: false,
    data: null,
    failure: null,
    notice: null,
    assignmentDirty: false,
    pendingService: null,
    section: currentSection(),
    selectedService: selectedServiceFromLocation(),
    sessionState: { status: "loading" },
  };
}

function AuthenticatedAdministration({
  assignments,
  assignmentPending,
  client,
  data,
  failure,
  loadGlobal,
  loadScope,
  navigate,
  notice,
  notify,
  onDismissNotice,
  onAssignmentDirtyChange,
  onAssignmentPendingChange,
  section,
  selectService,
  selectedService,
  session,
}: {
  readonly assignments: readonly Assignment[];
  readonly assignmentPending: boolean;
  readonly client: AdministrationClient;
  readonly data: AppData | null;
  readonly failure: string | null;
  readonly loadGlobal: () => Promise<void>;
  readonly loadScope: () => Promise<void>;
  readonly navigate: (id: string) => void;
  readonly notice: Notice | null;
  readonly notify: (tone: "success" | "error", message: string) => void;
  readonly onDismissNotice: () => void;
  readonly onAssignmentDirtyChange: (dirty: boolean) => void;
  readonly onAssignmentPendingChange: (pending: boolean) => void;
  readonly section: Section;
  readonly selectService: (value: string) => void;
  readonly selectedService: string;
  readonly session: AdministratorSession;
}) {
  const sidebar = (
    <ApplicationSidebar
      brand={
        <div className="application-brand">
          <span>
            <Icon name="spark" size={19} />
          </span>
          <strong>LLM Router</strong>
        </div>
      }
      context={
        <WorkspaceSelector
          avatar={<Icon name="server" />}
          detail="Global administrator"
          name={
            data?.services.find((item) => item.api_name === selectedService)
              ?.display_name ?? "All services"
          }
        />
      }
      footer={
        <>
          <AccountMenu
            avatar={session.display_name.slice(0, 2).toUpperCase()}
            detail={`Expires ${displayTime(session.expires_at)}`}
            name={session.display_name}
          />
          <Button
            onClick={() => {
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
        </>
      }
      navigation={
        <ApplicationNavigation aria-label="Administration navigation">
          {(["Manage", "Observe"] as const).map((group) => (
            <ApplicationNavigationGroup key={group} label={group}>
              {routes.flatMap((route) =>
                route.group === group
                  ? [
                      <NavigationItem
                        active={route.id === section}
                        icon={<Icon name={route.icon} size={17} />}
                        key={route.id}
                        label={route.label}
                        onClick={() => {
                          navigate(route.id);
                        }}
                      />,
                    ]
                  : [],
              )}
            </ApplicationNavigationGroup>
          ))}
        </ApplicationNavigation>
      }
    />
  );
  const topbar = (
    <ApplicationTopbar
      actions={
        <div className="administration-topbar-actions">
          <label>
            Service
            <select
              aria-label="Selected service"
              disabled={assignmentPending}
              onChange={(event) => {
                selectService(event.currentTarget.value);
              }}
              value={selectedService}
            >
              <option value="">All services</option>
              {data?.services.map((item) => (
                <option key={item.api_name} value={item.api_name}>
                  {item.display_name}
                </option>
              ))}
            </select>
          </label>
          <Button
            disabled={assignmentPending}
            onClick={() => {
              void Promise.all([loadGlobal(), loadScope()]);
            }}
            variant="secondary"
          >
            <Icon name="refresh" size={16} /> Refresh
          </Button>
        </div>
      }
      title={routes.find((item) => item.id === section)?.label}
    />
  );
  let content: ReactNode =
    data === null ? (
      <AdministrationStatePage>
        {failure === null ? (
          <LoadingPage />
        ) : (
          <FailurePage
            message={failure}
            onRetry={() => {
              void loadGlobal();
            }}
          />
        )}
      </AdministrationStatePage>
    ) : (
      <Overview data={data} />
    );
  if (data !== null && section === "services")
    content = (
      <PageSurface className="administration-page">
        <PageHeading
          description="Create, move, inspect, and delete services in the one-parent tree."
          eyebrow="Global administration"
          title="Services and parent relationships"
        />
        <ServiceManagement
          client={client}
          csrf={session.csrf_token}
          onNotice={notify}
          onRefresh={loadGlobal}
          onSelect={selectService}
          selectedService={selectedService}
          services={data.services}
        />
      </PageSurface>
    );
  if (data !== null && section === "configuration")
    content = (
      <PageSurface className="administration-page">
        <PageHeading
          description="Manage global providers, canonical models, provider-model mappings, prices, credentials, and the selected service assignments in one graph."
          eyebrow="Global catalog and selected service context"
          title="LLM configuration"
        />
        <ConfigurationGraph
          assignments={assignments}
          client={client}
          credentials={data.credentials}
          csrf={session.csrf_token}
          globalPhase={data.configurationPhase}
          models={data.models}
          onAssignmentDirtyChange={onAssignmentDirtyChange}
          onAssignmentPendingChange={onAssignmentPendingChange}
          onNotice={notify}
          onRefreshAssignments={loadScope}
          onRefreshGlobal={loadGlobal}
          providerModels={data.providerModels}
          providers={data.providers}
          selectedService={selectedService}
        />
      </PageSurface>
    );
  if (data !== null && section === "logs")
    content = <LogsPage client={client} onNotice={notify} />;
  if (data !== null && section === "statistics")
    content = (
      <StatisticsPage
        client={client}
        onNotice={notify}
        services={data.services}
      />
    );
  if (data !== null && section === "operations")
    content = (
      <OperationsPage
        client={client}
        csrf={session.csrf_token}
        health={data.health}
        onNotice={notify}
        onRefresh={loadGlobal}
        providerModels={data.providerModels}
        retentionDays={data.retentionDays}
      />
    );
  return (
    <ShellErrorBoundary
      fallbackMessage="Reload the page. No automatic write was attempted."
      fallbackTitle="The administration interface stopped"
      resetKey={section}
    >
      <ApplicationShell
        mainProps={{ id: "main-content", tabIndex: -1 }}
        mobileNavigation={
          <MobileNavigation
            aria-label="Mobile administration navigation"
            items={routes.map((route) => ({
              id: route.id,
              label: route.label,
              icon: <Icon name={route.icon} />,
              active: route.id === section,
            }))}
            onSelect={navigate}
          />
        }
        sidebar={sidebar}
        topbar={topbar}
      >
        <div className="administration-content">{content}</div>
        {notice === null ? null : (
          <Toast
            className={`notice-${notice.tone}`}
            role={notice.tone === "error" ? "alert" : "status"}
            onDismiss={onDismissNotice}
          >
            {notice.message}
          </Toast>
        )}
      </ApplicationShell>
    </ShellErrorBoundary>
  );
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- This session coordinator owns authentication, fenced global and selected-service loads, history, and the dirty-service transition as one boundary.
export function App({ client = defaultAdministrationClient }: AppProps) {
  const [main, update] = useReducer(
    (state: MainState, patch: Partial<MainState>) => ({ ...state, ...patch }),
    undefined,
    initialMainState,
  );
  const {
    assignments,
    assignmentPending,
    assignmentDirty,
    data,
    failure,
    notice,
    pendingService,
    section,
    selectedService,
    sessionState,
  } = main;
  const notify = useCallback(
    (nextTone: "success" | "error", message: string) => {
      update({ notice: { tone: nextTone, message } });
    },
    [],
  );
  const [scopeLoadGuard] = useState(createScopeLoadGuard);
  const [globalLoadGuard] = useState(createScopeLoadGuard);
  const selectedServiceRef = useRef(selectedService);
  const replaceLegacyConfigurationPath = useCallback(() => {
    if (!legacyConfigurationPaths.has(globalThis.location.pathname.slice(1)))
      return;
    globalThis.history.replaceState(
      {},
      "",
      `/configuration${globalThis.location.search}`,
    );
  }, []);
  const preventUnsafeLocationRestore = useEffectEvent((): boolean => {
    if (!assignmentDirty && !assignmentPending) return false;
    const service = selectedServiceRef.current;
    const query =
      service === "" ? "" : `?service=${encodeURIComponent(service)}`;
    globalThis.history.replaceState({}, "", `/${section}${query}`);
    notify(
      "error",
      assignmentPending
        ? "Wait for the selected service assignment write to finish."
        : "Close the assignment form and confirm that you want to discard its changes before you change pages or services.",
    );
    return true;
  });
  const expireAdministratorSession = useCallback(() => {
    expireAdministratorSessionLoads(globalLoadGuard, scopeLoadGuard, () => {
      update({
        assignments: [],
        assignmentPending: false,
        data: null,
        sessionState: { status: "expired" },
      });
    });
  }, [globalLoadGuard, scopeLoadGuard]);
  const authenticatedClient = useMemo(
    () => withUnauthorizedSessionHandler(client, expireAdministratorSession),
    [client, expireAdministratorSession],
  );
  const inspectSession = useCallback(async () => {
    try {
      update({
        sessionState: { status: "active", session: await client.session() },
      });
    } catch (error) {
      if (error instanceof AdministrationApiError && error.status === 403)
        update({ sessionState: { status: "denied", message: error.message } });
      else if (error instanceof AdministrationApiError && error.status === 401)
        update({ sessionState: { status: "signed-out" } });
      else
        update({
          sessionState: {
            status: "failed",
            message: errorMessage(error),
          },
        });
    }
  }, [client]);
  const loadGlobal = useCallback(async () => {
    const generation = globalLoadGuard.begin();
    update({ failure: null });
    try {
      const [
        services,
        providers,
        models,
        providerModels,
        credentials,
        health,
        retention,
      ] = await Promise.all([
        authenticatedClient.services(),
        authenticatedClient.providers(),
        authenticatedClient.models(),
        authenticatedClient.providerModels(),
        authenticatedClient.credentials(),
        authenticatedClient.health(),
        authenticatedClient.retention(),
      ]);
      if (globalLoadGuard.isCurrent(generation)) {
        update({
          data: {
            services: services.items,
            providers: providers.items,
            models: models.items,
            providerModels: providerModels.items,
            credentials: credentials.items,
            health,
            retentionDays: retention.duration_days,
            configurationPhase: [
              providers,
              models,
              providerModels,
              credentials,
            ].some(
              (page) =>
                page.page.has_more || page.retrieval?.complete === false,
            )
              ? "partial"
              : "ready",
          },
        });
        const currentService = selectedServiceRef.current;
        if (
          currentService !== "" &&
          !services.items.some((item) => item.api_name === currentService)
        ) {
          selectedServiceRef.current = "";
          update({ selectedService: "" });
          const url = new URL(globalThis.location.href);
          url.searchParams.delete("service");
          globalThis.history.replaceState(
            {},
            "",
            `${url.pathname}${url.search}`,
          );
        }
      }
    } catch (error) {
      if (!globalLoadGuard.isCurrent(generation)) return;
      update({ failure: errorMessage(error) });
      notify("error", errorMessage(error));
    }
  }, [authenticatedClient, globalLoadGuard, notify]);
  const loadScope = useCallback((): Promise<void> => {
    const generation = scopeLoadGuard.begin();
    if (selectedService === "") {
      update({ assignments: [] });
      return Promise.resolve();
    }
    return authenticatedClient
      .assignments(selectedService)
      .then((assignmentPage) => {
        if (!scopeLoadGuard.isCurrent(generation)) return;
        update({ assignments: assignmentPage.items });
      })
      .catch((error: unknown) => {
        if (!scopeLoadGuard.isCurrent(generation)) return;
        notify("error", errorMessage(error));
      });
  }, [authenticatedClient, notify, scopeLoadGuard, selectedService]);
  const sessionExpiresAt = sessionState.session?.expires_at;
  useEffect(() => {
    const timer = globalThis.setTimeout(() => {
      void inspectSession();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
    };
  }, [inspectSession]);
  useEffect(() => {
    const timer = globalThis.setTimeout(() => {
      if (sessionState.status === "active") void loadGlobal();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
    };
  }, [loadGlobal, sessionState.status]);
  useEffect(() => {
    const timer = globalThis.setTimeout(() => {
      if (sessionState.status === "active") void loadScope();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
    };
  }, [loadScope, sessionState.status]);
  useEffect(() => {
    if (sessionState.status !== "active" || sessionExpiresAt === undefined)
      return;
    return scheduleSessionExpiry(sessionExpiresAt, expireAdministratorSession);
  }, [expireAdministratorSession, sessionExpiresAt, sessionState.status]);
  useEffect(() => {
    replaceLegacyConfigurationPath();
    const restoreLocation = () => {
      if (preventUnsafeLocationRestore()) return;
      replaceLegacyConfigurationPath();
      scopeLoadGuard.invalidate();
      selectedServiceRef.current = selectedServiceFromLocation();
      update({
        assignments: [],
        section: currentSection(),
        selectedService: selectedServiceFromLocation(),
      });
    };
    globalThis.addEventListener("popstate", restoreLocation);
    return () => {
      globalThis.removeEventListener("popstate", restoreLocation);
    };
  }, [replaceLegacyConfigurationPath, scopeLoadGuard]);
  function applyServiceSelection(value: string) {
    scopeLoadGuard.invalidate();
    selectedServiceRef.current = value;
    update({
      assignments: [],
      assignmentPending: false,
      assignmentDirty: false,
      pendingService: null,
      selectedService: value,
    });
    const url = new URL(globalThis.location.href);
    if (value === "") url.searchParams.delete("service");
    else url.searchParams.set("service", value);
    globalThis.history.replaceState({}, "", `${url.pathname}${url.search}`);
  }
  function selectService(value: string) {
    if (value === selectedService) return;
    if (assignmentPending) {
      notify(
        "error",
        "Wait for the selected service assignment write to finish.",
      );
      return;
    }
    if (assignmentDirty) {
      update({ pendingService: value });
      return;
    }
    applyServiceSelection(value);
  }
  function navigate(id: string) {
    const next = routes.find((item) => item.id === id)?.id;
    if (next === undefined) return;
    if (assignmentPending) {
      notify(
        "error",
        "Wait for the selected service assignment write to finish.",
      );
      return;
    }
    if (assignmentDirty && next !== section) {
      notify(
        "error",
        "Close the assignment form and confirm that you want to discard its changes before you leave this page.",
      );
      return;
    }
    update({ section: next });
    const query =
      selectedService === ""
        ? ""
        : `?service=${encodeURIComponent(selectedService)}`;
    globalThis.history.pushState({}, "", `/${next}${query}`);
    globalThis.scrollTo({ behavior: "auto", left: 0, top: 0 });
    globalThis.document
      .getElementById("main-content")
      ?.focus({ preventScroll: true });
  }
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
                update({ sessionState: { status: "loading" } });
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
                update({ sessionState: { status: "signed-out" } });
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
  const session = sessionState.session;
  if (session === undefined) return null;
  const discardImpact = `discard assignment changes for ${selectedService || "the selected service"}`;
  return (
    <>
      <AuthenticatedAdministration
        assignments={assignments}
        assignmentPending={assignmentPending}
        client={authenticatedClient}
        data={data}
        failure={failure}
        loadGlobal={loadGlobal}
        loadScope={loadScope}
        navigate={navigate}
        notice={notice}
        notify={notify}
        onAssignmentDirtyChange={(dirty) => {
          update({ assignmentDirty: dirty });
        }}
        onAssignmentPendingChange={(pending) => {
          update({ assignmentPending: pending });
        }}
        onDismissNotice={() => {
          update({ notice: null });
        }}
        section={section}
        selectService={selectService}
        selectedService={selectedService}
        session={session}
      />
      <ConfirmationDialog
        confirmLabel="Discard and change service"
        description="The open assignment form has unsaved values. The service change closes that form and replaces only the assignment column."
        impactStatement={discardImpact}
        onCancel={() => {
          update({ pendingService: null });
        }}
        onConfirm={() => {
          if (pendingService !== null) applyServiceSelection(pendingService);
        }}
        open={pendingService !== null}
        pending={assignmentPending}
        title="Discard assignment changes?"
      />
    </>
  );
}
