import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
  type ReactNode,
  type SubmitEvent,
} from "react";
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
  StatePanel,
  layoutTree,
  treeEdgePath,
  type TreeLayoutResult,
} from "@opendle/ui";
import {
  errorMessage,
  type AdministrationClient,
  type Service,
} from "./api.js";

function formText(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function descendants(
  services: readonly Service[],
  root: string,
): ReadonlySet<string> {
  const values = new Set<string>();
  const pending = [root];
  while (pending.length > 0) {
    const parent = pending.pop();
    for (const service of services) {
      if (
        service.parent_service_api_name === parent &&
        !values.has(service.api_name)
      ) {
        values.add(service.api_name);
        pending.push(service.api_name);
      }
    }
  }
  return values;
}

type Mutate = (
  action: () => Promise<unknown>,
  message: string,
) => Promise<boolean>;

function ServiceInspector({
  busy,
  csrf,
  client,
  mutate,
  onClose,
  selected,
  services,
}: {
  readonly busy: boolean;
  readonly csrf: string;
  readonly client: AdministrationClient;
  readonly mutate: Mutate;
  readonly onClose: () => void;
  readonly selected: Service;
  readonly services: readonly Service[];
}) {
  const blockedParents = descendants(services, selected.api_name);
  const hasChildren = services.some(
    (item) => item.parent_service_api_name === selected.api_name,
  );
  const parentOptions = services.flatMap((item) =>
    item.api_name === selected.api_name || blockedParents.has(item.api_name)
      ? []
      : [item],
  );
  return (
    <GraphInspector
      eyebrow={
        selected.parent_service_api_name == null
          ? "Root service"
          : "Child service"
      }
      onClose={onClose}
      onKeyDown={(event) => {
        if (event.defaultPrevented || event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
      title={selected.display_name}
    >
      <dl className="record-facts">
        <div>
          <dt>API name</dt>
          <dd>{selected.api_name}</dd>
        </div>
        <div>
          <dt>Parent</dt>
          <dd>{selected.parent_service_api_name ?? "None"}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>
            <time dateTime={selected.created_at}>{selected.created_at}</time>
          </dd>
        </div>
      </dl>
      <form
        className="service-inspector-form"
        onSubmit={(event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          const parent = formText(form, "parent");
          void mutate(
            () =>
              client.updateService(
                selected.api_name,
                {
                  display_name: formText(form, "display_name"),
                  parent_service_api_name: parent === "" ? null : parent,
                },
                csrf,
              ),
            "The service was updated.",
          );
        }}
      >
        <label>
          Display name
          <input
            defaultValue={selected.display_name}
            maxLength={200}
            name="display_name"
            required
          />
        </label>
        <label>
          Parent service
          <select
            defaultValue={selected.parent_service_api_name ?? ""}
            name="parent"
          >
            <option value="">No parent</option>
            {parentOptions.map((item) => (
              <option key={item.api_name} value={item.api_name}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        <Button disabled={busy} type="submit">
          Save service
        </Button>
      </form>
      <Button
        disabled={busy || hasChildren}
        onClick={() => {
          if (
            !globalThis.confirm(
              `Delete service ${selected.api_name} and its keys, workspaces, assignments, logs, accounting, jobs, and retained media?`,
            )
          )
            return;
          void mutate(
            () => client.deleteService(selected.api_name, csrf),
            "The service was deleted.",
          );
        }}
        variant="secondary"
      >
        Delete service
      </Button>
      {hasChildren ? (
        <p className="field-note">
          Move or delete each child before you delete this service.
        </p>
      ) : null}
    </GraphInspector>
  );
}

function ServiceGraph({
  inspector,
  layout,
  onCreate,
  onSelect,
  selectedService,
  services,
}: {
  readonly inspector: ReactNode;
  readonly layout: TreeLayoutResult;
  readonly onCreate: (trigger: HTMLButtonElement) => void;
  readonly onSelect: (name: string, trigger: HTMLButtonElement) => void;
  readonly selectedService: string;
  readonly services: readonly Service[];
}) {
  const height = Math.max(layout.height, 480);
  const width = Math.max(layout.width, 760);
  return (
    <GraphWorkspace
      aria-label="Service parent relationships"
      inspector={inspector}
      toolbar={
        <GraphToolbar
          actions={
            <Button
              onClick={(event) => {
                onCreate(event.currentTarget);
              }}
            >
              <Icon name="plus" size={16} /> Create service
            </Button>
          }
          leading={<strong>Service tree</strong>}
        />
      }
    >
      <GraphViewport
        aria-label="Service tree canvas"
        canvasHeight={height}
        canvasProps={{ "aria-label": `${String(services.length)} services` }}
        canvasWidth={width}
      >
        {services.length === 0 ? (
          <StatePanel kind="empty" title="No services">
            Create a root service to start the service tree.
          </StatePanel>
        ) : null}
        <GraphEdges height={height} width={width}>
          {layout.edges.map((edge) => {
            const source = layout.nodes.find(
              (node) => node.id === edge.sourceId,
            );
            const target = layout.nodes.find(
              (node) => node.id === edge.targetId,
            );
            return source === undefined || target === undefined ? null : (
              <GraphEdge key={edge.id} path={treeEdgePath(source, target)} />
            );
          })}
        </GraphEdges>
        {layout.nodes.map((node) => {
          const service = services.find((item) => item.api_name === node.id);
          return service === undefined ? null : (
            <GraphNode
              aria-label={`Inspect ${service.display_name}`}
              eyebrow={
                service.parent_service_api_name == null
                  ? "Root service"
                  : "Child service"
              }
              key={service.api_name}
              meta={service.api_name}
              onClick={(event) => {
                onSelect(service.api_name, event.currentTarget);
              }}
              root={service.parent_service_api_name == null}
              selected={selectedService === service.api_name}
              title={service.display_name}
              tone="lime"
              x={node.x}
              y={node.y}
            />
          );
        })}
      </GraphViewport>
    </GraphWorkspace>
  );
}

function ServiceList({
  onSelect,
  services,
}: {
  readonly onSelect: (name: string, trigger: HTMLButtonElement) => void;
  readonly services: readonly Service[];
}) {
  return (
    <section aria-labelledby="service-list-title" className="record-section">
      <h2 id="service-list-title">Accessible service list</h2>
      <p>
        The list contains the same service records and inspection action as the
        graph.
      </p>
      <div className="administration-table-region">
        <table>
          <thead>
            <tr>
              <th>Service</th>
              <th>API name</th>
              <th>Parent</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {services.length === 0 ? (
              <tr>
                <td colSpan={4}>No services</td>
              </tr>
            ) : (
              services.map((service) => (
                <tr key={service.api_name}>
                  <th scope="row">{service.display_name}</th>
                  <td>{service.api_name}</td>
                  <td>{service.parent_service_api_name ?? "Root"}</td>
                  <td>
                    <Button
                      onClick={(event) => {
                        onSelect(service.api_name, event.currentTarget);
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
    </section>
  );
}

function CreateService({
  busy,
  onClose,
  onSubmit,
  panelRef,
  services,
}: {
  readonly busy: boolean;
  readonly onClose: () => void;
  readonly onSubmit: (event: SubmitEvent<HTMLFormElement>) => void;
  readonly panelRef: RefObject<HTMLElement | null>;
  readonly services: readonly Service[];
}) {
  return (
    <section
      aria-labelledby="create-service-title"
      className="service-create-panel"
      onKeyDown={(event) => {
        if (event.defaultPrevented || event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
      ref={panelRef}
    >
      <div>
        <h2 id="create-service-title">Create service</h2>
        <Button
          aria-label="Close create service"
          onClick={onClose}
          variant="quiet"
        >
          Close
        </Button>
      </div>
      <form className="administration-form" onSubmit={onSubmit}>
        <label>
          API name
          <input
            maxLength={63}
            name="api_name"
            pattern="[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
            required
          />
        </label>
        <label>
          Display name
          <input maxLength={200} name="display_name" required />
        </label>
        <label>
          Parent service
          <select name="parent">
            <option value="">No parent</option>
            {services.map((service) => (
              <option key={service.api_name} value={service.api_name}>
                {service.display_name}
              </option>
            ))}
          </select>
        </label>
        <Button disabled={busy} type="submit">
          Create service
        </Button>
      </form>
    </section>
  );
}

export function ServiceManagement({
  client,
  csrf,
  onNotice,
  onRefresh,
  onSelect,
  selectedService,
  services,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefresh: () => Promise<void>;
  readonly onSelect: (name: string) => void;
  readonly selectedService: string;
  readonly services: readonly Service[];
}) {
  const [busy, setBusy] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const createPanelRef = useRef<HTMLElement | null>(null);
  const selected =
    services.find((item) => item.api_name === selectedService) ?? null;
  const layout = useMemo(
    () =>
      layoutTree(
        services.map((service) => ({
          id: service.api_name,
          parentId: service.parent_service_api_name ?? null,
        })),
        {
          direction: "vertical",
          padding: 36,
          horizontalGap: 34,
          verticalGap: 86,
        },
      ),
    [services],
  );
  useEffect(() => {
    if (showCreate)
      createPanelRef.current?.querySelector<HTMLElement>("input")?.focus();
  }, [showCreate]);

  function closePanel(): void {
    setShowCreate(false);
    const target = returnFocusRef.current;
    if (target?.isConnected) target.focus();
    returnFocusRef.current = null;
  }

  function closeInspector(): void {
    const target = returnFocusRef.current;
    if (target?.isConnected) target.focus();
    returnFocusRef.current = null;
    onSelect("");
  }

  async function mutate(
    action: () => Promise<unknown>,
    message: string,
  ): Promise<boolean> {
    setBusy(true);
    try {
      await action();
      await onRefresh();
      onNotice("success", message);
      return true;
    } catch (error) {
      onNotice("error", errorMessage(error));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function create(event: SubmitEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const parent = formText(form, "parent");
    const created = await mutate(
      () =>
        client.createService(
          {
            api_name: formText(form, "api_name"),
            display_name: formText(form, "display_name"),
            parent_service_api_name: parent === "" ? null : parent,
          },
          csrf,
        ),
      "The service was created.",
    );
    if (created) closePanel();
  }

  const inspector =
    selected === null ? undefined : (
      <ServiceInspector
        busy={busy}
        client={client}
        csrf={csrf}
        mutate={mutate}
        onClose={closeInspector}
        selected={selected}
        services={services}
      />
    );
  return (
    <div className="service-management">
      <ServiceGraph
        inspector={inspector}
        layout={layout}
        onCreate={(trigger) => {
          returnFocusRef.current = trigger;
          setShowCreate(true);
        }}
        onSelect={(name, trigger) => {
          returnFocusRef.current = trigger;
          onSelect(name);
          globalThis.setTimeout(() => {
            globalThis.document
              .querySelector<HTMLElement>(
                ".service-management .od-graph-inspector-close",
              )
              ?.focus();
          }, 0);
        }}
        selectedService={selectedService}
        services={services}
      />
      <ServiceList
        onSelect={(name, trigger) => {
          returnFocusRef.current = trigger;
          onSelect(name);
          globalThis.setTimeout(() => {
            globalThis.document
              .querySelector<HTMLElement>(
                ".service-management .od-graph-inspector-close",
              )
              ?.focus();
          }, 0);
        }}
        services={services}
      />
      {showCreate ? (
        <CreateService
          busy={busy}
          onClose={closePanel}
          onSubmit={(event) => {
            void create(event);
          }}
          panelRef={createPanelRef}
          services={services}
        />
      ) : null}
    </div>
  );
}
