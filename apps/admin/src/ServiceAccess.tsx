import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
} from "react";
import {
  Button,
  DateTime,
  FormActions,
  EditableTable,
  Panel,
  PanelHeader,
  SecretRevealPanel,
  StatePanel,
  TextControl,
  type DataTableState,
  type EditableTableColumn,
  type EditableTableRow,
} from "@opendle/ui";
import {
  errorMessage,
  type AdministrationClient,
  type Service,
  type ServiceKey,
  type Workspace,
} from "./api.ts";
import { createScopeLoadGuard, uniqueDraftRowId } from "./accessState.ts";
function ServiceDateTime({
  value,
}: {
  readonly value: string | null | undefined;
}) {
  return <DateTime fallback={value ?? "Never"} value={value} />;
}
function workspaceMetadata(value: Workspace): Workspace {
  return {
    api_name: value.api_name,
    display_name: value.display_name,
    created_at: value.created_at,
  };
}
function keyMetadata(value: ServiceKey): ServiceKey {
  return {
    id: value.id,
    name: value.name,
    created_at: value.created_at,
    last_used_at: value.last_used_at ?? null,
  };
}
type LoadPhase = "loading" | "ready" | "error" | "stale";

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
  readonly keyConfirmed: boolean;
  readonly keys: readonly ServiceKey[];
  readonly workspaceDraft: WorkspaceDraft | null;
  readonly workspacePhase: LoadPhase;
  readonly workspaceConfirmed: boolean;
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
  keyConfirmed: false,
  keyPhase: "loading",
  keys: [],
  workspaceDraft: null,
  workspaceConfirmed: false,
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
    renderEdit: ({ row, update, validationId, errorId, disabled }) => (
      <TextControl
        disabled={disabled}
        aria-describedby={`${validationId} ${errorId}`}
        className="editable-table-form-control"
        label={
          <span className="od-visually-hidden">Workspace display name</span>
        }
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
    renderEdit: ({ row, update, validationId, errorId, disabled }) => (
      <TextControl
        disabled={disabled}
        aria-describedby={`${validationId} ${errorId}`}
        className="editable-table-form-control"
        label={<span className="od-visually-hidden">Workspace API name</span>}
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
    renderEdit: ({ row, update, validationId, errorId, disabled }) => (
      <TextControl
        disabled={disabled}
        aria-describedby={`${validationId} ${errorId}`}
        className="editable-table-form-control"
        label={<span className="od-visually-hidden">Key name</span>}
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
  if (phase === "error" || phase === "stale")
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
      copyLabel="Copy secret"
      copySecret={async (value) => {
        await navigator.clipboard.writeText(value);
        onNotice("success", "The key was copied.");
      }}
      description="The Router will not show it again. Deploy it to the service backend, and then clear this value."
      dismissLabel="Clear secret"
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
      title="One-time secret"
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
  writable,
  isCurrent,
  readBlocked,
  rows,
  service,
  update,
  workspaceDraft,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly load: () => Promise<void>;
  readonly onMutationBegin: () => boolean;
  readonly onMutationEnd: () => void;
  readonly onNotice: NoticeHandler;
  readonly phase: LoadPhase;
  readonly writable: boolean;
  readonly isCurrent: () => boolean;
  readonly readBlocked: boolean;
  readonly rows: readonly EditableTableRow<WorkspaceDraft>[];
  readonly service: Service;
  readonly update: Dispatch<ServiceAccessPatch>;
  readonly workspaceDraft: WorkspaceDraft | null;
}) {
  return (
    <Panel aria-label="Workspaces">
      <PanelHeader
        title="Workspaces"
        actions={
          <Button
            disabled={phase === "loading" || readBlocked}
            onClick={() => {
              void load();
            }}
            variant="secondary"
          >
            Refresh workspaces
          </Button>
        }
      />
      <p>Accounting labels for this service.</p>
      <Button
        disabled={workspaceDraft !== null || !writable}
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
        {...(workspaceDraft === null
          ? {}
          : {
              onCancel: () => {
                update({ workspaceDraft: null });
              },
              onCreate: async (_rowId: string, draft: WorkspaceDraft) => {
                if (!writable || !onMutationBegin())
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
                  if (!isCurrent()) return created.api_name;
                  update((current) => ({
                    workspaces: [
                      ...current.workspaces,
                      workspaceMetadata(created),
                    ],
                    workspaceDraft: null,
                    workspacePhase: "ready",
                  }));
                  onNotice("success", "The workspace was created.");
                  return created.api_name;
                } catch (error) {
                  const message = errorMessage(error);
                  if (!isCurrent()) throw new Error(message);
                  onNotice("error", message);
                  throw new Error(message);
                } finally {
                  onMutationEnd();
                }
              },
            })}
        onDelete={async (rowId) => {
          if (!writable || !onMutationBegin())
            throw new Error(
              "Wait for the current service request to finish before you delete a workspace.",
            );
          try {
            // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a result after its route is no longer active.
            await client.deleteWorkspace(service.api_name, rowId, csrf);
            if (!isCurrent()) return;
            update((current) => ({
              workspaces: current.workspaces.filter(
                (workspace) => workspace.api_name !== rowId,
              ),
            }));
            onNotice("success", "The workspace was deleted.");
          } catch (error) {
            const message = errorMessage(error);
            if (!isCurrent()) throw new Error(message);
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
        rows={rows.map((row) => ({
          ...row,
          locked: !writable,
        }))}
        saveLabel="Create"
        saveMode="explicit"
        state={tableState(
          phase,
          rows.length,
          "Loading workspaces…",
          "This service has no workspaces.",
          phase === "stale"
            ? "The workspace records are stale. Refresh workspaces before you make changes."
            : "The workspaces are unavailable.",
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
    </Panel>
  );
}

function KeyAccessSection({
  client,
  csrf,
  keyDraft,
  keyLifecycleActive,
  load,
  onKeyCreated,
  onKeyCreationBegin,
  onKeyCreationFailed,
  onMutationBegin,
  onMutationEnd,
  onNotice,
  phase,
  writable,
  isCurrent,
  readBlocked,
  rows,
  service,
  update,
}: {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly keyDraft: KeyDraft | null;
  readonly keyLifecycleActive: boolean;
  readonly load: () => Promise<void>;
  readonly onKeyCreated: (serviceApiName: string, secret: string) => void;
  readonly onKeyCreationBegin: (serviceApiName: string) => boolean;
  readonly onKeyCreationFailed: (serviceApiName: string) => void;
  readonly onMutationBegin: () => boolean;
  readonly onMutationEnd: () => void;
  readonly onNotice: NoticeHandler;
  readonly phase: LoadPhase;
  readonly writable: boolean;
  readonly isCurrent: () => boolean;
  readonly readBlocked: boolean;
  readonly rows: readonly EditableTableRow<KeyDraft>[];
  readonly service: Service;
  readonly update: Dispatch<ServiceAccessPatch>;
}) {
  return (
    <>
      <FormActions alignment="start">
        <Button
          disabled={phase === "loading" || readBlocked}
          onClick={() => {
            void load();
          }}
          variant="secondary"
        >
          Refresh keys
        </Button>
      </FormActions>
      <p>Backend-only bearer credentials with full service authority.</p>
      <Button
        disabled={keyDraft !== null || !writable || keyLifecycleActive}
        onClick={() => {
          update({
            keyDraft: { name: "", createdAt: "", lastUsedAt: "" },
          });
        }}
        variant="secondary"
      >
        Create key
      </Button>
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
        {...(keyDraft === null
          ? {}
          : {
              onCancel: () => {
                update({ keyDraft: null });
              },
              onCreate: async (_rowId: string, draft: KeyDraft) => {
                if (!writable || !onMutationBegin())
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
                  if (!isCurrent()) return created.key.id;
                  update((current) => ({
                    keys: [...current.keys, keyMetadata(created.key)],
                    keyDraft: null,
                    keyPhase: "ready",
                  }));
                  onKeyCreated(service.api_name, created.secret);
                  onNotice("success", "The service API key was created.");
                  return created.key.id;
                } catch (error) {
                  onKeyCreationFailed(service.api_name);
                  const message = errorMessage(error);
                  if (!isCurrent()) throw new Error(message);
                  onNotice("error", message);
                  throw new Error(message);
                } finally {
                  onMutationEnd();
                }
              },
            })}
        onDelete={async (rowId) => {
          if (!writable || !onMutationBegin())
            throw new Error(
              "Wait for the current service request to finish before you revoke a key.",
            );
          try {
            // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a result after its route is no longer active.
            await client.revokeKey(service.api_name, rowId, csrf);
            if (!isCurrent()) return;
            update((current) => ({
              keys: current.keys.filter((key) => key.id !== rowId),
            }));
            onNotice("success", "The service API key was revoked.");
          } catch (error) {
            const message = errorMessage(error);
            if (!isCurrent()) throw new Error(message);
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
        rows={rows.map((row) => ({
          ...row,
          locked: !writable,
        }))}
        saveLabel="Create"
        saveMode="explicit"
        state={tableState(
          phase,
          rows.length,
          "Loading service API keys…",
          "This service has no active API keys.",
          phase === "stale"
            ? "The key records are stale. Refresh keys before you make changes."
            : "The service API keys are unavailable.",
          load,
        )}
        validate={(row) =>
          row.isNew && row.draft.name.trim() === ""
            ? "Enter a key name."
            : undefined
        }
      />
    </>
  );
}

export interface ServiceAccessProps {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly serviceApiName: string;
  readonly service: Service | null;
  readonly serviceAvailable: boolean;
  readonly serviceFresh: boolean;
  readonly hostBlocked: boolean;
  readonly onNotice: NoticeHandler;
  readonly onPendingChange: (count: number) => void;
  readonly onDirtyChange: (dirty: boolean) => void;
  readonly onSecretStateChange: (visible: boolean) => void;
}

export function ServiceAccess(props: ServiceAccessProps) {
  return <ServiceAccessForRoute key={props.serviceApiName} {...props} />;
}

function ServiceAccessForRoute({
  client,
  csrf,
  serviceApiName,
  service,
  serviceAvailable,
  serviceFresh,
  hostBlocked,
  onNotice,
  onPendingChange,
  onDirtyChange,
  onSecretStateChange,
}: ServiceAccessProps) {
  const [access, updateAccess] = useReducer(
    reduceServiceAccess,
    initialServiceAccess,
  );
  const [secret, setSecret] = useState<string | null>(null);
  const [keyCreating, setKeyCreating] = useState(false);
  const [pending, setPending] = useState({ workspace: 0, key: 0 });
  const pendingRef = useRef({ workspace: 0, key: 0 });
  const keyActive = useRef(false);
  const active = useRef(true);
  const workspaceGuard = useRef(createScopeLoadGuard());
  const keyGuard = useRef(createScopeLoadGuard());
  const canRead = serviceAvailable && service?.api_name === serviceApiName;
  const currentService = service ?? {
    api_name: serviceApiName,
    display_name: serviceApiName,
    created_at: "",
  };
  useLayoutEffect(() => {
    onDirtyChange(
      [
        access.workspaceDraft?.apiName,
        access.workspaceDraft?.displayName,
        access.keyDraft?.name,
      ].some((value) => value !== undefined && value.length > 0),
    );
  }, [access.workspaceDraft, access.keyDraft, onDirtyChange]);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  useEffect(() => {
    const workspaces = workspaceGuard.current;
    const keys = keyGuard.current;
    if (!canRead) return;
    const workspaceGeneration = workspaces.begin();
    const keyGeneration = keys.begin();
    updateAccess({ workspacePhase: "loading", keyPhase: "loading" });
    void client
      .workspaces(serviceApiName)
      .then((page) => {
        if (!workspaces.isCurrent(workspaceGeneration)) return;
        updateAccess({
          workspaces: page.items.map(workspaceMetadata),
          workspacePhase: "ready",
          workspaceConfirmed: true,
        });
      })
      .catch(() => {
        if (workspaces.isCurrent(workspaceGeneration))
          updateAccess((current) => ({
            workspacePhase: current.workspaceConfirmed ? "stale" : "error",
          }));
      });
    void client
      .keys(serviceApiName)
      .then((page) => {
        if (!keys.isCurrent(keyGeneration)) return;
        updateAccess({
          keys: page.items.map(keyMetadata),
          keyPhase: "ready",
          keyConfirmed: true,
        });
      })
      .catch(() => {
        if (keys.isCurrent(keyGeneration))
          updateAccess((current) => ({
            keyPhase: current.keyConfirmed ? "stale" : "error",
          }));
      });
    return () => {
      workspaces.invalidate();
      keys.invalidate();
    };
  }, [canRead, client, serviceApiName]);

  async function refresh(section: "workspace" | "key"): Promise<void> {
    if (!canRead || pendingRef.current[section] > 0) return;
    const guard =
      section === "workspace" ? workspaceGuard.current : keyGuard.current;
    const generation = guard.begin();
    updateAccess(
      section === "workspace"
        ? { workspacePhase: "loading" }
        : { keyPhase: "loading" },
    );
    try {
      if (section === "workspace") {
        const page = await client.workspaces(serviceApiName);
        if (guard.isCurrent(generation))
          updateAccess({
            workspaces: page.items.map(workspaceMetadata),
            workspacePhase: "ready",
            workspaceConfirmed: true,
          });
      } else {
        const page = await client.keys(serviceApiName);
        if (guard.isCurrent(generation))
          updateAccess({
            keys: page.items.map(keyMetadata),
            keyPhase: "ready",
            keyConfirmed: true,
          });
      }
    } catch {
      if (!guard.isCurrent(generation)) return;
      updateAccess((current) =>
        section === "workspace"
          ? { workspacePhase: current.workspaceConfirmed ? "stale" : "error" }
          : { keyPhase: current.keyConfirmed ? "stale" : "error" },
      );
    }
  }
  function begin(section: "workspace" | "key"): boolean {
    if (
      !canRead ||
      !serviceFresh ||
      hostBlocked ||
      pendingRef.current[section] > 0
    )
      return false;
    (section === "workspace"
      ? workspaceGuard.current
      : keyGuard.current
    ).invalidate();
    pendingRef.current = {
      ...pendingRef.current,
      [section]: pendingRef.current[section] + 1,
    };
    setPending(pendingRef.current);
    onPendingChange(pendingRef.current.workspace + pendingRef.current.key);
    return true;
  }
  function end(section: "workspace" | "key"): void {
    if (!active.current) return;
    pendingRef.current = {
      ...pendingRef.current,
      [section]: Math.max(0, pendingRef.current[section] - 1),
    };
    setPending(pendingRef.current);
    onPendingChange(pendingRef.current.workspace + pendingRef.current.key);
  }
  const { workspaceRows, keyRows } = useServiceAccessRows(access);
  if (!canRead && !keyCreating && secret === null) return null;
  return (
    <>
      {canRead ? (
        <WorkspaceAccessSection
          client={client}
          csrf={csrf}
          load={() => refresh("workspace")}
          onMutationBegin={() => begin("workspace")}
          onMutationEnd={() => {
            end("workspace");
          }}
          onNotice={onNotice}
          phase={access.workspacePhase}
          writable={
            serviceFresh &&
            !hostBlocked &&
            access.workspacePhase === "ready" &&
            pending.workspace === 0
          }
          isCurrent={() => active.current}
          readBlocked={pending.workspace > 0}
          rows={workspaceRows}
          service={currentService}
          update={updateAccess}
          workspaceDraft={access.workspaceDraft}
        />
      ) : null}
      <Panel aria-label="Service API keys">
        <PanelHeader title="Service API keys" />
        {keyCreating ? (
          <StatePanel kind="loading" title="Creating the service API key">
            Keep this page open. The one-time secret will appear here.
          </StatePanel>
        ) : null}
        {secret === null ? null : (
          <OneTimeKey
            secret={secret}
            onNotice={onNotice}
            onClear={() => {
              setSecret(null);
              keyActive.current = false;
              onSecretStateChange(false);
            }}
          />
        )}
        {canRead ? (
          <KeyAccessSection
            client={client}
            csrf={csrf}
            keyDraft={access.keyDraft}
            keyLifecycleActive={secret !== null || keyCreating}
            load={() => refresh("key")}
            onKeyCreated={(_name, value) => {
              if (!active.current) return;
              setSecret(value);
              setKeyCreating(false);
              onSecretStateChange(true);
            }}
            onKeyCreationBegin={() => {
              if (keyActive.current) return false;
              keyActive.current = true;
              setKeyCreating(true);
              return true;
            }}
            onKeyCreationFailed={() => {
              if (!active.current) return;
              keyActive.current = false;
              setKeyCreating(false);
            }}
            onMutationBegin={() => begin("key")}
            onMutationEnd={() => {
              end("key");
            }}
            onNotice={onNotice}
            phase={access.keyPhase}
            writable={
              serviceFresh &&
              !hostBlocked &&
              access.keyPhase === "ready" &&
              pending.key === 0 &&
              secret === null
            }
            isCurrent={() => active.current}
            readBlocked={pending.key > 0}
            rows={keyRows}
            service={currentService}
            update={updateAccess}
          />
        ) : null}
      </Panel>
    </>
  );
}

function useServiceAccessRows(access: ServiceAccessState) {
  const workspaceRows = useMemo<readonly EditableTableRow<WorkspaceDraft>[]>(
    () => [
      ...access.workspaces.map((item) => ({
        id: item.api_name,
        label: item.display_name,
        draft: {
          apiName: item.api_name,
          displayName: item.display_name,
          createdAt: item.created_at,
        },
      })),
      ...(access.workspaceDraft === null
        ? []
        : [
            {
              id: WORKSPACE_CREATE_ROW_ID,
              label: "New workspace",
              draft: access.workspaceDraft,
              dirty: true,
              isNew: true,
            },
          ]),
    ],
    [access.workspaces, access.workspaceDraft],
  );
  const keyRows = useMemo<readonly EditableTableRow<KeyDraft>[]>(
    () => [
      ...access.keys.map((item) => ({
        id: item.id,
        label: item.name,
        draft: {
          name: item.name,
          createdAt: item.created_at,
          lastUsedAt: item.last_used_at,
        },
      })),
      ...(access.keyDraft === null
        ? []
        : [
            {
              id: uniqueDraftRowId(
                access.keys.map((item) => item.id),
                KEY_CREATE_ROW_ID_PREFIX,
              ),
              label: "New service API key",
              draft: access.keyDraft,
              dirty: true,
              isNew: true,
            },
          ]),
    ],
    [access.keys, access.keyDraft],
  );
  return { workspaceRows, keyRows };
}
