import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  type SubmitEvent,
} from "react";
import {
  Button,
  DateTime,
  FormActions,
  FormSection,
  GraphEdge,
  GraphEdges,
  GraphEmptyState,
  GraphInspector,
  GraphInspectorFact,
  GraphInspectorFacts,
  GraphInspectorNotice,
  GraphNode,
  GraphToolbar,
  GraphViewport,
  GraphWorkspace,
  Icon,
  PageSurface,
  SearchableSelect,
  TextControl,
  layoutTree,
  treeEdgePath,
  type TreeLayoutResult,
} from "@opendle/ui";
import {
  errorMessage,
  type AdministrationClient,
  type Service,
} from "./api.ts";

function formText(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function ServiceDateTime({
  value,
}: {
  readonly value: string | null | undefined;
}) {
  return <DateTime fallback={value ?? "Never"} value={value} />;
}

const NO_PARENT_OPTION = "__no_parent__";

function visibleTreeOrder(services: readonly Service[]): readonly Service[] {
  const visited = new Set<string>();
  const ordered: Service[] = [];
  const append = (service: Service) => {
    if (visited.has(service.api_name)) return;
    visited.add(service.api_name);
    ordered.push(service);
    for (const child of services)
      if (child.parent_service_api_name === service.api_name) append(child);
  };
  for (const service of services)
    if (service.parent_service_api_name == null) append(service);
  for (const service of services) append(service);
  return ordered;
}

function serviceTreeLevel(
  servicesByName: ReadonlyMap<string, Service>,
  service: Service,
): number {
  const visited = new Set([service.api_name]);
  let level = 1;
  let parentName = service.parent_service_api_name ?? null;
  while (parentName !== null) {
    if (visited.has(parentName)) return level;
    visited.add(parentName);
    const parent = servicesByName.get(parentName);
    if (parent === undefined) return level;
    level += 1;
    parentName = parent.parent_service_api_name ?? null;
  }
  return level;
}

function useServiceTreeRestoration({
  available,
  restoreTree,
  selectedControlRef,
  selectedService,
}: {
  readonly available: boolean;
  readonly restoreTree: ServiceTreeRestore | undefined;
  readonly selectedControlRef: RefObject<HTMLElement | null>;
  readonly selectedService: string;
}): void {
  // react-doctor-disable-next-line react-doctor/rerender-state-only-in-handlers -- The completed restoration must render GraphWorkspace again so its shared reachability effect checks the restored node position. A ref cannot trigger that check.
  const [restored, setRestored] = useState<ServiceTreeRestore | undefined>();
  const restoreStarted = useRef<ServiceTreeRestore | undefined>(undefined);
  useLayoutEffect(() => {
    if (
      !available ||
      restoreTree === undefined ||
      restored === restoreTree ||
      restoreStarted.current === restoreTree
    )
      return;
    restoreStarted.current = restoreTree;
    const frame = globalThis.requestAnimationFrame(() => {
      const nodes = [
        ...globalThis.document.querySelectorAll<HTMLElement>(
          ".service-management [data-service-api-name]",
        ),
      ];
      const node =
        nodes.find((item) => item.dataset.serviceApiName === selectedService) ??
        nodes[0];
      const viewport = node?.closest<HTMLElement>(".od-graph-viewport");
      const workspace = node?.closest<HTMLElement>(".od-graph-workspace");
      if (node === undefined || viewport == null || workspace == null) return;
      selectedControlRef.current = node;
      viewport.scrollLeft = restoreTree.left;
      viewport.scrollTop = restoreTree.top;
      const bounds = node.getBoundingClientRect();
      const visible = viewport.getBoundingClientRect();
      if (
        bounds.left < visible.left ||
        bounds.right > visible.right ||
        bounds.top < visible.top ||
        bounds.bottom > visible.bottom
      ) {
        node.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
      const inspector = workspace.querySelector<HTMLElement>(
        ".od-graph-inspector",
      );
      if (
        restoreTree.mode === "fallback" ||
        (restoreTree.mode === "history" && inspector?.dataset.mode === "sheet")
      ) {
        inspector
          ?.querySelector<HTMLElement>("h2")
          ?.focus({ preventScroll: true });
      } else {
        node.focus({ preventScroll: true });
      }
      // Let the shared inspector reachability effect check the restored position.
      setRestored(restoreTree);
    });
    return () => {
      globalThis.cancelAnimationFrame(frame);
      if (restored !== restoreTree) restoreStarted.current = undefined;
    };
  }, [available, restoreTree, restored, selectedControlRef, selectedService]);
}

function ServiceGraph({
  available,
  initialState,
  stateContent,
  graphActions,
  inspector,
  layout,
  onCreate,
  onOpenDetails,
  onTreeScroll,
  restoreTree,
  onSelect,
  selectedControlRef,
  selectionLocked,
  selectedService,
  services,
}: {
  readonly available: boolean;
  readonly initialState?: ReactNode;
  readonly stateContent?: ReactNode;
  readonly graphActions?: ReactNode;
  readonly inspector: ReactNode;
  readonly layout: TreeLayoutResult;
  readonly onCreate: (trigger: HTMLButtonElement) => void;
  readonly onOpenDetails: (name: string) => void;
  readonly onTreeScroll:
    ((position: { left: number; top: number }) => void) | undefined;
  readonly restoreTree: ServiceTreeRestore | undefined;
  readonly onSelect: (name: string, trigger: HTMLButtonElement) => void;
  readonly selectedControlRef: RefObject<HTMLElement | null>;
  readonly selectionLocked: boolean;
  readonly selectedService: string;
  readonly services: readonly Service[];
}) {
  const pointerTypeRef = useRef("mouse");
  const pendingPointerSelection = useRef<ReturnType<
    typeof globalThis.setTimeout
  > | null>(null);
  function cancelPointerSelection(): void {
    if (pendingPointerSelection.current === null) return;
    globalThis.clearTimeout(pendingPointerSelection.current);
    pendingPointerSelection.current = null;
  }
  useEffect(() => cancelPointerSelection, []);
  useEffect(() => {
    if (selectionLocked) cancelPointerSelection();
  }, [selectionLocked]);
  useServiceTreeRestoration({
    available,
    restoreTree,
    selectedControlRef,
    selectedService,
  });
  const ordered = useMemo(() => visibleTreeOrder(services), [services]);
  const servicesByName = useMemo(
    () => new Map(services.map((service) => [service.api_name, service])),
    [services],
  );
  const initialActive = services.some(
    (service) => service.api_name === selectedService,
  )
    ? selectedService
    : (ordered[0]?.api_name ?? "");
  const [rovingState, setRovingState] = useState({
    selection: selectedService,
    activeNode: initialActive,
  });
  const selectionChanged = rovingState.selection !== selectedService;
  const activeNodeUnavailable =
    rovingState.activeNode !== initialActive &&
    !services.some((service) => service.api_name === rovingState.activeNode);
  const resetActiveNode = selectionChanged || activeNodeUnavailable;
  const activeNode = resetActiveNode ? initialActive : rovingState.activeNode;
  if (resetActiveNode)
    setRovingState({ selection: selectedService, activeNode: initialActive });
  useEffect(() => {
    selectedControlRef.current =
      [
        ...globalThis.document.querySelectorAll<HTMLElement>(
          ".service-management [data-service-api-name]",
        ),
      ].find((node) => node.dataset.serviceApiName === selectedService) ?? null;
  }, [selectedControlRef, selectedService, services]);
  const height = Math.max(layout.height, 220);
  const width = Math.max(layout.width, 260);

  function focusNode(name: string): void {
    const nodes = globalThis.document.querySelectorAll<HTMLButtonElement>(
      ".service-management [data-service-api-name]",
    );
    const target = Array.from(nodes).find(
      (node) => node.dataset.serviceApiName === name,
    );
    if (target === undefined) return;
    setRovingState({ selection: selectedService, activeNode: name });
    target.focus();
    target.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function moveFocus(
    event: KeyboardEvent<HTMLButtonElement>,
    service: Service,
  ) {
    const currentIndex = ordered.findIndex(
      (item) => item.api_name === service.api_name,
    );
    let target: Service | undefined;
    if (event.key === "ArrowUp") target = ordered[currentIndex - 1];
    if (event.key === "ArrowDown") target = ordered[currentIndex + 1];
    if (event.key === "ArrowRight")
      target = ordered.find(
        (item) => item.parent_service_api_name === service.api_name,
      );
    if (event.key === "ArrowLeft")
      target = ordered.find(
        (item) => item.api_name === service.parent_service_api_name,
      );
    if (event.key === "Home") target = ordered[0];
    if (event.key === "End") target = ordered.at(-1);
    if (
      ![
        "ArrowUp",
        "ArrowDown",
        "ArrowRight",
        "ArrowLeft",
        "Home",
        "End",
      ].includes(event.key)
    )
      return;
    event.preventDefault();
    if (target !== undefined) focusNode(target.api_name);
  }

  return (
    <GraphWorkspace
      aria-label="Service graph workspace"
      fullPage
      inspector={inspector}
      selectedControlRef={selectedControlRef}
      toolbar={
        <GraphToolbar
          actions={
            <>
              {graphActions}
              {available ? (
                <Button
                  disabled={selectionLocked}
                  onClick={(event) => {
                    cancelPointerSelection();
                    onCreate(event.currentTarget);
                  }}
                >
                  <Icon name="plus" size={16} /> Create service
                </Button>
              ) : null}
            </>
          }
        />
      }
    >
      <GraphViewport
        aria-label="Services and parent relationships"
        viewportContent={stateContent}
        onScroll={(event) => {
          onTreeScroll?.({
            left: event.currentTarget.scrollLeft,
            top: event.currentTarget.scrollTop,
          });
        }}
        canvasAlignment={initialState === undefined ? "center" : "start"}
        {...(initialState === undefined
          ? { canvasHeight: height, canvasWidth: width }
          : {})}
        canvasProps={{
          "aria-label": `${String(services.length)} services in parent order`,
        }}
      >
        {initialState ??
          (services.length === 0 ? (
            <GraphEmptyState
              description="Create a root service to start the service tree."
              icon={<Icon name="layers" />}
              title="No services"
            />
          ) : null)}
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
          if (service === undefined) return null;
          const parent = services.find(
            (item) => item.api_name === service.parent_service_api_name,
          );
          return (
            <GraphNode
              aria-label={`${service.display_name}, API name ${service.api_name}, tree level ${String(serviceTreeLevel(servicesByName, service))}, ${
                parent === undefined
                  ? "root service with no parent"
                  : `child service of ${parent.display_name}`
              }`}
              aria-disabled={selectionLocked}
              data-service-api-name={service.api_name}
              eyebrow={parent === undefined ? "Root service" : "Child service"}
              key={service.api_name}
              meta={service.api_name}
              onPointerDown={(event) => {
                pointerTypeRef.current = event.pointerType;
              }}
              onClick={(event) => {
                if (selectionLocked) return;
                cancelPointerSelection();
                const trigger = event.currentTarget;
                if (event.detail === 0 || pointerTypeRef.current !== "mouse") {
                  onSelect(service.api_name, trigger);
                  return;
                }
                // Keep the mouse target in place until a double-click can finish.
                // Keyboard and touch activation do not need this mouse interval.
                pendingPointerSelection.current = globalThis.setTimeout(() => {
                  pendingPointerSelection.current = null;
                  if (trigger.isConnected) onSelect(service.api_name, trigger);
                }, 500);
              }}
              onDoubleClick={(event) => {
                if (selectionLocked || event.button !== 0) return;
                cancelPointerSelection();
                onOpenDetails(service.api_name);
              }}
              onFocus={() => {
                setRovingState({
                  selection: selectedService,
                  activeNode: service.api_name,
                });
              }}
              onKeyDown={(event) => {
                cancelPointerSelection();
                if (event.key === "Enter") {
                  event.preventDefault();
                  if (!selectionLocked) onOpenDetails(service.api_name);
                  return;
                }
                moveFocus(event, service);
              }}
              root={parent === undefined}
              selected={selectedService === service.api_name}
              tabIndex={activeNode === service.api_name ? 0 : -1}
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

function CreateServiceInspector({
  busy,
  error,
  onClose,
  onSubmit,
  returnFocusRef,
  services,
}: {
  readonly busy: boolean;
  readonly error: string | null;
  readonly onClose: () => void;
  readonly onSubmit: (event: SubmitEvent<HTMLFormElement>) => void;
  readonly returnFocusRef: RefObject<HTMLElement | null>;
  readonly services: readonly Service[];
}) {
  const [parentSelection, setParentSelection] = useState(NO_PARENT_OPTION);
  const [apiName, setApiName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const formRef = useRef<HTMLFormElement>(null);
  useEffect(() => {
    if (error === null) return;
    formRef.current
      ?.querySelector<HTMLButtonElement>('button[type="submit"]')
      ?.focus();
  }, [error]);
  return (
    <GraphInspector
      activationKey="create-service"
      closeDisabled={busy}
      closeLabel="Close create service"
      eyebrow="Service tree"
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      title="Create service"
      tone="lime"
    >
      <form className="service-create-form" onSubmit={onSubmit} ref={formRef}>
        <FormSection legend="Service details">
          <TextControl
            label="API name"
            maxLength={63}
            name="api_name"
            onChange={(event) => {
              setApiName(event.currentTarget.value);
            }}
            pattern="[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
            requirement="required"
            value={apiName}
          />
          <TextControl
            label="Display name"
            maxLength={200}
            name="display_name"
            onChange={(event) => {
              setDisplayName(event.currentTarget.value);
            }}
            requirement="required"
            value={displayName}
          />
          <SearchableSelect
            label="Parent service"
            onChange={(value) => {
              setParentSelection(value);
            }}
            options={[
              { label: "No parent", value: NO_PARENT_OPTION },
              ...services.map((service) => ({
                description: service.api_name,
                label: service.display_name,
                value: service.api_name,
              })),
            ]}
            placeholder="Search parent services"
            value={parentSelection}
          />
          <input
            name="parent"
            type="hidden"
            value={parentSelection === NO_PARENT_OPTION ? "" : parentSelection}
          />
        </FormSection>
        <FormActions alignment="start">
          <Button disabled={busy} type="submit">
            {busy ? "Creating service…" : "Create service"}
          </Button>
        </FormActions>
      </form>
      {error === null ? null : (
        <GraphInspectorNotice dynamic tone="error">
          <strong>The service was not created.</strong> {error} Correct the
          values and try again.
        </GraphInspectorNotice>
      )}
    </GraphInspector>
  );
}

function CompactServiceInspector({
  selected,
  services,
  onClose,
  onOpenDetails,
  returnFocusRef,
}: {
  readonly selected: Service;
  readonly services: readonly Service[];
  readonly onClose: () => void;
  readonly onOpenDetails: (name: string) => void;
  readonly returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const parent = services.find(
    (service) => service.api_name === selected.parent_service_api_name,
  );
  return (
    <GraphInspector
      activationKey={selected.api_name}
      title={selected.display_name}
      eyebrow={selected.api_name === "root" ? "Root service" : "Child service"}
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      tone="lime"
      actions={
        <Button
          onClick={() => {
            onOpenDetails(selected.api_name);
          }}
        >
          Open service details
        </Button>
      }
    >
      <GraphInspectorFacts>
        <GraphInspectorFact
          label="API name"
          value={<code>{selected.api_name}</code>}
        />
        <GraphInspectorFact
          label="Parent"
          value={
            selected.api_name === "root" ? (
              "None"
            ) : (
              <>
                {parent?.display_name ?? "Parent unavailable"}{" "}
                <code>{selected.parent_service_api_name}</code>
              </>
            )
          }
        />
        <GraphInspectorFact
          label="Created"
          value={<ServiceDateTime value={selected.created_at} />}
        />
      </GraphInspectorFacts>
    </GraphInspector>
  );
}

interface ServiceTreeRestore {
  readonly mode: "history" | "fallback" | "deleted";
  readonly left: number;
  readonly top: number;
}

interface ServiceManagementState {
  readonly busy: boolean;
  readonly showCreate: boolean;
  readonly closedSelection: string | null;
  readonly deletedReturn: boolean;
  readonly selection: string;
  readonly createError: string | null;
}

type ServiceManagementAction =
  | { readonly type: "selection-changed"; readonly selection: string }
  | { readonly type: "inspector-closed"; readonly selection: string }
  | { readonly type: "node-selected" }
  | { readonly type: "create-opened" }
  | { readonly type: "create-closed" }
  | { readonly type: "create-started" }
  | { readonly type: "create-succeeded" }
  | { readonly type: "create-failed"; readonly message: string }
  | { readonly type: "create-finished" };

function serviceManagementReducer(
  state: ServiceManagementState,
  action: ServiceManagementAction,
): ServiceManagementState {
  switch (action.type) {
    case "selection-changed":
      return { ...state, selection: action.selection, closedSelection: null };
    case "inspector-closed":
      return { ...state, closedSelection: action.selection };
    case "node-selected":
      return {
        ...state,
        showCreate: false,
        createError: null,
        closedSelection: null,
        deletedReturn: false,
      };
    case "create-opened":
      return { ...state, createError: null, showCreate: true };
    case "create-closed":
      return { ...state, showCreate: false, createError: null };
    case "create-started":
      return { ...state, busy: true, createError: null };
    case "create-succeeded":
      return { ...state, showCreate: false };
    case "create-failed":
      return { ...state, createError: action.message };
    case "create-finished":
      return { ...state, busy: false };
  }
}

export function ServiceManagement({
  available = true,
  initialState,
  stateContent,
  registerNavigationGuard,
  graphActions,
  client,
  csrf,
  onNotice,
  onRefresh,
  onSelect,
  onOpenDetails,
  onTreeScroll,
  restoreTree,
  selectedService,
  services,
}: {
  readonly available?: boolean;
  readonly initialState?: ReactNode;
  readonly stateContent?: ReactNode;
  readonly registerNavigationGuard?: (guard: () => boolean) => () => void;
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly graphActions?: ReactNode;
  readonly onRefresh: () => Promise<void>;
  readonly onSelect: (name: string) => void;
  readonly onOpenDetails: (name: string) => void;
  readonly onTreeScroll?: (position: { left: number; top: number }) => void;
  readonly restoreTree?: ServiceTreeRestore;
  readonly selectedService: string;
  readonly services: readonly Service[];
}) {
  const [state, dispatch] = useReducer(serviceManagementReducer, {
    busy: false,
    showCreate: false,
    closedSelection: null,
    deletedReturn: restoreTree?.mode === "deleted",
    selection: selectedService,
    createError: null,
  });
  const { busy, showCreate, closedSelection, deletedReturn, createError } =
    state;
  const busyRef = useRef(false);
  if (state.selection !== selectedService) {
    dispatch({ type: "selection-changed", selection: selectedService });
  }
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const selectedControlRef = useRef<HTMLElement | null>(null);
  const selected =
    services.find((service) => service.api_name === selectedService) ?? null;
  useLayoutEffect(
    () =>
      registerNavigationGuard?.(() => {
        if (!busyRef.current) return true;
        onNotice(
          "error",
          "Wait for the service request to finish before you leave this service.",
        );
        return false;
      }),
    [onNotice, registerNavigationGuard],
  );
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

  function closeCreate(): void {
    if (busyRef.current) {
      onNotice(
        "error",
        "Wait for the service request to finish before you close this inspector.",
      );
      return;
    }
    dispatch({ type: "create-closed" });
  }

  async function create(event: SubmitEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busyRef.current) return;
    const form = new FormData(event.currentTarget);
    const parent = formText(form, "parent");
    busyRef.current = true;
    dispatch({ type: "create-started" });
    try {
      await client.createService(
        {
          api_name: formText(form, "api_name"),
          display_name: formText(form, "display_name"),
          parent_service_api_name: parent === "" ? null : parent,
        },
        csrf,
      );
      await onRefresh();
      onNotice("success", "The service was created.");
      dispatch({ type: "create-succeeded" });
    } catch (error) {
      const message = errorMessage(error);
      dispatch({ type: "create-failed", message });
      onNotice("error", message);
    } finally {
      busyRef.current = false;
      dispatch({ type: "create-finished" });
    }
  }

  function openDetails(name: string): void {
    if (busyRef.current || !available) return;
    onOpenDetails(name);
  }

  const inspector = showCreate ? (
    <CreateServiceInspector
      busy={busy}
      error={createError}
      onClose={closeCreate}
      onSubmit={(event) => {
        void create(event);
      }}
      returnFocusRef={returnFocusRef}
      services={services}
    />
  ) : selected === null ||
    deletedReturn ||
    closedSelection === selectedService ? undefined : (
    <CompactServiceInspector
      selected={selected}
      services={services}
      onClose={() => {
        dispatch({ type: "inspector-closed", selection: selectedService });
      }}
      onOpenDetails={openDetails}
      returnFocusRef={selectedControlRef}
    />
  );
  return (
    <PageSurface className="service-management" edgeToEdge>
      <ServiceGraph
        available={available}
        initialState={available ? undefined : initialState}
        stateContent={stateContent}
        graphActions={graphActions}
        inspector={inspector}
        layout={layout}
        onCreate={(trigger) => {
          if (busyRef.current) return;
          returnFocusRef.current = trigger;
          dispatch({ type: "create-opened" });
        }}
        onOpenDetails={openDetails}
        onTreeScroll={onTreeScroll}
        restoreTree={restoreTree}
        onSelect={(name, trigger) => {
          if (busyRef.current) return;
          selectedControlRef.current = trigger;
          dispatch({ type: "node-selected" });
          onSelect(name);
        }}
        selectionLocked={!available || busy}
        selectedControlRef={selectedControlRef}
        selectedService={selectedService}
        services={services}
      />
    </PageSurface>
  );
}
