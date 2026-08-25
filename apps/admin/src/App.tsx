import {
  useCallback,
  useEffect,
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
  Icon,
  MobileNavigation,
  NavigationItem,
  PageHeading,
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
  type IconName,
} from "@opendle/ui";
import {
  AdministrationApiError,
  createAdministrationClient,
  errorMessage,
  isoRange,
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
function EmptyTable({
  columns,
  text,
}: {
  readonly columns: number;
  readonly text: string;
}) {
  return (
    <tr>
      <td colSpan={columns}>{text}</td>
    </tr>
  );
}
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
    <div className="administration-page">
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
              <StatusPill tone={tone(item.status)}>{item.status}</StatusPill>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}

interface LogsPageState {
  readonly from: string;
  readonly to: string;
  readonly items: readonly RequestLogSummary[];
  readonly detail: RequestLog | null;
  readonly phase: "idle" | "loading" | "error";
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
        description={`${detail.summary.service_api_name} / ${detail.summary.workspace_api_name}`}
        title={`Request ${detail.summary.id}`}
      />
      <div className="log-detail">
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
          <ol>
            {detail.attempts.map((item, index) => (
              <li key={`${item.provider_model_api_name}-${String(index)}`}>
                <strong>
                  {item.provider_model_api_name} · {item.outcome}
                </strong>
                <span>
                  {item.usage.currency} {item.usage.cost} ·{" "}
                  {item.usage.units
                    .map((unit) => `${unit.unit} ${unit.quantity}`)
                    .join(", ")}
                </span>
                {item.error == null ? null : (
                  <span>
                    {item.error.code}: {item.error.message}
                  </span>
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
                    Prepare retained media
                  </Button>
                  {mediaLink?.id === item.id ? (
                    <a href={mediaLink.url} rel="noreferrer" target="_blank">
                      Open retained media
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
        detail: null,
        phase: "idle",
      };
    },
  );
  const mediaUrl = useRef<string | null>(null);
  const mediaLoadGuard = useRef(createScopeLoadGuard());
  const listLoadGuard = useRef(createScopeLoadGuard());
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
    if (logs.detail !== null)
      globalThis.document.getElementById("request-log-close")?.focus();
  }, [logs.detail]);
  function closeDetail(): void {
    detailLoadGuard.current.invalidate();
    updateLogs({ detail: null });
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
  async function load() {
    const generation = listLoadGuard.current.begin();
    updateLogs({ phase: "loading" });
    try {
      const items = (
        await client.requestLogs(
          new Date(logs.from).toISOString(),
          new Date(logs.to).toISOString(),
        )
      ).items;
      if (listLoadGuard.current.isCurrent(generation))
        updateLogs({ items, phase: "idle" });
    } catch (error) {
      if (!listLoadGuard.current.isCurrent(generation)) return;
      updateLogs({ phase: "error" });
      onNotice("error", errorMessage(error));
    }
  }
  const detail = logs.detail;
  return (
    <div className="administration-page">
      <PageHeading
        description="Only global administrators can read complete retained model content and media."
        eyebrow="Best-effort diagnostics"
        title="Detailed request logs"
      />
      <Panel>
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
          <Button type="submit">Load logs</Button>
        </form>
        {logs.phase === "loading" ? (
          <LoadingPage title="Loading detailed logs" />
        ) : (
          <div className="administration-table-region">
            <table>
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Scope</th>
                  <th>Kind</th>
                  <th>Route</th>
                  <th>Tags</th>
                  <th>Outcome</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {logs.items.length === 0 ? (
                  <EmptyTable
                    columns={7}
                    text={
                      logs.phase === "error"
                        ? "Logs are unavailable"
                        : "No logs in this range"
                    }
                  />
                ) : (
                  logs.items.map((item) => (
                    <tr key={item.id}>
                      <td>{displayTime(item.started_at)}</td>
                      <td>
                        {item.service_api_name} / {item.workspace_api_name}
                      </td>
                      <td>{item.kind}</td>
                      <td>
                        {item.assignment_api_name ??
                          item.provider_model_api_name ??
                          "Unavailable"}
                      </td>
                      <td>{item.tags?.join(", ") ?? "None"}</td>
                      <td>
                        <StatusPill tone={tone(item.outcome)}>
                          {item.outcome}
                        </StatusPill>
                      </td>
                      <td>
                        <Button
                          onClick={(event) => {
                            const generation = detailLoadGuard.current.begin();
                            detailReturnFocus.current = event.currentTarget;
                            mediaUrl.current = invalidateRetainedMediaLoad(
                              mediaLoadGuard.current,
                              mediaUrl.current,
                              (url) => {
                                URL.revokeObjectURL(url);
                              },
                            );
                            setMediaLink(null);
                            updateLogs({ detail: null });
                            void client
                              .requestLog(item.id)
                              .then((value) => {
                                if (
                                  !detailLoadGuard.current.isCurrent(generation)
                                )
                                  return;
                                updateLogs({ detail: value });
                              })
                              .catch((error: unknown) => {
                                if (
                                  !detailLoadGuard.current.isCurrent(generation)
                                )
                                  return;
                                onNotice("error", errorMessage(error));
                              });
                          }}
                          variant="quiet"
                        >
                          Inspect
                        </Button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      {detail === null ? null : (
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
                mediaUrl.current = URL.createObjectURL(blob);
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
    </div>
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
      if (loadGuard.current.isCurrent(generation)) setResult(nextResult);
    } catch (error) {
      if (!loadGuard.current.isCurrent(generation)) return;
      onNotice("error", errorMessage(error));
    }
  }
  return (
    <div className="administration-page">
      <PageHeading
        description="Group calls, attempts, typed units, and fixed-decimal cost across at most 366 days."
        eyebrow="Durable accounting"
        title="Usage and cost statistics"
      />
      <Panel>
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
      </Panel>
      {result === null ? (
        <StatePanel kind="empty" title="No statistics query">
          Choose filters and run the statistics query.
        </StatePanel>
      ) : (
        <Panel>
          <PanelHeader
            description={`${displayTime(result.from)} through ${displayTime(result.to)}`}
            title="Statistics result"
          />
          <div className="administration-table-region">
            <table>
              <thead>
                <tr>
                  <th>Dimensions</th>
                  <th>Calls</th>
                  <th>Attempts</th>
                  <th>Typed usage</th>
                  <th>Cost</th>
                </tr>
              </thead>
              <tbody>
                {result.buckets.length === 0 ? (
                  <EmptyTable columns={5} text="No accounting groups" />
                ) : (
                  result.buckets.map((item, index) => (
                    <tr
                      key={`${item.currency}-${item.dimensions.join("-")}-${String(index)}`}
                    >
                      <th scope="row">
                        {item.dimensions.join(" / ") || "Total"}
                      </th>
                      <td>{item.calls}</td>
                      <td>{item.attempts}</td>
                      <td>
                        {item.units
                          .map((unit) => `${unit.unit} ${unit.quantity}`)
                          .join(", ") || "None"}
                      </td>
                      <td>
                        {item.currency} {item.cost}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </div>
  );
}

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
  const [activity, setActivity] = useState<readonly ActivityEvent[]>([]);
  const [activityPhase, setActivityPhase] = useState<
    "loading" | "ready" | "error"
  >("loading");
  const activityLoadGuard = useRef(createScopeLoadGuard());
  const loadActivity = useCallback(() => {
    const generation = activityLoadGuard.current.begin();
    setActivityPhase("loading");
    return client
      .activity(range.from, range.to)
      .then((page) => {
        if (!activityLoadGuard.current.isCurrent(generation)) return;
        setActivity(page.items);
        setActivityPhase("ready");
      })
      .catch((error: unknown) => {
        if (!activityLoadGuard.current.isCurrent(generation)) return;
        setActivity([]);
        setActivityPhase("error");
        onNotice("error", errorMessage(error));
      });
  }, [client, onNotice, range.from, range.to]);
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
    <div className="administration-page">
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
                <StatusPill tone={tone(item.status)}>{item.status}</StatusPill>
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
        <div aria-live="polite" className="administration-table-region">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Target</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {activity.length === 0 ? (
                <EmptyTable
                  columns={5}
                  text={
                    activityPhase === "loading"
                      ? "Loading retained activity"
                      : activityPhase === "error"
                        ? "Retained activity is unavailable"
                        : "No retained activity"
                  }
                />
              ) : (
                activity.map((item) => (
                  <tr key={item.id}>
                    <td>{displayTime(item.occurred_at)}</td>
                    <td>{item.actor_subject}</td>
                    <td>{item.action}</td>
                    <td>
                      {item.resource_type} ·{" "}
                      {item.resource_api_name ?? item.resource_id}
                    </td>
                    <td>
                      <StatusPill tone={tone(item.result)}>
                        {item.result}
                      </StatusPill>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

export interface AppProps {
  readonly client?: AdministrationClient;
}
const defaultAdministrationClient = createAdministrationClient();

interface MainState {
  readonly assignments: readonly Assignment[];
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
  section,
  selectService,
  selectedService,
  session,
}: {
  readonly assignments: readonly Assignment[];
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
      failure === null ? (
        <LoadingPage />
      ) : (
        <FailurePage
          message={failure}
          onRetry={() => {
            void loadGlobal();
          }}
        />
      )
    ) : (
      <Overview data={data} />
    );
  if (data !== null && section === "services")
    content = (
      <div className="administration-page">
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
      </div>
    );
  if (data !== null && section === "configuration")
    content = (
      <div className="administration-page">
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
          key={selectedService}
          models={data.models}
          onAssignmentDirtyChange={onAssignmentDirtyChange}
          onNotice={notify}
          onRefreshAssignments={loadScope}
          onRefreshGlobal={loadGlobal}
          providerModels={data.providerModels}
          providers={data.providers}
          selectedService={selectedService}
        />
      </div>
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
  const expireAdministratorSession = useCallback(() => {
    expireAdministratorSessionLoads(globalLoadGuard, scopeLoadGuard, () => {
      update({
        assignments: [],
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
    update({ assignments: [] });
    return authenticatedClient
      .assignments(selectedService)
      .then((assignmentPage) => {
        if (!scopeLoadGuard.isCurrent(generation)) return;
        update({ assignments: assignmentPage.items });
      })
      .catch((error: unknown) => {
        if (!scopeLoadGuard.isCurrent(generation)) return;
        update({ assignments: [] });
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
    if (assignmentDirty) {
      update({ pendingService: value });
      return;
    }
    applyServiceSelection(value);
  }
  function navigate(id: string) {
    const next = routes.find((item) => item.id === id)?.id;
    if (next === undefined) return;
    update({ section: next });
    const query =
      selectedService === ""
        ? ""
        : `?service=${encodeURIComponent(selectedService)}`;
    globalThis.history.pushState({}, "", `/${next}${query}`);
    globalThis.document.getElementById("main-content")?.focus();
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
        title="Discard assignment changes?"
      />
    </>
  );
}
