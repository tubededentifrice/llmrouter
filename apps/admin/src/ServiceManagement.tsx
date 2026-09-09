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
  ConfirmationDialog,
  DateTime,
  FormActions,
  FormSection,
  GraphEdge,
  GraphEdges,
  GraphInspector,
  GraphInspectorFact,
  GraphInspectorFacts,
  GraphInspectorNotice,
  GraphNode,
  GraphNodeAction,
  GraphToolbar,
  GraphViewport,
  GraphWorkspace,
  PageSurface,
  TextControl,
  layoutTree,
  treeEdgePath,
  type TreeLayoutResult,
} from "@opendle/ui";
import {
  AdministrationApiError,
  errorMessage,
  type AdministrationClient,
  type Service,
} from "./api.ts";

function ServiceDateTime({
  value,
}: {
  readonly value: string | null | undefined;
}) {
  return <DateTime fallback={value ?? "Never"} value={value} />;
}

const CREATE_ACTION = "+new-service";

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

function useServiceGraphNavigation({
  available,
  services,
  selectedService,
  selectedControlRef,
  layout,
}: {
  readonly available: boolean;
  readonly services: readonly Service[];
  readonly selectedService: string;
  readonly selectedControlRef: RefObject<HTMLElement | null>;
  readonly layout: TreeLayoutResult;
}) {
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
    rovingState.activeNode !== CREATE_ACTION &&
    rovingState.activeNode !== initialActive &&
    !services.some((service) => service.api_name === rovingState.activeNode);
  const resetActiveNode = selectionChanged || activeNodeUnavailable;
  const activeNode = resetActiveNode ? initialActive : rovingState.activeNode;
  if (resetActiveNode)
    setRovingState({ selection: selectedService, activeNode: initialActive });
  const selectedNode = layout.nodes.find((node) => node.id === selectedService);
  const selected = servicesByName.get(selectedService);
  const actionRef = useRef<HTMLButtonElement>(null);
  const [actionSize, setActionSize] = useState({
    nodeHeight: 72,
    nodeWidth: 176,
    width: 160,
    height: 44,
  });
  useLayoutEffect(() => {
    const node = selectedControlRef.current;
    const action = globalThis.document.querySelector<HTMLButtonElement>(
      ".service-management [data-service-create-action]",
    );
    actionRef.current = action;
    if (node === null || action === null) return;
    const measure = () => {
      setActionSize({
        nodeHeight: node.offsetHeight,
        nodeWidth: node.offsetWidth,
        width: action.offsetWidth,
        height: action.offsetHeight,
      });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    observer.observe(action);
    return () => {
      observer.disconnect();
    };
  }, [selectedControlRef, selectedService, available]);
  const actionX = Math.max(
    0,
    (selectedNode?.x ?? 0) + (actionSize.nodeWidth - actionSize.width) / 2,
  );
  const actionY = (selectedNode?.y ?? 0) + actionSize.nodeHeight + 8;
  const height = Math.max(
    layout.height,
    220,
    selectedNode === undefined ? 0 : actionY + actionSize.height + 36,
  );
  const width = Math.max(
    layout.width,
    260,
    selectedNode === undefined ? 0 : actionX + actionSize.width + 36,
  );

  function focusNode(name: string): void {
    if (name === CREATE_ACTION) {
      setRovingState({ selection: selectedService, activeNode: name });
      actionRef.current?.focus();
      actionRef.current?.scrollIntoView({
        block: "nearest",
        inline: "nearest",
      });
      return;
    }
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
    if (
      (event.key === "ArrowDown" && service.api_name === selectedService) ||
      (event.key === "ArrowUp" &&
        ordered[currentIndex - 1]?.api_name === selectedService)
    ) {
      event.preventDefault();
      focusNode(CREATE_ACTION);
      return;
    }
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

  return {
    ordered,
    servicesByName,
    initialActive,
    activeNode,
    selectedNode,
    selected,
    actionX,
    actionY,
    width,
    height,
    focusNode,
    moveFocus,
    setRovingState,
  };
}

function ServiceGraph({
  available,
  initialState,
  stateContent,
  graphActions,
  inspector,
  layout,
  nodeSize,
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
  readonly nodeSize: {
    readonly nodeWidth: number;
    readonly nodeHeight: number;
    readonly nodeHeights: ReadonlyMap<string, number>;
  };
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
  const {
    ordered,
    servicesByName,
    initialActive,
    activeNode,
    selectedNode,
    selected,
    actionX,
    actionY,
    width,
    height,
    focusNode,
    moveFocus,
    setRovingState,
  } = useServiceGraphNavigation({
    available,
    services,
    selectedService,
    selectedControlRef,
    layout,
  });

  return (
    <GraphWorkspace
      aria-label="Service graph workspace"
      fullPage
      inspector={inspector}
      selectedControlRef={selectedControlRef}
      toolbar={<GraphToolbar actions={graphActions} />}
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
          onBlur: (event) => {
            if (!event.currentTarget.contains(event.relatedTarget))
              setRovingState({
                selection: selectedService,
                activeNode: initialActive,
              });
          },
        }}
      >
        {initialState}
        <GraphEdges height={height} width={width}>
          {layout.edges.map((edge) => {
            const source = layout.nodes.find(
              (node) => node.id === edge.sourceId,
            );
            const target = layout.nodes.find(
              (node) => node.id === edge.targetId,
            );
            return source === undefined || target === undefined ? null : (
              <GraphEdge
                key={edge.id}
                path={treeEdgePath(source, target, {
                  nodeWidth: nodeSize.nodeWidth,
                  nodeHeight:
                    nodeSize.nodeHeights.get(source.id) ?? nodeSize.nodeHeight,
                })}
              />
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
              ref={
                service.api_name === selectedService
                  ? (control) => {
                      selectedControlRef.current = control;
                    }
                  : null
              }
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
        {available && selectedNode !== undefined && selected !== undefined ? (
          <GraphNodeAction
            aria-label={`New service under ${selected.display_name}, API name ${selected.api_name}`}
            aria-disabled={selectionLocked}
            data-service-create-action="true"
            variant="text"
            x={actionX}
            y={actionY}
            tabIndex={activeNode === CREATE_ACTION ? 0 : -1}
            onFocus={() => {
              setRovingState({
                selection: selectedService,
                activeNode: CREATE_ACTION,
              });
            }}
            onClick={(event) => {
              cancelPointerSelection();
              if (!selectionLocked) onCreate(event.currentTarget);
            }}
            onKeyDown={(event) => {
              const index = ordered.findIndex(
                (service) => service.api_name === selectedService,
              );
              let target: string | undefined;
              if (event.key === "ArrowUp" || event.key === "ArrowLeft")
                target = selectedService;
              if (event.key === "ArrowDown")
                target = ordered[index + 1]?.api_name;
              if (event.key === "Home") target = ordered[0]?.api_name;
              if (event.key === "End") target = ordered.at(-1)?.api_name;
              if (
                [
                  "ArrowUp",
                  "ArrowLeft",
                  "ArrowDown",
                  "ArrowRight",
                  "Home",
                  "End",
                ].includes(event.key)
              )
                event.preventDefault();
              if (target !== undefined) focusNode(target);
            }}
          >
            + New service
          </GraphNodeAction>
        ) : null}
      </GraphViewport>
    </GraphWorkspace>
  );
}

interface ServiceDraft {
  readonly parent: Service;
  readonly displayName: string;
  readonly apiName: string;
}
interface CreateFailure {
  readonly message: string;
  readonly field?: "display_name" | "api_name";
}

function CreateServiceInspector({
  busy,
  error,
  draft,
  onChange,
  onClose,
  onSubmit,
  returnFocusRef,
  formRef,
}: {
  readonly busy: boolean;
  readonly error: CreateFailure | null;
  readonly draft: ServiceDraft;
  readonly onChange: (field: "displayName" | "apiName", value: string) => void;
  readonly onClose: () => void;
  readonly onSubmit: (event: SubmitEvent<HTMLFormElement>) => void;
  readonly returnFocusRef: RefObject<HTMLElement | null>;
  readonly formRef: RefObject<HTMLFormElement | null>;
}) {
  useLayoutEffect(() => {
    if (error === null || busy) return;
    const selector =
      error.field === undefined
        ? 'button[type="submit"]'
        : '[name="' + error.field + '"]';
    formRef.current?.querySelector<HTMLElement>(selector)?.focus();
  }, [busy, error, formRef]);
  return (
    <GraphInspector
      activationKey="create-service"
      aria-busy={busy}
      closeDisabled={busy}
      closeLabel="Close new service"
      onKeyDown={(event) => {
        if (
          !busy &&
          event.key === "Tab" &&
          !event.shiftKey &&
          event.target instanceof HTMLElement &&
          event.target.tagName === "H2"
        ) {
          event.preventDefault();
          formRef.current
            ?.querySelector<HTMLElement>('[name="display_name"]')
            ?.focus();
        }
      }}
      eyebrow="Service tree"
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      title="New service"
      tone="lime"
    >
      <GraphInspectorFacts>
        <GraphInspectorFact
          label="Parent"
          value={
            <>
              {draft.parent.display_name} <code>{draft.parent.api_name}</code>
            </>
          }
        />
      </GraphInspectorFacts>
      <form
        className="service-create-form"
        noValidate
        onSubmit={onSubmit}
        ref={formRef}
      >
        <FormSection legend="Service details">
          <TextControl
            label="Display name"
            disabled={busy}
            maxLength={200}
            name="display_name"
            onChange={(event) => {
              onChange("displayName", event.currentTarget.value);
            }}
            requirement="required"
            aria-invalid={error?.field === "display_name" || undefined}
            value={draft.displayName}
          />
          <TextControl
            label="API name"
            disabled={busy}
            maxLength={63}
            name="api_name"
            onChange={(event) => {
              onChange("apiName", event.currentTarget.value);
            }}
            pattern="[a-z](?:(?:[a-z0-9]|-){0,61}[a-z0-9])?"
            requirement="required"
            aria-invalid={error?.field === "api_name" || undefined}
            value={draft.apiName}
          />
        </FormSection>
        <FormActions alignment="start">
          <Button disabled={busy} type="submit">
            {busy ? "Creating service" : "Create service"}
          </Button>
        </FormActions>
      </form>
      {error === null ? null : (
        <GraphInspectorNotice dynamic tone="error">
          <strong>The service was not created.</strong> {error.message} Correct
          the values and try again.
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

export type ServiceCreationEvent =
  | { readonly type: "opened"; readonly parent: Service }
  | { readonly type: "started" }
  | { readonly type: "closed" }
  | { readonly type: "failed"; readonly parentUnavailable: boolean }
  | { readonly type: "succeeded"; readonly service: Service };

interface ServiceManagementState {
  readonly busy: boolean;
  readonly draft: ServiceDraft | null;
  readonly closedSelection: string | null;
  readonly deletedReturn: boolean;
  readonly selection: string;
  readonly error: CreateFailure | null;
  readonly discardSelection: string | null;
  readonly focusReturn: string | null;
}

function serviceControl(name: string): HTMLElement | null {
  return (
    [
      ...globalThis.document.querySelectorAll<HTMLElement>(
        ".service-management [data-service-api-name]",
      ),
    ].find((node) => node.dataset.serviceApiName === name) ?? null
  );
}

interface ServiceManagementProps {
  readonly available?: boolean;
  readonly initialSelectionRequested?: boolean;
  readonly initialState?: ReactNode;
  readonly stateContent?: ReactNode;
  readonly registerNavigationGuard?: (
    guard: (intent?: "unload") => boolean,
  ) => () => void;
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly graphActions?: ReactNode;
  readonly onRefresh?: () => Promise<void>;
  readonly onCreationEvent?: (event: ServiceCreationEvent) => void;
  readonly servicesReadVersion?: number;
  readonly onSelect: (name: string) => void;
  readonly onOpenDetails: (name: string) => void;
  readonly onTreeScroll?: (position: { left: number; top: number }) => void;
  readonly restoreTree?: ServiceTreeRestore;
  readonly selectedService: string;
  readonly services: readonly Service[];
}

function useServiceCreationState({
  initialSelectionRequested = true,
  registerNavigationGuard,
  onNotice,
  onSelect,
  onCreationEvent,
  servicesReadVersion = 0,
  restoreTree,
  selectedService,
  services,
}: ServiceManagementProps) {
  const [state, update] = useReducer(
    (
      current: ServiceManagementState,
      patch: Partial<ServiceManagementState>,
    ): ServiceManagementState => ({ ...current, ...patch }),
    {
      busy: false,
      draft: null,
      closedSelection: null,
      deletedReturn: restoreTree?.mode === "deleted",
      selection: selectedService,
      error: null,
      discardSelection: null,
      focusReturn: null,
    },
  );
  const stateRef = useRef(state);
  useLayoutEffect(() => {
    stateRef.current = state;
  }, [state]);
  const busyRef = useRef(false);
  if (state.selection !== selectedService) {
    update({
      selection: selectedService,
      closedSelection:
        state.selection === "" && !initialSelectionRequested
          ? selectedService
          : null,
    });
  }
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const selectedControlRef = useRef<HTMLElement | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const discardReturnRef = useRef<HTMLElement | null>(null);
  const selected =
    services.find((service) => service.api_name === selectedService) ?? null;
  useLayoutEffect(
    () =>
      registerNavigationGuard?.((intent) => {
        if (busyRef.current) {
          if (intent !== "unload") {
            onNotice(
              "error",
              "Wait for the service request to finish before you leave this service.",
            );
            queueMicrotask(() =>
              formRef.current
                ?.closest("dialog")
                ?.querySelector<HTMLElement>("h2")
                ?.focus(),
            );
          }
          return false;
        }
        const values = stateRef.current.draft;
        if (values === null) return true;
        const dirty = values.displayName !== "" || values.apiName !== "";
        if (intent === "unload") return !dirty;
        if (
          dirty &&
          !globalThis.confirm(
            "Discard the unsaved values and leave this service?",
          )
        ) {
          queueMicrotask(() => firstEnteredField()?.focus());
          return false;
        }
        // Both the route opener and the history writer can check the guard.
        // Consume this accepted discard once, including for same-route history.
        stateRef.current = { ...stateRef.current, draft: null };
        onCreationEvent?.({ type: "closed" });
        update({ draft: null, error: null, discardSelection: null });
        return true;
      }),
    [onCreationEvent, onNotice, registerNavigationGuard],
  );

  // This revision advances only after an authoritative complete service read or create response.
  const lastRead = useRef({
    version: servicesReadVersion,
    selection: selectedService,
  });
  useLayoutEffect(() => {
    const previous = lastRead.current;
    lastRead.current = {
      version: servicesReadVersion,
      selection: selectedService,
    };
    if (previous.version === servicesReadVersion || busyRef.current) return;
    const parent = stateRef.current.draft?.parent.api_name;
    const lost = parent ?? previous.selection;
    if (lost === "" || services.some((service) => service.api_name === lost))
      return;
    const first = visibleTreeOrder(services)[0]?.api_name ?? "";
    onCreationEvent?.({ type: "closed" });
    update({
      draft: null,
      error: null,
      discardSelection: null,
      closedSelection: first,
      selection: first,
    });
    onSelect(first);
    onNotice(
      "error",
      parent === undefined
        ? "The selected service is unavailable."
        : "The parent service is unavailable. Select a service to create a child.",
    );
    update({ focusReturn: first });
  }, [
    onCreationEvent,
    onNotice,
    onSelect,
    selectedService,
    services,
    servicesReadVersion,
  ]);

  useLayoutEffect(() => {
    if (state.focusReturn === null) return;
    const name = state.focusReturn;
    const frame = globalThis.requestAnimationFrame(() => {
      serviceControl(name)?.focus();
      update({ focusReturn: null });
    });
    return () => {
      globalThis.cancelAnimationFrame(frame);
    };
  }, [state.focusReturn]);

  function firstEnteredField(): HTMLElement | null {
    const values = stateRef.current.draft;
    const name = values?.displayName !== "" ? "display_name" : "api_name";
    return (
      formRef.current?.querySelector<HTMLElement>('[name="' + name + '"]') ??
      null
    );
  }

  const activeRef = useRef(true);
  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
    };
  }, []);
  return {
    state,
    stateRef,
    update,
    busyRef,
    returnFocusRef,
    selectedControlRef,
    formRef,
    discardReturnRef,
    selected,
    firstEnteredField,
    activeRef,
  };
}

function useServiceTreeLayout(
  services: readonly Service[],
  selectedService: string,
) {
  const [size, setSize] = useState({
    nodeWidth: 176,
    nodeHeight: 72,
    verticalGap: 86,
    nodeHeights: new Map<string, number>(),
  });
  useLayoutEffect(() => {
    const nodes = [
      ...globalThis.document.querySelectorAll<HTMLElement>(
        ".service-management [data-service-api-name]",
      ),
    ];
    const action = globalThis.document.querySelector<HTMLElement>(
      ".service-management [data-service-create-action]",
    );
    const measure = () => {
      const next = {
        nodeWidth: Math.max(176, ...nodes.map((node) => node.offsetWidth)),
        nodeHeight: Math.max(72, ...nodes.map((node) => node.offsetHeight)),
        verticalGap: Math.max(86, (action?.offsetHeight ?? 0) + 16),
        nodeHeights: new Map(
          nodes.map((node) => [
            node.dataset.serviceApiName ?? "",
            node.offsetHeight,
          ]),
        ),
      };
      setSize((previous) =>
        previous.nodeWidth === next.nodeWidth &&
        previous.nodeHeight === next.nodeHeight &&
        previous.verticalGap === next.verticalGap &&
        previous.nodeHeights.size === next.nodeHeights.size &&
        [...next.nodeHeights].every(
          ([name, height]) => previous.nodeHeights.get(name) === height,
        )
          ? previous
          : next,
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    for (const node of nodes) observer.observe(node);
    if (action !== null) observer.observe(action);
    return () => {
      observer.disconnect();
    };
  }, [services, selectedService]);
  const layout = useMemo(
    () =>
      layoutTree(
        services.map((service) => ({
          id: service.api_name,
          parentId: service.parent_service_api_name ?? null,
        })),
        { direction: "vertical", padding: 36, horizontalGap: 34, ...size },
      ),
    [services, size],
  );
  return { layout, nodeSize: size };
}

export function ServiceManagement(props: ServiceManagementProps) {
  const {
    available = true,
    initialState,
    stateContent,
    graphActions,
    client,
    csrf,
    onNotice,
    onSelect,
    onOpenDetails,
    onCreationEvent,
    onTreeScroll,
    restoreTree,
    selectedService,
    services,
  } = props;

  const {
    state,
    update,
    busyRef,
    returnFocusRef,
    selectedControlRef,
    formRef,
    discardReturnRef,
    selected,
    firstEnteredField,
    activeRef,
  } = useServiceCreationState(props);
  const {
    busy,
    draft,
    closedSelection,
    deletedReturn,
    error,
    discardSelection,
  } = state;
  const { layout, nodeSize } = useServiceTreeLayout(services, selectedService);

  function closeCreate(): void {
    if (busyRef.current) return;
    onCreationEvent?.({ type: "closed" });
    update({
      draft: null,
      error: null,
      discardSelection: null,
      closedSelection: selectedService,
    });
  }
  function selectService(name: string, trigger?: HTMLElement): void {
    if (busyRef.current) return;
    if (draft !== null && name === draft.parent.api_name) {
      formRef.current
        ?.closest("dialog")
        ?.querySelector<HTMLElement>("h2")
        ?.focus();
      return;
    }
    if (draft !== null && (draft.displayName !== "" || draft.apiName !== "")) {
      discardReturnRef.current = firstEnteredField();
      update({ discardSelection: name });
      return;
    }
    completeSelection(name, trigger);
  }
  function completeSelection(name: string, trigger?: HTMLElement): void {
    update({ focusReturn: null });
    selectedControlRef.current = trigger ?? serviceControl(name);
    onCreationEvent?.({ type: "closed" });
    update({
      draft: null,
      error: null,
      discardSelection: null,
      closedSelection: null,
      deletedReturn: false,
      selection: name,
    });
    onSelect(name);
  }
  async function create(event: SubmitEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busyRef.current || draft === null) return;
    const invalid =
      event.currentTarget.querySelector<HTMLInputElement>("input:invalid");
    const field =
      draft.displayName.trim() === "" ? "display_name" : invalid?.name;
    if (field === "display_name" || field === "api_name") {
      update({
        error: {
          message:
            field === "display_name"
              ? "Enter a display name."
              : "Enter an API name with 1 through 63 lowercase letters, digits, or hyphens. Start with a letter and end with a letter or digit.",
          field,
        },
      });
      return;
    }
    busyRef.current = true;
    onCreationEvent?.({ type: "started" });
    update({ busy: true, error: null });
    try {
      // react-doctor-disable-next-line react-doctor/async-defer-await -- Session expiry can unmount this route during the write. Check its active state after the response before changing selection or history.
      const child = await client.createService(
        {
          api_name: draft.apiName.trim(),
          display_name: draft.displayName.trim(),
          parent_service_api_name: draft.parent.api_name,
        },
        csrf,
      );
      if (!activeRef.current) return;
      onCreationEvent?.({ type: "succeeded", service: child });
      update({
        busy: false,
        draft: null,
        error: null,
        closedSelection: null,
        deletedReturn: false,
        selection: child.api_name,
      });
      onSelect(child.api_name);
      onNotice("success", "The service was created.");
    } catch (caught) {
      if (!activeRef.current) return;
      const parentUnavailable =
        caught instanceof AdministrationApiError &&
        caught.status === 404 &&
        caught.code === "not_found" &&
        caught.message === "Parent service was not found.";
      onCreationEvent?.({ type: "failed", parentUnavailable });
      if (parentUnavailable) {
        const first =
          visibleTreeOrder(services).find(
            (service) => service.api_name !== draft.parent.api_name,
          )?.api_name ?? "";
        onCreationEvent?.({ type: "closed" });
        update({
          busy: false,
          draft: null,
          error: null,
          closedSelection: first,
          selection: first,
        });
        onSelect(first);
        onNotice(
          "error",
          "The parent service is unavailable. Select a service to create a child.",
        );
        update({ focusReturn: first });
      } else {
        const field =
          caught instanceof AdministrationApiError
            ? caught.details?.field
            : undefined;
        update({
          busy: false,
          error: {
            message: errorMessage(caught),
            ...(field === "api_name" || field === "display_name"
              ? { field }
              : {}),
          },
        });
      }
    } finally {
      busyRef.current = false;
    }
  }
  function openDetails(name: string): void {
    if (busyRef.current || !available) return;
    onOpenDetails(name);
  }
  const inspector =
    draft !== null ? (
      <CreateServiceInspector
        busy={busy}
        error={error}
        draft={draft}
        formRef={formRef}
        onChange={(field, value) => {
          update({ draft: { ...draft, [field]: value }, error: null });
        }}
        onClose={closeCreate}
        onSubmit={(event) => {
          void create(event);
        }}
        returnFocusRef={returnFocusRef}
      />
    ) : selected === null ||
      deletedReturn ||
      closedSelection === selectedService ? undefined : (
      <CompactServiceInspector
        selected={selected}
        services={services}
        onClose={() => {
          update({ closedSelection: selectedService });
        }}
        onOpenDetails={openDetails}
        returnFocusRef={selectedControlRef}
      />
    );
  return (
    <PageSurface className="service-management" edgeToEdge>
      <span className="od-visually-hidden" role="status">
        {busy ? "Creating service" : ""}
      </span>
      <ServiceGraph
        available={available}
        initialState={available ? undefined : initialState}
        stateContent={stateContent}
        graphActions={graphActions}
        inspector={inspector}
        layout={layout}
        nodeSize={nodeSize}
        onCreate={(trigger) => {
          if (busyRef.current || selected === null) return;
          if (draft !== null) {
            formRef.current
              ?.closest("dialog")
              ?.querySelector<HTMLElement>("h2")
              ?.focus();
            return;
          }
          returnFocusRef.current = trigger;
          const parent = { ...selected };
          onCreationEvent?.({ type: "opened", parent });
          update({
            draft: { parent, displayName: "", apiName: "" },
            error: null,
          });
        }}
        onOpenDetails={openDetails}
        onTreeScroll={onTreeScroll}
        restoreTree={restoreTree}
        onSelect={selectService}
        selectionLocked={!available || busy}
        selectedControlRef={selectedControlRef}
        selectedService={selectedService}
        services={services}
      />
      <ConfirmationDialog
        open={discardSelection !== null}
        title="Discard service values?"
        description="The new service has not been created. Discard the entered values to select another service."
        confirmLabel="Discard values"
        cancelLabel="Keep editing"
        returnFocusRef={discardReturnRef}
        onCancel={() => {
          update({ discardSelection: null });
        }}
        onConfirm={() => {
          if (discardSelection !== null) completeSelection(discardSelection);
        }}
      />
    </PageSurface>
  );
}
