import { useMemo, useReducer, type SubmitEvent } from "react";
import {
  Button,
  Icon,
  PageHeading,
  Panel,
  PanelHeader,
  StatCard,
  StatusPill,
} from "@opendle/ui";
import {
  AdministrationApiError,
  errorMessage,
  type AdministrationClient,
  type ServiceCreated,
  type ServiceSummary,
} from "./api.js";

const modelAccess = {
  audiences: ["data_plane"],
  operations: ["model.create", "model.read", "model.cancel"],
  workspace_limit: "explicit_only",
} as const;

function formText(values: FormData, name: string): string {
  const value = values.get(name);
  return typeof value === "string" ? value.trim() : "";
}

interface ViewState {
  readonly createOpen: boolean;
  readonly busy: boolean;
  readonly retireServiceId: string | null;
}

type ViewAction =
  | { readonly type: "toggle_create" }
  | { readonly type: "busy"; readonly value: boolean }
  | { readonly type: "confirm_retire"; readonly value: string | null };

function viewReducer(state: ViewState, action: ViewAction): ViewState {
  if (action.type === "toggle_create") {
    return { ...state, createOpen: !state.createOpen };
  }
  if (action.type === "busy") return { ...state, busy: action.value };
  return { ...state, retireServiceId: action.value };
}

function displayState(state: ServiceSummary["state"]): string {
  if (state === "active") return "Active";
  if (state === "disabled") return "Disabled";
  return "Retired";
}

function stateTone(state: ServiceSummary["state"]): "green" | "amber" | "red" {
  if (state === "active") return "green";
  if (state === "disabled") return "amber";
  return "red";
}

function parentOf(
  services: readonly ServiceSummary[],
  service: ServiceSummary,
): ServiceSummary | undefined {
  return services.find(
    (candidate) => candidate.service_id === service.parent_service_id,
  );
}

function ancestorNames(
  services: readonly ServiceSummary[],
  service: ServiceSummary,
): readonly string[] {
  const names: string[] = [];
  const visited = new Set([service.service_id]);
  let current = parentOf(services, service);
  while (current !== undefined && !visited.has(current.service_id)) {
    names.unshift(current.display_name);
    visited.add(current.service_id);
    current = parentOf(services, current);
  }
  return names;
}

function descendantIds(
  services: readonly ServiceSummary[],
  serviceId: string,
): ReadonlySet<string> {
  const descendants = new Set<string>();
  const pending = [serviceId];
  while (pending.length > 0) {
    const parentId = pending.pop();
    for (const service of services) {
      if (
        service.parent_service_id === parentId &&
        !descendants.has(service.service_id)
      ) {
        descendants.add(service.service_id);
        pending.push(service.service_id);
      }
    }
  }
  return descendants;
}

function countServices(services: readonly ServiceSummary[]): {
  readonly active: number;
  readonly roots: number;
} {
  let active = 0;
  let roots = 0;
  for (const service of services) {
    if (service.state === "active") active += 1;
    if (service.parent_service_id == null) roots += 1;
  }
  return { active, roots };
}

function ServiceTree({
  services,
  selectedServiceId,
  onSelect,
}: {
  readonly services: readonly ServiceSummary[];
  readonly selectedServiceId: string;
  readonly onSelect: (serviceId: string) => void;
}) {
  const knownIds = new Set(services.map((service) => service.service_id));
  const roots = services.filter(
    (service) =>
      service.parent_service_id == null ||
      !knownIds.has(service.parent_service_id),
  );

  function branch(service: ServiceSummary, path: ReadonlySet<string>) {
    const nextPath = new Set(path).add(service.service_id);
    const children = services.filter(
      (candidate) =>
        candidate.parent_service_id === service.service_id &&
        !nextPath.has(candidate.service_id),
    );
    return (
      <li key={service.service_id}>
        <button
          type="button"
          aria-current={
            selectedServiceId === service.service_id ? "true" : undefined
          }
          data-selected={selectedServiceId === service.service_id}
          onClick={() => {
            onSelect(service.service_id);
          }}
        >
          <span className="service-tree-icon">
            <Icon name="server" size={16} />
          </span>
          <span>
            <strong>{service.display_name}</strong>
            <small>
              {children.length === 0
                ? "No child services"
                : `${String(children.length)} child services`}
            </small>
          </span>
          <StatusPill tone={stateTone(service.state)}>
            {displayState(service.state)}
          </StatusPill>
        </button>
        {children.length === 0 ? null : (
          <ul>{children.map((child) => branch(child, nextPath))}</ul>
        )}
      </li>
    );
  }

  if (services.length === 0) {
    return (
      <div className="service-registry-empty">
        <Icon name="server" size={24} />
        <strong>No services yet</strong>
        <p>Create the first service to start the Router hierarchy.</p>
      </div>
    );
  }
  const visibleRoots = roots.length === 0 ? services : roots;
  return (
    <ul className="service-tree">
      {visibleRoots.map((root) => branch(root, new Set()))}
    </ul>
  );
}

export interface ServiceManagementProps {
  readonly client: AdministrationClient;
  readonly services: readonly ServiceSummary[];
  readonly selectedServiceId: string;
  readonly onSelect: (serviceId: string) => void;
  readonly onChanged: () => Promise<void>;
  readonly onContinueSetup: () => void;
  readonly pendingBootstrap: ServiceCreated | null;
  readonly onBootstrapPending: (created: ServiceCreated | null) => void;
  readonly onSuccess: (message: string) => void;
  readonly onError: (message: string) => void;
}

function CreateServicePanel({
  busy,
  parents,
  onCancel,
  onCreate,
}: {
  readonly busy: boolean;
  readonly parents: readonly ServiceSummary[];
  readonly onCancel: () => void;
  readonly onCreate: (event: SubmitEvent<HTMLFormElement>) => void;
}) {
  return (
    <Panel>
      <PanelHeader
        kicker="New service"
        title="Create a Router service"
        description="Give the service a clear name. Choose a parent only when it should inherit eligible routing configuration from another service."
      />
      <form className="service-create-form" onSubmit={onCreate}>
        <label>
          Service name
          <input
            required
            name="display_name"
            maxLength={200}
            placeholder="For example, Xbot production"
          />
        </label>
        <label>
          Inherit from
          <select name="parent_service_id" defaultValue="">
            <option value="">No parent · start a new service chain</option>
            {parents.map((service) => (
              <option key={service.service_id} value={service.service_id}>
                {service.display_name}
              </option>
            ))}
          </select>
        </label>
        <div className="service-access-summary">
          <Icon name="shield" size={18} />
          <div>
            <strong>Machine access: model calls only</strong>
            <p>
              The first key can create, read, and cancel model requests. It
              cannot run agents, use tools, or manage Router configuration.
            </p>
          </div>
        </div>
        <div className="service-form-actions">
          <Button type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create service"}
          </Button>
          <Button type="button" variant="quiet" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </form>
    </Panel>
  );
}

function BootstrapSecretPanel({
  created,
  onContinue,
  onError,
  onSuccess,
}: {
  readonly created: ServiceCreated;
  readonly onContinue: () => void;
  readonly onError: (message: string) => void;
  readonly onSuccess: (message: string) => void;
}) {
  const secret = created.bootstrap_secret;
  if (secret === undefined) return null;
  return (
    <Panel className="bootstrap-secret-panel">
      <PanelHeader
        kicker="One-time service key"
        title="Store this key now"
        description="Router will not show this value again. Put it in the service's secret store before you continue."
      />
      <div className="bootstrap-secret-value">
        <code>{secret}</code>
        <Button
          variant="secondary"
          onClick={() => {
            void navigator.clipboard
              .writeText(secret)
              .then(() => {
                onSuccess("The one-time key was copied.");
              })
              .catch(() => {
                onError("The key was not copied. Select and copy it manually.");
              });
          }}
        >
          Copy key
        </Button>
      </div>
      <div className="service-form-actions">
        <Button onClick={onContinue}>I stored it · open setup</Button>
      </div>
    </Panel>
  );
}

function ServiceRegistryHeader({
  activeCount,
  rootCount,
  serviceCount,
  onCreate,
}: {
  readonly activeCount: number;
  readonly rootCount: number;
  readonly serviceCount: number;
  readonly onCreate: () => void;
}) {
  return (
    <>
      <PageHeading
        eyebrow="Global administration"
        title="Services and inheritance"
        description="Create services, organize their parent chain, and choose which service you want to configure."
        actions={
          <Button icon={<Icon name="plus" size={16} />} onClick={onCreate}>
            Create service
          </Button>
        }
      />
      <section
        className="service-registry-stats"
        aria-label="Service registry summary"
      >
        <StatCard
          icon={<Icon name="server" />}
          label="Services"
          value={serviceCount}
          note="Retained service identities"
          tone="blue"
        />
        <StatCard
          icon={<Icon name="health" />}
          label="Active"
          value={activeCount}
          note="Can accept new work"
          tone="lime"
        />
        <StatCard
          icon={<Icon name="layers" />}
          label="Root services"
          value={rootCount}
          note="Start an inheritance chain"
          tone="purple"
        />
      </section>
    </>
  );
}

function ServiceDetailsPanel({
  ancestors,
  availableParents,
  busy,
  childCount,
  parent,
  retireServiceId,
  selected,
  onChangeState,
  onContinueSetup,
  onRetirePrompt,
  onUpdate,
}: {
  readonly ancestors: readonly string[];
  readonly availableParents: readonly ServiceSummary[];
  readonly busy: boolean;
  readonly childCount: number;
  readonly parent: ServiceSummary | undefined;
  readonly retireServiceId: string | null;
  readonly selected: ServiceSummary | undefined;
  readonly onChangeState: (action: "disable" | "restore" | "retire") => void;
  readonly onContinueSetup: () => void;
  readonly onRetirePrompt: (serviceId: string | null) => void;
  readonly onUpdate: (event: SubmitEvent<HTMLFormElement>) => void;
}) {
  if (selected === undefined) {
    return (
      <Panel className="service-details-panel">
        <div className="service-registry-empty">
          <Icon name="workspace" size={24} />
          <strong>Select a service</strong>
          <p>
            Choose a service in the tree to view its parent and management
            actions.
          </p>
        </div>
      </Panel>
    );
  }
  const confirmRetire = retireServiceId === selected.service_id;
  return (
    <Panel className="service-details-panel">
      <PanelHeader
        kicker="Selected service"
        title={selected.display_name}
        description={
          parent === undefined
            ? "This service starts its own inheritance chain."
            : `This service inherits eligible configuration from ${parent.display_name} and its parents.`
        }
        actions={
          <StatusPill tone={stateTone(selected.state)}>
            {displayState(selected.state)}
          </StatusPill>
        }
      />
      <div className="service-inheritance-summary">
        <Icon name="layers" size={18} />
        <div>
          <strong>Configuration path</strong>
          <p>{[...ancestors, selected.display_name].join(" → ")}</p>
          <small>
            Local items override the effective inherited result. Parent items
            are not changed.
          </small>
        </div>
      </div>
      <form className="service-details-form" onSubmit={onUpdate}>
        <label>
          Service name
          <input
            required
            name="display_name"
            maxLength={200}
            defaultValue={selected.display_name}
          />
        </label>
        <label>
          Parent service
          <select
            name="parent_service_id"
            defaultValue={selected.parent_service_id ?? ""}
          >
            <option value="">No parent</option>
            {availableParents.map((service) => (
              <option key={service.service_id} value={service.service_id}>
                {service.display_name} · {displayState(service.state)}
              </option>
            ))}
          </select>
        </label>
        <p className="service-parent-effect">
          {childCount === 0
            ? "No descendant services depend on this service."
            : `${String(childCount)} descendant services can be affected by parent and lifecycle changes.`}
        </p>
        <div className="service-form-actions">
          <Button type="submit" disabled={busy || selected.state === "retired"}>
            Save name and parent
          </Button>
          <Button type="button" variant="secondary" onClick={onContinueSetup}>
            Configure this service
          </Button>
        </div>
      </form>
      <details className="service-danger-zone">
        <summary>Service lifecycle actions</summary>
        <p>
          Disable stops new work for this service and its children. Retire is
          permanent.
        </p>
        <div className="service-form-actions">
          {selected.state === "disabled" ? (
            <Button
              type="button"
              variant="secondary"
              disabled={busy}
              onClick={() => {
                onChangeState("restore");
              }}
            >
              Restore service
            </Button>
          ) : selected.state === "active" ? (
            <Button
              type="button"
              variant="secondary"
              disabled={busy}
              onClick={() => {
                onChangeState("disable");
              }}
            >
              Disable service
            </Button>
          ) : null}
          {selected.state === "retired" ? null : confirmRetire ? (
            <>
              <Button
                type="button"
                variant="quiet"
                disabled={busy}
                onClick={() => {
                  onChangeState("retire");
                }}
              >
                Confirm permanent retirement
              </Button>
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  onRetirePrompt(null);
                }}
              >
                Cancel
              </Button>
            </>
          ) : (
            <Button
              type="button"
              variant="quiet"
              disabled={busy}
              onClick={() => {
                onRetirePrompt(selected.service_id);
              }}
            >
              Retire permanently
            </Button>
          )}
        </div>
      </details>
      <details className="service-technical-details">
        <summary>Technical details</summary>
        <dl>
          <div>
            <dt>Service ID</dt>
            <dd>{selected.service_id}</dd>
          </div>
          <div>
            <dt>Revision</dt>
            <dd>{selected.revision}</dd>
          </div>
          <div>
            <dt>Key generation</dt>
            <dd>{selected.credential_generation ?? "Not available"}</dd>
          </div>
        </dl>
      </details>
    </Panel>
  );
}

export function ServiceManagement({
  client,
  services,
  selectedServiceId,
  onSelect,
  onChanged,
  onContinueSetup,
  pendingBootstrap,
  onBootstrapPending,
  onSuccess,
  onError,
}: ServiceManagementProps) {
  const [view, dispatch] = useReducer(viewReducer, {
    createOpen: services.length === 0,
    busy: false,
    retireServiceId: null,
  });
  const selected = useMemo(
    () => services.find((service) => service.service_id === selectedServiceId),
    [selectedServiceId, services],
  );
  const serviceCounts = countServices(services);

  async function create(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    dispatch({ type: "busy", value: true });
    const values = new FormData(event.currentTarget);
    try {
      const created = await client.createService({
        displayName: formText(values, "display_name"),
        parentServiceId: formText(values, "parent_service_id") || null,
        bootstrapScope: modelAccess,
      });
      onSelect(created.service_id);
      onBootstrapPending(created);
      await onChanged();
      onSuccess("The service was created. Store its one-time access key.");
    } catch (error) {
      if (error instanceof AdministrationApiError && error.staleRevision) {
        await onChanged();
      }
      onError(errorMessage(error));
    } finally {
      dispatch({ type: "busy", value: false });
    }
  }

  async function updateDetails(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (selected === undefined) return;
    dispatch({ type: "busy", value: true });
    const values = new FormData(event.currentTarget);
    try {
      await client.putService(selected.service_id, {
        expectedRevision: selected.revision,
        displayName: formText(values, "display_name"),
        newParentServiceId: formText(values, "parent_service_id") || null,
        reason: "Update the service name and parent",
      });
      await onChanged();
      onSuccess("The service name and parent were updated.");
    } catch (error) {
      if (error instanceof AdministrationApiError && error.staleRevision) {
        await onChanged();
      }
      onError(errorMessage(error));
    } finally {
      dispatch({ type: "busy", value: false });
    }
  }

  async function changeState(action: "disable" | "restore" | "retire") {
    if (selected === undefined) return;
    dispatch({ type: "busy", value: true });
    try {
      await client.changeService(selected.service_id, action, {
        expectedRevision: selected.revision,
        reason: `${action[0]?.toUpperCase() ?? ""}${action.slice(1)} the service from global administration`,
      });
      await onChanged();
      onSuccess(
        `The service is now ${action === "restore" ? "active" : action === "disable" ? "disabled" : "retired"}.`,
      );
    } catch (error) {
      if (error instanceof AdministrationApiError && error.staleRevision) {
        await onChanged();
      }
      onError(errorMessage(error));
    } finally {
      dispatch({ type: "busy", value: false });
    }
  }

  const parent =
    selected === undefined ? undefined : parentOf(services, selected);
  const ancestors =
    selected === undefined ? [] : ancestorNames(services, selected);
  const descendants =
    selected === undefined
      ? new Set<string>()
      : descendantIds(services, selected.service_id);
  const creationParents: ServiceSummary[] = [];
  const availableParents: ServiceSummary[] = [];
  for (const service of services) {
    if (service.state === "active") creationParents.push(service);
    if (
      selected !== undefined &&
      service.service_id !== selected.service_id &&
      !descendants.has(service.service_id) &&
      (service.state === "active" ||
        service.service_id === selected.parent_service_id)
    ) {
      availableParents.push(service);
    }
  }

  return (
    <div className="service-management">
      <ServiceRegistryHeader
        activeCount={serviceCounts.active}
        rootCount={serviceCounts.roots}
        serviceCount={services.length}
        onCreate={() => {
          dispatch({ type: "toggle_create" });
        }}
      />

      {view.createOpen ? (
        <CreateServicePanel
          busy={view.busy}
          parents={creationParents}
          onCancel={() => {
            dispatch({ type: "toggle_create" });
          }}
          onCreate={(event) => {
            void create(event);
          }}
        />
      ) : null}

      {pendingBootstrap === null ? null : (
        <BootstrapSecretPanel
          created={pendingBootstrap}
          onError={onError}
          onSuccess={onSuccess}
          onContinue={() => {
            onBootstrapPending(null);
            onContinueSetup();
          }}
        />
      )}

      <div className="service-registry-layout">
        <Panel className="service-tree-panel">
          <PanelHeader
            kicker="Parent chain"
            title="Service hierarchy"
            description="A child inherits eligible configuration from the services above it."
          />
          <ServiceTree
            services={services}
            selectedServiceId={selected?.service_id ?? ""}
            onSelect={onSelect}
          />
        </Panel>

        <ServiceDetailsPanel
          ancestors={ancestors}
          availableParents={availableParents}
          busy={view.busy}
          childCount={descendants.size}
          parent={parent}
          retireServiceId={view.retireServiceId}
          selected={selected}
          onChangeState={(action) => {
            void changeState(action);
          }}
          onContinueSetup={onContinueSetup}
          onRetirePrompt={(serviceId) => {
            dispatch({ type: "confirm_retire", value: serviceId });
          }}
          onUpdate={(event) => {
            void updateDetails(event);
          }}
        />
      </div>
    </div>
  );
}
