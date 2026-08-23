import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
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
  type IconName,
} from "@opendle/ui";
import {
  activateLocalAdministrator,
  configurationRevisionForScope,
  consumeTrustedGrantToken,
  createFetchAdministrationClient,
  endAdministratorSession,
  errorMessage,
  inspectLocalAdministratorSession,
  newLogicalRequestId,
  scheduleAdministrationSessionInspection,
  scopeFromSearch,
  scopeSearch,
  startPocketIDAdministratorSession,
  startPocketIDRecentAuthentication,
  AdministrationApiError,
  type AccountingSummary,
  type AdministrationClient,
  type AdministrationSnapshot,
  type Assignment,
  type AuditEvent,
  type BudgetSummary,
  type CatalogEntry,
  type Credential,
  type DiagnosticPhase,
  type DiagnosticRun,
  type ProviderInstance,
  type ProviderModelRoute,
  type RequestStatus,
  type RequestAttemptStatus,
  type RequestFailureClass,
  type ScopeSelection,
  type ServiceCreated,
  type ServiceSummary,
} from "./api.js";
import { recoverAfterMutationFailure } from "./mutationRecovery.js";
import { ServiceManagement } from "./ServiceManagement.js";

const initialTrustedGrantToken =
  typeof window === "undefined" ? undefined : consumeTrustedGrantToken();

type Section =
  | "overview"
  | "services"
  | "credentials"
  | "audit"
  | "setup"
  | "configuration"
  | "assignments"
  | "requests"
  | "diagnostics"
  | "accounting"
  | "budgets";
type SessionAction =
  "idle" | "sign_in_pending" | "sign_out_pending" | "recent_pending" | "error";
export interface Notice {
  readonly tone: "success" | "error";
  readonly message: string;
  readonly staleRevision?: boolean;
}

function committedRefreshNotice(message: string, error: unknown): Notice {
  return {
    tone: "error",
    message: `${message} The change was committed, but current data did not refresh. ${errorMessage(error)}`,
  };
}

async function refreshAfterCommit(
  message: string,
  onChanged: () => Promise<void>,
  onNotice: (notice: Notice) => void,
): Promise<void> {
  onNotice({ tone: "success", message });
  try {
    await onChanged();
  } catch (error) {
    onNotice(committedRefreshNotice(message, error));
  }
}

interface SectionItem {
  readonly id: Section;
  readonly label: string;
  readonly icon: IconName;
}

const globalSections: readonly SectionItem[] = [
  { id: "overview", label: "Overview", icon: "grid" },
  { id: "services", label: "Services & inheritance", icon: "layers" },
  { id: "credentials", label: "Provider credentials", icon: "key" },
  { id: "audit", label: "Audit events", icon: "audit" },
];

const serviceSections: readonly SectionItem[] = [
  { id: "configuration", label: "Effective configuration", icon: "settings" },
  { id: "setup", label: "Setup", icon: "health" },
  { id: "assignments", label: "Assignments", icon: "layers" },
  { id: "requests", label: "Requests", icon: "list" },
  { id: "diagnostics", label: "Diagnostics", icon: "health" },
  { id: "budgets", label: "Budgets", icon: "shield" },
  { id: "accounting", label: "Usage & cost", icon: "activity" },
];

const sections = [...globalSections, ...serviceSections];
const emptyServices: readonly ServiceSummary[] = [];
const emptyCredentials: readonly Credential[] = [];
const emptyCatalogEntries: readonly CatalogEntry[] = [];
type GlobalFailures = Readonly<
  Partial<Record<"services" | "credentials" | "catalog", string>>
>;
const emptyGlobalFailures: GlobalFailures = {};

function initialScope(): ScopeSelection {
  const search = "location" in globalThis ? globalThis.location.search : "";
  return scopeFromSearch(search);
}

function toneForState(state: string): "green" | "amber" | "red" | "blue" {
  if (state === "active" || state === "succeeded" || state === "current") {
    return "green";
  }
  if (
    state === "disabled" ||
    state === "running" ||
    state === "waiting_for_tool" ||
    state === "cancel_requested" ||
    state === "distributing"
  ) {
    return "amber";
  }
  if (
    state === "failed" ||
    state === "cancelled" ||
    state === "interrupted" ||
    state === "uncertain" ||
    state === "retired"
  ) {
    return "red";
  }
  return "blue";
}

function revisionLabel(revision: string): string {
  return revision.length > 16
    ? `${revision.slice(0, 8)}…${revision.slice(-6)}`
    : revision;
}

function configurationSource(
  inherited: boolean,
  sourceLayer: string,
  ownerScope: string,
  services: readonly ServiceSummary[],
): string {
  if (sourceLayer === "router_default") return "Router default";
  if (!inherited) return "Set on this service";
  if (ownerScope === "global") return "Inherited from global administration";
  const owner = services.find((service) => service.service_id === ownerScope);
  return owner === undefined
    ? "Inherited from a parent service"
    : `Inherited from ${owner.display_name}`;
}

export function StateMessage({
  kind,
  children,
  onRetry,
}: {
  readonly kind: "loading" | "empty" | "error";
  readonly children: ReactNode;
  readonly onRetry?: () => void;
}) {
  return (
    <StatePanel
      kind={kind}
      title={
        kind === "error"
          ? "The selected service is not available"
          : kind === "loading"
            ? "Loading service data"
            : "Select a service"
      }
      {...(onRetry === undefined ? {} : { onRetry })}
    >
      {children}
    </StatePanel>
  );
}

function EmptyRow({
  columns,
  children,
}: {
  readonly columns: number;
  readonly children: ReactNode;
}) {
  return (
    <tr>
      <td className="empty-cell" colSpan={columns}>
        {children}
      </td>
    </tr>
  );
}

function Revision({
  value,
  inherited,
}: {
  readonly value: string;
  readonly inherited?: boolean;
}) {
  return (
    <span className="revision" title={value}>
      {revisionLabel(value)}
      {inherited ? " · inherited" : ""}
    </span>
  );
}

function ScopedReadFailure({
  title,
  message,
}: {
  readonly title: string;
  readonly message: string;
}) {
  return (
    <StatePanel kind="error" title={title}>
      {message}
    </StatePanel>
  );
}

function CredentialForm({
  client,
  ownerScope,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly ownerScope: string;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const [secret, setSecret] = useState("");
  const [safeLabel, setSafeLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    try {
      await client.createCredential({
        ownerScope,
        secret,
        safeLabel,
      });
      setSecret("");
      setSafeLabel("");
    } catch (error) {
      setSecret("");
      await recoverAfterMutationFailure(error, onChanged, onNotice);
      setSubmitting(false);
      return;
    }
    const message = "The write-only OpenRouter credential was stored.";
    onNotice({
      tone: "success",
      message,
    });
    try {
      await onChanged();
    } catch (error) {
      onNotice(committedRefreshNotice(message, error));
    } finally {
      setSecret("");
      setSubmitting(false);
    }
  }
  return (
    <form
      className="configuration-form"
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <h3>Store OpenRouter credential</h3>
      <p>The secret is write-only. This field clears after each submit.</p>
      <div className="form-grid">
        <label>
          Safe label
          <input
            maxLength={200}
            value={safeLabel}
            onChange={(event) => {
              setSafeLabel(event.target.value);
            }}
            autoComplete="off"
          />
        </label>
        <label>
          Provider secret
          <input
            required
            type="password"
            value={secret}
            onChange={(event) => {
              setSecret(event.target.value);
            }}
            autoComplete="new-password"
            spellCheck={false}
          />
        </label>
      </div>
      <Button
        type="submit"
        disabled={submitting || secret === ""}
        icon={<Icon name="key" size={16} />}
      >
        {submitting ? "Storing…" : "Store credential"}
      </Button>
    </form>
  );
}

function CredentialTable({
  client,
  values,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly values: readonly Credential[];
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Label fingerprint</th>
            <th>Owner</th>
            <th>State</th>
            <th>Revision</th>
            <th>
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {values.length === 0 ? (
            <EmptyRow columns={5}>
              No credential metadata is in this authority.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <CredentialRow
                key={item.credential_id}
                client={client}
                item={item}
                onChanged={onChanged}
                onNotice={onNotice}
              />
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function CredentialRow({
  client,
  item,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly item: Credential;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const [replacementSecret, setReplacementSecret] = useState("");
  const [busy, setBusy] = useState(false);

  async function change(action: "rotate" | "disable" | "retire") {
    setBusy(true);
    const stateMessage =
      action === "rotate"
        ? "The credential was replaced."
        : action === "disable"
          ? "The credential was disabled."
          : "The credential was retired.";
    try {
      await client.changeCredential(item.credential_id, action, {
        expectedRevision: item.revision,
        reason: `${action === "rotate" ? "Replace" : action === "disable" ? "Disable" : "Retire"} the provider credential`,
        ...(action === "rotate" ? { replacementSecret } : {}),
      });
    } catch (error) {
      setReplacementSecret("");
      await recoverAfterMutationFailure(error, onChanged, onNotice);
      setBusy(false);
      return;
    }
    setReplacementSecret("");
    onNotice({ tone: "success", message: stateMessage });
    try {
      await onChanged();
    } catch (error) {
      onNotice(committedRefreshNotice(stateMessage, error));
    } finally {
      setReplacementSecret("");
      setBusy(false);
    }
  }

  return (
    <tr>
      <td>
        <strong>{item.fingerprint}</strong>
        <small>{item.credential_id}</small>
      </td>
      <td>{item.owner_scope}</td>
      <td>
        <StatusPill tone={toneForState(item.state)}>{item.state}</StatusPill>
      </td>
      <td>
        <Revision value={item.revision} />
      </td>
      <td>
        {item.state === "retired" ? (
          <span className="muted-action">Read only</span>
        ) : (
          <div className="credential-actions">
            <label>
              <span className="sr-only">
                Replacement secret for {item.fingerprint}
              </span>
              <input
                type="password"
                value={replacementSecret}
                placeholder="Write-only replacement"
                autoComplete="new-password"
                spellCheck={false}
                onChange={(event) => {
                  setReplacementSecret(event.currentTarget.value);
                }}
              />
            </label>
            <Button
              type="button"
              variant="quiet"
              disabled={busy || replacementSecret === ""}
              onClick={() => void change("rotate")}
            >
              Replace
            </Button>
            {item.state === "active" ? (
              <Button
                type="button"
                variant="quiet"
                disabled={busy}
                onClick={() => void change("disable")}
              >
                Disable
              </Button>
            ) : null}
            <Button
              type="button"
              variant="quiet"
              disabled={busy}
              onClick={() => void change("retire")}
            >
              Retire
            </Button>
          </div>
        )}
      </td>
    </tr>
  );
}

function ProviderForm({
  client,
  scope,
  credentials,
  canBrowseCredentials,
  expectedRevision,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly credentials: readonly Credential[];
  readonly canBrowseCredentials: boolean;
  readonly expectedRevision: string | null;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const [displayName, setDisplayName] = useState("OpenRouter");
  const [credentialId, setCredentialId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    try {
      const result = await client.putProvider(scope, null, {
        provider_catalog_id: "openai_compatible.v1",
        display_name: displayName,
        endpoint: "https://openrouter.ai/api/v1",
        credential_id: credentialId,
        state: "active",
        settings: {
          schema_name: "adapter.openai_compatible.settings",
          major_version: 1,
          document: {
            profile: "openrouter",
            supported_operations: ["chat.complete", "chat.stream"],
          },
        },
        expected_revision: expectedRevision,
        reason: "Create the OpenRouter provider instance",
        eligible_service_ids: [],
      });
      await refreshAfterCommit(
        `Provider instance published at ${revisionLabel(result.active_revision)} (${result.distribution_state}).`,
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    } finally {
      setSubmitting(false);
    }
  }
  const credentialOptions = credentials.reduce<ReactNode[]>((options, item) => {
    if (item.state === "active") {
      options.push(
        <option key={item.credential_id} value={item.credential_id}>
          {item.fingerprint}
        </option>,
      );
    }
    return options;
  }, []);
  return (
    <form
      className="configuration-form"
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <h3>Add OpenRouter instance</h3>
      <p>
        The endpoint and supported operations use the accepted OpenRouter
        profile.
      </p>
      <div className="form-grid">
        <label>
          Display name
          <input
            required
            maxLength={200}
            value={displayName}
            onChange={(event) => {
              setDisplayName(event.target.value);
            }}
          />
        </label>
        <label>
          {canBrowseCredentials
            ? "Credential"
            : "Eligible credential reference ID"}
          {canBrowseCredentials ? (
            <select
              required
              value={credentialId}
              onChange={(event) => {
                setCredentialId(event.target.value);
              }}
            >
              <option value="">Select write-only credential</option>
              {credentialOptions}
            </select>
          ) : (
            <input
              required
              maxLength={200}
              value={credentialId}
              onChange={(event) => {
                setCredentialId(event.target.value);
              }}
              autoComplete="off"
              spellCheck={false}
            />
          )}
        </label>
      </div>
      <Button type="submit" disabled={submitting || credentialId === ""}>
        {submitting ? "Publishing…" : "Publish instance"}
      </Button>
    </form>
  );
}

function ProviderTable({
  client,
  scope,
  values,
  services,
  writable,
  expectedRevision,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly values: readonly ProviderInstance[];
  readonly services: readonly ServiceSummary[];
  readonly writable: boolean;
  readonly expectedRevision: string | null;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  async function change(item: ProviderInstance) {
    try {
      const nextState = item.state === "active" ? "disabled" : "active";
      const result = await client.putProvider(
        scope,
        item.provider_instance_id,
        {
          provider_catalog_id: item.provider_catalog_id,
          display_name: item.display_name,
          endpoint: item.endpoint,
          credential_id: item.credential_id,
          state: nextState,
          settings: item.settings,
          expected_revision: item.active_revision,
          reason: `${nextState === "active" ? "Restore" : "Disable"} the provider instance`,
          eligible_service_ids: item.eligible_service_ids,
        },
      );
      await refreshAfterCommit(
        `Provider instance ${nextState}. Active revision ${revisionLabel(result.active_revision)}.`,
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    }
  }
  async function override(item: ProviderInstance) {
    try {
      await client.putProvider(scope, item.provider_instance_id, {
        provider_catalog_id: item.provider_catalog_id,
        display_name: item.display_name,
        endpoint: item.endpoint,
        credential_id: item.credential_id,
        state: item.state,
        settings: item.settings,
        expected_revision: expectedRevision,
        reason: "Override the inherited provider connection for this service",
        eligible_service_ids: item.eligible_service_ids,
      });
      await refreshAfterCommit(
        "The inherited provider connection was copied to this service.",
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    }
  }
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Provider instance</th>
            <th>Source</th>
            <th>State</th>
            <th>Revision</th>
            <th>
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {values.length === 0 ? (
            <EmptyRow columns={5}>
              No provider connection is available for this service.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.provider_instance_id}>
                <td>
                  <strong>{item.display_name}</strong>
                </td>
                <td>
                  {configurationSource(
                    item.inherited,
                    item.source_layer,
                    item.owner_scope,
                    services,
                  )}
                </td>
                <td>
                  <StatusPill tone={toneForState(item.state)}>
                    {item.state}
                  </StatusPill>
                </td>
                <td>
                  <Revision
                    value={item.active_revision}
                    inherited={item.inherited}
                  />
                </td>
                <td>
                  {!writable || item.state === "retired" ? (
                    <span className="muted-action">Read only</span>
                  ) : item.inherited ? (
                    <Button variant="quiet" onClick={() => void override(item)}>
                      Override for this service
                    </Button>
                  ) : (
                    <Button variant="quiet" onClick={() => void change(item)}>
                      {item.state === "active" ? "Disable" : "Restore"}
                    </Button>
                  )}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

interface RouteFormState {
  readonly providerId: string;
  readonly canonicalModelId: string;
  readonly wireModel: string;
  readonly inputPrice: string;
  readonly outputPrice: string;
}

const initialRouteForm: RouteFormState = {
  providerId: "",
  canonicalModelId: "",
  wireModel: "",
  inputPrice: "",
  outputPrice: "",
};

const nonNegativeDecimal = /^(0|[1-9][0-9]*)(\.[0-9]+)?$/;
const mvpRouteCapabilities = ["chat.complete", "chat.stream"] as const;

function RouteForm({
  client,
  scope,
  providers,
  models,
  expectedRevision,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly providers: readonly ProviderInstance[];
  readonly models: readonly CatalogEntry[];
  readonly expectedRevision: string | null;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const [form, updateForm] = useReducer(
    (state: RouteFormState, update: Partial<RouteFormState>) => ({
      ...state,
      ...update,
    }),
    initialRouteForm,
  );
  const [submitting, setSubmitting] = useState(false);
  const routeReady =
    form.providerId !== "" &&
    models.some(
      (model) =>
        model.stable_id === form.canonicalModelId &&
        model.state === "active" &&
        mvpRouteCapabilities.every((capability) =>
          model.capabilities.includes(capability),
        ),
    ) &&
    form.wireModel.trim() !== "" &&
    nonNegativeDecimal.test(form.inputPrice) &&
    nonNegativeDecimal.test(form.outputPrice);
  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!routeReady) return;
    setSubmitting(true);
    try {
      const result = await client.putRoute(scope, null, {
        provider_instance_id: form.providerId,
        canonical_model_id: form.canonicalModelId,
        wire_model: form.wireModel,
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
        prices: [
          {
            unit: "input_token",
            price: form.inputPrice,
            currency: "USD",
            raw_source_value: `${form.inputPrice} USD per 1000000 input tokens`,
            unit_quantity: "1000000",
          },
          {
            unit: "output_token",
            price: form.outputPrice,
            currency: "USD",
            raw_source_value: `${form.outputPrice} USD per 1000000 output tokens`,
            unit_quantity: "1000000",
          },
        ],
        synchronization_schedule: "0 0 * * 0",
        stale_after_seconds: 1209600,
        state: "active",
        expected_revision: expectedRevision,
        reason: "Create the OpenRouter model route",
        eligible_service_ids: [],
      });
      await refreshAfterCommit(
        `Model route published at ${revisionLabel(result.active_revision)} (${result.distribution_state}).`,
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    } finally {
      setSubmitting(false);
    }
  }
  const providerOptions = providers.reduce<ReactNode[]>((options, item) => {
    if (item.state === "active") {
      options.push(
        <option
          key={item.provider_instance_id}
          value={item.provider_instance_id}
        >
          {item.display_name}
        </option>,
      );
    }
    return options;
  }, []);
  const modelOptions = models.reduce<ReactNode[]>((options, model) => {
    if (model.state === "active") {
      options.push(
        <option key={model.stable_id} value={model.stable_id}>
          {model.display_name}
        </option>,
      );
    }
    return options;
  }, []);
  return (
    <form
      className="configuration-form"
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <h3>Add provider-model route</h3>
      <p>
        Select the named canonical model, then enter the provider model name.
        Prices are USD per one million tokens.
      </p>
      <div className="form-grid form-grid-three">
        <label>
          Provider instance
          <select
            required
            value={form.providerId}
            onChange={(event) => {
              updateForm({ providerId: event.target.value });
            }}
          >
            <option value="">Select instance</option>
            {providerOptions}
          </select>
        </label>
        <label>
          Supported model
          <select
            required
            value={form.canonicalModelId}
            onChange={(event) => {
              updateForm({ canonicalModelId: event.target.value });
            }}
          >
            <option value="">Select a named model</option>
            {modelOptions}
          </select>
        </label>
        <label>
          Provider model name
          <input
            required
            placeholder="For example, deepseek/deepseek-v4-flash"
            value={form.wireModel}
            onChange={(event) => {
              updateForm({ wireModel: event.target.value });
            }}
          />
        </label>
        <label>
          Input price
          <input
            required
            inputMode="decimal"
            pattern="(0|[1-9][0-9]*)(\.[0-9]+)?"
            placeholder="Explicit USD price"
            value={form.inputPrice}
            onChange={(event) => {
              updateForm({ inputPrice: event.target.value });
            }}
          />
        </label>
        <label>
          Output price
          <input
            required
            inputMode="decimal"
            pattern="(0|[1-9][0-9]*)(\.[0-9]+)?"
            placeholder="Explicit USD price"
            value={form.outputPrice}
            onChange={(event) => {
              updateForm({ outputPrice: event.target.value });
            }}
          />
        </label>
      </div>
      <Button type="submit" disabled={submitting || !routeReady}>
        {submitting ? "Publishing…" : "Publish route"}
      </Button>
    </form>
  );
}

function RouteTable({
  client,
  scope,
  values,
  services,
  writable,
  expectedRevision,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly values: readonly ProviderModelRoute[];
  readonly services: readonly ServiceSummary[];
  readonly writable: boolean;
  readonly expectedRevision: string | null;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  async function change(item: ProviderModelRoute) {
    try {
      const nextState = item.state === "active" ? "disabled" : "active";
      const result = await client.putRoute(
        scope,
        item.provider_model_route_id,
        {
          provider_instance_id: item.provider_instance_id,
          canonical_model_id: item.canonical_model_id,
          wire_model: item.wire_model,
          capabilities: item.capabilities,
          settings: item.settings,
          price_authority: item.price_authority,
          prices: item.prices,
          synchronization_schedule: item.synchronization_schedule,
          stale_after_seconds: item.stale_after_seconds,
          state: nextState,
          expected_revision: item.active_revision,
          reason: `${nextState === "active" ? "Restore" : "Disable"} the provider-model route`,
          eligible_service_ids: item.eligible_service_ids,
        },
      );
      await refreshAfterCommit(
        `Provider-model route ${nextState}. Active revision ${revisionLabel(result.active_revision)}.`,
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    }
  }
  async function override(item: ProviderModelRoute) {
    try {
      await client.putRoute(scope, item.provider_model_route_id, {
        provider_instance_id: item.provider_instance_id,
        canonical_model_id: item.canonical_model_id,
        wire_model: item.wire_model,
        capabilities: item.capabilities,
        settings: item.settings,
        price_authority: item.price_authority,
        prices: item.prices,
        synchronization_schedule: item.synchronization_schedule,
        stale_after_seconds: item.stale_after_seconds,
        state: item.state,
        expected_revision: expectedRevision,
        reason: "Override the inherited model route for this service",
        eligible_service_ids: item.eligible_service_ids,
      });
      await refreshAfterCommit(
        "The inherited model route was copied to this service.",
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    }
  }
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Model route</th>
            <th>Source</th>
            <th>Capabilities</th>
            <th>State</th>
            <th>Revision</th>
            <th>
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {values.length === 0 ? (
            <EmptyRow columns={6}>
              No model route is available for this service.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.provider_model_route_id}>
                <td>
                  <strong>{item.wire_model}</strong>
                </td>
                <td>
                  {configurationSource(
                    item.inherited,
                    item.source_layer,
                    item.owner_scope,
                    services,
                  )}
                </td>
                <td>{item.capabilities.join(", ")}</td>
                <td>
                  <StatusPill tone={toneForState(item.state)}>
                    {item.state}
                  </StatusPill>
                </td>
                <td>
                  <Revision
                    value={item.active_revision}
                    inherited={item.inherited}
                  />
                </td>
                <td>
                  {!writable || item.state === "retired" ? (
                    <span className="muted-action">Read only</span>
                  ) : item.inherited ? (
                    <Button variant="quiet" onClick={() => void override(item)}>
                      Override for this service
                    </Button>
                  ) : (
                    <Button variant="quiet" onClick={() => void change(item)}>
                      {item.state === "active" ? "Disable" : "Restore"}
                    </Button>
                  )}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function ConfigurationView(props: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot;
  readonly services: readonly ServiceSummary[];
  readonly models: readonly CatalogEntry[];
  readonly catalogFailure?: string;
  readonly catalogLoading: boolean;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const { client, scope, snapshot, onChanged, onNotice } = props;
  const expectedRevision = configurationRevisionForScope(snapshot, scope);
  const eligibleModels = props.models.filter(
    (model) =>
      model.state === "active" &&
      mvpRouteCapabilities.every((capability) =>
        model.capabilities.includes(capability),
      ),
  );
  const serviceConfigurationWritable =
    scope.workspaceId === "" && snapshot.failures.state === undefined;
  return (
    <div className="panel-stack">
      <PageHeading
        eyebrow="Selected service"
        title="What this service will use"
        description="See the final provider and model route configuration. Inherited items are read only. Items set on this service can override inherited results."
      />
      <Panel className="permission-note">
        <Icon name="key" />
        <div>
          <h2>Provider keys are managed globally</h2>
          <p>
            Use Global administration → Provider credentials to store or replace
            secret values.
          </p>
        </div>
      </Panel>
      {!serviceConfigurationWritable ? (
        <Panel className="permission-note">
          <Icon name="lock" />
          <div>
            <h2>Provider configuration stays at service level</h2>
            <p>
              This workspace view shows effective provider and route state. Load
              the service-level scope to create, disable, or restore these
              items.
            </p>
          </div>
        </Panel>
      ) : null}
      <Panel>
        <PanelHeader
          kicker="Effective result"
          title="Provider connections"
          description="These are the provider connections that this service can use."
        />
        {snapshot.failures.providers === undefined ? (
          <ProviderTable
            client={client}
            scope={scope}
            values={snapshot.providers}
            services={props.services}
            writable={serviceConfigurationWritable}
            expectedRevision={expectedRevision}
            onChanged={onChanged}
            onNotice={onNotice}
          />
        ) : (
          <ScopedReadFailure
            title="Provider connections are not available"
            message={snapshot.failures.providers}
          />
        )}
      </Panel>
      <Panel>
        <PanelHeader
          kicker="Effective result"
          title="Model routes"
          description="These are the models and provider routes that assignments can use."
        />
        {snapshot.failures.routes === undefined ? (
          <RouteTable
            client={client}
            scope={scope}
            values={snapshot.routes}
            services={props.services}
            writable={serviceConfigurationWritable}
            expectedRevision={expectedRevision}
            onChanged={onChanged}
            onNotice={onNotice}
          />
        ) : (
          <ScopedReadFailure
            title="Model routes are not available"
            message={snapshot.failures.routes}
          />
        )}
      </Panel>
      {serviceConfigurationWritable &&
      snapshot.failures.providers === undefined &&
      snapshot.failures.credentials === undefined ? (
        <Panel>
          <PanelHeader
            kicker="Set on this service"
            title="Add a provider connection"
            description="Connect this service to one supported provider endpoint."
          />
          <ProviderForm
            client={client}
            scope={scope}
            credentials={snapshot.credentials}
            canBrowseCredentials={scope.mode === "global"}
            expectedRevision={expectedRevision}
            onChanged={onChanged}
            onNotice={onNotice}
          />
        </Panel>
      ) : null}
      {serviceConfigurationWritable &&
      snapshot.failures.providers === undefined &&
      snapshot.failures.routes === undefined ? (
        <Panel>
          <PanelHeader
            kicker="Set on this service"
            title="Add a model route"
            description="Connect a model name to one provider connection."
          />
          {props.catalogLoading ? (
            <p role="status">Supported models are loading.</p>
          ) : props.catalogFailure !== undefined ? (
            <ScopedReadFailure
              title="Supported models are not available"
              message={props.catalogFailure}
            />
          ) : eligibleModels.length === 0 ? (
            <p>
              No active model supports both chat completion and streaming. Add
              or enable a compatible model in the global catalog.
            </p>
          ) : (
            <RouteForm
              client={client}
              scope={scope}
              providers={snapshot.providers}
              models={eligibleModels}
              expectedRevision={expectedRevision}
              onChanged={onChanged}
              onNotice={onNotice}
            />
          )}
        </Panel>
      ) : null}
    </div>
  );
}

function AssignmentForm({
  client,
  scope,
  routes,
  expectedRevision,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly routes: readonly ProviderModelRoute[];
  readonly expectedRevision: string | null;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const [name, setName] = useState("general");
  const [orderedRoutes, setOrderedRoutes] = useState<readonly string[]>([""]);
  const [timeout, setTimeoutValue] = useState("30000");
  const [submitting, setSubmitting] = useState(false);
  const candidates = orderedRoutes.filter((routeId) => routeId !== "");
  const hasDuplicate = new Set(candidates).size !== candidates.length;
  const activeRoutes: ProviderModelRoute[] = [];
  for (const route of routes) {
    if (route.state === "active") activeRoutes.push(route);
  }
  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    try {
      const result = await client.putAssignment(scope, name, {
        expected_revision: expectedRevision,
        state: "active",
        candidates: candidates.map((provider_model_route_id) => ({
          provider_model_route_id,
          attempt_timeout_ms: Number(timeout),
        })),
        required_capabilities: ["chat.complete", "chat.stream"],
        reason: "Publish the ordered MVP fallback chain",
      });
      await refreshAfterCommit(
        `Assignment published at ${revisionLabel(result.active_revision)} (${result.distribution_state}).`,
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <form
      className="configuration-form"
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <h3>Set the model route order</h3>
      <p>
        Choose the primary route first. Add fallback routes in the order that
        Router must try them.
      </p>
      <div className="form-grid">
        <label>
          Assignment name
          <input
            required
            maxLength={100}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </label>
        <label>
          Attempt timeout (ms)
          <input
            required
            type="number"
            min={100}
            max={120000}
            value={timeout}
            onChange={(event) => {
              setTimeoutValue(event.target.value);
            }}
          />
        </label>
      </div>
      <div className="assignment-route-fields">
        {orderedRoutes.map((routeId, index) => (
          <div
            className="assignment-route-field"
            key={`route-position-${String(index)}`}
          >
            <label>
              {index === 0 ? "Primary route" : `Fallback ${String(index)}`}
              <select
                required
                value={routeId}
                onChange={(event) => {
                  const nextRoutes = [...orderedRoutes];
                  nextRoutes[index] = event.currentTarget.value;
                  setOrderedRoutes(nextRoutes);
                }}
              >
                <option value="">Choose a model route</option>
                {activeRoutes.map((route) => (
                  <option
                    key={route.provider_model_route_id}
                    value={route.provider_model_route_id}
                  >
                    {route.wire_model} ·{" "}
                    {route.inherited ? "Inherited" : "Set here"}
                  </option>
                ))}
              </select>
            </label>
            {index === 0 ? null : (
              <Button
                type="button"
                variant="quiet"
                onClick={() => {
                  setOrderedRoutes(
                    orderedRoutes.filter(
                      (_value, position) => position !== index,
                    ),
                  );
                }}
              >
                Remove
              </Button>
            )}
          </div>
        ))}
        <Button
          type="button"
          variant="secondary"
          disabled={orderedRoutes.length >= routes.length}
          onClick={() => {
            setOrderedRoutes((current) => [...current, ""]);
          }}
        >
          Add fallback
        </Button>
        {hasDuplicate ? <p role="alert">Choose each route only once.</p> : null}
      </div>
      <ol className="fallback-preview" aria-label="Ordered fallback preview">
        {candidates.map((candidate, index) => (
          <li key={`${candidate}-${String(index)}`}>
            <span>{index + 1}</span>
            <strong>
              {routes.find(
                (route) => route.provider_model_route_id === candidate,
              )?.wire_model ?? "Unknown route"}
            </strong>
            {index === 0 ? (
              <strong>Primary</strong>
            ) : (
              <small>Fallback {index}</small>
            )}
          </li>
        ))}
      </ol>
      <Button
        type="submit"
        disabled={submitting || candidates.length === 0 || hasDuplicate}
      >
        {submitting ? "Publishing…" : "Publish chain"}
      </Button>
    </form>
  );
}

function AssignmentTable({
  client,
  scope,
  values,
  routes,
  services,
  writable,
  expectedRevision,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly values: readonly Assignment[];
  readonly routes: readonly ProviderModelRoute[];
  readonly services: readonly ServiceSummary[];
  readonly writable: boolean;
  readonly expectedRevision: string | null;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  async function change(item: Assignment) {
    try {
      const nextState = item.state === "active" ? "disabled" : "active";
      const result = await client.putAssignment(scope, item.name, {
        expected_revision: item.active_revision,
        state: nextState,
        candidates: item.candidates,
        required_capabilities: item.required_capabilities,
        reason: `${nextState === "active" ? "Restore" : "Disable"} the assignment`,
      });
      await refreshAfterCommit(
        `Assignment ${nextState}. Active revision ${revisionLabel(result.active_revision)}.`,
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    }
  }
  async function override(item: Assignment) {
    try {
      await client.putAssignment(scope, item.name, {
        expected_revision: expectedRevision,
        state: item.state,
        candidates: item.candidates,
        required_capabilities: item.required_capabilities,
        reason: "Override the inherited assignment for this service",
      });
      await refreshAfterCommit(
        "The complete inherited fallback chain was copied to this service.",
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    }
  }
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Assignment or route</th>
            <th>Complete ordered chain</th>
            <th>State</th>
            <th>Revision</th>
            <th>
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {values.length === 0 ? (
            <EmptyRow columns={5}>
              No assignment is available for this service.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.name}>
                <td>
                  <strong>{item.name}</strong>
                  <small>
                    {configurationSource(
                      item.inherited,
                      item.source_layer,
                      item.owner_scope,
                      services,
                    )}
                  </small>
                </td>
                <td>
                  <ol className="table-chain">
                    {item.candidates.map((candidate, index) => (
                      <li key={candidate.provider_model_route_id}>
                        <span>{index + 1}</span>
                        <div>
                          <strong>
                            {routes.find(
                              (route) =>
                                route.provider_model_route_id ===
                                candidate.provider_model_route_id,
                            )?.wire_model ?? "Unavailable route"}
                          </strong>
                          <small>
                            {index === 0
                              ? "Primary"
                              : `Fallback ${String(index)}`}
                          </small>
                        </div>
                      </li>
                    ))}
                  </ol>
                </td>
                <td>
                  <StatusPill tone={toneForState(item.state)}>
                    {item.state}
                  </StatusPill>
                </td>
                <td>
                  <Revision
                    value={item.active_revision}
                    inherited={item.inherited}
                  />
                </td>
                <td>
                  {!writable || item.state === "retired" ? (
                    <span className="muted-action">Read only</span>
                  ) : item.inherited ? (
                    <Button variant="quiet" onClick={() => void override(item)}>
                      Override for this service
                    </Button>
                  ) : (
                    <Button variant="quiet" onClick={() => void change(item)}>
                      {item.state === "active" ? "Disable" : "Restore"}
                    </Button>
                  )}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function AssignmentsView(props: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot;
  readonly services: readonly ServiceSummary[];
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  return (
    <Panel>
      <PanelHeader
        kicker="Immediate publication"
        title="Assignments and ordered fallbacks"
        description="An assignment set on this service replaces the complete inherited fallback chain with the order that you choose."
      />
      {props.snapshot.failures.assignments === undefined &&
      props.snapshot.failures.routes === undefined &&
      props.snapshot.failures.state === undefined ? (
        <AssignmentForm
          client={props.client}
          scope={props.scope}
          routes={props.snapshot.routes}
          expectedRevision={configurationRevisionForScope(
            props.snapshot,
            props.scope,
          )}
          onChanged={props.onChanged}
          onNotice={props.onNotice}
        />
      ) : null}
      {props.snapshot.failures.assignments === undefined ? (
        <AssignmentTable
          client={props.client}
          scope={props.scope}
          values={props.snapshot.assignments}
          routes={props.snapshot.routes}
          services={props.services}
          writable={props.snapshot.failures.state === undefined}
          expectedRevision={configurationRevisionForScope(
            props.snapshot,
            props.scope,
          )}
          onChanged={props.onChanged}
          onNotice={props.onNotice}
        />
      ) : (
        <ScopedReadFailure
          title="Assignments are not available"
          message={props.snapshot.failures.assignments}
        />
      )}
    </Panel>
  );
}

function requestFailureLabel(value: RequestFailureClass): string {
  const labels: Readonly<Record<RequestFailureClass, string>> = {
    authentication: "Authentication",
    policy: "Policy",
    budget: "Budget",
    rate_limit: "Rate limit",
    timeout: "Availability: timeout",
    transport: "Availability: transport",
    provider_unavailable: "Availability: provider",
    invalid_provider_response: "Availability: invalid response",
    incompatible_request: "Compatibility",
    cancelled: "Cancellation",
    uncertain_effect: "Cancellation: uncertain effect",
    router_internal: "Router internal",
  };
  return labels[value];
}

function attemptDecision(value: RequestAttemptStatus["decision"]): string {
  switch (value) {
    case "next_candidate":
      return "No retry. Router used the next fallback.";
    case "stop_request":
      return "No retry or fallback. Router stopped the logical request.";
    case "commit_boundary":
      return "No retry or fallback. Router stopped after a committed effect.";
    case "cancelled":
      return "No retry or fallback. Router stopped for cancellation.";
    case "succeeded":
      return "The attempt succeeded. Router did not use another fallback.";
    case undefined:
      return "The Router decision is pending.";
  }
}

function RequestTime({ value }: { readonly value: string | undefined }) {
  return value === undefined ? (
    <>Not reported</>
  ) : (
    <time dateTime={value}>{value}</time>
  );
}

function RequestTable({
  scope,
  values,
  onSelect,
}: {
  readonly scope: ScopeSelection;
  readonly values: readonly RequestStatus[];
  readonly onSelect: (requestId: string) => void;
}) {
  return (
    <div
      className="table-scroll"
      role="region"
      aria-label="Logical requests table"
      tabIndex={0}
    >
      <table>
        <thead>
          <tr>
            <th>Logical request</th>
            <th>Workspace</th>
            <th>Assignment</th>
            <th>State</th>
            <th>Revision</th>
            <th>Safe diagnostic</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {values.length === 0 ? (
            <EmptyRow columns={7}>
              No request status is available for this service.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.request_id}>
                <td>
                  <strong>{item.request_id}</strong>
                </td>
                <td>
                  {scope.workspaceId === ""
                    ? "Service level"
                    : scope.workspaceId}
                </td>
                <td>{item.assignment ?? item.exact_route ?? "Not reported"}</td>
                <td>
                  <StatusPill tone={toneForState(item.state)}>
                    {item.state}
                  </StatusPill>
                </td>
                <td>{item.state_revision}</td>
                <td>{item.error?.message ?? "No safe diagnostic"}</td>
                <td>
                  <Button
                    data-request-id={item.request_id}
                    variant="quiet"
                    aria-label={`View request ${item.request_id}`}
                    onClick={() => {
                      onSelect(item.request_id);
                    }}
                  >
                    View request
                  </Button>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function RequestAttemptTable({
  values,
}: {
  readonly values: readonly RequestAttemptStatus[];
}) {
  return (
    <div
      className="table-scroll"
      role="region"
      aria-label="Ordered provider attempts table"
      tabIndex={0}
    >
      <table>
        <thead>
          <tr>
            <th>Order</th>
            <th>Provider-model route</th>
            <th>State and time</th>
            <th>Failure and scope</th>
            <th>Router decision</th>
            <th>Usage and price</th>
          </tr>
        </thead>
        <tbody>
          {values.length === 0 ? (
            <EmptyRow columns={6}>
              No provider attempt has started for this logical request.
            </EmptyRow>
          ) : (
            values.map((attempt, index) => (
              <tr key={attempt.attempt_id}>
                <td>
                  <strong>{String(index + 1)}</strong>
                  <small>{attempt.attempt_id}</small>
                </td>
                <td>
                  <strong>{attempt.provider_model_route_id}</strong>
                  <small>
                    Assignment revision {attempt.assignment_revision}
                  </small>
                </td>
                <td>
                  <StatusPill tone={toneForState(attempt.state)}>
                    {attempt.state}
                  </StatusPill>
                  <small>
                    Started <RequestTime value={attempt.started_at} />
                  </small>
                  <small>
                    Ended <RequestTime value={attempt.ended_at} />
                  </small>
                </td>
                <td>
                  {attempt.error === undefined ? (
                    "No normalized failure"
                  ) : (
                    <>
                      <strong>
                        {requestFailureLabel(attempt.error.class)}
                      </strong>
                      <small>Class: {attempt.error.class}</small>
                      <small>Scope: {attempt.error.affected_scope}</small>
                      {attempt.error.safe_provider_code === undefined ? null : (
                        <small>
                          Safe provider code: {attempt.error.safe_provider_code}
                        </small>
                      )}
                    </>
                  )}
                </td>
                <td>{attemptDecision(attempt.decision)}</td>
                <td>
                  {attempt.usage === undefined || attempt.usage.length === 0 ? (
                    <span>No usage reported</span>
                  ) : (
                    <ul className="request-usage-list">
                      {attempt.usage.map((usage) => (
                        <li key={usage.unit}>
                          {usage.quantity} {usage.unit}
                        </li>
                      ))}
                    </ul>
                  )}
                  <small>
                    Price version {attempt.price_version ?? "Not reported"}
                  </small>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function RequestDetail({
  scope,
  value,
  onBack,
  onRefresh,
}: {
  readonly scope: ScopeSelection;
  readonly value: RequestStatus;
  readonly onBack: () => void;
  readonly onRefresh: () => void;
}) {
  return (
    <div className="request-detail">
      <div className="request-detail-actions">
        <Button variant="secondary" onClick={onBack}>
          Back to requests
        </Button>
        <Button
          variant="quiet"
          icon={<Icon name="refresh" size={16} />}
          onClick={onRefresh}
        >
          Refresh detail
        </Button>
      </div>
      <PanelHeader
        kicker="Content-free request detail"
        title={value.request_id}
        description="This detail contains safe status and accounting only. It does not contain prompts, results, tool values, raw provider-error content, credentials, or secrets."
      />
      <dl className="request-detail-summary">
        <div>
          <dt>Service</dt>
          <dd>{scope.serviceId}</dd>
        </div>
        <div>
          <dt>Workspace</dt>
          <dd>
            {scope.workspaceId === "" ? "Service level" : scope.workspaceId}
          </dd>
        </div>
        <div>
          <dt>Assignment or diagnostic route</dt>
          <dd>{value.assignment ?? value.exact_route ?? "Not reported"}</dd>
        </div>
        <div>
          <dt>Configuration revision</dt>
          <dd>{value.configuration_revision}</dd>
        </div>
        <div>
          <dt>State</dt>
          <dd>
            <StatusPill tone={toneForState(value.state)}>
              {value.state}
            </StatusPill>
            <span>Revision {value.state_revision}</span>
          </dd>
        </div>
        <div>
          <dt>Admitted</dt>
          <dd>
            <RequestTime value={value.admitted_at} />
          </dd>
        </div>
        <div>
          <dt>Last transition</dt>
          <dd>
            <RequestTime value={value.last_transition_at} />
          </dd>
        </div>
        <div>
          <dt>Terminal</dt>
          <dd>
            <RequestTime value={value.terminal_at} />
          </dd>
        </div>
        <div>
          <dt>Partial output</dt>
          <dd>{value.partial_output ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt>Committed effect</dt>
          <dd>{value.committed_effects ? "Yes" : "No"}</dd>
        </div>
      </dl>
      {value.error === undefined || value.error === null ? null : (
        <section
          className="request-terminal-error"
          aria-labelledby="terminal-error-title"
        >
          <h3 id="terminal-error-title">Safe terminal diagnostic</h3>
          <p>
            {requestFailureLabel(value.error.class)} · {value.error.class} ·
            scope {value.error.affected_scope}
          </p>
          <p>{value.error.message}</p>
          {value.error.safe_provider_code === undefined ? null : (
            <p>Safe provider code: {value.error.safe_provider_code}</p>
          )}
        </section>
      )}
      <section
        className="request-detail-section"
        aria-labelledby="attempts-title"
      >
        <h3 id="attempts-title">Ordered provider attempts</h3>
        <RequestAttemptTable values={value.attempts} />
      </section>
      <section
        className="request-detail-section"
        aria-labelledby="accounting-title"
      >
        <h3 id="accounting-title">Bounded logical accounting</h3>
        <dl className="request-accounting-summary">
          <div>
            <dt>Estimated</dt>
            <dd>
              {value.accounting.estimated} {value.accounting.currency}
            </dd>
          </div>
          <div>
            <dt>Reserved</dt>
            <dd>
              {value.accounting.reserved} {value.accounting.currency}
            </dd>
          </div>
          <div>
            <dt>Used</dt>
            <dd>
              {value.accounting.used} {value.accounting.currency}
            </dd>
          </div>
          <div>
            <dt>Corrected total</dt>
            <dd>
              {value.accounting.corrected} {value.accounting.currency}
            </dd>
          </div>
        </dl>
      </section>
    </div>
  );
}

function requestDetailFailure(error: unknown): {
  readonly title: string;
  readonly message: string;
} {
  if (error instanceof AdministrationApiError && error.status === 404) {
    return {
      title: "The logical request is missing",
      message:
        "The request is absent, hidden from this scope, or no longer retained.",
    };
  }
  if (error instanceof AdministrationApiError && error.status === 403) {
    return {
      title: "The logical request is forbidden",
      message:
        "The administrator cannot read this request in the selected service and workspace.",
    };
  }
  return {
    title: "Request detail is not available",
    message: errorMessage(error),
  };
}

function RequestsView({
  client,
  scope,
  values,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly values: readonly RequestStatus[];
}) {
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(
    null,
  );
  const [detail, setDetail] = useState<RequestStatus | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailFailure, setDetailFailure] = useState<{
    readonly title: string;
    readonly message: string;
  } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const focusTarget = useRef<HTMLDivElement | null>(null);
  const returnFocusRequestId = useRef<string | null>(null);
  const focusAfterInteraction = useRef(false);

  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  useEffect(() => {
    if (!focusAfterInteraction.current) return;
    if (selectedRequestId === null && returnFocusRequestId.current !== null) {
      const actions = focusTarget.current?.querySelectorAll<HTMLButtonElement>(
        "button[data-request-id]",
      );
      const selected = Array.from(actions ?? []).find(
        (element) => element.dataset.requestId === returnFocusRequestId.current,
      );
      if (selected !== undefined) {
        selected.focus();
        return;
      }
    }
    focusTarget.current?.focus();
  }, [selectedRequestId, detail, detailFailure, detailLoading]);

  const loadDetail = useCallback(
    async (requestId: string) => {
      focusAfterInteraction.current = true;
      returnFocusRequestId.current = requestId;
      controller.current?.abort();
      const nextController = new AbortController();
      controller.current = nextController;
      setSelectedRequestId(requestId);
      setDetail(null);
      setDetailFailure(null);
      setDetailLoading(true);
      try {
        const value = await client.getRequest(
          scope,
          requestId,
          nextController.signal,
        );
        if (!nextController.signal.aborted) setDetail(value);
      } catch (error) {
        if (!nextController.signal.aborted) {
          setDetailFailure(requestDetailFailure(error));
        }
      } finally {
        if (!nextController.signal.aborted) setDetailLoading(false);
      }
    },
    [client, scope],
  );

  function backToList() {
    focusAfterInteraction.current = true;
    controller.current?.abort();
    setSelectedRequestId(null);
    setDetail(null);
    setDetailFailure(null);
    setDetailLoading(false);
  }

  let content: ReactNode;
  if (selectedRequestId === null) {
    content = (
      <RequestTable
        scope={scope}
        values={values}
        onSelect={(id) => void loadDetail(id)}
      />
    );
  } else if (detailLoading) {
    content = (
      <div className="request-detail-state">
        <StatePanel kind="loading" title="Loading request detail">
          The Router is loading safe status for {selectedRequestId}.
        </StatePanel>
        <Button variant="secondary" onClick={backToList}>
          Back to requests
        </Button>
      </div>
    );
  } else if (detailFailure !== null) {
    content = (
      <div className="request-detail-state">
        <StatePanel
          kind="error"
          title={detailFailure.title}
          onRetry={() => void loadDetail(selectedRequestId)}
        >
          {detailFailure.message}
        </StatePanel>
        <Button variant="secondary" onClick={backToList}>
          Back to requests
        </Button>
      </div>
    );
  } else {
    content =
      detail === null ? null : (
        <RequestDetail
          scope={scope}
          value={detail}
          onBack={backToList}
          onRefresh={() => void loadDetail(selectedRequestId)}
        />
      );
  }

  return (
    <div
      ref={focusTarget}
      className="request-view"
      tabIndex={-1}
      aria-busy={detailLoading}
      aria-label={
        selectedRequestId === null
          ? "Logical request list"
          : "Logical request detail"
      }
    >
      {content}
    </div>
  );
}

function AccountingView({ summary }: { readonly summary: AccountingSummary }) {
  return (
    <div className="panel-stack">
      <div className="stat-grid">
        <StatCard
          icon={<Icon name="list" size={17} />}
          label="Logical requests"
          value={String(summary.logical_requests)}
          note="Selected seven-day range"
          tone="blue"
        />
        <StatCard
          icon={<Icon name="activity" size={17} />}
          label="Provider attempts"
          value={String(summary.attempts)}
          note="Includes billable failures"
          tone="purple"
        />
        <StatCard
          icon={<Icon name="audit" size={17} />}
          label="Bounded cost"
          value={`${summary.cost} ${summary.currency}`}
          note={`Corrections ${summary.corrections}`}
          tone="lime"
        />
      </div>
      <Panel>
        <PanelHeader
          kicker="Bounded accounting"
          title="Usage by unit"
          description={`${summary.from} to ${summary.to}`}
        />
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Usage unit</th>
                <th>Quantity</th>
              </tr>
            </thead>
            <tbody>
              {summary.usage.length === 0 ? (
                <EmptyRow columns={2}>
                  No usage is recorded in this range.
                </EmptyRow>
              ) : (
                summary.usage.map((item) => (
                  <tr key={item.unit}>
                    <td>{item.unit}</td>
                    <td>{item.quantity}</td>
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

interface AuditRange {
  readonly from: string;
  readonly to: string;
}

interface AuditLoadState {
  readonly items: readonly AuditEvent[];
  readonly nextCursor: string | null;
  readonly loading: "initial" | "next" | null;
  readonly failure: string | null;
}

type AuditLoadAction =
  | { readonly type: "start"; readonly append: boolean }
  | {
      readonly type: "success";
      readonly append: boolean;
      readonly items: readonly AuditEvent[];
      readonly nextCursor: string | null;
    }
  | { readonly type: "failure"; readonly message: string }
  | { readonly type: "finish" }
  | { readonly type: "invalid_range"; readonly message: string };

function reduceAuditLoad(
  state: AuditLoadState,
  action: AuditLoadAction,
): AuditLoadState {
  switch (action.type) {
    case "start":
      return {
        ...state,
        items: action.append ? state.items : [],
        nextCursor: action.append ? state.nextCursor : null,
        loading: action.append ? "next" : "initial",
        failure: null,
      };
    case "success":
      return {
        ...state,
        items: action.append ? [...state.items, ...action.items] : action.items,
        nextCursor: action.nextCursor,
      };
    case "failure":
      return { ...state, failure: action.message };
    case "finish":
      return { ...state, loading: null };
    case "invalid_range":
      return {
        items: [],
        nextCursor: null,
        loading: null,
        failure: action.message,
      };
  }
}

function defaultAuditRange(now = new Date()): AuditRange {
  const to = new Date(Math.floor(now.getTime() / 60_000) * 60_000 + 60_000)
    .toISOString()
    .slice(0, 16);
  const from = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 16);
  return { from, to };
}

function auditRangeQuery(range: AuditRange): { from: string; to: string } {
  const from = new Date(`${range.from}:00.000Z`);
  const to = new Date(`${range.to}:00.000Z`);
  if (
    Number.isNaN(from.getTime()) ||
    Number.isNaN(to.getTime()) ||
    from >= to
  ) {
    throw new Error("Select a start time that is before the end time.");
  }
  return { from: from.toISOString(), to: to.toISOString() };
}

function auditLabel(value: string): string {
  return value.replaceAll(/[._:]+/g, " ");
}

function AuditEventCard({ value }: { readonly value: AuditEvent }) {
  const details = Object.entries(value.safe_detail ?? {});
  return (
    <li className="audit-event-card">
      <div className="audit-event-heading">
        <div>
          <strong>{auditLabel(value.action)}</strong>
          <time dateTime={value.occurred_at}>{value.occurred_at}</time>
        </div>
        <StatusPill tone={value.outcome === "permitted" ? "green" : "red"}>
          {value.outcome}
        </StatusPill>
      </div>
      <dl className="audit-event-detail">
        <div>
          <dt>Actor</dt>
          <dd>{value.actor}</dd>
        </div>
        <div>
          <dt>Authority</dt>
          <dd>{auditLabel(value.scope.authority_class)}</dd>
        </div>
        <div>
          <dt>Service</dt>
          <dd>{value.scope.service_id ?? "Router-wide"}</dd>
        </div>
        <div>
          <dt>Workspace</dt>
          <dd>{value.scope.workspace_id ?? "Not applicable"}</dd>
        </div>
        <div>
          <dt>Event ID</dt>
          <dd>{value.event_id}</dd>
        </div>
        {details.map(([name, detail]) => (
          <div key={name}>
            <dt>{auditLabel(name)}</dt>
            <dd>{detail}</dd>
          </div>
        ))}
      </dl>
    </li>
  );
}

export function AuditView({
  client,
}: {
  readonly client: AdministrationClient;
}) {
  const initialRange = useMemo(() => defaultAuditRange(), []);
  const [draft, updateDraft] = useReducer(
    (state: AuditRange, update: Partial<AuditRange>) => ({
      ...state,
      ...update,
    }),
    initialRange,
  );
  const [auditState, dispatchAudit] = useReducer(reduceAuditLoad, {
    items: [],
    nextCursor: null,
    loading: "initial",
    failure: null,
  });
  const { failure, items, loading, nextCursor } = auditState;
  const controller = useRef<AbortController | null>(null);
  const range = useRef<AuditRange>(initialRange);
  const failedCursor = useRef<string | undefined>(undefined);
  const results = useRef<HTMLDivElement | null>(null);
  const focusAfterLoad = useRef(false);

  const load = useCallback(
    async (selectedRange: AuditRange, cursor?: string) => {
      controller.current?.abort();
      const nextController = new AbortController();
      controller.current = nextController;
      const append = cursor !== undefined;
      dispatchAudit({ type: "start", append });
      failedCursor.current = undefined;
      try {
        const query = auditRangeQuery(selectedRange);
        const page = await client.listAuditEvents(
          { ...query, ...(cursor === undefined ? {} : { cursor }) },
          nextController.signal,
        );
        if (!nextController.signal.aborted) {
          dispatchAudit({
            type: "success",
            append,
            items: page.items,
            nextCursor: page.next_cursor,
          });
        }
      } catch (error) {
        if (!nextController.signal.aborted) {
          failedCursor.current = cursor;
          dispatchAudit({
            type: "failure",
            message:
              error instanceof Error &&
              !(error instanceof AdministrationApiError)
                ? error.message
                : errorMessage(error),
          });
        }
      } finally {
        if (!nextController.signal.aborted) dispatchAudit({ type: "finish" });
      }
    },
    [client],
  );

  useEffect(() => {
    const cancelLoad = scheduleAdministrationSessionInspection(() => {
      void load(range.current);
    });
    return () => {
      cancelLoad();
      controller.current?.abort();
    };
  }, [load]);

  useEffect(() => {
    if (!focusAfterLoad.current || loading !== null) return;
    results.current?.focus();
    focusAfterLoad.current = false;
  }, [failure, items, loading]);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    focusAfterLoad.current = true;
    try {
      auditRangeQuery(draft);
      range.current = draft;
      void load(range.current);
    } catch (error) {
      controller.current?.abort();
      controller.current = null;
      failedCursor.current = undefined;
      dispatchAudit({
        type: "invalid_range",
        message:
          error instanceof Error ? error.message : "The time range is invalid.",
      });
    }
  }

  let state: ReactNode;
  if (loading === "initial") {
    state = (
      <StatePanel kind="loading" title="Loading audit events">
        The Router is loading one bounded content-free audit page.
      </StatePanel>
    );
  } else if (failure !== null) {
    state = (
      <StatePanel
        kind="error"
        title="Audit events are not available"
        onRetry={() => {
          focusAfterLoad.current = true;
          void load(range.current, failedCursor.current);
        }}
      >
        {failure}
      </StatePanel>
    );
  } else if (items.length === 0) {
    state = (
      <StatePanel kind="empty" title="No audit events in this range">
        Change the UTC time range or refresh this page.
      </StatePanel>
    );
  } else {
    state = (
      <>
        <ol className="audit-event-list">
          {items.map((item) => (
            <AuditEventCard key={item.event_id} value={item} />
          ))}
        </ol>
        {nextCursor === null ? (
          <p className="audit-page-end">
            The complete selected range is shown.
          </p>
        ) : (
          <div className="audit-page-actions">
            <Button
              variant="secondary"
              disabled={loading === "next"}
              onClick={() => {
                focusAfterLoad.current = true;
                void load(range.current, nextCursor);
              }}
            >
              {loading === "next" ? "Loading next page…" : "Load next page"}
            </Button>
          </div>
        )}
      </>
    );
  }

  return (
    <div className="panel-stack">
      <PageHeading
        eyebrow="Global administration"
        title="Audit events"
        description="Review safe actions, authority, scope, and outcomes. This page does not return prompts, outputs, tool data, credentials, tokens, cookies, or provider error bodies."
      />
      <Panel>
        <PanelHeader
          kicker="Bounded discovery"
          title="Security and administration activity"
          description="Times use UTC. Each page contains at most 100 events in newest-first order."
        />
        <form className="audit-filter" onSubmit={submit}>
          <label>
            <span>From (UTC)</span>
            <input
              type="datetime-local"
              value={draft.from}
              onChange={(event) => {
                updateDraft({ from: event.currentTarget.value });
              }}
              required
            />
          </label>
          <label>
            <span>To (UTC)</span>
            <input
              type="datetime-local"
              value={draft.to}
              onChange={(event) => {
                updateDraft({ to: event.currentTarget.value });
              }}
              required
            />
          </label>
          <Button type="submit">Apply range</Button>
        </form>
        <div
          ref={results}
          className="audit-results"
          tabIndex={-1}
          aria-label="Audit event results"
          aria-live="polite"
          aria-busy={loading !== null}
        >
          {state}
        </div>
      </Panel>
    </div>
  );
}

function decimalLessThan(left: string, right: string): boolean {
  const [leftWhole = "0", leftFraction = ""] = left.split(".");
  const [rightWhole = "0", rightFraction = ""] = right.split(".");
  const leftNormalized = leftWhole.replace(/^0+(?=\d)/, "");
  const rightNormalized = rightWhole.replace(/^0+(?=\d)/, "");
  if (leftNormalized.length !== rightNormalized.length) {
    return leftNormalized.length < rightNormalized.length;
  }
  if (leftNormalized !== rightNormalized)
    return leftNormalized < rightNormalized;
  const width = Math.max(leftFraction.length, rightFraction.length);
  return leftFraction.padEnd(width, "0") < rightFraction.padEnd(width, "0");
}

function moneyText(value: {
  readonly amount: string;
  readonly currency: string;
}) {
  return `${value.amount} ${value.currency}`;
}

interface BudgetFormState {
  readonly hardLimit: string;
  readonly currency: string;
  readonly warning: string;
  readonly resetPeriod: "none" | "daily" | "monthly";
}

function BudgetView({
  client,
  scope,
  summary,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly summary: BudgetSummary | null;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const [form, updateForm] = useReducer(
    (state: BudgetFormState, update: Partial<BudgetFormState>) => ({
      ...state,
      ...update,
    }),
    {
      hardLimit: summary?.limit.amount ?? "",
      currency: summary?.limit.currency ?? "USD",
      warning: summary?.warning_threshold?.amount ?? "",
      resetPeriod: summary?.reset_period ?? "none",
    },
  );
  const [submitting, setSubmitting] = useState(false);
  const valid =
    nonNegativeDecimal.test(form.hardLimit) &&
    /^[A-Z]{3}$/.test(form.currency) &&
    (form.warning === "" ||
      (nonNegativeDecimal.test(form.warning) &&
        !decimalLessThan(form.hardLimit, form.warning)));
  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid) return;
    setSubmitting(true);
    try {
      const result = await client.putBudget(scope, {
        hardLimit: form.hardLimit,
        currency: form.currency,
        warningThreshold: form.warning === "" ? null : form.warning,
        resetPeriod: form.resetPeriod,
        expectedRevision: summary?.revision ?? "0",
      });
      await refreshAfterCommit(
        `Budget revision ${revisionLabel(result.revision)} is active.`,
        onChanged,
        onNotice,
      );
    } catch (error) {
      await recoverAfterMutationFailure(error, onChanged, onNotice);
    } finally {
      setSubmitting(false);
    }
  }
  const values =
    summary === null
      ? []
      : [
          ["Hard limit", moneyText(summary.limit)],
          [
            "Warning threshold",
            summary.warning_threshold === null
              ? "Not configured"
              : moneyText(summary.warning_threshold),
          ],
          ["Reserved", moneyText(summary.reserved)],
          ["Used", moneyText(summary.used)],
          ["Corrected", moneyText(summary.corrected)],
          ["Remaining", moneyText(summary.remaining)],
          ["Enforcement", summary.enforcement_state],
          ["Reset period", summary.reset_period],
          ["Revision", summary.revision],
        ];
  return (
    <div className="panel-stack">
      <PageHeading
        eyebrow={
          scope.workspaceId === "" ? "Selected service" : "Selected workspace"
        }
        title="Budget"
        description="Set one hard limit in one exact currency. Router does not convert currencies."
      />
      <Panel>
        <PanelHeader
          kicker="Current enforcement"
          title={
            summary === null
              ? "No limit is configured at this scope"
              : "Budget summary"
          }
          description={
            summary === null
              ? "A parent, global, or host ceiling can still enforce a limit. Create a local limit for this exact scope."
              : "Reservations, use, corrections, and remaining cost use the current budget revision."
          }
        />
        {summary === null ? null : (
          <dl className="budget-summary" data-state={summary.enforcement_state}>
            {values.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </Panel>
      <Panel>
        <PanelHeader
          kicker="Exact selected scope"
          title={summary === null ? "Create budget" : "Replace budget limit"}
          description="This change uses the current revision and takes effect immediately."
        />
        <form
          className="configuration-form"
          onSubmit={(event) => void submit(event)}
        >
          <div className="form-grid form-grid-three">
            <label>
              Hard limit
              <input
                required
                inputMode="decimal"
                pattern="(0|[1-9][0-9]*)(\.[0-9]+)?"
                value={form.hardLimit}
                onChange={(event) => {
                  updateForm({ hardLimit: event.currentTarget.value });
                }}
              />
            </label>
            <label>
              Currency
              <input
                required
                pattern="[A-Z]{3}"
                maxLength={3}
                disabled={summary !== null}
                value={form.currency}
                onChange={(event) => {
                  updateForm({
                    currency: event.currentTarget.value.toUpperCase(),
                  });
                }}
              />
              {summary === null ? null : (
                <small>
                  Currency cannot change after this budget is created.
                </small>
              )}
            </label>
            <label>
              Warning threshold (optional)
              <input
                inputMode="decimal"
                pattern="(0|[1-9][0-9]*)(\.[0-9]+)?"
                value={form.warning}
                onChange={(event) => {
                  updateForm({ warning: event.currentTarget.value });
                }}
              />
            </label>
            <label>
              Reset period
              <select
                value={form.resetPeriod}
                onChange={(event) => {
                  updateForm({
                    resetPeriod: event.currentTarget.value as
                      "none" | "daily" | "monthly",
                  });
                }}
              >
                <option value="none">No reset</option>
                <option value="daily">Daily</option>
                <option value="monthly">Monthly</option>
              </select>
            </label>
          </div>
          {form.warning !== "" &&
          decimalLessThan(form.hardLimit, form.warning) ? (
            <p role="alert">
              The warning threshold must not exceed the hard limit.
            </p>
          ) : null}
          <Button type="submit" disabled={!valid || submitting}>
            {submitting ? "Saving…" : "Save budget"}
          </Button>
        </form>
      </Panel>
    </div>
  );
}

export function StaleRevisionBanner() {
  return (
    <div className="stale-banner" role="alert">
      <Icon name="warning" />
      <span>
        <strong>This configuration changed.</strong> Refresh the service, review
        the active revision, and submit an intentional new change.
      </span>
    </div>
  );
}

function GlobalOverview({
  services,
  onOpenServices,
  onSelectService,
}: {
  readonly services: readonly ServiceSummary[];
  readonly onOpenServices: () => void;
  readonly onSelectService: (serviceId: string) => void;
}) {
  const activeServices = services.filter(
    (service) => service.state === "active",
  );
  const parentedServices = services.filter(
    (service) => service.parent_service_id != null,
  );
  return (
    <div className="overview-stack">
      <PageHeading
        eyebrow="Global administration"
        title="Run LLM Router"
        description="Create services, control what they inherit, and then configure each selected service."
        actions={
          <Button
            icon={<Icon name="plus" size={16} />}
            onClick={onOpenServices}
          >
            Create or manage services
          </Button>
        }
      />
      <section className="overview-stats" aria-label="Global Router summary">
        <StatCard
          icon={<Icon name="server" />}
          label="Services"
          value={services.length}
          note="All retained services"
          tone="blue"
        />
        <StatCard
          icon={<Icon name="health" />}
          label="Active services"
          value={activeServices.length}
          note="Can accept new work"
          tone="lime"
        />
        <StatCard
          icon={<Icon name="layers" />}
          label="Inherited services"
          value={parentedServices.length}
          note="Use a parent configuration chain"
          tone="purple"
        />
      </section>
      <Panel>
        <PanelHeader
          kicker="Start here"
          title={
            services.length === 0
              ? "Create your first service"
              : "Choose what you want to do"
          }
          description={
            services.length === 0
              ? "A service represents one application that uses Router, such as Xbot or Ontology."
              : "Global actions do not depend on the selected service. Select a service only when you want to configure or inspect it."
          }
        />
        <div className="global-action-grid">
          <button type="button" onClick={onOpenServices}>
            <Icon name="layers" size={20} />
            <span>
              <strong>Services and inheritance</strong>
              <small>
                Create, rename, organize, disable, restore, or retire services.
              </small>
            </span>
            <Icon name="chevron" size={16} />
          </button>
          {activeServices.slice(0, 3).map((service) => (
            <button
              type="button"
              key={service.service_id}
              onClick={() => {
                onSelectService(service.service_id);
              }}
            >
              <Icon name="server" size={20} />
              <span>
                <strong>Configure {service.display_name}</strong>
                <small>
                  Open its setup and effective inherited configuration.
                </small>
              </span>
              <Icon name="chevron" size={16} />
            </button>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function ServiceSetup({
  snapshot,
  bootstrapState,
  serviceName,
  onOpen,
}: {
  readonly snapshot: AdministrationSnapshot;
  readonly bootstrapState: ServiceSummary["bootstrap_state"];
  readonly serviceName: string;
  readonly onOpen: (section: Section) => void;
}) {
  const checks = [
    {
      label: "Service access key ready",
      complete: bootstrapState === "ready",
      section: "setup" as const,
    },
    {
      label: "Provider connection ready",
      complete: snapshot.providers.some((item) => item.state === "active"),
      section: "configuration" as const,
    },
    {
      label: "Model route ready",
      complete: snapshot.routes.some((item) => item.state === "active"),
      section: "configuration" as const,
    },
    {
      label: "Assignment ready",
      complete: snapshot.assignments.some(
        (item) => item.state === "active" && item.candidates.length > 0,
      ),
      section: "assignments" as const,
    },
  ];
  const complete = checks.filter((check) => check.complete).length;
  return (
    <div className="overview-stack">
      <PageHeading
        eyebrow={`Selected service · ${serviceName}`}
        title="Setup"
        description={`${String(complete)} of ${String(checks.length)} required steps are complete. Finish these steps before the service sends model requests.`}
      />
      <Panel>
        <PanelHeader
          kicker="Required setup"
          title={`Get ${serviceName} ready`}
          description="Router checks each result. You do not need to work with internal IDs."
        />
        <div className="service-setup-checklist">
          {checks.map((check, index) => (
            <div key={check.label} data-complete={check.complete}>
              <span>
                {check.complete ? <Icon name="health" size={18} /> : index + 1}
              </span>
              <div>
                <strong>{check.label}</strong>
                <small>{check.complete ? "Complete" : "Needs attention"}</small>
              </div>
              {check.complete ? (
                <StatusPill tone="green">Ready</StatusPill>
              ) : (
                <Button
                  variant="secondary"
                  onClick={() => {
                    onOpen(check.section);
                  }}
                >
                  Set up now
                </Button>
              )}
            </div>
          ))}
        </div>
      </Panel>
      <Panel>
        <PanelHeader
          kicker="Inheritance"
          title="What this service will use"
          description="Effective configuration includes eligible parent items. Router labels inherited items and keeps parent records read only here."
          actions={
            <Button
              variant="secondary"
              onClick={() => {
                onOpen("configuration");
              }}
            >
              View effective configuration
            </Button>
          }
        />
      </Panel>
    </div>
  );
}

function AdministratorMobileNavigation({
  section,
  selectedService,
  onOpen,
}: {
  readonly section: Section;
  readonly selectedService: ServiceSummary | undefined;
  readonly onOpen: (section: Section) => void;
}) {
  const serviceSectionActive = serviceSections.some(
    (item) => item.id === section,
  );
  const visibleSections = serviceSectionActive
    ? serviceSections
    : globalSections;
  const switchItem =
    selectedService === undefined
      ? []
      : [
          serviceSectionActive
            ? {
                id: "open-global",
                label: "Open global tasks",
                icon: <Icon name="grid" size={18} />,
                active: false,
              }
            : {
                id: "open-service",
                label: `Open ${selectedService.display_name} tasks`,
                icon: <Icon name="server" size={18} />,
                active: false,
              },
        ];
  return (
    <MobileNavigation
      aria-label={
        serviceSectionActive
          ? `${selectedService?.display_name ?? "Selected service"} tasks`
          : "Global administrator tasks"
      }
      items={[
        ...switchItem,
        ...visibleSections.map((item) => ({
          id: item.id,
          label: item.label,
          icon: <Icon name={item.icon} size={18} />,
          active: section === item.id,
        })),
      ]}
      onSelect={(id) => {
        if (id === "open-global") onOpen("overview");
        else if (id === "open-service") onOpen("configuration");
        else onOpen(id as Section);
      }}
    />
  );
}

function MobileServiceSelector({
  scope,
  services,
  onSelect,
}: {
  readonly scope: ScopeSelection;
  readonly services: readonly ServiceSummary[];
  readonly onSelect: (serviceId: string) => void;
}) {
  return (
    <label className="mobile-service-selector">
      <span>Service to manage</span>
      <select
        value={scope.serviceId}
        onChange={(event) => {
          onSelect(event.currentTarget.value);
        }}
      >
        <option value="">No service selected</option>
        {services.map((service) => (
          <option key={service.service_id} value={service.service_id}>
            {service.display_name} · {service.state}
          </option>
        ))}
      </select>
      <small>Global tasks stay available for all services.</small>
    </label>
  );
}

function DesktopServiceSelector({
  scope,
  services,
  selectedService,
  onSelect,
}: {
  readonly scope: ScopeSelection;
  readonly services: readonly ServiceSummary[];
  readonly selectedService: ServiceSummary | undefined;
  readonly onSelect: (serviceId: string) => void;
}) {
  return (
    <label className="service-selector">
      <span>Service to manage</span>
      <select
        aria-label="Service to manage"
        value={scope.serviceId}
        onChange={(event) => {
          onSelect(event.currentTarget.value);
        }}
      >
        <option value="">No service selected</option>
        {services.map((service) => (
          <option key={service.service_id} value={service.service_id}>
            {service.display_name} · {service.state}
          </option>
        ))}
      </select>
      <small>
        {selectedService === undefined
          ? "Global tasks are available."
          : "Service tasks apply to this service."}
      </small>
    </label>
  );
}

function AdministratorSidebarFooter() {
  return (
    <div className="sidebar-help">
      <Icon name="shield" size={16} />
      <span>
        <strong>Global administrator</strong>
        <small>All Router services</small>
      </span>
    </div>
  );
}

function SelectedServiceState({
  failure,
  loading,
  snapshot,
  onReload,
}: {
  readonly failure: string | null | undefined;
  readonly loading: boolean | undefined;
  readonly snapshot: AdministrationSnapshot | null;
  readonly onReload: () => Promise<void>;
}) {
  if (loading) {
    return (
      <StateMessage kind="loading">
        The selected service is loading.
      </StateMessage>
    );
  }
  if (failure != null) {
    return (
      <StateMessage kind="error" onRetry={() => void onReload()}>
        {failure}
      </StateMessage>
    );
  }
  return snapshot === null ? (
    <StateMessage kind="empty">Select a service to use this task.</StateMessage>
  ) : null;
}

function GlobalCredentialsView({
  client,
  credentials,
  failure,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly credentials: readonly Credential[];
  readonly failure?: string;
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  return (
    <div className="panel-stack">
      <PageHeading
        eyebrow="Global administration"
        title="Provider credentials"
        description="Store provider secrets once, then let eligible service configurations reference them without showing the secret value."
      />
      <Panel>
        <PanelHeader
          kicker="Global secret custody"
          title="Stored provider credentials"
          description="Secret values never return to this application."
        />
        {failure === undefined ? (
          <CredentialForm
            client={client}
            ownerScope="global"
            onChanged={onChanged}
            onNotice={onNotice}
          />
        ) : null}
        {failure === undefined ? null : (
          <ScopedReadFailure
            title="Credential metadata is not available"
            message={failure}
          />
        )}
        {failure === undefined ? (
          <CredentialTable
            client={client}
            values={credentials}
            onChanged={onChanged}
            onNotice={onNotice}
          />
        ) : null}
      </Panel>
    </div>
  );
}

export interface AdministrationDashboardProps {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot | null;
  readonly initialSection?: Section;
  readonly failure?: string | null;
  readonly loading?: boolean;
  readonly notice: Notice | null;
  readonly onNotice: (notice: Notice | null) => void;
  readonly onGlobalReload?: () => Promise<void>;
  readonly onReload: () => Promise<void>;
  readonly onScopeChange?: ((scope: ScopeSelection) => void) | undefined;
  readonly accountActions?: ReactNode;
  readonly credentials?: readonly Credential[];
  readonly services?: readonly ServiceSummary[];
  readonly catalogModels?: readonly CatalogEntry[];
  readonly catalogLoading?: boolean;
  readonly globalFailures?: GlobalFailures;
}

function AdministratorSidebar({
  scope,
  services,
  selectedService,
  section,
  onOpen,
  onSelect,
}: {
  readonly scope: ScopeSelection;
  readonly services: readonly ServiceSummary[];
  readonly selectedService: ServiceSummary | undefined;
  readonly section: Section;
  readonly onOpen: (section: Section) => void;
  readonly onSelect: (serviceId: string) => void;
}) {
  const navigation = (
    <ApplicationNavigation aria-label="Administrator tasks">
      <ApplicationNavigationGroup label="Global administration">
        {globalSections.map((item) => (
          <NavigationItem
            key={item.id}
            active={section === item.id}
            icon={<Icon name={item.icon} size={18} />}
            label={item.label}
            onClick={() => {
              onOpen(item.id);
            }}
          />
        ))}
      </ApplicationNavigationGroup>
      <ApplicationNavigationGroup
        className="selected-service-navigation"
        label={selectedService?.display_name ?? "Selected service"}
      >
        {serviceSections.map((item) => (
          <NavigationItem
            key={item.id}
            active={section === item.id}
            disabled={selectedService === undefined}
            icon={<Icon name={item.icon} size={18} />}
            label={item.label}
            onClick={() => {
              onOpen(item.id);
            }}
          />
        ))}
      </ApplicationNavigationGroup>
    </ApplicationNavigation>
  );
  return (
    <ApplicationSidebar
      className="router-sidebar"
      brand={
        <div className="brand">
          <span>
            <Icon name="layers" />
          </span>
          <div>
            <strong>LLM Router</strong>
            <small>Administration</small>
          </div>
        </div>
      }
      context={
        <DesktopServiceSelector
          scope={scope}
          services={services}
          selectedService={selectedService}
          onSelect={onSelect}
        />
      }
      navigation={navigation}
      footer={<AdministratorSidebarFooter />}
    />
  );
}

function SelectedBudgetSection({
  client,
  scope,
  snapshot,
  onReload,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot;
  readonly onReload: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  if (snapshot.failures.state !== undefined || snapshot.state === null) {
    return (
      <ScopedReadFailure
        title="Budget scope is not available"
        message={
          snapshot.failures.state ??
          "The exact service or workspace scope is not available."
        }
      />
    );
  }
  if (snapshot.failures.budget !== undefined) {
    return (
      <ScopedReadFailure
        title="Budget is not available"
        message={snapshot.failures.budget}
      />
    );
  }
  return (
    <BudgetView
      key={JSON.stringify([
        scope.serviceId,
        scope.workspaceId,
        snapshot.budget?.revision ?? null,
      ])}
      client={client}
      scope={scope}
      summary={snapshot.budget}
      onChanged={onReload}
      onNotice={onNotice}
    />
  );
}

type DiagnosticViewState =
  | "ready"
  | "submitting"
  | "refreshing"
  | "active"
  | "succeeded"
  | "failed"
  | "interrupted"
  | "cancel_requested"
  | "cancelled"
  | "uncertain"
  | "outcome_uncertain"
  | "expired"
  | "forbidden"
  | "recent_auth"
  | "read_only"
  | "stale"
  | "offline";

interface DiagnosticAttempt {
  readonly requestId: string;
  readonly exactRoute: string;
  readonly reason: string;
}

interface DiagnosticFormState {
  readonly routeId: string;
  readonly reason: string;
  readonly run: DiagnosticRun | null;
  readonly status: RequestStatus | null;
  readonly pending: "submit" | "refresh" | null;
  readonly failure: DiagnosticViewState | null;
  readonly recovery: DiagnosticAttempt | null;
  readonly safeMessage: string | null;
}

function updateDiagnosticFormState(
  state: DiagnosticFormState,
  update: Partial<DiagnosticFormState>,
): DiagnosticFormState {
  return { ...state, ...update };
}

const diagnosticTimestampFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});

function formatDiagnosticTimestamp(value: string): string {
  return diagnosticTimestampFormatter.format(Date.parse(value));
}

function diagnosticViewState(
  run: DiagnosticRun | null,
  status: RequestStatus | null,
  failure: DiagnosticViewState | null,
): DiagnosticViewState {
  if (failure !== null) return failure;
  if (status !== null) {
    if (status.state === "succeeded") return "succeeded";
    if (status.state === "failed") return "failed";
    if (status.state === "interrupted") return "interrupted";
    if (status.state === "cancel_requested") return "cancel_requested";
    if (status.state === "cancelled") return "cancelled";
    if (status.state === "uncertain") return "uncertain";
    return "active";
  }
  if (run === null) return "ready";
  return run.state;
}

function diagnosticPhases(
  run: DiagnosticRun | null,
  status: RequestStatus | null,
): readonly DiagnosticPhase[] {
  const initialPhases: readonly DiagnosticPhase[] = run?.phases ?? [
    { name: "authorization", state: "succeeded" },
    { name: "route_eligibility", state: "succeeded" },
    { name: "admission", state: "succeeded" },
    { name: "provider", state: "active" },
    { name: "accounting", state: "pending" },
  ];
  if (status === null) return initialPhases;
  const terminal = [
    "succeeded",
    "failed",
    "interrupted",
    "cancelled",
    "uncertain",
  ].includes(status.state);
  const failed = terminal && status.state !== "succeeded";
  const lastFailedAttempt = [...status.attempts]
    .reverse()
    .find((attempt) => attempt.error !== undefined);
  const failureClass =
    status.error?.class ??
    lastFailedAttempt?.error?.class ??
    (status.state === "cancelled"
      ? "cancelled"
      : status.state === "uncertain"
        ? "uncertain_effect"
        : undefined);
  return initialPhases.map((phase) => {
    if (phase.name === "provider") {
      return {
        name: phase.name,
        state: failed ? "failed" : terminal ? "succeeded" : "active",
        ...(failureClass === undefined ? {} : { failure_class: failureClass }),
      };
    }
    if (phase.name === "accounting") {
      return {
        name: phase.name,
        state: status.state === "succeeded" ? "succeeded" : "pending",
      };
    }
    return phase;
  });
}

function DiagnosticResult({
  state,
  title,
  tone,
  pending,
  safeMessage,
  recovery,
  run,
  status,
  scope,
  phases,
  failureClass,
  focusTarget,
  onReload,
  onRefresh,
  onRetry,
}: {
  readonly state: DiagnosticViewState;
  readonly title: string;
  readonly tone: "green" | "red" | "blue" | "amber";
  readonly pending: DiagnosticFormState["pending"];
  readonly safeMessage: string | null;
  readonly recovery: DiagnosticAttempt | null;
  readonly run: DiagnosticRun | null;
  readonly status: RequestStatus | null;
  readonly scope: ScopeSelection;
  readonly phases: readonly DiagnosticPhase[];
  readonly failureClass: RequestFailureClass | undefined;
  readonly focusTarget: RefObject<HTMLDivElement | null>;
  readonly onReload: () => Promise<void>;
  readonly onRefresh: (requestId: string) => void;
  readonly onRetry: (attempt: DiagnosticAttempt) => void;
}) {
  return (
    <div
      ref={focusTarget}
      className="diagnostic-result-focus"
      tabIndex={-1}
      aria-label="Safe diagnostic result"
    >
      <Panel className="diagnostic-result" aria-live="polite">
        <PanelHeader
          kicker="Content-free result"
          title={title}
          description="A read-only grant can inspect current data but cannot run this diagnostic."
          actions={
            <StatusPill tone={tone}>{state.replace("_", " ")}</StatusPill>
          }
        />
        {state === "recent_auth" ? (
          <p>Authenticate with Pocket ID again, then start a new diagnostic.</p>
        ) : null}
        {state === "forbidden" || state === "read_only" ? (
          <p>
            The current administrator grant does not permit this exact action
            and scope.
          </p>
        ) : null}
        {state === "outcome_uncertain" && recovery !== null ? (
          <p>
            Check the same request identity or retry it. Do not start a new
            diagnostic until the outcome is known.
          </p>
        ) : null}
        {state === "stale" ? (
          <Button
            variant="secondary"
            disabled={pending !== null}
            onClick={() => {
              void onReload();
            }}
          >
            Refresh selected service
          </Button>
        ) : null}
        {safeMessage === null ? null : <p>{safeMessage}</p>}
        {recovery === null ? null : (
          <dl className="diagnostic-scope">
            <div>
              <dt>Request</dt>
              <dd>{recovery.requestId}</dd>
            </div>
            <div>
              <dt>Route</dt>
              <dd>{recovery.exactRoute}</dd>
            </div>
          </dl>
        )}
        {run === null && status === null ? null : (
          <>
            <dl className="diagnostic-scope">
              <div>
                <dt>Service</dt>
                <dd>{run?.service_id ?? scope.serviceId}</dd>
              </div>
              <div>
                <dt>Workspace</dt>
                <dd>
                  {run?.workspace_id ??
                    (scope.workspaceId === ""
                      ? "Service level"
                      : scope.workspaceId)}
                </dd>
              </div>
              <div>
                <dt>Route</dt>
                <dd>{run?.exact_route ?? status?.exact_route}</dd>
              </div>
              <div>
                <dt>Route revision</dt>
                <dd>
                  {run?.route_configuration_revision ??
                    status?.configuration_revision}
                </dd>
              </div>
              {run === null ? null : (
                <div>
                  <dt>Authorization expires</dt>
                  <dd>
                    {formatDiagnosticTimestamp(run.authorization_expires_at)}
                  </dd>
                </div>
              )}
              <div>
                <dt>Failure class</dt>
                <dd>{failureClass ?? "None"}</dd>
              </div>
            </dl>
            <ol
              className="diagnostic-phases"
              aria-label="Safe diagnostic phases"
            >
              {phases.map((phase) => (
                <li key={phase.name}>
                  <span>{phase.name.replace("_", " ")}</span>
                  <StatusPill tone={toneForState(phase.state)}>
                    {phase.state}
                  </StatusPill>
                </li>
              ))}
            </ol>
            <Button
              variant="secondary"
              disabled={pending !== null}
              onClick={() => {
                const requestId = run?.request_id ?? status?.request_id;
                if (requestId !== undefined) onRefresh(requestId);
              }}
            >
              {pending === "refresh"
                ? "Refreshing diagnostic status"
                : "Refresh diagnostic status"}
            </Button>
          </>
        )}
        {recovery === null ? null : (
          <div className="diagnostic-actions">
            <Button
              variant="secondary"
              disabled={pending !== null}
              onClick={() => {
                onRefresh(recovery.requestId);
              }}
            >
              Check diagnostic status
            </Button>
            <Button
              disabled={pending !== null}
              onClick={() => {
                onRetry(recovery);
              }}
            >
              Retry same diagnostic request
            </Button>
          </div>
        )}
      </Panel>
    </div>
  );
}

function DiagnosticRouteForm({
  routes,
  routeFailure,
  scopeDescription,
  routeId,
  reason,
  pending,
  recovery,
  onRouteChange,
  onReasonChange,
  onSubmit,
}: {
  readonly routes: readonly ProviderModelRoute[];
  readonly routeFailure: string | undefined;
  readonly scopeDescription: string;
  readonly routeId: string;
  readonly reason: string;
  readonly pending: DiagnosticFormState["pending"];
  readonly recovery: DiagnosticAttempt | null;
  readonly onRouteChange: (routeId: string) => void;
  readonly onReasonChange: (reason: string) => void;
  readonly onSubmit: (event: SubmitEvent<HTMLFormElement>) => void;
}) {
  return (
    <Panel>
      <PanelHeader
        kicker="Exact scope"
        title="Select one eligible route"
        description={scopeDescription}
      />
      {routeFailure === undefined ? null : (
        <ScopedReadFailure
          title="Eligible routes are not available"
          message={routeFailure}
        />
      )}
      <form className="diagnostic-form" onSubmit={onSubmit}>
        <label>
          <span>Provider-model route</span>
          <select
            value={routeId}
            disabled={
              routes.length === 0 || pending !== null || recovery !== null
            }
            onChange={(event) => {
              onRouteChange(event.currentTarget.value);
            }}
          >
            {routes.length === 0 ? (
              <option value="">No eligible route</option>
            ) : null}
            {routes.map((route) => (
              <option
                key={route.provider_model_route_id}
                value={route.provider_model_route_id}
              >
                {route.wire_model} · {route.provider_model_route_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Audit reason</span>
          <input
            value={reason}
            maxLength={500}
            disabled={pending !== null || recovery !== null}
            onChange={(event) => {
              onReasonChange(event.currentTarget.value);
            }}
          />
        </label>
        <Button
          type="submit"
          disabled={
            routeId === "" ||
            reason.trim() === "" ||
            pending !== null ||
            recovery !== null
          }
        >
          {pending === "submit" ? "Starting diagnostic" : "Run diagnostic"}
        </Button>
      </form>
      {routes.length === 0 && routeFailure === undefined ? (
        <StateMessage kind="empty">
          No active chat route is eligible in this exact scope.
        </StateMessage>
      ) : null}
    </Panel>
  );
}

function DiagnosticsView({
  client,
  scope,
  snapshot,
  onReload,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot;
  readonly onReload: () => Promise<void>;
}) {
  const routes = snapshot.routes.filter(
    (route) =>
      route.state === "active" && route.capabilities.includes("chat.complete"),
  );
  const [form, updateForm] = useReducer(updateDiagnosticFormState, {
    routeId: routes[0]?.provider_model_route_id ?? "",
    reason: "Verify the selected provider-model route.",
    run: null,
    status: null,
    pending: null,
    failure: null,
    recovery: null,
    safeMessage: null,
  });
  const {
    routeId,
    reason,
    run,
    status,
    pending,
    failure,
    recovery,
    safeMessage,
  } = form;
  const state =
    pending === "submit"
      ? "submitting"
      : pending === "refresh"
        ? "refreshing"
        : diagnosticViewState(run, status, failure);
  const phases =
    run === null && status === null ? [] : diagnosticPhases(run, status);
  const failureClass = phases.find(
    (phase) => phase.failure_class !== undefined,
  )?.failure_class;
  const controller = useRef<AbortController | null>(null);
  const focusTarget = useRef<HTMLDivElement | null>(null);
  const focusAfterInteraction = useRef(false);

  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  useEffect(() => {
    if (!focusAfterInteraction.current) return;
    focusAfterInteraction.current = false;
    focusTarget.current?.focus();
  }, [state]);

  function mapFailure(error: unknown): DiagnosticViewState {
    if (!(error instanceof AdministrationApiError)) return "failed";
    if (error.code === "recent_auth_required") return "recent_auth";
    if (error.code === "insufficient_scope") return "read_only";
    if (error.code === "stale_configuration") return "stale";
    if (error.code === "offline" || error.status === 503) return "offline";
    if (error.status === 401 || error.status === 403) return "forbidden";
    return "failed";
  }

  async function refreshStatus(requestId: string) {
    controller.current?.abort();
    const nextController = new AbortController();
    controller.current = nextController;
    focusAfterInteraction.current = true;
    updateForm({ pending: "refresh", failure: null });
    try {
      const refreshed = await client.getRequest(
        scope,
        requestId,
        nextController.signal,
      );
      if (!nextController.signal.aborted) {
        focusAfterInteraction.current = true;
        updateForm({
          status: refreshed,
          pending: null,
          failure: null,
          recovery: null,
          safeMessage: refreshed.error?.message ?? null,
        });
      }
    } catch (error) {
      if (!nextController.signal.aborted) {
        focusAfterInteraction.current = true;
        const unavailableRecovery =
          recovery !== null &&
          error instanceof AdministrationApiError &&
          ["not_found", "request_not_found"].includes(error.code);
        updateForm({
          failure: unavailableRecovery
            ? "outcome_uncertain"
            : mapFailure(error),
          pending: null,
          safeMessage: unavailableRecovery
            ? "No admitted request is available. Retry the same request identity before you start a new diagnostic."
            : error instanceof AdministrationApiError
              ? error.message
              : "The diagnostic status did not load.",
        });
      }
    }
  }

  async function startDiagnostic(attempt: DiagnosticAttempt) {
    controller.current?.abort();
    const nextController = new AbortController();
    controller.current = nextController;
    focusAfterInteraction.current = true;
    updateForm({
      pending: "submit",
      failure: null,
      run: null,
      status: null,
      safeMessage: null,
    });
    try {
      const created = await client.runDiagnostic(
        scope,
        attempt,
        nextController.signal,
      );
      if (!nextController.signal.aborted) {
        focusAfterInteraction.current = true;
        updateForm({
          run: created,
          pending: null,
          failure: null,
          recovery: null,
        });
      }
    } catch (error) {
      if (!nextController.signal.aborted) {
        focusAfterInteraction.current = true;
        const outcomeUncertain =
          error instanceof AdministrationApiError && error.outcomeUncertain;
        updateForm({
          failure: outcomeUncertain ? "outcome_uncertain" : mapFailure(error),
          pending: null,
          recovery: outcomeUncertain ? attempt : null,
          safeMessage:
            error instanceof AdministrationApiError
              ? error.message
              : "The diagnostic did not start.",
        });
      }
    }
  }

  async function reloadDiagnosticScope() {
    controller.current?.abort();
    focusAfterInteraction.current = true;
    updateForm({ pending: "refresh" });
    try {
      await onReload();
      focusAfterInteraction.current = true;
      updateForm({
        pending: null,
        failure: null,
        safeMessage: null,
      });
    } catch {
      focusAfterInteraction.current = true;
      updateForm({
        pending: null,
        failure: "offline",
        safeMessage: "The selected service did not refresh.",
      });
    }
  }

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (routeId === "" || reason.trim() === "" || recovery !== null) return;
    void startDiagnostic({
      requestId: newLogicalRequestId(),
      exactRoute: routeId,
      reason: reason.trim(),
    });
  }

  const stateTitle = {
    ready: "Ready to run",
    submitting: "Diagnostic starting",
    refreshing: "Diagnostic status refreshing",
    active: "Diagnostic active",
    succeeded: "Diagnostic succeeded",
    failed: "Diagnostic failed",
    interrupted: "Diagnostic interrupted",
    cancel_requested: "Diagnostic cancellation requested",
    cancelled: "Diagnostic cancelled",
    uncertain: "Diagnostic result uncertain",
    outcome_uncertain: "Diagnostic admission outcome uncertain",
    expired: "Diagnostic permission expired",
    forbidden: "Diagnostic forbidden",
    recent_auth: "Recent authentication required",
    read_only: "Read-only administrator grant",
    stale: "Diagnostic configuration is stale",
    offline: "Diagnostic service is offline",
  }[state];
  const tone =
    state === "succeeded"
      ? "green"
      : [
            "failed",
            "interrupted",
            "cancelled",
            "uncertain",
            "outcome_uncertain",
            "expired",
            "forbidden",
            "recent_auth",
            "read_only",
            "stale",
            "offline",
          ].includes(state)
        ? "red"
        : state === "ready"
          ? "blue"
          : "amber";

  return (
    <div className="diagnostic-workspace">
      <PageHeading
        eyebrow="Selected service"
        title="Safe route diagnostic"
        description="Run one fixed, content-free probe through normal policy, budget, rate, accounting, and audit controls. Prompts, model output, provider error bodies, credentials, and bearer values do not return to this page."
      />
      <DiagnosticRouteForm
        routes={routes}
        routeFailure={snapshot.failures.routes}
        scopeDescription={`Service ${scope.serviceId} · ${scope.workspaceId === "" ? "Service level" : `Workspace ${scope.workspaceId}`}`}
        routeId={routeId}
        reason={reason}
        pending={pending}
        recovery={recovery}
        onRouteChange={(nextRouteId) => {
          updateForm({ routeId: nextRouteId });
        }}
        onReasonChange={(nextReason) => {
          updateForm({ reason: nextReason });
        }}
        onSubmit={submit}
      />
      <DiagnosticResult
        state={state}
        title={stateTitle}
        tone={tone}
        pending={pending}
        safeMessage={safeMessage}
        recovery={recovery}
        run={run}
        status={status}
        scope={scope}
        phases={phases}
        failureClass={failureClass}
        focusTarget={focusTarget}
        onReload={reloadDiagnosticScope}
        onRefresh={(requestId) => {
          void refreshStatus(requestId);
        }}
        onRetry={(attempt) => {
          void startDiagnostic(attempt);
        }}
      />
    </div>
  );
}

function OperationalSections({
  section,
  client,
  scope,
  snapshot,
  onReload,
}: {
  readonly section: Section;
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot | null;
  readonly onReload: () => Promise<void>;
}) {
  if (snapshot === null) return null;
  if (section === "requests") {
    return (
      <Panel>
        <PanelHeader
          kicker="Content-free status"
          title="Logical requests"
          description="This view does not contain prompts, model output, or provider secrets."
        />
        {snapshot.failures.requests === undefined ? (
          <RequestsView
            key={`${scope.serviceId}:${scope.workspaceId}`}
            client={client}
            scope={scope}
            values={snapshot.requests}
          />
        ) : (
          <ScopedReadFailure
            title="Request status is not available"
            message={snapshot.failures.requests}
          />
        )}
      </Panel>
    );
  }
  if (section === "diagnostics") {
    return (
      <DiagnosticsView
        key={`${scope.serviceId}:${scope.workspaceId}:${snapshot.configuration_revision ?? "none"}`}
        client={client}
        scope={scope}
        snapshot={snapshot}
        onReload={onReload}
      />
    );
  }
  return null;
}

export function AdministrationDashboard({
  client,
  scope,
  snapshot,
  initialSection = "overview",
  failure,
  loading,
  notice,
  onNotice,
  onGlobalReload,
  onReload,
  onScopeChange,
  accountActions,
  credentials = emptyCredentials,
  services = emptyServices,
  catalogModels = emptyCatalogEntries,
  catalogLoading = false,
  globalFailures = emptyGlobalFailures,
}: AdministrationDashboardProps) {
  const [section, setSection] = useReducer(
    (_current: Section, next: Section) => next,
    initialSection,
  );
  const [pendingBootstrap, setPendingBootstrap] = useReducer(
    (_current: ServiceCreated | null, next: ServiceCreated | null) => next,
    null,
  );
  const page = sections.find((item) => item.id === section) ?? sections.at(0);
  const selectedService = services.find(
    (service) => service.service_id === scope.serviceId,
  );
  useEffect(() => {
    if (pendingBootstrap === null) return;
    const preserveBootstrap = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", preserveBootstrap);
    return () => {
      window.removeEventListener("beforeunload", preserveBootstrap);
    };
  }, [pendingBootstrap]);
  if (page === undefined) return null;

  function openSection(nextSection: Section) {
    if (pendingBootstrap !== null) {
      onNotice({
        tone: "error",
        message: "Store and confirm the one-time service key before you leave.",
      });
      return;
    }
    setSection(nextSection);
  }

  function selectService(serviceId: string, destination?: Section) {
    if (pendingBootstrap !== null) {
      onNotice({
        tone: "error",
        message:
          "Store and confirm the one-time service key before you change services.",
      });
      return;
    }
    onScopeChange?.({ mode: "global", serviceId, workspaceId: "" });
    if (serviceId === "") setSection("overview");
    else if (destination !== undefined) setSection(destination);
  }

  return (
    <ApplicationShell
      className="router-application-shell"
      sidebar={
        <AdministratorSidebar
          scope={scope}
          services={services}
          selectedService={selectedService}
          section={section}
          onOpen={openSection}
          onSelect={(serviceId) => {
            selectService(serviceId, "configuration");
          }}
        />
      }
      mobileNavigation={
        <AdministratorMobileNavigation
          section={section}
          selectedService={selectedService}
          onOpen={openSection}
        />
      }
      mainProps={{
        className:
          section === "services" ? "content service-graph-page" : "content",
      }}
      topbar={
        <ApplicationTopbar
          className="router-topbar"
          title={
            <div className="topbar-title">
              <span>
                {serviceSections.some((item) => item.id === section)
                  ? (selectedService?.display_name ?? "Selected service")
                  : "Global administration"}
              </span>
              <strong>{page.label}</strong>
            </div>
          }
          actions={
            <div className="topbar-actions">
              {serviceSections.some((item) => item.id === section) &&
              selectedService !== undefined ? (
                <Button
                  variant="quiet"
                  icon={<Icon name="refresh" size={16} />}
                  onClick={() =>
                    void (
                      section === "configuration"
                        ? (onGlobalReload ?? onReload)()
                        : onReload()
                    ).catch(() => undefined)
                  }
                >
                  Refresh
                </Button>
              ) : null}
              {pendingBootstrap === null ? (
                accountActions
              ) : (
                <span className="pending-bootstrap-account-note">
                  Store the one-time key before account actions.
                </span>
              )}
            </div>
          }
        />
      }
    >
      <MobileServiceSelector
        scope={scope}
        services={services}
        onSelect={(serviceId) => {
          selectService(serviceId, "configuration");
        }}
      />
      {notice?.staleRevision === true ? <StaleRevisionBanner /> : null}
      {section === "overview" ? (
        <>
          {globalFailures.services === undefined ? null : (
            <ScopedReadFailure
              title="The service registry is not available"
              message={globalFailures.services}
            />
          )}
          <GlobalOverview
            services={services}
            onOpenServices={() => {
              openSection("services");
            }}
            onSelectService={(serviceId) => {
              selectService(serviceId, "configuration");
            }}
          />
        </>
      ) : null}
      {section === "services" ? (
        <>
          {globalFailures.services === undefined ? null : (
            <ScopedReadFailure
              title="The service registry is not available"
              message={globalFailures.services}
            />
          )}
          {globalFailures.services === undefined ? (
            <ServiceManagement
              client={client}
              services={services}
              selectedServiceId={scope.serviceId}
              onSelect={(serviceId) => {
                selectService(serviceId);
              }}
              onChanged={onGlobalReload ?? onReload}
              onContinueSetup={() => {
                setSection("setup");
              }}
              pendingBootstrap={pendingBootstrap}
              onBootstrapPending={setPendingBootstrap}
              onSuccess={(message) => {
                onNotice({ tone: "success", message });
              }}
              onError={(message) => {
                onNotice({ tone: "error", message });
              }}
            />
          ) : null}
        </>
      ) : null}
      {section === "credentials" ? (
        <GlobalCredentialsView
          client={client}
          credentials={credentials}
          {...(globalFailures.credentials === undefined
            ? {}
            : { failure: globalFailures.credentials })}
          onChanged={onGlobalReload ?? onReload}
          onNotice={(nextNotice) => {
            onNotice(nextNotice);
          }}
        />
      ) : null}
      {section === "audit" ? <AuditView client={client} /> : null}
      {section === "setup" && snapshot !== null ? (
        <ServiceSetup
          snapshot={snapshot}
          bootstrapState={selectedService?.bootstrap_state ?? "missing"}
          serviceName={selectedService?.display_name ?? "Selected service"}
          onOpen={openSection}
        />
      ) : null}
      {section === "configuration" && snapshot !== null ? (
        <ConfigurationView
          client={client}
          scope={scope}
          snapshot={snapshot}
          services={services}
          models={catalogModels}
          catalogLoading={catalogLoading}
          {...(globalFailures.catalog === undefined
            ? {}
            : { catalogFailure: globalFailures.catalog })}
          onChanged={onReload}
          onNotice={onNotice}
        />
      ) : null}
      {section === "assignments" && snapshot !== null ? (
        <AssignmentsView
          client={client}
          scope={scope}
          snapshot={snapshot}
          services={services}
          onChanged={onReload}
          onNotice={onNotice}
        />
      ) : null}
      <OperationalSections
        section={section}
        client={client}
        scope={scope}
        snapshot={snapshot}
        onReload={onReload}
      />
      {section === "accounting" && snapshot !== null ? (
        snapshot.accounting === null ? (
          <ScopedReadFailure
            title="Accounting is not available"
            message={
              snapshot.failures.accounting ??
              "No accounting summary is available for this scope."
            }
          />
        ) : (
          <AccountingView summary={snapshot.accounting} />
        )
      ) : null}
      {section === "budgets" && snapshot !== null ? (
        <SelectedBudgetSection
          client={client}
          scope={scope}
          snapshot={snapshot}
          onReload={onReload}
          onNotice={onNotice}
        />
      ) : null}
      {serviceSections.some((item) => item.id === section) ? (
        <SelectedServiceState
          failure={failure}
          loading={loading}
          snapshot={snapshot}
          onReload={onReload}
        />
      ) : null}
    </ApplicationShell>
  );
}

export function PersistentNotice({
  notice,
  onDismiss,
}: {
  readonly notice: Notice | null;
  readonly onDismiss: () => void;
}) {
  return notice ? (
    <Toast
      className={`notice notice-${notice.tone}`}
      role={notice.tone === "error" ? "alert" : "status"}
      onDismiss={onDismiss}
    >
      {notice.message}
    </Toast>
  ) : null;
}

export function AdministrationStateView({
  client,
  credentials = emptyCredentials,
  failure,
  loading,
  notice,
  onNotice,
  onGlobalReload,
  onReload,
  scope,
  snapshot,
  onScopeChange,
  accountActions,
  services = emptyServices,
  catalogModels = emptyCatalogEntries,
  catalogLoading = false,
  globalFailures,
}: {
  readonly client: AdministrationClient;
  readonly credentials?: readonly Credential[];
  readonly failure: string | null;
  readonly loading: boolean;
  readonly notice: Notice | null;
  readonly onNotice: (notice: Notice | null) => void;
  readonly onGlobalReload?: () => Promise<void>;
  readonly onReload: () => Promise<void>;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot | null;
  readonly onScopeChange?: ((scope: ScopeSelection) => void) | undefined;
  readonly accountActions?: ReactNode;
  readonly services?: readonly ServiceSummary[];
  readonly catalogModels?: readonly CatalogEntry[];
  readonly catalogLoading?: boolean;
  readonly globalFailures?: Readonly<
    Partial<Record<"services" | "credentials" | "catalog", string>>
  >;
}) {
  return (
    <>
      <AdministrationDashboard
        client={client}
        credentials={credentials}
        failure={failure}
        loading={loading}
        scope={scope}
        snapshot={snapshot}
        notice={notice}
        onNotice={onNotice}
        {...(onGlobalReload === undefined ? {} : { onGlobalReload })}
        onReload={onReload}
        onScopeChange={onScopeChange}
        accountActions={accountActions}
        services={services}
        catalogModels={catalogModels}
        catalogLoading={catalogLoading}
        {...(globalFailures === undefined ? {} : { globalFailures })}
      />
      <PersistentNotice
        notice={notice}
        onDismiss={() => {
          onNotice(null);
        }}
      />
    </>
  );
}

export interface AppProps {
  readonly client?: AdministrationClient;
  readonly startingScope?: ScopeSelection;
  readonly accountActions?: ReactNode;
}

interface GlobalAdministrationData {
  readonly services: readonly ServiceSummary[];
  readonly credentials: readonly Credential[];
  readonly catalogModels: readonly CatalogEntry[];
}

interface GlobalAdministrationState {
  readonly data: GlobalAdministrationData;
  readonly failures: GlobalFailures;
}

export function LocalAdministratorActivation({
  onActivate,
}: {
  readonly onActivate: (secret: string) => Promise<void>;
}) {
  const [secret, setSecret] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setFailure(null);
    try {
      await onActivate(secret);
    } catch {
      setFailure("The local administrator session was not activated.");
    } finally {
      setSecret("");
      setSubmitting(false);
    }
  }
  return (
    <SessionPage aria-label="Local administrator activation">
      <SessionCard
        eyebrow="Localhost only"
        title="Activate administrator session"
        description="Enter the generated local administrator secret. The control clears the value after each attempt."
        actions={
          <form
            className="local-activation-form"
            onSubmit={(event) => void submit(event)}
          >
            <label>
              Local administrator secret
              <input
                name="local-administrator-secret"
                type="password"
                autoComplete="off"
                spellCheck="false"
                value={secret}
                required
                minLength={20}
                onChange={(event) => {
                  setSecret(event.currentTarget.value);
                }}
              />
            </label>
            {failure === null ? null : <p role="alert">{failure}</p>}
            <Button type="submit" disabled={submitting || secret.length < 20}>
              {submitting ? "Activating…" : "Activate local session"}
            </Button>
          </form>
        }
      />
    </SessionPage>
  );
}

function ActivatedAdministrationApp({
  csrfToken,
  authenticationMode,
  identityAccountUrl,
  onSignOut,
  onRecentAuthentication,
  sessionAction = "idle",
}: {
  readonly csrfToken: string;
  readonly authenticationMode: "local" | "oidc";
  readonly identityAccountUrl?: string;
  readonly onSignOut: () => Promise<void>;
  readonly onRecentAuthentication: () => Promise<void>;
  readonly sessionAction?: SessionAction;
}) {
  const client = useMemo(
    () =>
      createFetchAdministrationClient({
        csrfToken,
        onRecentAuthenticationRequired: onRecentAuthentication,
      }),
    [csrfToken, onRecentAuthentication],
  );
  const accountActions = (
    <div
      className="administrator-session-actions"
      aria-label="Administrator account"
    >
      {authenticationMode !== "oidc" ||
      identityAccountUrl === undefined ? null : (
        <AccountMenu
          className="administrator-account"
          compact
          avatar="ID"
          name="Pocket ID account"
          aria-label="Manage Pocket ID account"
          onClick={() => {
            globalThis.location.assign(identityAccountUrl);
          }}
        />
      )}
      <Button
        variant="quiet"
        type="button"
        disabled={sessionAction.endsWith("_pending")}
        icon={<Icon name="logout" size={16} />}
        onClick={() => void onSignOut()}
      >
        {sessionAction === "sign_out_pending" ? "Signing out…" : "Sign out"}
      </Button>
      {sessionAction === "error" ? (
        <span role="alert">The administrator action failed. Try again.</span>
      ) : null}
      {sessionAction === "recent_pending" ? (
        <span role="status">Pocket ID verification is opening…</span>
      ) : null}
    </div>
  );
  return <App client={client} accountActions={accountActions} />;
}

export type LocalSessionGate =
  | { readonly state: "checking" }
  | { readonly state: "required" }
  | { readonly state: "oidc_required" }
  | {
      readonly state: "active";
      readonly csrfToken: string;
      readonly authenticationMode: "local" | "oidc";
      readonly identityAccountUrl?: string;
    }
  | { readonly state: "unavailable" }
  | { readonly state: "failed" };

export function LocalAdministrationGateView({
  session,
  onActivate,
  onSignIn,
  onSignOut,
  onRecentAuthentication,
  sessionAction = "idle",
}: {
  readonly session: LocalSessionGate;
  readonly onActivate: (secret: string) => Promise<void>;
  readonly onSignIn?: () => Promise<void>;
  readonly onSignOut?: () => Promise<void>;
  readonly onRecentAuthentication?: () => Promise<void>;
  readonly sessionAction?: SessionAction;
}) {
  if (session.state === "checking")
    return (
      <SessionPage aria-label="Administrator session">
        <StatePanel kind="loading" title="Checking administrator session">
          The administrator session state is loading.
        </StatePanel>
      </SessionPage>
    );
  if (session.state === "required")
    return <LocalAdministratorActivation onActivate={onActivate} />;
  if (session.state === "oidc_required")
    return (
      <SessionPage aria-label="Administrator sign-in">
        <SessionCard
          eyebrow="OpenDLE Identity"
          title="Administrator sign-in"
          description="Use your Pocket ID passkey to start a bounded Router session."
          actions={
            <Button
              type="button"
              disabled={sessionAction.endsWith("_pending")}
              onClick={() => void onSignIn?.()}
            >
              {sessionAction === "sign_in_pending"
                ? "Opening Pocket ID…"
                : "Sign in with Pocket ID"}
            </Button>
          }
          feedback={
            sessionAction === "error" ? (
              <p role="alert">Pocket ID sign-in did not start. Try again.</p>
            ) : null
          }
        />
      </SessionPage>
    );
  if (session.state === "failed")
    return (
      <SessionPage aria-label="Administrator session">
        <StatePanel kind="error" title="Administrator session is not available">
          The local administrator session is not available.
        </StatePanel>
      </SessionPage>
    );
  if (session.state === "active")
    return (
      <ActivatedAdministrationApp
        csrfToken={session.csrfToken}
        authenticationMode={session.authenticationMode}
        {...(session.identityAccountUrl === undefined
          ? {}
          : { identityAccountUrl: session.identityAccountUrl })}
        onSignOut={() => onSignOut?.() ?? Promise.resolve()}
        onRecentAuthentication={() =>
          onRecentAuthentication?.() ?? Promise.resolve()
        }
        sessionAction={sessionAction}
      />
    );
  return <App />;
}

export function LocalAdministrationApp() {
  const [session, setSession] = useState<LocalSessionGate>({
    state: "checking",
  });
  const [sessionAction, setSessionAction] = useState<SessionAction>("idle");
  useEffect(() => {
    let mounted = true;
    const cancelInspection = scheduleAdministrationSessionInspection(() => {
      void inspectLocalAdministratorSession()
        .then((result) => {
          if (mounted) setSession(result);
        })
        .catch(() => {
          if (mounted) setSession({ state: "failed" });
        });
    });
    return () => {
      mounted = false;
      cancelInspection();
    };
  }, []);
  return (
    <LocalAdministrationGateView
      session={session}
      sessionAction={sessionAction}
      onActivate={async (secret) => {
        const csrfToken = await activateLocalAdministrator(secret);
        setSession({
          state: "active",
          csrfToken,
          authenticationMode: "local",
        });
      }}
      onSignIn={async () => {
        setSessionAction("sign_in_pending");
        try {
          const authorizationUrl = await startPocketIDAdministratorSession(
            initialTrustedGrantToken,
          );
          window.location.assign(authorizationUrl);
        } catch {
          setSessionAction("error");
        }
      }}
      onSignOut={async () => {
        if (session.state !== "active") return;
        setSessionAction("sign_out_pending");
        try {
          await endAdministratorSession(session.csrfToken);
          setSessionAction("idle");
          setSession({
            state:
              session.authenticationMode === "local"
                ? "required"
                : "oidc_required",
          });
        } catch {
          setSessionAction("error");
        }
      }}
      onRecentAuthentication={async () => {
        setSessionAction("recent_pending");
        try {
          const authorizationUrl = await startPocketIDRecentAuthentication();
          window.location.assign(authorizationUrl);
        } catch (error) {
          setSessionAction("error");
          throw error;
        }
      }}
    />
  );
}

export function App({
  client: suppliedClient,
  startingScope,
  accountActions,
}: AppProps = {}) {
  const client = useMemo(
    () => suppliedClient ?? createFetchAdministrationClient(),
    [suppliedClient],
  );
  const [scope, setScope] = useState(startingScope ?? initialScope);
  const [globalState, setGlobalState] = useState<GlobalAdministrationState>({
    data: {
      services: emptyServices,
      credentials: emptyCredentials,
      catalogModels: [],
    },
    failures: emptyGlobalFailures,
  });
  const { services, credentials, catalogModels } = globalState.data;
  const { failures: globalFailures } = globalState;
  const [snapshot, setSnapshot] = useState<AdministrationSnapshot | null>(null);
  const [loading, setLoading] = useState(scope.serviceId !== "");
  const [failure, setFailure] = useReducer(
    (_current: string | null, next: string | null) => next,
    null,
  );
  const [notice, setNotice] = useReducer(
    (_current: Notice | null, next: Notice | null) => next,
    null,
  );
  const [catalogLoading, setCatalogLoading] = useReducer(
    (_current: boolean, next: boolean) => next,
    true,
  );
  const loadGeneration = useRef(0);
  const globalLoadGeneration = useRef(0);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (signal?.aborted) return false;
      const generation = ++loadGeneration.current;
      if (scope.serviceId === "") {
        setSnapshot(null);
        setLoading(false);
        setFailure(null);
        return true;
      }
      setLoading(true);
      setFailure(null);
      setSnapshot(null);
      try {
        const nextSnapshot = await client.load(scope, signal);
        if (generation === loadGeneration.current && !signal?.aborted) {
          setSnapshot(nextSnapshot);
        }
        return true;
      } catch (error) {
        if (
          generation === loadGeneration.current &&
          !(error instanceof DOMException && error.name === "AbortError")
        ) {
          setFailure(errorMessage(error));
        }
        return false;
      } finally {
        if (generation === loadGeneration.current && !signal?.aborted) {
          setLoading(false);
        }
      }
    },
    [client, scope],
  );
  useEffect(() => {
    const controller = new AbortController();
    void Promise.resolve()
      .then(() => load(controller.signal))
      .catch(() => undefined);
    return () => {
      controller.abort();
    };
  }, [load]);
  const reloadGlobalData = useCallback(
    async (signal?: AbortSignal) => {
      if (signal?.aborted) return false;
      const generation = ++globalLoadGeneration.current;
      setCatalogLoading(true);
      const results = await Promise.allSettled([
        client.listServices(signal),
        client.listCredentials(signal),
        client.listCatalog("models", signal),
      ] as const);
      const failures: Partial<
        Record<"services" | "credentials" | "catalog", string>
      > = {};
      if (results[0].status === "rejected") {
        failures.services = errorMessage(results[0].reason);
      }
      if (results[1].status === "rejected") {
        failures.credentials = errorMessage(results[1].reason);
      }
      if (results[2].status === "rejected") {
        failures.catalog = "The supported model catalog is not available.";
      }
      if (generation === globalLoadGeneration.current && !signal?.aborted) {
        setGlobalState((current) => ({
          failures,
          data: {
            services:
              results[0].status === "fulfilled"
                ? results[0].value
                : current.data.services,
            credentials:
              results[1].status === "fulfilled"
                ? results[1].value
                : current.data.credentials,
            catalogModels:
              results[2].status === "fulfilled"
                ? results[2].value
                : current.data.catalogModels,
          },
        }));
        setCatalogLoading(false);
      }
      return (
        generation === globalLoadGeneration.current &&
        !signal?.aborted &&
        Object.keys(failures).length === 0
      );
    },
    [client],
  );
  useEffect(() => {
    const controller = new AbortController();
    void Promise.resolve()
      .then(() => reloadGlobalData(controller.signal))
      .catch(() => undefined);
    return () => {
      controller.abort();
    };
  }, [reloadGlobalData]);
  const reloadAllData = useCallback(async () => {
    const [globalReady, selectedReady] = await Promise.all([
      reloadGlobalData(),
      load(),
    ]);
    if (!globalReady || !selectedReady) {
      throw new Error("Current administrator data did not refresh.");
    }
  }, [load, reloadGlobalData]);
  function applyScope(next: ScopeSelection) {
    loadGeneration.current += 1;
    setNotice(null);
    setSnapshot(null);
    setFailure(null);
    setLoading(next.serviceId !== "");
    setScope(next);
    const suffix = scopeSearch(next);
    globalThis.history.replaceState(
      null,
      "",
      suffix === "" ? "?" : `?${suffix}`,
    );
  }
  return (
    <ShellErrorBoundary
      fallbackClassName="fatal-state"
      fallbackTitle="LLM Router administration is not available"
      fallbackMessage="Reload the page and review current data before you send another change."
      resetKey={scope}
    >
      <AdministrationStateView
        client={client}
        credentials={credentials}
        catalogModels={catalogModels}
        catalogLoading={catalogLoading}
        failure={failure}
        loading={loading}
        notice={notice}
        onNotice={setNotice}
        onGlobalReload={reloadAllData}
        onReload={async () => {
          if (!(await load())) {
            throw new Error("The selected service did not refresh.");
          }
        }}
        scope={scope}
        snapshot={snapshot}
        onScopeChange={applyScope}
        accountActions={accountActions}
        services={services}
        globalFailures={globalFailures}
      />
    </ShellErrorBoundary>
  );
}
