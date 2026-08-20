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
  Button,
  Card,
  Icon,
  PageHeading,
  Panel,
  PanelHeader,
  ShellErrorBoundary,
  StatCard,
  StatusPill,
  Toast,
  type IconName,
} from "@opendle/ui";
import {
  AdministrationApiError,
  activateLocalAdministrator,
  configurationRevisionForScope,
  createFetchAdministrationClient,
  errorMessage,
  inspectLocalAdministratorSession,
  type AccountingSummary,
  type AdministrationClient,
  type AdministrationSnapshot,
  type Assignment,
  type Credential,
  type ProviderInstance,
  type ProviderModelRoute,
  type RequestStatus,
  type ScopeSelection,
} from "./api.js";

type Section = "configuration" | "assignments" | "requests" | "accounting";
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

const sections: readonly {
  readonly id: Section;
  readonly label: string;
  readonly icon: IconName;
}[] = [
  { id: "configuration", label: "Configuration", icon: "settings" },
  { id: "assignments", label: "Assignments", icon: "layers" },
  { id: "requests", label: "Request status", icon: "list" },
  { id: "accounting", label: "Accounting", icon: "activity" },
];

function initialScope(): ScopeSelection {
  const search = "location" in globalThis ? globalThis.location.search : "";
  const query = new URLSearchParams(search);
  return {
    mode: query.get("view") === "service" ? "service" : "global",
    serviceId: query.get("service_id") ?? "",
    workspaceId: query.get("workspace_id") ?? "",
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

function formText(values: FormData, name: string): string {
  const value = values.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function ScopeBanner({
  scope,
  snapshot,
}: {
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot;
}) {
  return (
    <section className="scope-banner" aria-labelledby="scope-heading">
      <span className="scope-banner-icon">
        <Icon name="shield" size={18} />
      </span>
      <div>
        <p id="scope-heading">Exact administration scope</p>
        <strong>
          {scope.mode === "global"
            ? "Global administrator"
            : "Service administrator"}
        </strong>
      </div>
      <dl>
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
          <dt>State revision</dt>
          <dd title={snapshot.state.revision}>
            {revisionLabel(snapshot.state.revision)}
          </dd>
        </div>
      </dl>
    </section>
  );
}

function ScopeForm({
  current,
  onApply,
}: {
  readonly current: ScopeSelection;
  readonly onApply: (scope: ScopeSelection) => void;
}) {
  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    onApply({
      mode: values.get("mode") === "service" ? "service" : "global",
      serviceId: formText(values, "service_id"),
      workspaceId: formText(values, "workspace_id"),
    });
  }
  return (
    <form className="scope-form" onSubmit={submit}>
      <label>
        View
        <select name="mode" defaultValue={current.mode}>
          <option value="global">Global administration</option>
          <option value="service">Service administration</option>
        </select>
      </label>
      <label>
        Service ID
        <input
          required
          name="service_id"
          defaultValue={current.serviceId}
          placeholder="Service UUID"
        />
      </label>
      <label>
        Workspace ID <span>(optional)</span>
        <input
          name="workspace_id"
          defaultValue={current.workspaceId}
          placeholder="Service-level scope"
        />
      </label>
      <Button type="submit" icon={<Icon name="refresh" size={16} />}>
        Load exact scope
      </Button>
    </form>
  );
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
    <Card
      className={`state-message state-message-${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      <Icon
        name={
          kind === "error" ? "warning" : kind === "loading" ? "refresh" : "list"
        }
        size={22}
      />
      <div>
        <h2>
          {kind === "error"
            ? "The scope is not available"
            : kind === "loading"
              ? "Loading protected state"
              : "Select an exact scope"}
        </h2>
        <p>{children}</p>
      </div>
      {onRetry ? (
        <Button variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </Card>
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
  scope,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
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
        ownerScope: scope.serviceId,
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
  writable,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly values: readonly ProviderInstance[];
  readonly writable: boolean;
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
              No provider instance is effective in this scope.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.provider_instance_id}>
                <td>
                  <strong>{item.display_name}</strong>
                  <small>{item.provider_instance_id}</small>
                </td>
                <td>{item.source_layer}</td>
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
                  {!writable || item.inherited || item.state === "retired" ? (
                    <span className="muted-action">Read only</span>
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
  writable,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly values: readonly ProviderModelRoute[];
  readonly writable: boolean;
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
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Model route</th>
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
            <EmptyRow columns={5}>
              No provider-model route is effective in this scope.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.provider_model_route_id}>
                <td>
                  <strong>{item.wire_model}</strong>
                  <small>{item.provider_model_route_id}</small>
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
                  {!writable || item.inherited || item.state === "retired" ? (
                    <span className="muted-action">Read only</span>
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
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  const { client, scope, snapshot, onChanged, onNotice } = props;
  const expectedRevision = configurationRevisionForScope(snapshot, scope);
  const serviceConfigurationWritable = scope.workspaceId === "";
  return (
    <div className="panel-stack">
      {scope.mode === "global" && serviceConfigurationWritable ? (
        <Panel>
          <PanelHeader
            kicker="Write-only secret control"
            title="Provider credentials"
            description="Secret values never return to this application."
          />
          <CredentialForm
            client={client}
            scope={scope}
            onChanged={onChanged}
            onNotice={onNotice}
          />
          <CredentialTable values={snapshot.credentials} />
        </Panel>
      ) : (
        <Panel className="permission-note">
          <Icon name="lock" />
          <div>
            <h2>Secret custody stays global</h2>
            <p>
              This service view can select eligible credential references. It
              cannot read or change secret material.
            </p>
          </div>
        </Panel>
      )}
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
          kicker="Provider access"
          title="OpenRouter instances"
          description="Create one accepted OpenRouter endpoint and inspect its effective source."
        />
        {serviceConfigurationWritable ? (
          <ProviderForm
            client={client}
            scope={scope}
            credentials={snapshot.credentials}
            canBrowseCredentials={scope.mode === "global"}
            expectedRevision={expectedRevision}
            onChanged={onChanged}
            onNotice={onNotice}
          />
        ) : null}
        <ProviderTable
          client={client}
          scope={scope}
          values={snapshot.providers}
          writable={serviceConfigurationWritable}
          onChanged={onChanged}
          onNotice={onNotice}
        />
      </Panel>
      <Panel>
        <PanelHeader
          kicker="Provider model"
          title="OpenRouter routes"
          description="Connect a canonical model to the exact OpenRouter wire model and prices."
        />
        {serviceConfigurationWritable ? (
          <RouteForm
            client={client}
            scope={scope}
            providers={snapshot.providers}
            expectedRevision={expectedRevision}
            onChanged={onChanged}
            onNotice={onNotice}
          />
        ) : null}
        <RouteTable
          client={client}
          scope={scope}
          values={snapshot.routes}
          writable={serviceConfigurationWritable}
          onChanged={onChanged}
          onNotice={onNotice}
        />
      </Panel>
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
  const [orderedRoutes, setOrderedRoutes] = useState("");
  const [timeout, setTimeoutValue] = useState("30000");
  const [submitting, setSubmitting] = useState(false);
  const candidates = orderedRoutes.split("\n").flatMap((value) => {
    const candidate = value.trim();
    return candidate === "" ? [] : [candidate];
  });
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
      <h3>Publish complete fallback chain</h3>
      <p>
        Put one route ID on each line. The first line is the primary route.
        Later lines are ordered fallbacks.
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
      <label className="full-field">
        Ordered route IDs
        <textarea
          required
          rows={Math.max(3, candidates.length + 1)}
          value={orderedRoutes}
          onChange={(event) => {
            setOrderedRoutes(event.target.value);
          }}
          placeholder={routes
            .map((item) => item.provider_model_route_id)
            .join("\n")}
        />
      </label>
      <ol className="fallback-preview" aria-label="Ordered fallback preview">
        {candidates.map((candidate, index) => (
          <li key={candidate}>
            <span>{index + 1}</span>
            <code>{candidate}</code>
            {index === 0 ? (
              <strong>Primary</strong>
            ) : (
              <small>Fallback {index}</small>
            )}
          </li>
        ))}
      </ol>
      <Button type="submit" disabled={submitting || candidates.length === 0}>
        {submitting ? "Publishing…" : "Publish chain"}
      </Button>
    </form>
  );
}

function AssignmentTable({
  client,
  scope,
  values,
  onChanged,
  onNotice,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly values: readonly Assignment[];
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
              No assignment is effective in this scope.
            </EmptyRow>
          ) : (
            values.map((item) => (
              <tr key={item.name}>
                <td>
                  <strong>{item.name}</strong>
                  <small>{item.source_layer}</small>
                </td>
                <td>
                  <ol className="table-chain">
                    {item.candidates.map((candidate, index) => (
                      <li key={candidate.provider_model_route_id}>
                        <span>{index + 1}</span>
                        <div>
                          <code>{candidate.provider_model_route_id}</code>
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
                  {item.inherited || item.state === "retired" ? (
                    <span className="muted-action">Read only</span>
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
  readonly onChanged: () => Promise<void>;
  readonly onNotice: (notice: Notice) => void;
}) {
  return (
    <Panel>
      <PanelHeader
        kicker="Immediate publication"
        title="Assignments and ordered fallbacks"
        description="The nearest scope replaces the complete inherited fallback chain."
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
              No request status is in this bounded scope.
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
        <strong>Stale configuration revision.</strong> Refresh the scope, review
        the active revision, and submit an intentional new change.
      </span>
    </div>
  );
}

export function AdministrationDashboard({
  client,
  scope,
  snapshot,
  initialSection = "configuration",
  notice,
  onNotice,
  onReload,
}: {
  readonly client: AdministrationClient;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot;
  readonly initialSection?: Section;
  readonly notice: Notice | null;
  readonly onNotice: (notice: Notice | null) => void;
  readonly onReload: () => Promise<void>;
}) {
  const [section, setSection] = useReducer(
    (_current: Section, next: Section) => next,
    initialSection,
  );
  const page = sections.find((item) => item.id === section) ?? sections.at(0);
  if (page === undefined) return null;
  const stale = notice?.staleRevision === true;
  return (
    <div className="application-shell">
      <aside className="sidebar">
        <div className="brand">
          <span>
            <Icon name="layers" />
          </span>
          <div>
            <strong>LLM Router</strong>
            <small>Administration</small>
          </div>
        </div>
        <nav aria-label="Administration areas">
          {sections.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-current={section === item.id ? "page" : undefined}
              onClick={() => {
                setSection(item.id);
              }}
            >
              <Icon name={item.icon} size={17} />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-scope">
          <small>Exact scope</small>
          <strong>{scope.serviceId}</strong>
          <span>{scope.workspaceId || "Service level"}</span>
        </div>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <span>
            {scope.mode === "global" ? "Global authority" : "Service authority"}
          </span>
          <Button
            variant="quiet"
            icon={<Icon name="refresh" size={16} />}
            onClick={() => void onReload()}
          >
            Refresh
          </Button>
        </header>
        <main className="content">
          <ScopeBanner scope={scope} snapshot={snapshot} />
          <PageHeading
            eyebrow={page.label}
            title={page.label}
            description="Protected MVP administration with exact scope and bounded operational data."
          />
          {stale ? <StaleRevisionBanner /> : null}
          {section === "configuration" ? (
            <ConfigurationView
              client={client}
              scope={scope}
              snapshot={snapshot}
              onChanged={onReload}
              onNotice={onNotice}
            />
          ) : null}
          {section === "assignments" ? (
            <AssignmentsView
              client={client}
              scope={scope}
              snapshot={snapshot}
              onChanged={onReload}
              onNotice={onNotice}
            />
          ) : null}
          {section === "requests" ? (
            <Panel>
              <PanelHeader
                kicker="Content-free status"
                title="Logical requests"
                description="This view does not contain prompts, model output, or provider secrets."
              />
              <RequestTable values={snapshot.requests} />
            </Panel>
          ) : null}
          {section === "accounting" ? (
            <AccountingView summary={snapshot.accounting} />
          ) : null}
        </main>
      </div>
    </div>
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
  failure,
  loading,
  notice,
  onNotice,
  onReload,
  scope,
  snapshot,
}: {
  readonly client: AdministrationClient;
  readonly failure: string | null;
  readonly loading: boolean;
  readonly notice: Notice | null;
  readonly onNotice: (notice: Notice | null) => void;
  readonly onReload: () => Promise<void>;
  readonly scope: ScopeSelection;
  readonly snapshot: AdministrationSnapshot | null;
}) {
  return (
    <>
      {loading ? (
        <main className="entry-state">
          <StateMessage kind="loading">
            Protected service and workspace state is loading.
          </StateMessage>
        </main>
      ) : null}
      {!loading && failure !== null ? (
        <main className="entry-state">
          <StateMessage kind="error" onRetry={() => void onReload()}>
            {failure}
          </StateMessage>
        </main>
      ) : null}
      {!loading && failure === null && snapshot === null ? (
        <main className="entry-state">
          <StateMessage kind="empty">
            Enter a service ID. Add a workspace ID only when the authority is
            for one exact workspace.
          </StateMessage>
        </main>
      ) : null}
      {!loading && failure === null && snapshot !== null ? (
        <AdministrationDashboard
          client={client}
          scope={scope}
          snapshot={snapshot}
          notice={notice}
          onNotice={onNotice}
          onReload={onReload}
        />
      ) : null}
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
    <main className="local-activation">
      <Card className="local-activation-card">
        <PageHeading
          eyebrow="Localhost only"
          title="Activate administrator session"
          description="Enter the generated local administrator secret. The control clears the value after each attempt."
        />
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
      </Card>
    </main>
  );
}

function ActivatedAdministrationApp({
  csrfToken,
}: {
  readonly csrfToken: string;
}) {
  const client = useMemo(
    () => createFetchAdministrationClient({ csrfToken }),
    [csrfToken],
  );
  return <App client={client} />;
}

export type LocalSessionGate =
  | { readonly state: "checking" }
  | { readonly state: "required" }
  | { readonly state: "active"; readonly csrfToken: string }
  | { readonly state: "unavailable" }
  | { readonly state: "failed" };

export function LocalAdministrationGateView({
  session,
  onActivate,
}: {
  readonly session: LocalSessionGate;
  readonly onActivate: (secret: string) => Promise<void>;
}) {
  if (session.state === "checking")
    return (
      <main className="entry-state">
        <StateMessage kind="loading">
          The administrator session state is loading.
        </StateMessage>
      </main>
    );
  if (session.state === "required")
    return <LocalAdministratorActivation onActivate={onActivate} />;
  if (session.state === "failed")
    return (
      <main className="entry-state">
        <StateMessage kind="error">
          The local administrator session is not available.
        </StateMessage>
      </main>
    );
  if (session.state === "active")
    return <ActivatedAdministrationApp csrfToken={session.csrfToken} />;
  return <App />;
}

export function LocalAdministrationApp() {
  const [session, setSession] = useState<LocalSessionGate>({
    state: "checking",
  });
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
      onActivate={async (secret) => {
        const csrfToken = await activateLocalAdministrator(secret);
        setSession({ state: "active", csrfToken });
      }}
    />
  );
}

export function App({ client: suppliedClient, startingScope }: AppProps = {}) {
  const client = useMemo(
    () => suppliedClient ?? createFetchAdministrationClient(),
    [suppliedClient],
  );
  const [scope, setScope] = useState(startingScope ?? initialScope);
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
  function applyScope(next: ScopeSelection) {
    loadGeneration.current += 1;
    setNotice(null);
    setSnapshot(null);
    setFailure(null);
    setLoading(next.serviceId !== "");
    setScope(next);
    const query = new URLSearchParams();
    query.set("view", next.mode);
    if (next.serviceId !== "") query.set("service_id", next.serviceId);
    if (next.workspaceId !== "") query.set("workspace_id", next.workspaceId);
    globalThis.history.replaceState(null, "", `?${query.toString()}`);
  }
  return (
    <ShellErrorBoundary
      fallbackClassName="fatal-state"
      fallbackTitle="LLM Router administration is not available"
      fallbackMessage="Reload the page. No change was sent."
      resetKey={scope}
    >
      <div className="scope-entry">
        <ScopeForm current={scope} onApply={applyScope} />
      </div>
      <AdministrationStateView
        client={client}
        failure={failure}
        loading={loading}
        notice={notice}
        onNotice={setNotice}
        onReload={async () => load()}
        scope={scope}
        snapshot={snapshot}
      />
    </ShellErrorBoundary>
  );
}
