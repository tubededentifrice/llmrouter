import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useReducer,
  useState,
} from "react";
import {
  Button,
  ConfirmationDialog,
  DateTime,
  FormActions,
  FormSection,
  GraphInspectorFact,
  GraphInspectorFacts,
  InlineAlert,
  PageHeading,
  PageSurface,
  Panel,
  PanelHeader,
  SearchableSelect,
  StatePanel,
  TextControl,
} from "@opendle/ui";
import {
  AdministrationApiError,
  errorMessage,
  type AdministrationClient,
  type Service,
} from "./api.ts";
import { ServiceAccess } from "./ServiceAccess.tsx";

type Phase = "loading" | "ready" | "error" | "absent";
type Notice = (tone: "success" | "error", message: string) => void;
export interface ServiceDetailsProps {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly serviceApiName: string;
  readonly onBack: (unavailable: boolean) => void;
  readonly onDeleted: () => void;
  readonly onContext: (name: string) => void;
  readonly onNotice: Notice;
  readonly registerNavigationGuard: (
    guard: (intent?: "unload") => boolean,
  ) => () => void;
}
function parentChoices(
  services: readonly Service[],
  name: string,
): readonly Service[] {
  const blocked = new Set([name]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const service of services)
      if (
        service.parent_service_api_name &&
        blocked.has(service.parent_service_api_name) &&
        !blocked.has(service.api_name)
      ) {
        blocked.add(service.api_name);
        changed = true;
      }
  }
  return services.filter((service) => !blocked.has(service.api_name));
}
interface DetailsReadState {
  service: Service | null;
  phase: Phase;
  error: string | null;
  services: readonly Service[] | null;
  parentPhase: Phase;
  parentError: string | null;
  draft: { displayName: string; parent: string } | null;
}
type DetailsReadPatch =
  | Partial<DetailsReadState>
  | ((state: DetailsReadState) => Partial<DetailsReadState>);
function reduceDetailsRead(
  state: DetailsReadState,
  patch: DetailsReadPatch,
): DetailsReadState {
  return { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
}
const initialDetailsRead: DetailsReadState = {
  service: null,
  phase: "loading",
  error: null,
  services: null,
  parentPhase: "loading",
  parentError: null,
  draft: null,
};

function useServiceDetails({
  client,
  csrf,
  serviceApiName,
  onDeleted,
  onContext,
  onNotice,
  registerNavigationGuard,
}: ServiceDetailsProps) {
  const [read, updateRead] = useReducer(reduceDetailsRead, initialDetailsRead);
  const { service, phase, error, services, parentPhase, parentError, draft } =
    read;
  const [busy, setBusy] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [accessPending, setAccessPending] = useState(0);
  const [secret, setSecret] = useState(false);
  const guard = useRef({
    busy: false,
    pending: 0,
    secret: false,
    accessDirty: false,
    formDirty: false,
  });
  const currentService = useRef<Service | null>(null);
  const generation = useRef(0);
  const parentGeneration = useRef(0);
  const pendingRead = useRef(false);
  const pendingParents = useRef(false);
  const active = useRef(true);
  const deleteRef = useRef<HTMLElement | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  const headingFocused = useRef(false);
  const root = serviceApiName === "root";
  const dirty =
    draft !== null &&
    service !== null &&
    (draft.displayName !== service.display_name ||
      draft.parent !== (service.parent_service_api_name ?? ""));
  useLayoutEffect(() => {
    guard.current.formDirty = dirty;
  }, [dirty]);
  const onPendingChange = useCallback((count: number) => {
    guard.current.pending = count;
    setAccessPending(count);
  }, []);
  const onDirtyChange = useCallback((value: boolean) => {
    guard.current.accessDirty = value;
  }, []);
  const onSecretStateChange = useCallback((value: boolean) => {
    guard.current.secret = value;
    setSecret(value);
  }, []);
  useLayoutEffect(
    () =>
      registerNavigationGuard((intent) => {
        const state = guard.current;
        if (state.busy || state.pending > 0 || state.secret) {
          if (intent !== "unload")
            onNotice(
              "error",
              state.secret
                ? "Copy and clear the one-time secret before you leave this service."
                : "Wait for each write to finish before you leave this service.",
            );
          return false;
        }
        if (!state.formDirty && !state.accessDirty) return true;
        return intent === "unload"
          ? false
          : globalThis.confirm(
              "Discard the unsaved values and leave this service?",
            );
      }),
    [onNotice, registerNavigationGuard],
  );
  const loadParents = useCallback(async () => {
    if (pendingParents.current) return;
    pendingParents.current = true;
    const sequence = ++parentGeneration.current;
    updateRead({ parentPhase: "loading" });
    try {
      // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a result after this request loses its active route or generation.
      const result = await client.services();
      if (!active.current || sequence !== parentGeneration.current) return;
      updateRead({ services: result.items });
      updateRead({ parentPhase: "ready" });
      updateRead({ parentError: null });
    } catch (reason) {
      if (!active.current || sequence !== parentGeneration.current) return;
      updateRead({ parentPhase: "error" });
      updateRead({ parentError: errorMessage(reason) });
    } finally {
      if (sequence === parentGeneration.current) pendingParents.current = false;
    }
  }, [client]);
  const load = useCallback(async () => {
    if (pendingRead.current) return;
    pendingRead.current = true;
    const sequence = ++generation.current;
    updateRead({ phase: "loading" });
    try {
      if (!/^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(serviceApiName))
        throw new AdministrationApiError(
          404,
          "not_found",
          "Service was not found.",
        );
      // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a result after this request loses its active route or generation.
      const result = await client.service(serviceApiName);
      if (!active.current || sequence !== generation.current) return;
      if (result.api_name !== serviceApiName)
        throw new Error(
          "The service response does not match this route. Try again.",
        );
      const previous = currentService.current;
      currentService.current = result;
      updateRead({ service: result });
      updateRead({ phase: "ready" });
      updateRead({ error: null });
      onContext(serviceApiName);
      updateRead(({ draft: current }) => ({
        draft:
          current === null ||
          (previous !== null &&
            current.displayName === previous.display_name &&
            current.parent === (previous.parent_service_api_name ?? ""))
            ? {
                displayName: result.display_name,
                parent: result.parent_service_api_name ?? "",
              }
            : current,
      }));
      parentGeneration.current += 1;
      pendingParents.current = false;
      void loadParents();
    } catch (reason) {
      if (!active.current || sequence !== generation.current) return;
      if (reason instanceof AdministrationApiError && reason.status === 404) {
        updateRead({ phase: "absent" });
        updateRead({ service: null });
        currentService.current = null;
        onContext("");
      } else updateRead({ phase: "error" });
      updateRead({ error: errorMessage(reason) });
    } finally {
      if (sequence === generation.current) pendingRead.current = false;
    }
  }, [client, loadParents, onContext, serviceApiName]);
  useEffect(() => {
    active.current = true;
    const timer = globalThis.setTimeout(() => {
      void load();
    }, 0);
    return () => {
      globalThis.clearTimeout(timer);
      active.current = false;

      pendingRead.current = false;
      pendingParents.current = false;
    };
  }, [load]);
  useLayoutEffect(() => {
    if (headingFocused.current || phase === "loading") return;
    const heading = document.querySelector<HTMLElement>("#main-content h1");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
      document.title = `${heading.textContent} · LLM Router`;
      headingFocused.current = true;
    }
  }, [phase]);
  const blocked = busy || accessPending > 0 || secret;
  const fresh = phase === "ready";
  const children = (services ?? []).filter(
    (item) => item.parent_service_api_name === serviceApiName,
  ).length;
  async function save() {
    if (
      guard.current.busy ||
      guard.current.pending > 0 ||
      guard.current.secret ||
      !fresh ||
      (!root && parentPhase !== "ready") ||
      draft === null
    )
      return;
    guard.current.busy = true;
    setBusy(true);
    setWriteError(null);
    generation.current++;
    pendingRead.current = false;
    try {
      // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a result after this request loses its active route or generation.
      const confirmed = await client.updateService(
        serviceApiName,
        {
          display_name: draft.displayName.trim(),
          parent_service_api_name: root ? null : draft.parent,
        },
        csrf,
      );
      if (!active.current) return;
      if (confirmed.api_name !== serviceApiName)
        throw new Error(
          "The service response does not match this route. Refresh the service.",
        );
      currentService.current = confirmed;
      updateRead({ service: confirmed });
      updateRead({
        draft: {
          displayName: confirmed.display_name,
          parent: confirmed.parent_service_api_name ?? "",
        },
      });
      guard.current.formDirty = false;
      updateRead({ phase: "ready" });
      onNotice("success", "The service was updated.");
      parentGeneration.current += 1;
      pendingParents.current = false;
      void loadParents();
    } catch (reason) {
      if (active.current) {
        setWriteError(errorMessage(reason));
        formRef.current
          ?.querySelector<HTMLButtonElement>('button[type="submit"]')
          ?.focus();
      }
    } finally {
      guard.current.busy = false;
      if (active.current) setBusy(false);
    }
  }
  async function remove() {
    if (
      guard.current.busy ||
      guard.current.pending > 0 ||
      guard.current.secret ||
      !fresh ||
      root ||
      parentPhase !== "ready" ||
      children > 0
    )
      return;
    guard.current.busy = true;
    setBusy(true);
    setWriteError(null);
    try {
      // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a result after this request loses its active route or generation.
      await client.deleteService(serviceApiName, csrf);
      if (!active.current) return;
      guard.current.busy = false;
      guard.current.formDirty = false;
      guard.current.accessDirty = false;
      setDeleteOpen(false);
      onDeleted();
    } catch (reason) {
      if (active.current) setWriteError(errorMessage(reason));
    } finally {
      guard.current.busy = false;
      if (active.current) setBusy(false);
    }
  }
  return {
    service,
    phase,
    error,
    services,
    parentPhase,
    parentError,
    draft,
    busy,
    deleteOpen,
    writeError,
    blocked,
    fresh,
    root,
    children,
    deleteRef,
    formRef,
    load,
    loadParents,
    save,
    remove,
    updateRead,
    setDeleteOpen,
    setWriteError,
    onPendingChange,
    onDirtyChange,
    onSecretStateChange,
  };
}

export function ServiceDetails(props: ServiceDetailsProps) {
  const { client, csrf, serviceApiName, onBack, onNotice } = props;
  const {
    service,
    phase,
    error,
    services,
    parentPhase,
    parentError,
    draft,
    busy,
    deleteOpen,
    writeError,
    blocked,
    fresh,
    root,
    children,
    deleteRef,
    formRef,
    load,
    loadParents,
    save,
    remove,
    updateRead,
    setDeleteOpen,
    setWriteError,
    onPendingChange,
    onDirtyChange,
    onSecretStateChange,
  } = useServiceDetails(props);
  const back = (
    <Button
      disabled={blocked}
      onClick={() => {
        onBack(phase === "absent");
      }}
      variant="secondary"
    >
      Back to services
    </Button>
  );
  return (
    <PageSurface className="administration-page">
      <PageHeading
        eyebrow={serviceApiName}
        title={service?.display_name ?? "Service details"}
        actions={
          <>
            {back}
            {service === null ? null : (
              <Button
                disabled={phase === "loading" || busy}
                onClick={() => {
                  void load();
                }}
                variant="secondary"
              >
                {phase === "loading"
                  ? "Refreshing service…"
                  : "Refresh service"}
              </Button>
            )}
          </>
        }
      />
      {phase === "loading" && service === null ? (
        <StatePanel kind="loading" title="Loading service details." />
      ) : null}
      {phase === "absent" ? (
        <StatePanel kind="error" title="The service is unavailable.">
          Return to services to continue.
        </StatePanel>
      ) : null}
      {phase === "error" ? (
        <InlineAlert
          title={
            service === null
              ? "Service details are unavailable."
              : "Service details are stale."
          }
          tone="error"
        >
          {error}{" "}
          <Button
            onClick={() => {
              void load();
            }}
          >
            Try again
          </Button>
        </InlineAlert>
      ) : null}
      {service !== null ? (
        <>
          <ServiceFacts
            service={service}
            services={services}
            root={root}
            parentPhase={parentPhase}
            childrenCount={children}
          />
          <Panel>
            <form
              ref={formRef}
              aria-busy={busy}
              onSubmit={(event) => {
                event.preventDefault();
                void save();
              }}
            >
              <FormSection legend="Service details">
                <TextControl
                  label="Display name"
                  value={draft?.displayName ?? service.display_name}
                  maxLength={200}
                  requirement="required"
                  disabled={blocked || !fresh}
                  onChange={(event) => {
                    updateRead({
                      draft: {
                        displayName: event.currentTarget.value,
                        parent:
                          draft?.parent ??
                          service.parent_service_api_name ??
                          "",
                      },
                    });
                  }}
                />
                {root ? (
                  <GraphInspectorFacts>
                    <GraphInspectorFact label="Parent" value="None" />
                  </GraphInspectorFacts>
                ) : (
                  <SearchableSelect
                    label="Parent service"
                    value={
                      draft?.parent ?? service.parent_service_api_name ?? ""
                    }
                    options={parentChoices(services ?? [], serviceApiName).map(
                      (item) => ({
                        label: item.display_name,
                        description: item.api_name,
                        value: item.api_name,
                      }),
                    )}
                    disabled={blocked || !fresh || parentPhase !== "ready"}
                    onChange={(parent) => {
                      updateRead({
                        draft: {
                          displayName:
                            draft?.displayName ?? service.display_name,
                          parent,
                        },
                      });
                    }}
                  />
                )}
              </FormSection>
              {parentPhase === "error" ? (
                <InlineAlert
                  title={
                    root
                      ? services === null
                        ? "The child count is unavailable."
                        : "The child count is stale."
                      : "Parent options are unavailable."
                  }
                  tone="error"
                >
                  {parentError}{" "}
                  <Button
                    onClick={() => {
                      void loadParents();
                    }}
                    variant="secondary"
                  >
                    {root ? "Retry child count" : "Retry parent options"}
                  </Button>
                </InlineAlert>
              ) : null}
              {writeError !== null && !deleteOpen ? (
                <InlineAlert title="The service change failed." tone="error">
                  {writeError} Correct the problem and try again.
                </InlineAlert>
              ) : null}
              <FormActions alignment="start">
                <Button
                  type="submit"
                  disabled={
                    blocked || !fresh || (!root && parentPhase !== "ready")
                  }
                >
                  {busy ? "Saving changes…" : "Save changes"}
                </Button>
              </FormActions>
            </form>
          </Panel>
        </>
      ) : null}
      <ServiceAccess
        hostBlocked={busy}
        client={client}
        csrf={csrf}
        serviceApiName={serviceApiName}
        service={service}
        serviceAvailable={service !== null}
        serviceFresh={fresh}
        onNotice={onNotice}
        onPendingChange={onPendingChange}
        onDirtyChange={onDirtyChange}
        onSecretStateChange={onSecretStateChange}
      />
      {service !== null ? (
        <Panel>
          <PanelHeader title="Delete service" />
          {root ? (
            <p>The permanent root service cannot be deleted.</p>
          ) : (
            <>
              <p>
                This action deletes the service, API keys, workspaces, local
                assignments, logs, raw accounting, daily aggregates, media jobs,
                and retained media. It keeps parent and child services.
              </p>
              {children > 0 ? (
                <InlineAlert
                  title="This service has child services."
                  tone="warning"
                >
                  Move or delete each child before you delete this service.
                </InlineAlert>
              ) : null}
              <Button
                disabled={
                  blocked || !fresh || parentPhase !== "ready" || children > 0
                }

                variant="secondary"
                onClick={(event) => {
                  deleteRef.current = event.currentTarget;
                  setWriteError(null);
                  setDeleteOpen(true);
                }}
              >
                Delete service
              </Button>
            </>
          )}
        </Panel>
      ) : null}
      <ConfirmationDialog
        open={deleteOpen}
        title={`Delete service ${serviceApiName}?`}
        description={
          <>
            <p>
              This action deletes the service, API keys, workspaces, local
              assignments, logs, raw accounting, daily aggregates, media jobs,
              and retained media. It keeps parent and child services.
            </p>
            {writeError === null ? null : (
              <InlineAlert title="The service was not deleted." tone="error">
                {writeError} Correct the problem and try again.
              </InlineAlert>
            )}
          </>
        }
        confirmLabel="Delete service"
        impactStatement={`Service ${serviceApiName} will be deleted.`}
        pending={busy}
        pendingLabel="Deleting service…"
        returnFocusRef={deleteRef}
        onCancel={() => {
          setDeleteOpen(false);
          setWriteError(null);
        }}
        onConfirm={() => {
          void remove();
        }}
      />
    </PageSurface>
  );
}

function ServiceFacts({
  service,
  services,
  root,
  parentPhase,
  childrenCount,
}: {
  readonly service: Service;
  readonly services: readonly Service[] | null;
  readonly root: boolean;
  readonly parentPhase: Phase;
  readonly childrenCount: number;
}) {
  return (
    <Panel aria-label="Service facts">
      <GraphInspectorFacts>
        <GraphInspectorFact label="API name" value={service.api_name} />
        <GraphInspectorFact
          label="Parent"
          value={
            root
              ? "None"
              : `${(services ?? []).find((item) => item.api_name === service.parent_service_api_name)?.display_name ?? service.parent_service_api_name ?? "Unavailable"} (${service.parent_service_api_name ?? "Unavailable"})`
          }
        />
        <GraphInspectorFact
          label="Created"
          value={<DateTime value={service.created_at} />}
        />
        <GraphInspectorFact
          label="Direct children"
          value={
            services !== null
              ? `${String(childrenCount)}${parentPhase === "error" ? " (stale)" : parentPhase === "loading" ? " (refreshing)" : ""}`
              : parentPhase === "loading"
                ? "Loading…"
                : "Unavailable"
          }
        />
      </GraphInspectorFacts>
    </Panel>
  );
}
