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
  Icon,
  MobileNavigation,
  NavigationItem,
  OperationPlayground,
  PageHeading,
  Panel,
  PanelHeader,
  ServiceAssignmentGraph,
  SessionCard,
  SessionPage,
  ShellErrorBoundary,
  StatCard,
  StatePanel,
  StatusPill,
  Toast,
  WorkspaceSelector,
  type IconName,
  type PlaygroundRunState,
  type PlaygroundValue,
  type ServiceAssignmentItem,
} from "@opendle/ui";
import {
  AdministrationApiError,
  createAdministrationClient,
  createRuntimeClient,
  errorMessage,
  isoRange,
  type ActivityEvent,
  type AdministrationClient,
  type AdministratorHealth,
  type AdministratorSession,
  type Assignment,
  type Credential,
  type Model,
  type ModelCapability,
  type ModelImportPreview,
  type ModelWrite,
  type ObservedRequirement,
  type OutputModality,
  type Price,
  type PriceSyncResult,
  type Provider,
  type ProviderAdapter,
  type ProviderModel,
  type ProviderModelWrite,
  type RequestLog,
  type RequestLogSummary,
  type RuntimeInputImage,
  type Service,
  type StatisticsResult,
  type Workspace,
} from "./api.js";
import { ServiceManagement } from "./ServiceManagement.js";
import {
  createScopeLoadGuard,
  initialAccessScopeState,
  reduceAccessScopeState,
} from "./accessState.js";
import {
  createInputImageSelectionQueue,
  parseManualPrice,
  usageUnits,
  validateInputImageSelection,
} from "./formContracts.js";
import { scheduleSessionExpiry } from "./sessionExpiry.js";
import { waitForMediaJob } from "./mediaPolling.js";

type Section =
  | "overview"
  | "services"
  | "access"
  | "providers"
  | "models"
  | "assignments"
  | "playground"
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
}
const routes: readonly {
  readonly id: Section;
  readonly label: string;
  readonly icon: IconName;
  readonly group: "Manage" | "Observe";
}[] = [
  { id: "overview", label: "Overview", icon: "grid", group: "Manage" },
  { id: "services", label: "Services", icon: "layers", group: "Manage" },
  { id: "access", label: "Workspaces & keys", icon: "key", group: "Manage" },
  { id: "providers", label: "Providers", icon: "server", group: "Manage" },
  { id: "models", label: "Models & prices", icon: "spark", group: "Manage" },
  { id: "assignments", label: "Assignments", icon: "layers", group: "Manage" },
  { id: "playground", label: "Playground", icon: "spark", group: "Manage" },
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

function currentSection(): Section {
  const value =
    typeof location === "undefined" ? "" : location.pathname.slice(1);
  return routes.some((route) => route.id === value)
    ? (value as Section)
    : "overview";
}
function selectedServiceFromLocation(): string {
  const search = typeof location === "undefined" ? "" : location.search;
  return new URLSearchParams(search).get("service") ?? "";
}
function displayTime(value: string | null | undefined): string {
  if (value == null) return "Never";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "Unavailable"
    : parsed.toLocaleString();
}
function words(value: string): readonly string[] {
  return [
    ...new Set(
      value.split(",").flatMap((item) => {
        const trimmed = item.trim();
        return trimmed === "" ? [] : [trimmed];
      }),
    ),
  ];
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
function correction(error: unknown): {
  readonly title: string;
  readonly message: string;
  readonly correction: string;
  readonly code?: string;
} {
  if (error instanceof AdministrationApiError)
    return {
      title: "The operation did not complete",
      message: error.message,
      correction:
        error.details?.reason ??
        (error.status === 401
          ? "Enter an active service key and try again."
          : "Check the selected service, workspace, route, and input."),
      code: error.code,
    };
  return {
    title: "The operation did not complete",
    message: errorMessage(error),
    correction:
      "Try again. If the problem continues, inspect the Router health summary.",
  };
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
      const path = `${globalThis.location.pathname}${globalThis.location.search}`;
      globalThis.location.assign(
        await client.startSession(path.startsWith("/") ? path : "/"),
      );
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
          failure ??
          (expired
            ? "Your local administrator session expired. Sign in again."
            : "Use an allowlisted Pocket ID identity.")
        }
        eyebrow="LLM Router administration"
        footer="A Pocket ID account does not give Router access. The subject must be on the deployment allowlist."
        icon={<Icon name="shield" size={25} />}
        title={expired ? "Your session expired" : "Administrator sign-in"}
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

function AccessPage({
  client,
  csrf,
  onNotice,
  selectedService,
  setPlaygroundKey,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly selectedService: string;
  readonly setPlaygroundKey: (key: string) => void;
}) {
  const [access, updateAccess] = useReducer(
    reduceAccessScopeState,
    selectedService,
    initialAccessScopeState,
  );
  const visibleAccess =
    access.service === selectedService
      ? access
      : initialAccessScopeState(selectedService);
  const { keys, phase, secret, workspaces } = visibleAccess;
  const accessLoadGuard = useRef(createScopeLoadGuard());
  const load = useCallback((): Promise<void> => {
    const generation = accessLoadGuard.current.begin();
    if (selectedService === "") {
      updateAccess({ type: "begin", service: selectedService });
      return Promise.resolve();
    }
    updateAccess({ type: "refresh", service: selectedService });
    return Promise.all([
      client.workspaces(selectedService),
      client.keys(selectedService),
    ])
      .then(([workspacePage, keyPage]) => {
        if (!accessLoadGuard.current.isCurrent(generation)) return;
        updateAccess({
          type: "success",
          service: selectedService,
          keys: keyPage.items,
          workspaces: workspacePage.items,
        });
      })
      .catch((error: unknown) => {
        if (!accessLoadGuard.current.isCurrent(generation)) return;
        updateAccess({ type: "failure", service: selectedService });
        onNotice("error", errorMessage(error));
      });
  }, [client, onNotice, selectedService]);
  useEffect(() => {
    const loadGuard = accessLoadGuard.current;
    updateAccess({ type: "begin", service: selectedService });
    const timer = globalThis.setTimeout(() => {
      void load();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
      loadGuard.invalidate();
    };
  }, [load, selectedService]);
  if (selectedService === "")
    return (
      <StatePanel kind="empty" title="Select a service">
        Select one service to manage its workspaces and API keys.
      </StatePanel>
    );
  if (phase === "loading")
    return <LoadingPage title="Loading service access" />;
  return (
    <div className="administration-page">
      <PageHeading
        description="Workspaces are accounting labels. Service keys are backend-only bearer credentials."
        eyebrow={selectedService}
        title="Workspaces and service keys"
      />
      {secret === null ? null : (
        <section aria-labelledby="one-time-key" className="secret-panel">
          <h2 id="one-time-key">Copy this key now</h2>
          <p>
            The Router will not show it again. Deploy it to the calling-service
            backend, then clear this panel.
          </p>
          <output>{secret}</output>
          <div>
            <Button
              onClick={() => {
                setPlaygroundKey(secret);
              }}
              variant="secondary"
            >
              Use in playground
            </Button>
            <Button
              onClick={() => {
                updateAccess({
                  type: "clear-secret",
                  service: selectedService,
                });
              }}
              variant="quiet"
            >
              Clear key
            </Button>
          </div>
        </section>
      )}
      {phase === "error" ? (
        <StatePanel kind="error" title="Service access is unavailable">
          <p>
            The Router could not load workspaces and keys for this service. No
            prior service data is shown.
          </p>
          <Button onClick={() => void load()}>Try again</Button>
        </StatePanel>
      ) : null}
      {phase === "error" ? null : (
        <div className="administration-sections">
          <Panel>
            <PanelHeader title="Workspaces" />
            <form
              className="administration-form"
              onSubmit={(event) => {
                event.preventDefault();
                const formElement = event.currentTarget;
                const form = new FormData(formElement);
                void client
                  .createWorkspace(
                    selectedService,
                    {
                      api_name: formText(form, "api_name"),
                      display_name: formText(form, "display_name"),
                    },
                    csrf,
                  )
                  .then(() => {
                    formElement.reset();
                    onNotice("success", "The workspace was created.");
                    return load();
                  })
                  .catch((error: unknown) => {
                    onNotice("error", errorMessage(error));
                  });
              }}
            >
              <label>
                API name
                <input name="api_name" required />
              </label>
              <label>
                Display name
                <input name="display_name" required />
              </label>
              <Button type="submit">Create workspace</Button>
            </form>
            <div className="administration-table-region">
              <table>
                <thead>
                  <tr>
                    <th>Workspace</th>
                    <th>Created</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {workspaces.length === 0 ? (
                    <EmptyTable columns={3} text="No workspaces" />
                  ) : (
                    workspaces.map((item) => (
                      <tr key={item.api_name}>
                        <th scope="row">
                          <strong>{item.display_name}</strong>
                          <small>{item.api_name}</small>
                        </th>
                        <td>{displayTime(item.created_at)}</td>
                        <td>
                          <Button
                            onClick={() =>
                              void client
                                .deleteWorkspace(
                                  selectedService,
                                  item.api_name,
                                  csrf,
                                )
                                .then(() => load())
                                .catch((error: unknown) => {
                                  onNotice("error", errorMessage(error));
                                })
                            }
                            variant="quiet"
                          >
                            Delete
                          </Button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </Panel>
          <Panel>
            <PanelHeader title="Service API keys" />
            <form
              className="administration-form service-key-form"
              onSubmit={(event) => {
                event.preventDefault();
                const formElement = event.currentTarget;
                const form = new FormData(formElement);
                void client
                  .createKey(selectedService, formText(form, "name"), csrf)
                  .then((created) => {
                    formElement.reset();
                    updateAccess({
                      type: "show-secret",
                      service: selectedService,
                      secret: created.secret,
                    });
                    onNotice("success", "The service API key was created.");
                    return load();
                  })
                  .catch((error: unknown) => {
                    onNotice("error", errorMessage(error));
                  });
              }}
            >
              <label>
                Key name
                <input name="name" required />
              </label>
              <Button type="submit">Create key</Button>
            </form>
            <div className="administration-table-region">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Last use</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.length === 0 ? (
                    <EmptyTable columns={3} text="No active keys" />
                  ) : (
                    keys.map((item) => (
                      <tr key={item.id}>
                        <th scope="row">{item.name}</th>
                        <td>{displayTime(item.last_used_at)}</td>
                        <td>
                          <Button
                            onClick={() =>
                              void client
                                .revokeKey(selectedService, item.id, csrf)
                                .then(() => load())
                                .catch((error: unknown) => {
                                  onNotice("error", errorMessage(error));
                                })
                            }
                            variant="quiet"
                          >
                            Revoke
                          </Button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}

function ProvidersPage({
  client,
  csrf,
  credentials,
  onNotice,
  onRefresh,
  providers,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly credentials: readonly Credential[];
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
  readonly providers: readonly Provider[];
}) {
  const adapters: readonly ProviderAdapter[] = [
    "openai",
    "openai_compatible",
    "openrouter",
    "custom",
    "wavespeed",
    "ollama",
    "local_embeddings",
    "fake",
  ];
  async function saveProvider(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const name = formText(form, "api_name");
    const endpoint = formText(form, "endpoint");
    const credential = formText(form, "credential");
    const value = {
      api_name: name,
      display_name: formText(form, "display_name"),
      adapter: formText(form, "adapter") as ProviderAdapter,
      enabled: form.get("enabled") === "on",
      ...(endpoint === "" ? {} : { endpoint }),
      ...(credential === "" ? {} : { credential_api_name: credential }),
    };
    try {
      if (providers.some((item) => item.api_name === name))
        await client.putProvider(name, value, csrf);
      else await client.createProvider(value, csrf);
      formElement.reset();
      await onRefresh();
      onNotice("success", "The provider connection was saved.");
    } catch (error) {
      onNotice("error", errorMessage(error));
    }
  }
  async function saveCredential(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const name = formText(form, "api_name");
    const secretValue = form.get("secret");
    const secret = typeof secretValue === "string" ? secretValue : "";
    try {
      if (credentials.some((item) => item.api_name === name))
        await client.replaceCredential(name, secret, csrf);
      else await client.createCredential(name, secret, csrf);
      formElement.reset();
      await onRefresh();
      onNotice(
        "success",
        "The encrypted credential was stored. The value is no longer available.",
      );
    } catch (error) {
      onNotice("error", errorMessage(error));
    }
  }
  return (
    <div className="administration-page">
      <PageHeading
        description="Connections and encrypted credentials are global. Credential values are write-only."
        eyebrow="Global catalog"
        title="Providers and credentials"
      />
      <div className="administration-sections">
        <Panel>
          <PanelHeader
            description="Use an existing API name to replace the complete current connection."
            title="Provider connection"
          />
          <form
            className="administration-form"
            onSubmit={(event) => void saveProvider(event)}
          >
            <label>
              API name
              <input name="api_name" required />
            </label>
            <label>
              Display name
              <input name="display_name" required />
            </label>
            <label>
              Adapter
              <select name="adapter">
                {adapters.map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </label>
            <label>
              Endpoint
              <input
                name="endpoint"
                placeholder="Optional exact endpoint"
                type="url"
              />
            </label>
            <label>
              Credential
              <select name="credential">
                <option value="">No credential</option>
                {credentials.map((item) => (
                  <option key={item.api_name} value={item.api_name}>
                    {item.api_name}
                  </option>
                ))}
              </select>
            </label>
            <label className="checkbox-field">
              <input defaultChecked name="enabled" type="checkbox" /> Enabled
            </label>
            <Button type="submit">Save provider</Button>
          </form>
        </Panel>
        <Panel>
          <PanelHeader
            description="Replacement changes the fingerprint and update time."
            title="Encrypted credentials"
          />
          <form
            className="administration-form"
            onSubmit={(event) => void saveCredential(event)}
          >
            <label>
              API name
              <input name="api_name" required />
            </label>
            <label>
              Secret
              <input
                autoComplete="new-password"
                name="secret"
                required
                type="password"
              />
            </label>
            <Button type="submit">Store or replace</Button>
          </form>
          <ul className="record-list">
            {credentials.length === 0 ? (
              <li>No credential metadata</li>
            ) : (
              credentials.map((item) => (
                <li key={item.api_name}>
                  <span>
                    <strong>{item.api_name}</strong>
                    <small>
                      Fingerprint {item.fingerprint} · Updated{" "}
                      {displayTime(item.updated_at)}
                    </small>
                  </span>
                  <Button
                    onClick={() =>
                      void client
                        .deleteCredential(item.api_name, csrf)
                        .then(onRefresh)
                        .catch((error: unknown) => {
                          onNotice("error", errorMessage(error));
                        })
                    }
                    variant="quiet"
                  >
                    Delete
                  </Button>
                </li>
              ))
            )}
          </ul>
        </Panel>
      </div>
      <Panel>
        <PanelHeader title="Current provider connections" />
        <div className="administration-table-region">
          <table>
            <thead>
              <tr>
                <th>Connection</th>
                <th>Adapter</th>
                <th>Endpoint</th>
                <th>Credential</th>
                <th>State</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {providers.length === 0 ? (
                <EmptyTable columns={6} text="No providers" />
              ) : (
                providers.map((item) => (
                  <tr key={item.api_name}>
                    <th scope="row">
                      <strong>{item.display_name}</strong>
                      <small>{item.api_name}</small>
                    </th>
                    <td>{item.adapter}</td>
                    <td>{item.endpoint ?? "Adapter default"}</td>
                    <td>{item.credential_api_name ?? "None"}</td>
                    <td>
                      <StatusPill tone={item.enabled ? "green" : "red"}>
                        {item.enabled ? "enabled" : "disabled"}
                      </StatusPill>
                    </td>
                    <td>
                      <Button
                        onClick={() =>
                          void client
                            .deleteProvider(item.api_name, csrf)
                            .then(onRefresh)
                            .catch((error: unknown) => {
                              onNotice("error", errorMessage(error));
                            })
                        }
                        variant="quiet"
                      >
                        Delete
                      </Button>
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

function manualPrice(form: FormData): Price | null {
  return parseManualPrice(
    formText(form, "currency"),
    formText(form, "unit_prices"),
  );
}

function modelConstraints(form: FormData) {
  const dimensions = words(formText(form, "dimensions")).map(Number);
  const maximumImages = formText(form, "max_input_images");
  const maximumImageBytes = formText(form, "max_input_image_bytes");
  const maximumDuration = formText(form, "max_output_duration_seconds");
  if (
    dimensions.length === 0 &&
    maximumImages === "" &&
    maximumImageBytes === "" &&
    maximumDuration === ""
  )
    return undefined;
  return {
    ...(dimensions.length === 0 ? {} : { embedding_dimensions: dimensions }),
    ...(maximumImages === ""
      ? {}
      : { max_input_images: Number(maximumImages) }),
    ...(maximumImageBytes === ""
      ? {}
      : { max_input_image_bytes: Number(maximumImageBytes) }),
    ...(maximumDuration === ""
      ? {}
      : { max_output_duration_seconds: Number(maximumDuration) }),
  };
}

function ConstraintFields() {
  return (
    <>
      <label>
        Embedding dimensions
        <input name="dimensions" placeholder="1536, 3072" />
      </label>
      <label>
        Maximum input images
        <input max={8} min={1} name="max_input_images" type="number" />
      </label>
      <label>
        Maximum input image bytes
        <input
          max={20_971_520}
          min={1}
          name="max_input_image_bytes"
          type="number"
        />
      </label>
      <label>
        Maximum output duration in seconds
        <input
          max={86_400}
          min={1}
          name="max_output_duration_seconds"
          type="number"
        />
      </label>
    </>
  );
}

function CanonicalModelEditor({
  onSubmit,
}: {
  readonly onSubmit: (event: SubmitEvent<HTMLFormElement>) => void;
}) {
  return (
    <Panel>
      <PanelHeader
        description="Comma-separate modality, capability, and embedding-dimension values."
        title="Canonical model"
      />
      <form className="administration-form" onSubmit={onSubmit}>
        <label>
          API name
          <input name="api_name" required />
        </label>
        <label>
          Display name
          <input name="display_name" required />
        </label>
        <label>
          Input modalities
          <input defaultValue="text" name="inputs" required />
        </label>
        <label>
          Output modalities
          <input defaultValue="text" name="outputs" required />
        </label>
        <label>
          Capabilities
          <input name="capabilities" placeholder="streaming, reasoning" />
        </label>
        <ConstraintFields />
        <label>
          Price source
          <input name="price_source" placeholder="Optional" />
        </label>
        <label>
          Source lookup key
          <input name="price_lookup_key" placeholder="Required with source" />
        </label>
        <PriceFields />
        <Button type="submit">Save canonical model</Button>
      </form>
    </Panel>
  );
}

function ProviderModelEditor({
  models,
  onSubmit,
  providers,
}: {
  readonly models: readonly Model[];
  readonly onSubmit: (event: SubmitEvent<HTMLFormElement>) => void;
  readonly providers: readonly Provider[];
}) {
  return (
    <Panel>
      <PanelHeader
        description="Optional capability fields narrow the canonical model."
        title="Provider-model mapping"
      />
      <form className="administration-form" onSubmit={onSubmit}>
        <label>
          API name
          <input name="api_name" required />
        </label>
        <label>
          Provider
          <select name="provider" required>
            <option value="">Select provider</option>
            {providers.map((item) => (
              <option key={item.api_name} value={item.api_name}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Canonical model
          <select name="model" required>
            <option value="">Select model</option>
            {models.map((item) => (
              <option key={item.api_name} value={item.api_name}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Provider wire model
          <input name="wire_model" required />
        </label>
        <label>
          Input override
          <input name="inputs" />
        </label>
        <label>
          Output override
          <input name="outputs" />
        </label>
        <label>
          Capability override
          <input name="capabilities" />
        </label>
        <label>
          Reasoning mappings
          <input name="reasoning" placeholder="none=disabled, high=high" />
        </label>
        <ConstraintFields />
        <label>
          Price source
          <input name="price_source" placeholder="Optional" />
        </label>
        <label>
          Source lookup key
          <input name="price_lookup_key" placeholder="Required with source" />
        </label>
        <label className="checkbox-field">
          <input defaultChecked name="enabled" type="checkbox" /> Enabled
        </label>
        <PriceFields />
        <Button type="submit">Save mapping</Button>
      </form>
    </Panel>
  );
}

function ProviderModelTable({
  client,
  csrf,
  onNotice,
  onRefresh,
  providerModels,
  setSync,
  sync,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
  readonly providerModels: readonly ProviderModel[];
  readonly setSync: (value: PriceSyncResult | null) => void;
  readonly sync: PriceSyncResult | null;
}) {
  return (
    <Panel>
      <PanelHeader
        actions={
          <Button
            onClick={() => {
              void client
                .synchronizePrices(null, csrf)
                .then((result) => {
                  setSync(result);
                  return onRefresh();
                })
                .catch((error: unknown) => {
                  onNotice("error", errorMessage(error));
                });
            }}
          >
            <Icon name="refresh" size={16} /> Synchronize now
          </Button>
        }
        description="The daily synchronization runs at 02:00 UTC. Missing or failed values keep the last accepted price."
        title="Provider-model availability and prices"
      />
      {sync === null ? null : (
        <p className="price-synchronization-status" role="status">
          Last synchronization:{" "}
          {sync.items
            .map((item) => `${item.provider_model_api_name} ${item.outcome}`)
            .join(" · ") || "No selected price rows"}
        </p>
      )}
      <div className="administration-table-region">
        <table>
          <thead>
            <tr>
              <th>Provider-model</th>
              <th>Route</th>
              <th>Capabilities</th>
              <th>Constraints</th>
              <th>Reasoning</th>
              <th>Price</th>
              <th>Cooldown</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {providerModels.length === 0 ? (
              <EmptyTable columns={8} text="No provider-model mappings" />
            ) : (
              providerModels.map((item) => (
                <tr key={item.api_name}>
                  <th scope="row">{item.api_name}</th>
                  <td>
                    {item.provider_api_name} / {item.provider_model_name}
                  </td>
                  <td>
                    {[
                      ...item.input_modalities,
                      ...item.output_modalities,
                      ...item.capabilities,
                    ].join(", ")}
                  </td>
                  <td>
                    {item.constraints == null
                      ? "None"
                      : JSON.stringify(item.constraints)}
                  </td>
                  <td>
                    {item.reasoning_mappings
                      .map(
                        (mapping) =>
                          `${mapping.level}=${mapping.provider_value}`,
                      )
                      .join(", ") || "None"}
                  </td>
                  <td>
                    {item.effective_price == null
                      ? "Unavailable"
                      : `${item.effective_price.currency} · ${item.effective_price.unit_prices.map((price) => `${price.unit} ${price.amount}`).join(", ")}`}
                  </td>
                  <td>
                    {item.cooldown == null
                      ? "Ready"
                      : `${item.cooldown.reason} until ${displayTime(item.cooldown.until)}`}
                  </td>
                  <td>
                    <Button
                      onClick={() => {
                        void client
                          .deleteProviderModel(item.api_name, csrf)
                          .then(onRefresh)
                          .catch((error: unknown) => {
                            onNotice("error", errorMessage(error));
                          });
                      }}
                      variant="quiet"
                    >
                      Delete
                    </Button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function CatalogImportPanel({
  client,
  csrf,
  onNotice,
  onRefresh,
  preview,
  providers,
  setPreview,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
  readonly preview: ModelImportPreview | null;
  readonly providers: readonly Provider[];
  readonly setPreview: (value: ModelImportPreview | null) => void;
}) {
  async function previewCatalog(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      setPreview(
        await client.previewImport(
          formText(new FormData(event.currentTarget), "provider"),
          csrf,
        ),
      );
    } catch (error) {
      onNotice("error", errorMessage(error));
    }
  }
  return (
    <Panel>
      <PanelHeader
        description="Preview makes no change. Select entries before import."
        title="Catalog import"
      />
      <form
        className="administration-form catalog-preview-request-form"
        onSubmit={(event) => {
          void previewCatalog(event);
        }}
      >
        <label>
          Provider
          <select name="provider" required>
            <option value="">Select provider</option>
            {providers.map((item) => (
              <option key={item.api_name} value={item.api_name}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        <Button type="submit">Preview catalog</Button>
      </form>
      {preview === null ? null : (
        <form
          className="catalog-preview"
          onSubmit={(event) => {
            event.preventDefault();
            const selectedKeys = new Set(
              new FormData(event.currentTarget).getAll("candidate"),
            );
            const selected = preview.candidates.flatMap((item) =>
              selectedKeys.has(item.catalog_key)
                ? [
                    {
                      catalog_key: item.catalog_key,
                      model_api_name: item.catalog_key
                        .toLowerCase()
                        .replaceAll(/[^a-z0-9-]/g, "-")
                        .slice(0, 63),
                      provider_model_api_name:
                        `${preview.provider_api_name}-${item.catalog_key}`
                          .toLowerCase()
                          .replaceAll(/[^a-z0-9-]/g, "-")
                          .slice(0, 63),
                    },
                  ]
                : [],
            );
            void client
              .importModels(preview.provider_api_name, selected, csrf)
              .then(() => {
                setPreview(null);
                return onRefresh();
              })
              .catch((error: unknown) => {
                onNotice("error", errorMessage(error));
              });
          }}
        >
          <ul>
            {preview.candidates.length === 0 ? (
              <li>No catalog entries</li>
            ) : (
              preview.candidates.map((item) => (
                <li key={item.catalog_key}>
                  <label>
                    <input
                      aria-label={`Import ${item.display_name}`}
                      name="candidate"
                      type="checkbox"
                      value={item.catalog_key}
                    />
                    <span>
                      <strong>{item.display_name}</strong>
                      <small>
                        {item.provider_model_name} ·{" "}
                        {[...item.output_modalities, ...item.capabilities].join(
                          ", ",
                        )}
                      </small>
                    </span>
                  </label>
                </li>
              ))
            )}
          </ul>
          <Button disabled={preview.candidates.length === 0} type="submit">
            Import selected entries
          </Button>
        </form>
      )}
    </Panel>
  );
}

function CanonicalModelTable({
  client,
  csrf,
  models,
  onNotice,
  onRefresh,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly models: readonly Model[];
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
}) {
  return (
    <Panel>
      <PanelHeader title="Canonical models" />
      <div className="administration-table-region">
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th>Inputs</th>
              <th>Outputs</th>
              <th>Capabilities</th>
              <th>Constraints</th>
              <th>Price</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {models.length === 0 ? (
              <EmptyTable columns={7} text="No canonical models" />
            ) : (
              models.map((item) => (
                <tr key={item.api_name}>
                  <th scope="row">
                    <strong>{item.display_name}</strong>
                    <small>{item.api_name}</small>
                  </th>
                  <td>{item.input_modalities.join(", ")}</td>
                  <td>{item.output_modalities.join(", ")}</td>
                  <td>{item.capabilities.join(", ") || "None"}</td>
                  <td>
                    {item.constraints == null
                      ? "None"
                      : JSON.stringify(item.constraints)}
                  </td>
                  <td>
                    {item.current_price == null
                      ? "Unavailable"
                      : `${item.current_price.currency} · ${String(item.current_price.unit_prices.length)} units`}
                  </td>
                  <td>
                    <Button
                      onClick={() => {
                        void client
                          .deleteModel(item.api_name, csrf)
                          .then(onRefresh)
                          .catch((error: unknown) => {
                            onNotice("error", errorMessage(error));
                          });
                      }}
                      variant="quiet"
                    >
                      Delete
                    </Button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function ModelsPage({
  client,
  csrf,
  models,
  onNotice,
  onRefresh,
  providerModels,
  providers,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly models: readonly Model[];
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
  readonly providerModels: readonly ProviderModel[];
  readonly providers: readonly Provider[];
}) {
  const [preview, setPreview] = useState<ModelImportPreview | null>(null);
  const [sync, setSync] = useState<PriceSyncResult | null>(null);
  async function saveModel(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const name = formText(form, "api_name");
    const constraints = modelConstraints(form);
    let price: Price | null;
    try {
      price = manualPrice(form);
    } catch (error) {
      onNotice(
        "error",
        error instanceof Error ? error.message : "The manual price is invalid.",
      );
      return;
    }
    const value: ModelWrite = {
      api_name: name,
      display_name: formText(form, "display_name"),
      input_modalities: words(formText(form, "inputs")) as readonly (
        "text" | "image"
      )[],
      output_modalities: words(
        formText(form, "outputs"),
      ) as readonly OutputModality[],
      capabilities: words(
        formText(form, "capabilities"),
      ) as readonly ModelCapability[],
      ...(constraints === undefined ? {} : { constraints }),
      ...(formText(form, "price_source") === ""
        ? {}
        : {
            price_source: formText(form, "price_source"),
            price_lookup_key: formText(form, "price_lookup_key"),
          }),
      manual_price: price,
    };
    try {
      if (models.some((item) => item.api_name === name))
        await client.putModel(name, value, csrf);
      else await client.createModel(value, csrf);
      formElement.reset();
      await onRefresh();
      onNotice("success", "The canonical model was saved.");
    } catch (error) {
      onNotice("error", errorMessage(error));
    }
  }
  async function saveMapping(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const name = formText(form, "api_name");
    const constraints = modelConstraints(form);
    let price: Price | null;
    try {
      price = manualPrice(form);
    } catch (error) {
      onNotice(
        "error",
        error instanceof Error ? error.message : "The manual price is invalid.",
      );
      return;
    }
    const reasoning = words(formText(form, "reasoning")).map((item) => {
      const [level, providerValue] = item.split("=");
      return {
        level: (level ?? "none") as "none",
        provider_value: providerValue ?? level ?? "none",
      };
    });
    const value: ProviderModelWrite = {
      api_name: name,
      provider_api_name: formText(form, "provider"),
      model_api_name: formText(form, "model"),
      provider_model_name: formText(form, "wire_model"),
      enabled: form.get("enabled") === "on",
      ...(formText(form, "inputs") === ""
        ? {}
        : {
            input_modalities: words(formText(form, "inputs")) as readonly (
              "text" | "image"
            )[],
          }),
      ...(formText(form, "outputs") === ""
        ? {}
        : {
            output_modalities: words(
              formText(form, "outputs"),
            ) as readonly OutputModality[],
          }),
      ...(formText(form, "capabilities") === ""
        ? {}
        : {
            capabilities: words(
              formText(form, "capabilities"),
            ) as readonly ModelCapability[],
          }),
      ...(reasoning.length === 0 ? {} : { reasoning_mappings: reasoning }),
      ...(constraints === undefined ? {} : { constraints }),
      ...(formText(form, "price_source") === ""
        ? {}
        : {
            price_source: formText(form, "price_source"),
            price_lookup_key: formText(form, "price_lookup_key"),
          }),
      manual_price: price,
    };
    try {
      if (providerModels.some((item) => item.api_name === name))
        await client.putProviderModel(name, value, csrf);
      else await client.createProviderModel(value, csrf);
      formElement.reset();
      await onRefresh();
      onNotice("success", "The provider-model mapping was saved.");
    } catch (error) {
      onNotice("error", errorMessage(error));
    }
  }
  return (
    <div className="administration-page">
      <PageHeading
        description="Manage canonical capabilities, provider mappings, fixed-decimal prices, and catalog imports."
        eyebrow="Global catalog"
        title="Models and prices"
      />
      <div className="administration-sections">
        <CanonicalModelEditor
          onSubmit={(event) => {
            void saveModel(event);
          }}
        />
        <ProviderModelEditor
          models={models}
          onSubmit={(event) => {
            void saveMapping(event);
          }}
          providers={providers}
        />
      </div>
      <ProviderModelTable
        client={client}
        csrf={csrf}
        onNotice={onNotice}
        onRefresh={onRefresh}
        providerModels={providerModels}
        setSync={setSync}
        sync={sync}
      />
      <CatalogImportPanel
        client={client}
        csrf={csrf}
        onNotice={onNotice}
        onRefresh={onRefresh}
        preview={preview}
        providers={providers}
        setPreview={setPreview}
      />
      <CanonicalModelTable
        client={client}
        csrf={csrf}
        models={models}
        onNotice={onNotice}
        onRefresh={onRefresh}
      />
    </div>
  );
}
function PriceFields() {
  return (
    <>
      <label>
        Manual price currency
        <input maxLength={3} name="currency" placeholder="USD" />
      </label>
      <label>
        Typed unit amounts
        <textarea
          name="unit_prices"
          placeholder="input_token=0.001, output_token=0.002"
          rows={3}
        />
        <small>
          Use unit=amount pairs. Supported units: {usageUnits.join(", ")}.
        </small>
      </label>
    </>
  );
}

export function AssignmentsPage({
  assignments,
  client,
  csrf,
  onNotice,
  onRefresh,
  providerModels,
  selectedService,
}: {
  readonly assignments: readonly Assignment[];
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
  readonly providerModels: readonly ProviderModel[];
  readonly selectedService: string;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const directLocalAssignments = new Set(
    assignments.flatMap((item) =>
      item.definition_kind === "direct_chain" &&
      item.defined_by_service_api_name === selectedService
        ? [item.api_name]
        : [],
    ),
  );
  if (selectedService === "")
    return (
      <StatePanel kind="empty" title="Select a service">
        Select one service to inspect and configure its effective assignments.
      </StatePanel>
    );
  const graphItems: readonly ServiceAssignmentItem[] = assignments.map(
    (item) => ({
      id: item.api_name,
      name: item.display_name,
      source:
        item.definition_kind === "implicit"
          ? {
              kind: "implicit",
              label: item.defined_by_service_api_name ?? selectedService,
            }
          : item.defined_by_service_api_name === selectedService
            ? { kind: "direct", label: selectedService }
            : {
                kind: "inherited",
                label: item.defined_by_service_api_name ?? "parent service",
              },
      candidates: item.effective_chain.map((candidate) => ({
        id: candidate.provider_model_api_name,
        label: candidate.provider_model_api_name,
      })),
      ...(item.inherits_assignment_api_name == null
        ? {}
        : { inheritsFrom: item.inherits_assignment_api_name }),
      isDefault: item.api_name === "default",
      lastUsed:
        item.last_used_at == null
          ? null
          : {
              label: displayTime(item.last_used_at),
              dateTime: item.last_used_at,
            },
      observedRequirements: item.observed_requirements,
    }),
  );
  async function save(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const mode = formText(form, "mode");
    const reasoning = formText(form, "reasoning");
    const displayName = formText(form, "display_name");
    const value =
      mode === "inherit"
        ? {
            ...(displayName === "" ? {} : { display_name: displayName }),
            inherits_assignment_api_name: formText(form, "inherits"),
            ...(reasoning === ""
              ? {}
              : { reasoning_level: reasoning as "none" }),
          }
        : {
            ...(displayName === "" ? {} : { display_name: displayName }),
            direct_chain: words(formText(form, "chain")).map((name) => ({
              provider_model_api_name: name,
            })),
            ...(reasoning === ""
              ? {}
              : { reasoning_level: reasoning as "none" }),
          };
    try {
      await client.putAssignment(
        selectedService,
        formText(form, "api_name"),
        value,
        csrf,
      );
      formElement.reset();
      await onRefresh();
      onNotice("success", "The assignment was saved.");
    } catch (error) {
      onNotice("error", errorMessage(error));
    }
  }
  return (
    <div className="administration-page">
      <PageHeading
        description="The nearest service definition replaces the complete inherited fallback chain."
        eyebrow={selectedService}
        title="Assignment graph"
      />
      <Panel>
        <PanelHeader
          description="Use one inherited assignment name or one ordered comma-separated provider-model chain."
          title="Create or replace a local assignment"
        />
        <form
          className="administration-form"
          onSubmit={(event) => void save(event)}
        >
          <label>
            Assignment API name
            <input name="api_name" required />
          </label>
          <label>
            Display name
            <input name="display_name" />
          </label>
          <label>
            Definition
            <select name="mode">
              <option value="direct">Direct chain</option>
              <option value="inherit">Inherit assignment</option>
            </select>
          </label>
          <label>
            Ordered direct chain
            <input
              list="provider-model-options"
              name="chain"
              placeholder="primary, fallback"
            />
            <datalist id="provider-model-options">
              {providerModels.map((item) => (
                <option key={item.api_name}>{item.api_name}</option>
              ))}
            </datalist>
          </label>
          <label>
            Inherited assignment
            <input name="inherits" placeholder="default" />
          </label>
          <label>
            Reasoning
            <select name="reasoning">
              <option value="">Model default</option>
              <option>none</option>
              <option>low</option>
              <option>medium</option>
              <option>high</option>
            </select>
          </label>
          <Button type="submit">Save assignment</Button>
        </form>
      </Panel>
      <ServiceAssignmentGraph
        actionsForAssignment={(item) => {
          const canDelete = directLocalAssignments.has(item.id);
          if (!canDelete && item.observedRequirements.length === 0) return null;
          return (
            <div className="assignment-actions">
              {canDelete ? (
                <Button
                  onClick={() =>
                    void client
                      .deleteAssignment(selectedService, item.id, csrf)
                      .then(onRefresh)
                      .catch((error: unknown) => {
                        onNotice("error", errorMessage(error));
                      })
                  }
                  variant="quiet"
                >
                  Delete local definition
                </Button>
              ) : null}
              {item.observedRequirements.map((requirement) => (
                <Button
                  key={requirement}
                  onClick={() =>
                    void client
                      .removeRequirement(
                        selectedService,
                        item.id,
                        requirement as ObservedRequirement,
                        csrf,
                      )
                      .then(onRefresh)
                      .catch((error: unknown) => {
                        onNotice("error", errorMessage(error));
                      })
                  }
                  variant="quiet"
                >
                  Remove {requirement}
                </Button>
              ))}
            </div>
          );
        }}
        aria-label={`${selectedService} assignment configuration`}
        assignments={graphItems}
        id="router-assignments"
        onSelectionChange={setSelected}
        selectedAssignmentId={selected}
      />
    </div>
  );
}

interface PlaygroundPageState {
  readonly inputImages: readonly PlaygroundImage[];
  readonly serviceKey: string;
  readonly workspace: string;
  readonly tags: string;
  readonly runState: PlaygroundRunState;
  readonly value: PlaygroundValue;
}

interface PlaygroundImage extends RuntimeInputImage {
  readonly id: string;
  readonly name: string;
  readonly detail: string;
  readonly sizeBytes: number;
}

async function readInputImage(file: File): Promise<PlaygroundImage> {
  validateInputImageSelection([], [file]);
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      if (typeof reader.result === "string") resolve(reader.result);
      else reject(new Error("The image could not be read."));
    });
    reader.addEventListener("error", () => {
      reject(new Error("The image could not be read."));
    });
    reader.readAsDataURL(file);
  });
  const marker = dataUrl.indexOf(",");
  return {
    id: globalThis.crypto.randomUUID(),
    name: file.name,
    detail: `${String(Math.ceil(file.size / 1024))} KB`,
    sizeBytes: file.size,
    media_type: file.type as RuntimeInputImage["media_type"],
    data_base64: dataUrl.slice(marker + 1),
  };
}

function PlaygroundView({
  assignments,
  onNotice,
  onRun,
  playground,
  providerModels,
  selectedService,
  updatePlayground,
  workspaces,
}: {
  readonly assignments: readonly Assignment[];
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRun: (value: PlaygroundValue) => Promise<void>;
  readonly playground: PlaygroundPageState;
  readonly providerModels: readonly ProviderModel[];
  readonly selectedService: string;
  readonly updatePlayground: (patch: Partial<PlaygroundPageState>) => void;
  readonly workspaces: readonly Workspace[];
}) {
  const inputImageQueue = useRef(
    createInputImageSelectionQueue(playground.inputImages, (inputImages) => {
      updatePlayground({ inputImages });
    }),
  );
  return (
    <div className="administration-page">
      <PageHeading
        description="Calls use the native service API. The key stays only in this page memory."
        eyebrow={
          selectedService === "" ? "No service selected" : selectedService
        }
        title="Model and media playground"
      />
      <Panel>
        <PanelHeader
          description="Use a key that belongs to the selected service. The browser does not save it."
          title="Service call context"
        />
        <div className="administration-form">
          <label>
            Service API key
            <input
              autoComplete="off"
              onChange={(event) => {
                updatePlayground({ serviceKey: event.currentTarget.value });
              }}
              type="password"
              value={playground.serviceKey}
            />
          </label>
          <label>
            Workspace
            <select
              onChange={(event) => {
                updatePlayground({ workspace: event.currentTarget.value });
              }}
              value={playground.workspace}
            >
              <option value="">Select workspace</option>
              {workspaces.map((item) => (
                <option key={item.api_name} value={item.api_name}>
                  {item.display_name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Tags
            <input
              onChange={(event) => {
                updatePlayground({ tags: event.currentTarget.value });
              }}
              placeholder="evaluation, manual"
              value={playground.tags}
            />
          </label>
        </div>
      </Panel>
      <OperationPlayground
        assignmentOptions={assignments.map((item) => ({
          id: item.api_name,
          label: item.display_name,
          detail:
            item.effective_chain.length === 0
              ? "Empty effective chain"
              : `${String(item.effective_chain.length)} candidates`,
          disabled: item.effective_chain.length === 0,
        }))}
        description="Run model, embedding, image, video, and audio operations through an assignment or one exact provider-model."
        id="router-playground"
        inputImages={playground.inputImages}
        onAddInputImages={(files) => {
          void inputImageQueue.current
            .add(files, readInputImage)
            .catch((error: unknown) => {
              onNotice(
                "error",
                error instanceof Error
                  ? error.message
                  : "The image could not be read.",
              );
            });
        }}
        onRemoveInputImage={(imageId) => {
          inputImageQueue.current.remove((image) => image.id === imageId);
        }}
        onReset={() => {
          updatePlayground({ runState: { status: "empty" } });
        }}
        onRun={(next) => {
          void onRun(next);
        }}
        onValueChange={(value) => {
          updatePlayground({ value });
        }}
        providerModelOptions={providerModels.flatMap((item) =>
          item.enabled
            ? [
                {
                  id: item.api_name,
                  label: item.api_name,
                  detail: item.provider_model_name,
                },
              ]
            : [],
        )}
        runState={playground.runState}
        title="Operation playground"
        value={playground.value}
      />
    </div>
  );
}

function PlaygroundPage({
  assignments,
  initialKey,
  onNotice,
  providerModels,
  selectedService,
  workspaces,
}: {
  readonly assignments: readonly Assignment[];
  readonly initialKey: string;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly providerModels: readonly ProviderModel[];
  readonly selectedService: string;
  readonly workspaces: readonly Workspace[];
}) {
  const [playground, updatePlayground] = useReducer(
    (state: PlaygroundPageState, patch: Partial<PlaygroundPageState>) => ({
      ...state,
      ...patch,
    }),
    {
      inputImages: [],
      serviceKey: initialKey,
      workspace: workspaces[0]?.api_name ?? "",
      tags: "",
      runState: {
        status: "empty",
        message:
          "Run one operation to see its selected route, latency, usage, cost, and output.",
      },
      value: {
        operation: "model",
        selection: { kind: "assignment", id: assignments[0]?.api_name ?? "" },
        input: "",
        systemPrompt: "",
        temperature: null,
        outputLimit: null,
      },
    },
  );
  const objectUrl = useRef<string | null>(null);
  useEffect(
    () => () => {
      if (objectUrl.current !== null) URL.revokeObjectURL(objectUrl.current);
    },
    [],
  );
  async function run(next: PlaygroundValue) {
    if (playground.serviceKey === "" || playground.workspace === "") {
      updatePlayground({
        runState: {
          status: "error",
          error: {
            title: "Service access is required",
            message:
              "The playground needs one active service key and owned workspace.",
            correction: "Enter the one-time key and select its workspace.",
          },
        },
      });
      return;
    }
    updatePlayground({
      runState: {
        status: "loading",
        message: "The Router is calling the selected route.",
      },
    });
    const started = performance.now();
    const selector =
      next.selection.kind === "assignment"
        ? { assignment_api_name: next.selection.id }
        : { provider_model_api_name: next.selection.id };
    const client = createRuntimeClient(playground.serviceKey);
    try {
      if (next.operation === "model") {
        const result = await client.model(
          playground.workspace,
          selector,
          next.input,
          next.systemPrompt,
          playground.inputImages,
          next.temperature,
          next.outputLimit,
          words(playground.tags),
        );
        const output =
          result.output_type === "structured_json"
            ? {
                kind: "json" as const,
                content: result.structured_output_json ?? "",
              }
            : {
                kind: "text" as const,
                content:
                  result.content
                    ?.map((item) =>
                      item.type === "text"
                        ? item.text
                        : `${item.name}(${item.arguments_json})`,
                    )
                    .join("\n") ?? "",
              };
        updatePlayground({
          runState: {
            status: "success",
            result: {
              output,
              selectedRoute: {
                label: result.provider_model_api_name,
                detail:
                  next.selection.kind === "assignment"
                    ? `Selected by ${next.selection.id}`
                    : "Exact provider-model",
              },
              latencyMs: Math.round(performance.now() - started),
              usage: result.usage.units.map((item) => ({
                id: item.unit,
                label: item.unit,
                value: item.quantity,
              })),
              cost: {
                amount: result.usage.cost,
                currency: result.usage.currency,
              },
            },
          },
        });
        return;
      }
      if (next.operation === "embedding") {
        const result = await client.embedding(
          playground.workspace,
          selector,
          next.input.split("\n").filter(Boolean),
          words(playground.tags),
        );
        updatePlayground({
          runState: {
            status: "success",
            result: {
              output: {
                kind: "embedding",
                vectorCount: result.embeddings.length,
                dimensions: result.embeddings[0]?.values.length ?? 0,
                ...(result.embeddings[0] === undefined
                  ? {}
                  : { preview: result.embeddings[0].values.slice(0, 8) }),
              },
              selectedRoute: { label: result.provider_model_api_name },
              latencyMs: Math.round(performance.now() - started),
              usage: result.usage.units.map((item) => ({
                id: item.unit,
                label: item.unit,
                value: item.quantity,
              })),
              cost: {
                amount: result.usage.cost,
                currency: result.usage.currency,
              },
            },
          },
        });
        return;
      }
      const job = await client.createMedia(
        playground.workspace,
        selector,
        next.operation,
        next.input,
        next.operation === "audio" ? [] : playground.inputImages,
        words(playground.tags),
      );
      const current = await waitForMediaJob(client, job);
      if (current.state !== "succeeded")
        throw new AdministrationApiError(
          502,
          current.error?.code ?? "upstream_failed",
          current.error?.message ?? "The media job did not succeed.",
          current.error?.details ?? undefined,
        );
      const blob = await client.mediaContent(job.id);
      if (objectUrl.current !== null) URL.revokeObjectURL(objectUrl.current);
      objectUrl.current = URL.createObjectURL(blob);
      updatePlayground({
        runState: {
          status: "success",
          result: {
            output: {
              kind: next.operation,
              objectUrl: objectUrl.current,
              label: `${next.operation} result`,
              mediaType: blob.type,
            },
            selectedRoute: {
              label: current.provider_model_api_name,
              detail: `Media job ${current.id}`,
            },
            latencyMs: Math.round(performance.now() - started),
            usage: [],
            cost: null,
          },
        },
      });
    } catch (error) {
      updatePlayground({
        runState: { status: "error", error: correction(error) },
      });
      onNotice("error", errorMessage(error));
    }
  }
  return (
    <PlaygroundView
      assignments={assignments}
      onNotice={onNotice}
      onRun={run}
      playground={playground}
      providerModels={providerModels}
      selectedService={selectedService}
      updatePlayground={updatePlayground}
      workspaces={workspaces}
    />
  );
}

interface LogsPageState {
  readonly from: string;
  readonly to: string;
  readonly items: readonly RequestLogSummary[];
  readonly detail: RequestLog | null;
  readonly phase: "idle" | "loading" | "error";
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
  useEffect(
    () => () => {
      if (mediaUrl.current !== null) URL.revokeObjectURL(mediaUrl.current);
    },
    [],
  );
  async function load() {
    updateLogs({ phase: "loading" });
    try {
      const items = (
        await client.requestLogs(
          new Date(logs.from).toISOString(),
          new Date(logs.to).toISOString(),
        )
      ).items;
      updateLogs({ items, phase: "idle" });
    } catch (error) {
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
                          onClick={() =>
                            void client
                              .requestLog(item.id)
                              .then((value) => {
                                updateLogs({ detail: value });
                              })
                              .catch((error: unknown) => {
                                onNotice("error", errorMessage(error));
                              })
                          }
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
        <Panel aria-live="polite">
          <PanelHeader
            actions={
              <Button
                onClick={() => {
                  updateLogs({ detail: null });
                }}
                variant="quiet"
              >
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
                        {item.role} · {item.media_type} ·{" "}
                        {String(item.size_bytes)} bytes
                      </span>
                      <Button
                        onClick={() =>
                          void client
                            .requestLogMedia(detail.summary.id, item.id)
                            .then((blob) => {
                              if (mediaUrl.current !== null)
                                URL.revokeObjectURL(mediaUrl.current);
                              mediaUrl.current = URL.createObjectURL(blob);
                              globalThis.open(
                                mediaUrl.current,
                                "_blank",
                                "noopener,noreferrer",
                              );
                            })
                            .catch((error: unknown) => {
                              onNotice(
                                "error",
                                `The retained media is unavailable. ${errorMessage(error)}`,
                              );
                            })
                        }
                        variant="quiet"
                      >
                        Open retained media
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </Panel>
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
  async function load(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const outcome = formText(form, "outcome");
    try {
      setResult(
        await client.statistics({
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
          ...(outcome === "succeeded" || outcome === "failed"
            ? { outcome }
            : {}),
          ...(formText(form, "tag") === ""
            ? {}
            : { tag: formText(form, "tag") }),
          group_by: form.getAll("group_by").map(String),
        }),
      );
    } catch (error) {
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
  useEffect(() => {
    void client
      .activity(range.from, range.to)
      .then((page) => {
        setActivity(page.items);
      })
      .catch((error: unknown) => {
        onNotice("error", errorMessage(error));
      });
  }, [client, onNotice, range.from, range.to]);
  const cooldowns = providerModels.filter((item) => item.cooldown != null);
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
              event.preventDefault();
              void client
                .putRetention(
                  Number(formText(new FormData(event.currentTarget), "days")),
                  csrf,
                )
                .then(onRefresh)
                .catch((error: unknown) => {
                  onNotice("error", errorMessage(error));
                });
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
          description="This is a basic activity record. It is not immutable configuration history."
          title="Configuration activity, last 7 days"
        />
        <div className="administration-table-region">
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
                <EmptyTable columns={5} text="No retained activity" />
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
  readonly playgroundKey: string;
  readonly section: Section;
  readonly selectedService: string;
  readonly sessionState: {
    readonly status: "loading" | "active" | "signed-out" | "expired" | "denied";
    readonly session?: AdministratorSession;
    readonly message?: string;
  };
  readonly workspaces: readonly Workspace[];
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
  onPlaygroundKey,
  playgroundKey,
  section,
  selectService,
  selectedService,
  session,
  workspaces,
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
  readonly onPlaygroundKey: (key: string) => void;
  readonly playgroundKey: string;
  readonly section: Section;
  readonly selectService: (value: string) => void;
  readonly selectedService: string;
  readonly session: AdministratorSession;
  readonly workspaces: readonly Workspace[];
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
  if (data !== null && section === "access")
    content = (
      <AccessPage
        client={client}
        csrf={session.csrf_token}
        key={selectedService}
        onNotice={notify}
        selectedService={selectedService}
        setPlaygroundKey={onPlaygroundKey}
      />
    );
  if (data !== null && section === "providers")
    content = (
      <ProvidersPage
        client={client}
        credentials={data.credentials}
        csrf={session.csrf_token}
        onNotice={notify}
        onRefresh={loadGlobal}
        providers={data.providers}
      />
    );
  if (data !== null && section === "models")
    content = (
      <ModelsPage
        client={client}
        csrf={session.csrf_token}
        models={data.models}
        onNotice={notify}
        onRefresh={loadGlobal}
        providerModels={data.providerModels}
        providers={data.providers}
      />
    );
  if (data !== null && section === "assignments")
    content = (
      <AssignmentsPage
        assignments={assignments}
        client={client}
        csrf={session.csrf_token}
        onNotice={notify}
        onRefresh={loadScope}
        providerModels={data.providerModels}
        selectedService={selectedService}
      />
    );
  if (data !== null && section === "playground")
    content = (
      <PlaygroundPage
        assignments={assignments}
        initialKey={playgroundKey}
        key={`${selectedService}:${assignments.map((item) => item.api_name).join(",")}:${workspaces.map((item) => item.api_name).join(",")}`}
        onNotice={notify}
        providerModels={data.providerModels}
        selectedService={selectedService}
        workspaces={workspaces}
      />
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

export function App({ client = defaultAdministrationClient }: AppProps) {
  const [main, update] = useReducer(
    (state: MainState, patch: Partial<MainState>) => ({ ...state, ...patch }),
    undefined,
    (): MainState => ({
      assignments: [],
      data: null,
      failure: null,
      notice: null,
      playgroundKey: "",
      section: currentSection(),
      selectedService: selectedServiceFromLocation(),
      sessionState: { status: "loading" },
      workspaces: [],
    }),
  );
  const {
    assignments,
    data,
    failure,
    notice,
    playgroundKey,
    section,
    selectedService,
    sessionState,
    workspaces,
  } = main;
  const notify = useCallback(
    (nextTone: "success" | "error", message: string) => {
      update({ notice: { tone: nextTone, message } });
    },
    [],
  );
  const scopeLoadGuard = useRef(createScopeLoadGuard());
  const inspectSession = useCallback(async () => {
    try {
      update({
        sessionState: { status: "active", session: await client.session() },
      });
    } catch (error) {
      if (error instanceof AdministrationApiError && error.status === 403)
        update({ sessionState: { status: "denied", message: error.message } });
      else update({ sessionState: { status: "signed-out" } });
    }
  }, [client]);
  const loadGlobal = useCallback(async () => {
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
        client.services(),
        client.providers(),
        client.models(),
        client.providerModels(),
        client.credentials(),
        client.health(),
        client.retention(),
      ]);
      update({
        data: {
          services: services.items,
          providers: providers.items,
          models: models.items,
          providerModels: providerModels.items,
          credentials: credentials.items,
          health,
          retentionDays: retention.duration_days,
        },
      });
      if (
        selectedService !== "" &&
        !services.items.some((item) => item.api_name === selectedService)
      )
        update({ playgroundKey: "", selectedService: "" });
    } catch (error) {
      if (error instanceof AdministrationApiError && error.status === 401) {
        update({ sessionState: { status: "expired" } });
        return;
      }
      update({ failure: errorMessage(error) });
    }
  }, [client, selectedService]);
  const loadScope = useCallback((): Promise<void> => {
    const generation = scopeLoadGuard.current.begin();
    if (selectedService === "") {
      update({ assignments: [], workspaces: [] });
      return Promise.resolve();
    }
    update({ assignments: [], workspaces: [] });
    return Promise.all([
      client.assignments(selectedService),
      client.workspaces(selectedService),
    ])
      .then(([assignmentPage, workspacePage]) => {
        if (!scopeLoadGuard.current.isCurrent(generation)) return;
        update({
          assignments: assignmentPage.items,
          workspaces: workspacePage.items,
        });
      })
      .catch((error: unknown) => {
        if (!scopeLoadGuard.current.isCurrent(generation)) return;
        update({ assignments: [], workspaces: [] });
        notify("error", errorMessage(error));
      });
  }, [client, notify, selectedService]);
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
    return scheduleSessionExpiry(sessionExpiresAt, () => {
      scopeLoadGuard.current.invalidate();
      update({
        assignments: [],
        data: null,
        playgroundKey: "",
        sessionState: { status: "expired" },
        workspaces: [],
      });
    });
  }, [sessionExpiresAt, sessionState.status]);
  useEffect(() => {
    const restoreLocation = () => {
      scopeLoadGuard.current.invalidate();
      update({
        assignments: [],
        playgroundKey: "",
        section: currentSection(),
        selectedService: selectedServiceFromLocation(),
        workspaces: [],
      });
    };
    globalThis.addEventListener("popstate", restoreLocation);
    return () => {
      globalThis.removeEventListener("popstate", restoreLocation);
    };
  }, []);
  function selectService(value: string) {
    scopeLoadGuard.current.invalidate();
    update({
      assignments: [],
      playgroundKey: "",
      selectedService: value,
      workspaces: [],
    });
    const url = new URL(globalThis.location.href);
    if (value === "") url.searchParams.delete("service");
    else url.searchParams.set("service", value);
    globalThis.history.replaceState({}, "", `${url.pathname}${url.search}`);
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
  return (
    <AuthenticatedAdministration
      assignments={assignments}
      client={client}
      data={data}
      failure={failure}
      loadGlobal={loadGlobal}
      loadScope={loadScope}
      navigate={navigate}
      notice={notice}
      notify={notify}
      onDismissNotice={() => {
        update({ notice: null });
      }}
      onPlaygroundKey={(key) => {
        update({ playgroundKey: key });
      }}
      playgroundKey={playgroundKey}
      section={section}
      selectService={selectService}
      selectedService={selectedService}
      session={session}
      workspaces={workspaces}
    />
  );
}
