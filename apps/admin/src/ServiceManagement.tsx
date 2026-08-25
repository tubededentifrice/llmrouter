import {
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  type SubmitEvent,
} from "react";
import {
  Button,
  ConfirmationDialog,
  DateTime,
  EditableTable,
  FormActions,
  FormField,
  FormSection,
  GraphEdge,
  GraphEdges,
  GraphEmptyState,
  GraphInspector,
  GraphNode,
  GraphToolbar,
  GraphViewport,
  GraphWorkspace,
  Icon,
  InlineAlert,
  PageSurface,
  SearchableSelect,
  SecretRevealPanel,
  StatePanel,
  layoutTree,
  treeEdgePath,
  type DataTableState,
  type EditableTableColumn,
  type EditableTableRow,
  type TreeLayoutResult,
} from "@opendle/ui";
import {
  errorMessage,
  type AdministrationClient,
  type Service,
  type ServiceKey,
  type Workspace,
} from "./api.js";
import {
  createScopeLoadGuard,
  protectedServiceApiName,
  reduceKeyCreationLifecycle,
  serviceInteractionLocked,
  uniqueDraftRowId,
  type KeyCreationLifecycle,
  type KeyCreationLifecycleAction,
} from "./accessState.js";

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

function focusFirstServiceControl(): void {
  const firstNode = globalThis.document.querySelector<HTMLElement>(
    ".service-management [data-service-api-name]",
  );
  const createButton = globalThis.document.querySelector<HTMLElement>(
    ".service-management .od-graph-toolbar-actions button",
  );
  (firstNode ?? createButton)?.focus();
}

type Mutate = (
  action: () => Promise<unknown>,
  message: string,
) => Promise<string | null>;
type LoadPhase = "loading" | "ready" | "error";

interface WorkspaceDraft {
  readonly apiName: string;
  readonly displayName: string;
  readonly createdAt: string | null;
}

interface KeyDraft {
  readonly name: string;
  readonly createdAt: string | null;
  readonly lastUsedAt: string | null | undefined;
}

interface ServiceAccessState {
  readonly keyDraft: KeyDraft | null;
  readonly keyPhase: LoadPhase;
  readonly keys: readonly ServiceKey[];
  readonly workspaceDraft: WorkspaceDraft | null;
  readonly workspacePhase: LoadPhase;
  readonly workspaces: readonly Workspace[];
}

type ServiceAccessPatch =
  | Partial<ServiceAccessState>
  | ((state: ServiceAccessState) => Partial<ServiceAccessState>);

function reduceServiceAccess(
  state: ServiceAccessState,
  patch: ServiceAccessPatch,
): ServiceAccessState {
  return {
    ...state,
    ...(typeof patch === "function" ? patch(state) : patch),
  };
}

const initialServiceAccess: ServiceAccessState = {
  keyDraft: null,
  keyPhase: "loading",
  keys: [],
  workspaceDraft: null,
  workspacePhase: "loading",
  workspaces: [],
};

const WORKSPACE_CREATE_ROW_ID = "__new_workspace__";
const KEY_CREATE_ROW_ID_PREFIX = "__new_service_key__";

const workspaceColumns: readonly EditableTableColumn<WorkspaceDraft>[] = [
  {
    key: "display-name",
    header: "Workspace",
    width: "42%",
    renderRead: ({ row }) => row.draft.displayName,
    renderEdit: ({ row, update, validationId, errorId }) => (
      <input
        aria-describedby={`${validationId} ${errorId}`}
        aria-label="Workspace display name"
        maxLength={200}
        onChange={(event) => {
          update({ displayName: event.currentTarget.value });
        }}
        required
        value={row.draft.displayName}
      />
    ),
  },
  {
    key: "api-name",
    header: "API name",
    width: "34%",
    renderRead: ({ row }) => <code>{row.draft.apiName}</code>,
    renderEdit: ({ row, update, validationId, errorId }) => (
      <input
        aria-describedby={`${validationId} ${errorId}`}
        aria-label="Workspace API name"
        maxLength={63}
        onChange={(event) => {
          update({ apiName: event.currentTarget.value });
        }}
        pattern="[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
        required
        value={row.draft.apiName}
      />
    ),
  },
  {
    key: "created",
    header: "Created",
    width: "24%",
    renderRead: ({ row }) => <ServiceDateTime value={row.draft.createdAt} />,
    renderEdit: () => "After creation",
  },
];

const keyColumns: readonly EditableTableColumn<KeyDraft>[] = [
  {
    key: "name",
    header: "Name",
    width: "48%",
    renderRead: ({ row }) => row.draft.name,
    renderEdit: ({ row, update, validationId, errorId }) => (
      <input
        aria-describedby={`${validationId} ${errorId}`}
        aria-label="Key name"
        maxLength={200}
        onChange={(event) => {
          update({ name: event.currentTarget.value });
        }}
        required
        value={row.draft.name}
      />
    ),
  },
  {
    key: "created",
    header: "Created",
    width: "26%",
    renderRead: ({ row }) => <ServiceDateTime value={row.draft.createdAt} />,
    renderEdit: () => "After creation",
  },
  {
    key: "last-use",
    header: "Last use",
    width: "26%",
    renderRead: ({ row }) => <ServiceDateTime value={row.draft.lastUsedAt} />,
    renderEdit: () => "Never",
  },
];

function tableState(
  phase: LoadPhase,
  rowCount: number,
  loadingMessage: string,
  emptyMessage: string,
  unavailableMessage: string,
  onRetry: () => Promise<void>,
): DataTableState {
  if (phase === "loading") return { kind: "loading", message: loadingMessage };
  if (phase === "error")
    return {
      kind: "error",
      message: unavailableMessage,
      retryLabel: "Try again",
      onRetry,
    };
  if (rowCount === 0) return { kind: "empty", message: emptyMessage };
  return { kind: "ready" };
}

type NoticeHandler = (tone: "success" | "error", message: string) => void;

function OneTimeKey({
  onClear,
  onNotice,
  secret,
}: {
  readonly onClear: () => void;
  readonly onNotice: NoticeHandler;
  readonly secret: string;
}) {
  return (
    <SecretRevealPanel
      copiedLabel="Key copied"
      copyLabel="Copy key"
      copySecret={async (value) => {
        await navigator.clipboard.writeText(value);
        onNotice("success", "The key was copied.");
      }}
      description="The Router will not show it again. Deploy it to the service backend, and then clear this value."
      dismissLabel="Clear key"
      headingLevel="h3"
      onCopyError={() => {
        onNotice(
          "error",
          "The browser could not copy the key. Select and copy it manually.",
        );
      }}
      onDismiss={onClear}
      secret={secret}
      secretLabel="Service API key"
      title="Copy this key now"
    />
  );
}

function WorkspaceAccessSection({
  client,
  csrf,
  load,
  onMutationBegin,
  onMutationEnd,
  onNotice,
  phase,
  rows,
  service,
  update,
  workspaces,
  workspaceDraft,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly load: () => Promise<void>;
  readonly onMutationBegin: () => boolean;
  readonly onMutationEnd: () => void;
  readonly onNotice: NoticeHandler;
  readonly phase: LoadPhase;
  readonly rows: readonly EditableTableRow<WorkspaceDraft>[];
  readonly service: Service;
  readonly update: Dispatch<ServiceAccessPatch>;
  readonly workspaces: readonly Workspace[];
  readonly workspaceDraft: WorkspaceDraft | null;
}) {
  return (
    <section
      aria-labelledby="service-workspaces-title"
      className="service-access-section"
    >
      <div className="service-access-heading">
        <div>
          <h3 id="service-workspaces-title">Workspaces</h3>
          <p>Accounting labels for this service.</p>
        </div>
        <Button
          disabled={workspaceDraft !== null || phase === "loading"}
          onClick={() => {
            update({
              workspaceDraft: {
                apiName: "",
                displayName: "",
                createdAt: "",
              },
            });
          }}
          variant="secondary"
        >
          Create workspace
        </Button>
      </div>
      <EditableTable
        ariaLabel={`Workspaces for ${service.display_name}`}
        columns={workspaceColumns}
        density="compact"
        deleteLabel="Delete"
        getDeleteConfirmation={(row) => ({
          title: `Delete workspace ${row.draft.apiName}?`,
          description:
            "This action deletes its logs, accounting, jobs, uploaded images, and retained generated media.",
          confirmLabel: "Delete workspace",
          impactStatement: `Workspace ${row.draft.apiName} will be deleted.`,
        })}
        minimumWidth="31rem"
        onCancel={() => {
          update({ workspaceDraft: null });
        }}
        onCreate={async (_rowId, draft) => {
          if (!onMutationBegin())
            throw new Error(
              "Wait for the current service request to finish before you create a workspace.",
            );
          try {
            const created = await client.createWorkspace(
              service.api_name,
              {
                api_name: draft.apiName.trim(),
                display_name: draft.displayName.trim(),
              },
              csrf,
            );
            update((current) => ({
              workspaces: [...current.workspaces, created],
              workspaceDraft: null,
              workspacePhase: "ready",
            }));
            onNotice("success", "The workspace was created.");
            return created.api_name;
          } catch (error) {
            const message = errorMessage(error);
            onNotice("error", message);
            throw new Error(message);
          } finally {
            onMutationEnd();
          }
        }}
        onDelete={async (rowId) => {
          if (!onMutationBegin())
            throw new Error(
              "Wait for the current service request to finish before you delete a workspace.",
            );
          try {
            await client.deleteWorkspace(service.api_name, rowId, csrf);
            update((current) => ({
              workspaces: current.workspaces.filter(
                (workspace) => workspace.api_name !== rowId,
              ),
            }));
            onNotice("success", "The workspace was deleted.");
          } catch (error) {
            const message = errorMessage(error);
            onNotice("error", message);
            throw new Error(message);
          } finally {
            onMutationEnd();
          }
        }}
        onDraftChange={(_rowId, patch) => {
          update((current) => ({
            workspaceDraft:
              current.workspaceDraft === null
                ? null
                : { ...current.workspaceDraft, ...patch },
          }));
        }}
        rows={rows}
        saveLabel="Create"
        saveMode="explicit"
        state={tableState(
          phase,
          rows.length,
          "Loading workspaces…",
          "This service has no workspaces.",
          workspaces.length === 0
            ? "The workspaces are unavailable."
            : "The Router could not refresh the workspaces. The current records remain visible.",
          load,
        )}
        validate={(row) => {
          if (!row.isNew) return undefined;
          if (row.draft.apiName.trim() === "") return "Enter an API name.";
          if (row.draft.displayName.trim() === "")
            return "Enter a display name.";
          return undefined;
        }}
      />
    </section>
  );
}

function KeyAccessSection({
  client,
  csrf,
  keyDraft,
  keyLifecycleActive,
  keys,
  load,
  onKeyCreated,
  onKeyCreationBegin,
  onKeyCreationFailed,
  onMutationBegin,
  onMutationEnd,
  onNotice,
  phase,
  rows,
  service,
  update,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly keyDraft: KeyDraft | null;
  readonly keyLifecycleActive: boolean;
  readonly keys: readonly ServiceKey[];
  readonly load: () => Promise<void>;
  readonly onKeyCreated: (serviceApiName: string, secret: string) => void;
  readonly onKeyCreationBegin: (serviceApiName: string) => boolean;
  readonly onKeyCreationFailed: (serviceApiName: string) => void;
  readonly onMutationBegin: () => boolean;
  readonly onMutationEnd: () => void;
  readonly onNotice: NoticeHandler;
  readonly phase: LoadPhase;
  readonly rows: readonly EditableTableRow<KeyDraft>[];
  readonly service: Service;
  readonly update: Dispatch<ServiceAccessPatch>;
}) {
  return (
    <section
      aria-labelledby="service-keys-title"
      className="service-access-section"
    >
      <div className="service-access-heading">
        <div>
          <h3 id="service-keys-title">Service API keys</h3>
          <p>Backend-only bearer credentials with full service authority.</p>
        </div>
        <Button
          disabled={
            keyDraft !== null || phase === "loading" || keyLifecycleActive
          }
          onClick={() => {
            update({
              keyDraft: { name: "", createdAt: "", lastUsedAt: "" },
            });
          }}
          variant="secondary"
        >
          Create key
        </Button>
      </div>
      <EditableTable
        ariaLabel={`Service API keys for ${service.display_name}`}
        columns={keyColumns}
        density="compact"
        deleteLabel="Revoke"
        getDeleteConfirmation={(row) => ({
          title: `Revoke service API key ${row.draft.name}?`,
          description:
            "Each later request that uses this key will fail authentication.",
          confirmLabel: "Revoke key",
          impactStatement: `Key ${row.draft.name} will stop working.`,
        })}
        minimumWidth="31rem"
        onCancel={() => {
          update({ keyDraft: null });
        }}
        onCreate={async (_rowId, draft) => {
          if (!onMutationBegin())
            throw new Error(
              "Wait for the current service request to finish before you create a key.",
            );
          try {
            if (!onKeyCreationBegin(service.api_name))
              throw new Error(
                "Copy and clear the current one-time key before you create another key.",
              );
            const created = await client.createKey(
              service.api_name,
              draft.name.trim(),
              csrf,
            );
            update((current) => ({
              keys: [...current.keys, created.key],
              keyDraft: null,
              keyPhase: "ready",
            }));
            onKeyCreated(service.api_name, created.secret);
            onNotice("success", "The service API key was created.");
            return created.key.id;
          } catch (error) {
            onKeyCreationFailed(service.api_name);
            const message = errorMessage(error);
            onNotice("error", message);
            throw new Error(message);
          } finally {
            onMutationEnd();
          }
        }}
        onDelete={async (rowId) => {
          if (!onMutationBegin())
            throw new Error(
              "Wait for the current service request to finish before you revoke a key.",
            );
          try {
            await client.revokeKey(service.api_name, rowId, csrf);
            update((current) => ({
              keys: current.keys.filter((key) => key.id !== rowId),
            }));
            onNotice("success", "The service API key was revoked.");
          } catch (error) {
            const message = errorMessage(error);
            onNotice("error", message);
            throw new Error(message);
          } finally {
            onMutationEnd();
          }
        }}
        onDraftChange={(_rowId, patch) => {
          update((current) => ({
            keyDraft:
              current.keyDraft === null
                ? null
                : { ...current.keyDraft, ...patch },
          }));
        }}
        rows={rows}
        saveLabel="Create"
        saveMode="explicit"
        state={tableState(
          phase,
          rows.length,
          "Loading service API keys…",
          "This service has no active API keys.",
          keys.length === 0
            ? "The service API keys are unavailable."
            : "The Router could not refresh the keys. The current records remain visible.",
          load,
        )}
        validate={(row) =>
          row.isNew && row.draft.name.trim() === ""
            ? "Enter a key name."
            : undefined
        }
      />
    </section>
  );
}

function ServiceAccessSections({
  client,
  csrf,
  keyLifecycle,
  onClearKey,
  onKeyCreated,
  onKeyCreationBegin,
  onKeyCreationFailed,
  onMutationBegin,
  onMutationEnd,
  onNotice,
  service,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly keyLifecycle: KeyCreationLifecycle | null;
  readonly onClearKey: () => void;
  readonly onKeyCreated: (serviceApiName: string, secret: string) => void;
  readonly onKeyCreationBegin: (serviceApiName: string) => boolean;
  readonly onKeyCreationFailed: (serviceApiName: string) => void;
  readonly onMutationBegin: () => boolean;
  readonly onMutationEnd: () => void;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly service: Service;
}) {
  const [access, updateAccess] = useReducer(
    reduceServiceAccess,
    initialServiceAccess,
  );
  const {
    keyDraft,
    keyPhase,
    keys,
    workspaceDraft,
    workspacePhase,
    workspaces,
  } = access;
  const workspaceLoadGuard = useRef(createScopeLoadGuard());
  const keyLoadGuard = useRef(createScopeLoadGuard());

  async function loadWorkspaces(): Promise<void> {
    const generation = workspaceLoadGuard.current.begin();
    updateAccess({ workspacePhase: "loading" });
    try {
      // react-doctor-disable-next-line react-doctor/async-defer-await -- The continuation must reject a stale service load after this request settles.
      const page = await client.workspaces(service.api_name);
      if (!workspaceLoadGuard.current.isCurrent(generation)) return;
      updateAccess({ workspaces: page.items, workspacePhase: "ready" });
    } catch (error) {
      if (!workspaceLoadGuard.current.isCurrent(generation)) return;
      updateAccess({ workspacePhase: "error" });
      onNotice("error", errorMessage(error));
    }
  }

  async function loadKeys(): Promise<void> {
    const generation = keyLoadGuard.current.begin();
    updateAccess({ keyPhase: "loading" });
    try {
      // react-doctor-disable-next-line react-doctor/async-defer-await -- The continuation must reject a stale service load after this request settles.
      const page = await client.keys(service.api_name);
      if (!keyLoadGuard.current.isCurrent(generation)) return;
      updateAccess({ keys: page.items, keyPhase: "ready" });
    } catch (error) {
      if (!keyLoadGuard.current.isCurrent(generation)) return;
      updateAccess({ keyPhase: "error" });
      onNotice("error", errorMessage(error));
    }
  }

  useEffect(() => {
    const workspaceGuard = workspaceLoadGuard.current;
    const keyGuard = keyLoadGuard.current;
    const workspaceGeneration = workspaceGuard.begin();
    const keyGeneration = keyGuard.begin();
    void client
      .workspaces(service.api_name)
      .then((page) => {
        if (!workspaceGuard.isCurrent(workspaceGeneration)) return;
        updateAccess({ workspaces: page.items, workspacePhase: "ready" });
      })
      .catch((error: unknown) => {
        if (!workspaceGuard.isCurrent(workspaceGeneration)) return;
        updateAccess({ workspacePhase: "error" });
        onNotice("error", errorMessage(error));
      });
    void client
      .keys(service.api_name)
      .then((page) => {
        if (!keyGuard.isCurrent(keyGeneration)) return;
        updateAccess({ keys: page.items, keyPhase: "ready" });
      })
      .catch((error: unknown) => {
        if (!keyGuard.isCurrent(keyGeneration)) return;
        updateAccess({ keyPhase: "error" });
        onNotice("error", errorMessage(error));
      });
    return () => {
      workspaceGuard.invalidate();
      keyGuard.invalidate();
    };
  }, [client, onNotice, service.api_name]);

  const workspaceRows = useMemo<readonly EditableTableRow<WorkspaceDraft>[]>(
    () => [
      ...workspaces.map((workspace) => ({
        id: workspace.api_name,
        label: workspace.display_name,
        draft: {
          apiName: workspace.api_name,
          displayName: workspace.display_name,
          createdAt: workspace.created_at,
        },
      })),
      ...(workspaceDraft === null
        ? []
        : [
            {
              id: WORKSPACE_CREATE_ROW_ID,
              label: "New workspace",
              draft: workspaceDraft,
              dirty: true,
              isNew: true,
            },
          ]),
    ],
    [workspaceDraft, workspaces],
  );
  const keyRows = useMemo<readonly EditableTableRow<KeyDraft>[]>(() => {
    const keyCreateRowId = uniqueDraftRowId(
      keys.map((key) => key.id),
      KEY_CREATE_ROW_ID_PREFIX,
    );
    return [
      ...keys.map((key) => ({
        id: key.id,
        label: key.name,
        draft: {
          name: key.name,
          createdAt: key.created_at,
          lastUsedAt: key.last_used_at,
        },
      })),
      ...(keyDraft === null
        ? []
        : [
            {
              id: keyCreateRowId,
              label: "New service API key",
              draft: keyDraft,
              dirty: true,
              isNew: true,
            },
          ]),
    ];
  }, [keyDraft, keys]);

  return (
    <div className="service-access-sections">
      {keyLifecycle?.phase === "pending" ? (
        <StatePanel kind="loading" title="Creating the service API key">
          Keep this service open. The one-time key will appear here.
        </StatePanel>
      ) : null}
      {keyLifecycle?.phase !== "shown" ? null : (
        <OneTimeKey
          onClear={onClearKey}
          onNotice={onNotice}
          secret={keyLifecycle.secret}
        />
      )}
      <WorkspaceAccessSection
        client={client}
        csrf={csrf}
        load={loadWorkspaces}
        onMutationBegin={onMutationBegin}
        onMutationEnd={onMutationEnd}
        onNotice={onNotice}
        phase={workspacePhase}
        rows={workspaceRows}
        service={service}
        update={updateAccess}
        workspaces={workspaces}
        workspaceDraft={workspaceDraft}
      />
      <KeyAccessSection
        client={client}
        csrf={csrf}
        keyDraft={keyDraft}
        keyLifecycleActive={keyLifecycle !== null}
        keys={keys}
        load={loadKeys}
        onKeyCreated={onKeyCreated}
        onKeyCreationBegin={onKeyCreationBegin}
        onKeyCreationFailed={onKeyCreationFailed}
        onMutationBegin={onMutationBegin}
        onMutationEnd={onMutationEnd}
        onNotice={onNotice}
        phase={keyPhase}
        rows={keyRows}
        service={service}
        update={updateAccess}
      />
    </div>
  );
}

function ServiceInspector({
  accessPending,
  busy,
  csrf,
  client,
  keyLifecycle,
  mutate,
  onClearKey,
  onClose,
  onDeleted,
  onKeyCreated,
  onKeyCreationBegin,
  onKeyCreationFailed,
  onMutationBegin,
  onMutationEnd,
  onNotice,
  returnFocusRef,
  selected,
  services,
}: {
  readonly accessPending: boolean;
  readonly busy: boolean;
  readonly csrf: string;
  readonly client: AdministrationClient;
  readonly keyLifecycle: KeyCreationLifecycle | null;
  readonly mutate: Mutate;
  readonly onClearKey: () => void;
  readonly onClose: () => void;
  readonly onDeleted: () => void;
  readonly onKeyCreated: (serviceApiName: string, secret: string) => void;
  readonly onKeyCreationBegin: (serviceApiName: string) => boolean;
  readonly onKeyCreationFailed: (serviceApiName: string) => void;
  readonly onMutationBegin: () => boolean;
  readonly onMutationEnd: () => void;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly returnFocusRef: RefObject<HTMLElement | null>;
  readonly selected: Service;
  readonly services: readonly Service[];
}) {
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deletePending, setDeletePending] = useState(false);
  const [parentSelection, setParentSelection] = useReducer(
    (_current: string, next: string) => next,
    selected.parent_service_api_name ?? NO_PARENT_OPTION,
  );
  useEffect(() => {
    setParentSelection(selected.parent_service_api_name ?? NO_PARENT_OPTION);
  }, [selected.api_name, selected.parent_service_api_name]);
  const keyLifecycleActive = keyLifecycle !== null;
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
      activationKey={selected.api_name}
      eyebrow={
        selected.parent_service_api_name == null
          ? "Root service"
          : "Child service"
      }
      {...(keyLifecycleActive || accessPending || busy ? {} : { onClose })}
      returnFocusRef={returnFocusRef}
      title={selected.display_name}
      tone="lime"
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
            <ServiceDateTime value={selected.created_at} />
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
          ).then(setMutationError);
        }}
      >
        <FormSection legend="Service details">
          <FormField label="Display name" requirement="required">
            <input
              defaultValue={selected.display_name}
              maxLength={200}
              name="display_name"
              required
            />
          </FormField>
          <SearchableSelect
            label="Parent service"
            onChange={(value) => {
              setParentSelection(value);
            }}
            options={[
              { label: "No parent", value: NO_PARENT_OPTION },
              ...parentOptions.map((item) => ({
                description: item.api_name,
                label: item.display_name,
                value: item.api_name,
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
          <Button
            disabled={busy || accessPending || keyLifecycleActive}
            type="submit"
          >
            Save service
          </Button>
        </FormActions>
      </form>
      {mutationError === null ? null : (
        <InlineAlert title="The service change failed" tone="error">
          {mutationError} Correct the values and try again.
        </InlineAlert>
      )}
      <ServiceAccessSections
        client={client}
        csrf={csrf}
        keyLifecycle={keyLifecycle}
        onClearKey={onClearKey}
        onKeyCreated={onKeyCreated}
        onKeyCreationBegin={onKeyCreationBegin}
        onKeyCreationFailed={onKeyCreationFailed}
        onMutationBegin={onMutationBegin}
        onMutationEnd={onMutationEnd}
        onNotice={onNotice}
        service={selected}
      />
      <section
        aria-labelledby="delete-service-title"
        className="service-delete-section"
      >
        <h3 id="delete-service-title">Delete service</h3>
        <Button
          disabled={busy || accessPending || hasChildren || keyLifecycleActive}
          onClick={() => {
            setDeleteError(null);
            setDeleteOpen(true);
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
      </section>
      <ConfirmationDialog
        confirmLabel="Delete service"
        description={
          <>
            <p>
              This action deletes the service, keys, workspaces, assignments,
              logs, accounting, jobs, and retained media.
            </p>
            {deleteError === null ? null : (
              <InlineAlert title="The service was not deleted" tone="error">
                {deleteError} Correct the problem and try again.
              </InlineAlert>
            )}
          </>
        }
        impactStatement={`Service ${selected.api_name} will be deleted.`}
        onCancel={() => {
          setDeleteOpen(false);
          setDeleteError(null);
        }}
        onConfirm={() => {
          if (keyLifecycleActive) return;
          setDeletePending(true);
          void mutate(
            () => client.deleteService(selected.api_name, csrf),
            "The service was deleted.",
          ).then((message) => {
            setDeletePending(false);
            setDeleteError(message);
            if (message === null) {
              setDeleteOpen(false);
              onDeleted();
            }
          });
        }}
        open={deleteOpen}
        pending={deletePending}
        pendingLabel="Deleting service…"
        title={`Delete service ${selected.api_name}?`}
      />
    </GraphInspector>
  );
}

export function MissingProtectedKeyInspector({
  keyLifecycle,
  onClearKey,
  onNotice,
}: {
  readonly keyLifecycle: KeyCreationLifecycle;
  readonly onClearKey: () => void;
  readonly onNotice: NoticeHandler;
}) {
  return (
    <GraphInspector
      activationKey={`protected-key-${keyLifecycle.serviceApiName}`}
      eyebrow="Service API key"
      title={keyLifecycle.serviceApiName}
      tone="lime"
    >
      <StatePanel kind="error" title="The service record is unavailable">
        A concurrent refresh removed this service from the graph. The Router
        keeps this key operation here so that its one-time value is not lost.
      </StatePanel>
      {keyLifecycle.phase === "pending" ? (
        <StatePanel kind="loading" title="Creating the service API key">
          Wait for this request to finish. The one-time key will appear here.
        </StatePanel>
      ) : (
        <OneTimeKey
          onClear={onClearKey}
          onNotice={onNotice}
          secret={keyLifecycle.secret}
        />
      )}
    </GraphInspector>
  );
}

function ServiceGraph({
  inspector,
  layout,
  onCreate,
  onSelect,
  selectionLocked,
  selectedService,
  services,
}: {
  readonly inspector: ReactNode;
  readonly layout: TreeLayoutResult;
  readonly onCreate: (trigger: HTMLButtonElement) => void;
  readonly onSelect: (name: string, trigger: HTMLButtonElement) => void;
  readonly selectionLocked: boolean;
  readonly selectedService: string;
  readonly services: readonly Service[];
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
    rovingState.activeNode !== "" &&
    !services.some((service) => service.api_name === rovingState.activeNode);
  const resetActiveNode = selectionChanged || activeNodeUnavailable;
  const activeNode = resetActiveNode ? initialActive : rovingState.activeNode;
  if (resetActiveNode)
    setRovingState({ selection: selectedService, activeNode: initialActive });
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
      aria-label="Services and parent relationships"
      inspector={inspector}
      toolbar={
        <GraphToolbar
          actions={
            <Button
              disabled={selectionLocked}
              onClick={(event) => {
                onCreate(event.currentTarget);
              }}
            >
              <Icon name="plus" size={16} /> Create service
            </Button>
          }
        />
      }
    >
      <GraphViewport
        aria-label="Service tree canvas"
        canvasAlignment="center"
        canvasHeight={height}
        canvasProps={{
          "aria-label": `${String(services.length)} services in parent order`,
        }}
        canvasWidth={width}
      >
        {services.length === 0 ? (
          <GraphEmptyState
            description="Create a root service to start the service tree."
            icon={<Icon name="layers" />}
            title="No services"
          />
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
              onClick={(event) => {
                if (selectionLocked) return;
                onSelect(service.api_name, event.currentTarget);
              }}
              onFocus={() => {
                setRovingState({
                  selection: selectedService,
                  activeNode: service.api_name,
                });
              }}
              onKeyDown={(event) => {
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
  inputRef,
  onClose,
  onSubmit,
  returnFocusRef,
  services,
}: {
  readonly busy: boolean;
  readonly error: string | null;
  readonly inputRef: RefObject<HTMLInputElement | null>;
  readonly onClose: () => void;
  readonly onSubmit: (event: SubmitEvent<HTMLFormElement>) => void;
  readonly returnFocusRef: RefObject<HTMLElement | null>;
  readonly services: readonly Service[];
}) {
  const [parentSelection, setParentSelection] = useState(NO_PARENT_OPTION);
  return (
    <GraphInspector
      activationKey="create-service"
      closeLabel="Close create service"
      eyebrow="Service tree"
      initialFocusRef={inputRef}
      {...(busy ? {} : { onClose })}
      returnFocusRef={returnFocusRef}
      title="Create service"
      tone="lime"
    >
      <form className="service-create-form" onSubmit={onSubmit}>
        <FormSection legend="Service details">
          <FormField label="API name" requirement="required">
            <input
              maxLength={63}
              name="api_name"
              pattern="[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
              ref={inputRef}
              required
            />
          </FormField>
          <FormField label="Display name" requirement="required">
            <input maxLength={200} name="display_name" required />
          </FormField>
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
        <InlineAlert title="The service was not created" tone="error">
          {error} Correct the values and try again.
        </InlineAlert>
      )}
    </GraphInspector>
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
  const [accessPendingCount, changeAccessPendingCount] = useReducer(
    (current: number, change: 1 | -1) => Math.max(0, current + change),
    0,
  );
  const [showCreate, setShowCreate] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [keyLifecycle, setKeyLifecycle] = useState<KeyCreationLifecycle | null>(
    null,
  );
  const busyRef = useRef(false);
  const accessPendingCountRef = useRef(0);
  const keyLifecycleRef = useRef<KeyCreationLifecycle | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const createInputRef = useRef<HTMLInputElement | null>(null);
  const protectedService = protectedServiceApiName(
    selectedService,
    keyLifecycle,
  );
  const selected =
    services.find((item) => item.api_name === protectedService) ?? null;
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
    setShowCreate(false);
    setCreateError(null);
    returnFocusRef.current = null;
  }

  function closeInspector(): void {
    if (busyRef.current) {
      onNotice(
        "error",
        "Wait for the service request to finish before you close this inspector.",
      );
      return;
    }
    if (keyLifecycleRef.current !== null) {
      onNotice(
        "error",
        keyLifecycleRef.current.phase === "pending"
          ? "Wait for the key request to finish before you close this service."
          : "Copy and clear the one-time key before you close this service.",
      );
      return;
    }
    if (accessPendingCountRef.current > 0) {
      onNotice(
        "error",
        "Wait for each workspace or key request to finish before you close this service.",
      );
      return;
    }
    onSelect("");
    returnFocusRef.current = null;
  }

  function changeKeyLifecycle(
    action: KeyCreationLifecycleAction,
  ): KeyCreationLifecycle | null {
    const next = reduceKeyCreationLifecycle(keyLifecycleRef.current, action);
    keyLifecycleRef.current = next;
    setKeyLifecycle(next);
    return next;
  }

  async function mutate(
    action: () => Promise<unknown>,
    message: string,
  ): Promise<string | null> {
    if (
      serviceInteractionLocked(
        busyRef.current,
        accessPendingCountRef.current,
        keyLifecycleRef.current,
      )
    ) {
      const correction =
        "Wait for the current service request to finish before you try again.";
      onNotice("error", correction);
      return correction;
    }
    busyRef.current = true;
    setBusy(true);
    try {
      await action();
      await onRefresh();
      onNotice("success", message);
      return null;
    } catch (error) {
      const correction = errorMessage(error);
      onNotice("error", correction);
      return correction;
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  async function create(event: SubmitEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const parent = formText(form, "parent");
    const result = await mutate(
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
    setCreateError(result);
    if (result === null) closeCreate();
  }

  const inspector =
    keyLifecycle !== null && selected === null ? (
      <MissingProtectedKeyInspector
        keyLifecycle={keyLifecycle}
        onClearKey={() => {
          changeKeyLifecycle({ type: "clear" });
          globalThis.requestAnimationFrame(focusFirstServiceControl);
        }}
        onNotice={onNotice}
      />
    ) : showCreate ? (
      <CreateServiceInspector
        busy={busy}
        error={createError}
        inputRef={createInputRef}
        onClose={closeCreate}
        onSubmit={(event) => {
          void create(event);
        }}
        returnFocusRef={returnFocusRef}
        services={services}
      />
    ) : selected === null ? undefined : (
      <ServiceInspector
        accessPending={accessPendingCount > 0}
        busy={busy}
        client={client}
        csrf={csrf}
        keyLifecycle={keyLifecycle}
        key={selected.api_name}
        mutate={mutate}
        onClearKey={() => {
          changeKeyLifecycle({ type: "clear" });
        }}
        onClose={closeInspector}
        onDeleted={() => {
          onSelect("");
          returnFocusRef.current = null;
          globalThis.requestAnimationFrame(() => {
            const firstNode = globalThis.document.querySelector<HTMLElement>(
              ".service-management [data-service-api-name]",
            );
            const createButton = globalThis.document.querySelector<HTMLElement>(
              ".service-management .od-graph-toolbar-actions button",
            );
            (firstNode ?? createButton)?.focus();
          });
        }}
        onKeyCreated={(serviceApiName, secret) => {
          changeKeyLifecycle({ type: "created", serviceApiName, secret });
        }}
        onKeyCreationBegin={(serviceApiName) => {
          if (keyLifecycleRef.current !== null) return false;
          changeKeyLifecycle({ type: "begin", serviceApiName });
          return true;
        }}
        onKeyCreationFailed={(serviceApiName) => {
          changeKeyLifecycle({ type: "failed", serviceApiName });
        }}
        onMutationBegin={() => {
          if (busyRef.current) return false;
          accessPendingCountRef.current += 1;
          changeAccessPendingCount(1);
          return true;
        }}
        onMutationEnd={() => {
          accessPendingCountRef.current = Math.max(
            0,
            accessPendingCountRef.current - 1,
          );
          changeAccessPendingCount(-1);
        }}
        onNotice={onNotice}
        returnFocusRef={returnFocusRef}
        selected={selected}
        services={services}
      />
    );
  return (
    <PageSurface className="service-management" edgeToEdge>
      <ServiceGraph
        inspector={inspector}
        layout={layout}
        onCreate={(trigger) => {
          if (
            serviceInteractionLocked(
              busyRef.current,
              accessPendingCountRef.current,
              keyLifecycleRef.current,
            )
          )
            return;
          returnFocusRef.current = trigger;
          setCreateError(null);
          setShowCreate(true);
        }}
        onSelect={(name, trigger) => {
          if (
            serviceInteractionLocked(
              busyRef.current,
              accessPendingCountRef.current,
              keyLifecycleRef.current,
            )
          )
            return;
          returnFocusRef.current = trigger;
          setShowCreate(false);
          setCreateError(null);
          onSelect(name);
        }}
        selectionLocked={serviceInteractionLocked(
          busy,
          accessPendingCount,
          keyLifecycle,
        )}
        selectedService={protectedService}
        services={services}
      />
    </PageSurface>
  );
}
