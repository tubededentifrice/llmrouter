import { useMemo, useReducer, useState, type SubmitEvent } from "react";
import {
  Button,
  GraphEdge,
  GraphEdges,
  GraphInspector,
  GraphNode,
  GraphToolbar,
  GraphViewport,
  GraphWorkspace,
  Icon,
  Panel,
  PanelHeader,
  StatusPill,
  layoutTree,
  treeEdgePath,
  type TreeLayoutItem,
  type TreeLayoutResult,
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
  readonly createParentId: string | null | undefined;
  readonly busy: boolean;
  readonly retireServiceId: string | null;
}

type ViewAction =
  | {
      readonly type: "open_create";
      readonly parentServiceId: string | null;
    }
  | { readonly type: "close_create" }
  | { readonly type: "busy"; readonly value: boolean }
  | { readonly type: "confirm_retire"; readonly value: string | null };

function viewReducer(state: ViewState, action: ViewAction): ViewState {
  if (action.type === "open_create")
    return { ...state, createParentId: action.parentServiceId };
  if (action.type === "close_create")
    return { ...state, createParentId: undefined };
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

function treeItems(
  services: readonly ServiceSummary[],
): readonly TreeLayoutItem[] {
  const parentById = new Map(
    services.map((service) => [
      service.service_id,
      service.parent_service_id ?? null,
    ]),
  );
  const safeParents = new Map(parentById);

  for (const service of services) {
    const path = new Set<string>();
    let currentId: string | null = service.service_id;
    while (currentId !== null && parentById.has(currentId)) {
      if (path.has(currentId)) {
        safeParents.set(currentId, null);
        break;
      }
      path.add(currentId);
      currentId = parentById.get(currentId) ?? null;
    }
  }

  return services.map((service) => ({
    id: service.service_id,
    parentId: safeParents.get(service.service_id) ?? null,
  }));
}

function childCount(services: readonly ServiceSummary[], serviceId: string) {
  return services.filter((service) => service.parent_service_id === serviceId)
    .length;
}

function nodeTone(state: ServiceSummary["state"]): "lime" | "amber" | "coral" {
  if (state === "active") return "lime";
  if (state === "disabled") return "amber";
  return "coral";
}

function ServiceGraphCanvas({
  layout,
  services,
  selectedServiceId,
  onCreateRoot,
  onReparent,
  onSelect,
}: {
  readonly layout: TreeLayoutResult;
  readonly services: readonly ServiceSummary[];
  readonly selectedServiceId: string;
  readonly onCreateRoot: () => void;
  readonly onReparent: (serviceId: string, parentServiceId: string) => void;
  readonly onSelect: (serviceId: string) => void;
}) {
  const serviceById = new Map(
    services.map((service) => [service.service_id, service]),
  );
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);
  const canvasWidth = Math.max(layout.width, 760);
  const canvasHeight = Math.max(layout.height, 520);

  function canDropOn(parentServiceId: string) {
    if (draggingId === null || draggingId === parentServiceId) return false;
    const parent = serviceById.get(parentServiceId);
    return (
      parent?.state === "active" &&
      !descendantIds(services, draggingId).has(parentServiceId)
    );
  }
  return (
    <GraphViewport
      aria-label="Service tree canvas"
      canvasWidth={canvasWidth}
      canvasHeight={canvasHeight}
      canvasProps={{
        "aria-label": `${String(services.length)} ${services.length === 1 ? "service" : "services"} in the inheritance tree`,
      }}
    >
      {services.length === 0 ? (
        <div className="service-graph-empty">
          <Icon name="layers" size={28} />
          <strong>Create the first root service</strong>
          <p>
            A root starts an inheritance tree. Add children from its details
            after you create it.
          </p>
          <Button onClick={onCreateRoot}>Create root service</Button>
        </div>
      ) : null}
      <GraphEdges width={canvasWidth} height={canvasHeight}>
        {layout.edges.map((edge) => {
          const source = layout.nodes.find((node) => node.id === edge.sourceId);
          const target = layout.nodes.find((node) => node.id === edge.targetId);
          if (source === undefined || target === undefined) return null;
          return (
            <GraphEdge key={edge.id} path={treeEdgePath(source, target)} />
          );
        })}
      </GraphEdges>
      {layout.nodes.map((node, index) => {
        const service = serviceById.get(node.id);
        if (service === undefined) return null;
        return (
          <GraphNode
            id={`service-node-${service.service_id}`}
            key={service.service_id}
            aria-label={`Open ${service.display_name} details`}
            eyebrow={node.depth === 0 ? "Root service" : "Child service"}
            icon={<Icon name="server" size={17} />}
            meta={`${displayState(service.state)} · ${String(childCount(services, service.service_id))} children`}
            root={node.depth === 0}
            selected={service.service_id === selectedServiceId}
            draggable={service.state !== "retired"}
            dragging={draggingId === service.service_id}
            dropTarget={dropTargetId === service.service_id}
            title={service.display_name}
            tone={nodeTone(service.state)}
            x={node.x}
            y={node.y}
            onClick={() => {
              onSelect(service.service_id);
            }}
            onDragStart={(event) => {
              setDraggingId(service.service_id);
              event.dataTransfer.effectAllowed = "move";
              event.dataTransfer.setData("text/plain", service.service_id);
            }}
            onDragOver={(event) => {
              if (!canDropOn(service.service_id)) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = "move";
              setDropTargetId(service.service_id);
            }}
            onDragLeave={() => {
              if (dropTargetId === service.service_id) setDropTargetId(null);
            }}
            onDrop={(event) => {
              event.preventDefault();
              if (draggingId !== null && canDropOn(service.service_id)) {
                onReparent(draggingId, service.service_id);
              }
              setDraggingId(null);
              setDropTargetId(null);
            }}
            onDragEnd={() => {
              setDraggingId(null);
              setDropTargetId(null);
            }}
            onKeyDown={(event) => {
              if (
                event.key !== "ArrowRight" &&
                event.key !== "ArrowDown" &&
                event.key !== "ArrowLeft" &&
                event.key !== "ArrowUp"
              )
                return;
              event.preventDefault();
              const offset =
                event.key === "ArrowRight" || event.key === "ArrowDown"
                  ? 1
                  : -1;
              const target =
                layout.nodes[
                  (index + offset + layout.nodes.length) % layout.nodes.length
                ];
              if (target === undefined) return;
              onSelect(target.id);
              document.getElementById(`service-node-${target.id}`)?.focus();
            }}
          />
        );
      })}
    </GraphViewport>
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
  defaultParentId,
  parents,
  onCancel,
  onCreate,
}: {
  readonly busy: boolean;
  readonly defaultParentId: string | null;
  readonly parents: readonly ServiceSummary[];
  readonly onCancel: () => void;
  readonly onCreate: (event: SubmitEvent<HTMLFormElement>) => void;
}) {
  return (
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
        <select name="parent_service_id" defaultValue={defaultParentId ?? ""}>
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
            The first key can create, read, and cancel model requests. It cannot
            run agents, use tools, or manage Router configuration.
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

function ServiceDetailsPanel({
  ancestors,
  availableParents,
  busy,
  childCount,
  parent,
  retireServiceId,
  selected,
  onChangeState,
  onCreateChild,
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
  readonly onCreateChild: () => void;
  readonly onContinueSetup: () => void;
  readonly onRetirePrompt: (serviceId: string | null) => void;
  readonly onUpdate: (event: SubmitEvent<HTMLFormElement>) => void;
}) {
  if (selected === undefined) {
    return null;
  }
  const confirmRetire = retireServiceId === selected.service_id;
  return (
    <GraphInspector
      tone={nodeTone(selected.state)}
      eyebrow="Selected service"
      icon={<Icon name="server" size={18} />}
      title={selected.display_name}
      actions={
        <>
          <Button
            type="button"
            disabled={busy || selected.state !== "active"}
            onClick={onCreateChild}
          >
            Create child
          </Button>
          <Button type="button" variant="secondary" onClick={onContinueSetup}>
            Configure service
          </Button>
          <StatusPill tone={stateTone(selected.state)}>
            {displayState(selected.state)}
          </StatusPill>
        </>
      }
    >
      <p className="service-inspector-description">
        {parent === undefined
          ? "This service starts an inheritance chain."
          : `This service inherits eligible configuration from ${parent.display_name} and its parents.`}
      </p>
      {selected.state === "retired" ? null : (
        <p className="service-drag-help">
          Drag this node onto an active service to move it. You can also choose
          its parent below.
        </p>
      )}
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
    </GraphInspector>
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
    createParentId: services.length === 0 ? null : undefined,
    busy: false,
    retireServiceId: null,
  });
  const selected = useMemo(
    () => services.find((service) => service.service_id === selectedServiceId),
    [selectedServiceId, services],
  );
  const serviceCounts = countServices(services);
  const layout = useMemo(
    () =>
      layoutTree(treeItems(services), {
        horizontalGap: 74,
        padding: 56,
        verticalGap: 36,
      }),
    [services],
  );

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
      dispatch({ type: "close_create" });
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

  async function reparent(serviceId: string, parentServiceId: string) {
    const service = services.find(
      (candidate) => candidate.service_id === serviceId,
    );
    if (service === undefined || service.parent_service_id === parentServiceId)
      return;
    dispatch({ type: "busy", value: true });
    try {
      await client.putService(service.service_id, {
        expectedRevision: service.revision,
        displayName: service.display_name,
        newParentServiceId: parentServiceId,
        reason: "Reattach the service in the inheritance tree",
      });
      await onChanged();
      onSuccess("The service was moved to its new parent.");
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

      <GraphWorkspace
        aria-label="Service inheritance"
        toolbar={
          <GraphToolbar
            leading={
              <div className="service-graph-heading">
                <span>Global administration</span>
                <h1>Services and inheritance</h1>
                <small>
                  {String(services.length)}{" "}
                  {services.length === 1 ? "service" : "services"} ·{" "}
                  {String(serviceCounts.active)} active ·{" "}
                  {String(serviceCounts.roots)}{" "}
                  {serviceCounts.roots === 1 ? "root" : "roots"}
                </small>
              </div>
            }
            actions={
              <Button
                icon={<Icon name="plus" size={16} />}
                onClick={() => {
                  dispatch({ type: "open_create", parentServiceId: null });
                }}
              >
                Create root
              </Button>
            }
          />
        }
        inspector={
          view.createParentId !== undefined ? (
            <GraphInspector
              eyebrow={view.createParentId === null ? "New root" : "New child"}
              icon={<Icon name="plus" size={18} />}
              title="Create a service"
              onClose={() => {
                dispatch({ type: "close_create" });
              }}
            >
              <CreateServicePanel
                busy={view.busy}
                defaultParentId={view.createParentId}
                parents={creationParents}
                onCancel={() => {
                  dispatch({ type: "close_create" });
                }}
                onCreate={(event) => {
                  void create(event);
                }}
              />
            </GraphInspector>
          ) : (
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
              onCreateChild={() => {
                if (selected === undefined) return;
                dispatch({
                  type: "open_create",
                  parentServiceId: selected.service_id,
                });
              }}
              onContinueSetup={onContinueSetup}
              onRetirePrompt={(serviceId) => {
                dispatch({ type: "confirm_retire", value: serviceId });
              }}
              onUpdate={(event) => {
                void updateDetails(event);
              }}
            />
          )
        }
      >
        <ServiceGraphCanvas
          layout={layout}
          services={services}
          selectedServiceId={selectedServiceId}
          onCreateRoot={() => {
            dispatch({ type: "open_create", parentServiceId: null });
          }}
          onReparent={(serviceId, parentServiceId) => {
            void reparent(serviceId, parentServiceId);
          }}
          onSelect={(serviceId) => {
            dispatch({ type: "close_create" });
            onSelect(serviceId);
          }}
        />
      </GraphWorkspace>
    </div>
  );
}
