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
  AdministrationApiError,
  activateLocalAdministrator,
  configurationRevisionForScope,
  consumeTrustedGrantToken,
  createFetchAdministrationClient,
  endAdministratorSession,
  errorMessage,
  inspectLocalAdministratorSession,
  startPocketIDAdministratorSession,
  startPocketIDRecentAuthentication,
  type AccountingSummary,
  type AdministrationClient,
  type AdministrationSnapshot,
  type Assignment,
  type Credential,
  type ProviderInstance,
  type ProviderModelRoute,
  type RequestStatus,
  type ScopeSelection,
  type ServiceCreated,
  type ServiceSummary,
} from "./api.js";
import { ServiceManagement } from "./ServiceManagement.js";

const initialTrustedGrantToken =
  typeof window === "undefined" ? undefined : consumeTrustedGrantToken();

type Section =
  | "overview"
  | "services"
  | "credentials"
  | "setup"
  | "configuration"
  | "assignments"
  | "requests"
  | "accounting";
type SessionAction =
  "idle" | "sign_in_pending" | "sign_out_pending" | "recent_pending" | "error";
export interface Notice {
  readonly tone: "success" | "error";
  readonly message: string;
  readonly staleRevision?: boolean;
}

function errorNotice(error: unknown): Notice {
  return {
    tone: "error",
    message: errorMessage(error),
    staleRevision:
      error instanceof AdministrationApiError && error.staleRevision,
  };
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
];

const serviceSections: readonly SectionItem[] = [
  { id: "configuration", label: "Effective configuration", icon: "settings" },
  { id: "setup", label: "Setup", icon: "health" },
  { id: "assignments", label: "Assignments", icon: "layers" },
  { id: "requests", label: "Requests", icon: "list" },
  { id: "accounting", label: "Usage & cost", icon: "activity" },
];

const sections = [...globalSections, ...serviceSections];
const emptyServices: readonly ServiceSummary[] = [];
const emptyCredentials: readonly Credential[] = [];

function initialScope(): ScopeSelection {
  const search = "location" in globalThis ? globalThis.location.search : "";
  const query = new URLSearchParams(search);
  return {
    mode: "global",
    serviceId: query.get("service_id") ?? "",
    workspaceId: "",
  };
}

function toneForState(state: string): "green" | "amber" | "red" | "blue" {
  if (state === "active" || state === "succeeded" || state === "current") {
    return "green";
  }
  if (state === "disabled" || state === "running" || state === "distributing") {
    return "amber";
  }
  if (state === "failed" || state === "cancelled" || state === "retired") {
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
      onNotice({
        tone: "success",
        message: "The write-only OpenRouter credential was stored.",
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
  values,
}: {
  readonly values: readonly Credential[];
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
          </tr>
        </thead>
        <tbody>
          {values.length === 0 ? (
            <EmptyRow columns={4}>
              No credential metadata is in this authority.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.credential_id}>
                <td>
                  <strong>{item.fingerprint}</strong>
                  <small>{item.credential_id}</small>
                </td>
                <td>{item.owner_scope}</td>
                <td>
                  <StatusPill tone={toneForState(item.state)}>
                    {item.state}
                  </StatusPill>
                </td>
                <td>
                  <Revision value={item.revision} />
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
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
      onNotice({
        tone: "success",
        message: `Provider instance published at ${revisionLabel(result.active_revision)} (${result.distribution_state}).`,
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
          eligible_service_ids: [],
        },
      );
      onNotice({
        tone: "success",
        message: `Provider instance ${nextState}. Active revision ${revisionLabel(result.active_revision)}.`,
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
        eligible_service_ids: [],
      });
      onNotice({
        tone: "success",
        message:
          "The inherited provider connection was copied to this service.",
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
  wireModel: "deepseek/deepseek-v4-flash",
  inputPrice: "",
  outputPrice: "",
};

const canonicalUuidPattern =
  "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const canonicalUuid = new RegExp(`^${canonicalUuidPattern}$`);
const nonNegativeDecimal = /^(0|[1-9][0-9]*)(\.[0-9]+)?$/;

function RouteForm({
  client,
  scope,
  providers,
  expectedRevision,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly providers: readonly ProviderInstance[];
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
    canonicalUuid.test(form.canonicalModelId) &&
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
      onNotice({
        tone: "success",
        message: `Model route published at ${revisionLabel(result.active_revision)} (${result.distribution_state}).`,
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
  return (
    <form
      className="configuration-form"
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <h3>Add provider-model route</h3>
      <p>
        DeepSeek V4 Flash is the default live-test route. Prices are USD per one
        million tokens.
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
          Canonical model UUID
          <input
            required
            pattern={canonicalUuidPattern}
            placeholder="Canonical model UUID"
            value={form.canonicalModelId}
            onChange={(event) => {
              updateForm({ canonicalModelId: event.target.value });
            }}
          />
        </label>
        <label>
          OpenRouter model
          <input
            required
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
          eligible_service_ids: [],
        },
      );
      onNotice({
        tone: "success",
        message: `Provider-model route ${nextState}. Active revision ${revisionLabel(result.active_revision)}.`,
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
        eligible_service_ids: [],
      });
      onNotice({
        tone: "success",
        message: "The inherited model route was copied to this service.",
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const { client, scope, snapshot, onChanged, onNotice } = props;
  const expectedRevision = configurationRevisionForScope(snapshot, scope);
  const serviceConfigurationWritable = scope.workspaceId === "";
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
      </Panel>
      <Panel>
        <PanelHeader
          kicker="Effective result"
          title="Model routes"
          description="These are the models and provider routes that assignments can use."
        />
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
      </Panel>
      {serviceConfigurationWritable ? (
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
      {serviceConfigurationWritable ? (
        <Panel>
          <PanelHeader
            kicker="Set on this service"
            title="Add a model route"
            description="Connect a model name to one provider connection."
          />
          <RouteForm
            client={client}
            scope={scope}
            providers={snapshot.providers}
            expectedRevision={expectedRevision}
            onChanged={onChanged}
            onNotice={onNotice}
          />
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
      onNotice({
        tone: "success",
        message: `Assignment published at ${revisionLabel(result.active_revision)} (${result.distribution_state}).`,
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
          <li key={candidate}>
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
  expectedRevision,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly values: readonly Assignment[];
  readonly routes: readonly ProviderModelRoute[];
  readonly services: readonly ServiceSummary[];
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
      onNotice({
        tone: "success",
        message: `Assignment ${nextState}. Active revision ${revisionLabel(result.active_revision)}.`,
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
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
      onNotice({
        tone: "success",
        message:
          "The complete inherited fallback chain was copied to this service.",
      });
      await onChanged();
    } catch (error) {
      onNotice(errorNotice(error));
    }
  }
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Assignment</th>
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
                  {item.state === "retired" ? (
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
      <AssignmentTable
        client={props.client}
        scope={props.scope}
        values={props.snapshot.assignments}
        routes={props.snapshot.routes}
        services={props.services}
        expectedRevision={configurationRevisionForScope(
          props.snapshot,
          props.scope,
        )}
        onChanged={props.onChanged}
        onNotice={props.onNotice}
      />
    </Panel>
  );
}

function RequestTable({
  values,
}: {
  readonly values: readonly RequestStatus[];
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Logical request</th>
            <th>Workspace</th>
            <th>Assignment</th>
            <th>State</th>
            <th>Revision</th>
            <th>Safe diagnostic</th>
          </tr>
        </thead>
        <tbody>
          {values.length === 0 ? (
            <EmptyRow columns={6}>
              No request status is available for this service.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.request_id}>
                <td>
                  <strong>{item.request_id}</strong>
                </td>
                <td>{item.workspace_id ?? "Service level"}</td>
                <td>{item.assignment ?? "Not reported"}</td>
                <td>
                  <StatusPill tone={toneForState(item.state)}>
                    {item.state}
                  </StatusPill>
                </td>
                <td>{item.state_revision ?? "—"}</td>
                <td>
                  {item.error?.message ??
                    item.error?.code ??
                    "No safe diagnostic"}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
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
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly credentials: readonly Credential[];
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
        <CredentialForm
          client={client}
          ownerScope="global"
          onChanged={onChanged}
          onNotice={onNotice}
        />
        <CredentialTable values={credentials} />
      </Panel>
    </div>
  );
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
}: {
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
}) {
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
              openSection(item.id);
            }}
          />
        ))}
      </ApplicationNavigationGroup>
      <ApplicationNavigationGroup
        className="selected-service-navigation"
        label={
          selectedService === undefined
            ? "Selected service"
            : selectedService.display_name
        }
      >
        {serviceSections.map((item) => (
          <NavigationItem
            key={item.id}
            active={section === item.id}
            disabled={selectedService === undefined}
            icon={<Icon name={item.icon} size={18} />}
            label={item.label}
            onClick={() => {
              openSection(item.id);
            }}
          />
        ))}
      </ApplicationNavigationGroup>
    </ApplicationNavigation>
  );

  const sidebar = (
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
          onSelect={(serviceId) => {
            selectService(serviceId, "configuration");
          }}
        />
      }
      navigation={navigation}
      footer={<AdministratorSidebarFooter />}
    />
  );

  return (
    <ApplicationShell
      className="router-application-shell"
      sidebar={sidebar}
      mobileNavigation={
        <AdministratorMobileNavigation
          section={section}
          selectedService={selectedService}
          onOpen={openSection}
        />
      }
      mainProps={{ className: "content" }}
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
                  onClick={() => void onReload()}
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
        <GlobalOverview
          services={services}
          onOpenServices={() => {
            openSection("services");
          }}
          onSelectService={(serviceId) => {
            selectService(serviceId, "configuration");
          }}
        />
      ) : null}
      {section === "services" ? (
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
      {section === "credentials" ? (
        <GlobalCredentialsView
          client={client}
          credentials={credentials}
          onChanged={onGlobalReload ?? onReload}
          onNotice={(nextNotice) => {
            onNotice(nextNotice);
          }}
        />
      ) : null}
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
      {section === "requests" && snapshot !== null ? (
        <Panel>
          <PanelHeader
            kicker="Content-free status"
            title="Logical requests"
            description="This view does not contain prompts, model output, or provider secrets."
          />
          <RequestTable values={snapshot.requests} />
        </Panel>
      ) : null}
      {section === "accounting" && snapshot !== null ? (
        <AccountingView summary={snapshot.accounting} />
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
  const accountActions =
    authenticationMode === "oidc" ? (
      <div
        className="administrator-session-actions"
        aria-label="Administrator account"
      >
        {identityAccountUrl === undefined ? null : (
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
          <span role="alert">The Pocket ID action failed. Try again.</span>
        ) : null}
        {sessionAction === "recent_pending" ? (
          <span role="status">Pocket ID verification is opening…</span>
        ) : null}
      </div>
    ) : undefined;
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
    void inspectLocalAdministratorSession()
      .then((result) => {
        if (mounted) setSession(result);
      })
      .catch(() => {
        if (mounted) setSession({ state: "failed" });
      });
    return () => {
      mounted = false;
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
          setSession({ state: "oidc_required" });
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
  const [globalData, setGlobalData] = useReducer(
    (_current: GlobalAdministrationData, next: GlobalAdministrationData) =>
      next,
    { services: emptyServices, credentials: emptyCredentials },
  );
  const { services, credentials } = globalData;
  const [snapshot, setSnapshot] = useState<AdministrationSnapshot | null>(null);
  const [loading, setLoading] = useState(scope.serviceId !== "");
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useReducer(
    (_current: Notice | null, next: Notice | null) => next,
    null,
  );
  const loadGeneration = useRef(0);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (signal?.aborted) return;
      const generation = ++loadGeneration.current;
      if (scope.serviceId === "") {
        setSnapshot(null);
        setLoading(false);
        setFailure(null);
        return;
      }
      setLoading(true);
      setFailure(null);
      try {
        const nextSnapshot = await client.load(scope, signal);
        if (generation === loadGeneration.current && !signal?.aborted) {
          setSnapshot(nextSnapshot);
        }
      } catch (error) {
        if (
          generation === loadGeneration.current &&
          !(error instanceof DOMException && error.name === "AbortError")
        ) {
          setFailure(errorMessage(error));
        }
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
    void Promise.resolve().then(() => load(controller.signal));
    return () => {
      controller.abort();
    };
  }, [load]);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      client.listServices(controller.signal),
      client.listCredentials(controller.signal),
    ])
      .then(([listedServices, listedCredentials]) => {
        if (controller.signal.aborted) return;
        setGlobalData({
          services: listedServices,
          credentials: listedCredentials,
        });
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError")
          return;
        setNotice(errorNotice(error));
      });
    return () => {
      controller.abort();
    };
  }, [client]);
  const reloadGlobalData = useCallback(async () => {
    const [listedServices, listedCredentials] = await Promise.all([
      client.listServices(),
      client.listCredentials(),
    ]);
    setGlobalData({
      services: listedServices,
      credentials: listedCredentials,
    });
  }, [client]);
  function applyScope(next: ScopeSelection) {
    loadGeneration.current += 1;
    setNotice(null);
    setSnapshot(null);
    setFailure(null);
    setLoading(next.serviceId !== "");
    setScope(next);
    const query = new URLSearchParams();
    if (next.serviceId !== "") query.set("service_id", next.serviceId);
    const suffix = query.toString();
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
      fallbackMessage="Reload the page. No change was sent."
      resetKey={scope}
    >
      <AdministrationStateView
        client={client}
        credentials={credentials}
        failure={failure}
        loading={loading}
        notice={notice}
        onNotice={setNotice}
        onGlobalReload={reloadGlobalData}
        onReload={async () => load()}
        scope={scope}
        snapshot={snapshot}
        onScopeChange={applyScope}
        accountActions={accountActions}
        services={services}
      />
    </ShellErrorBoundary>
  );
}
