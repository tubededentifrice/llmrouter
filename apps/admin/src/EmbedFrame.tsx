import { Card, Icon, PageHeading, StatusPill } from "@opendle/ui";
import { useEffect, useMemo, useReducer, useRef } from "react";
import type { AdministrationSnapshot } from "./api.js";
import {
  FrameProtocolController,
  scheduleFrameStart,
  type BootstrapResult,
  type EmbedSection,
  type EmbedTheme,
} from "./embedProtocol.js";
import { EmbedSnapshotLoader } from "./embedSnapshotLoader.js";

interface EmbedSnapshot extends Partial<AdministrationSnapshot> {
  readonly service_id: string;
  readonly workspace_id?: string | null;
  readonly expires_at: string;
  readonly permissions: readonly string[];
  readonly configuration?: {
    readonly providers: AdministrationSnapshot["providers"];
    readonly routes: AdministrationSnapshot["routes"];
    readonly assignments: AdministrationSnapshot["assignments"];
  };
}

export interface EmbedFrameProps {
  readonly sessionId: string;
  readonly hostOrigin: string;
  readonly parentWindow?: Window;
  readonly fetcher?: typeof fetch;
}

const sectionLabels: Record<EmbedSection, string> = {
  configuration: "Configuration",
  assignments: "Assignments",
  requests: "Request status",
  accounting: "Accounting",
};
const defaultFetcher = globalThis.fetch.bind(globalThis);

interface FrameViewState {
  readonly section: EmbedSection;
  readonly snapshot: EmbedSnapshot | null;
  readonly failure: string | null;
  readonly expired: boolean;
}

type FrameViewAction =
  | { readonly type: "clear" }
  | { readonly type: "loading" }
  | { readonly type: "loaded"; readonly snapshot: EmbedSnapshot }
  | { readonly type: "failed"; readonly message: string }
  | { readonly type: "expired" }
  | { readonly type: "navigate"; readonly section: EmbedSection };

function frameReducer(
  state: FrameViewState,
  action: FrameViewAction,
): FrameViewState {
  if (action.type === "clear") return { ...state, snapshot: null };
  if (action.type === "loading")
    return { ...state, snapshot: null, failure: null, expired: false };
  if (action.type === "loaded")
    return { ...state, snapshot: action.snapshot, failure: null };
  if (action.type === "failed")
    return { ...state, snapshot: null, failure: action.message };
  if (action.type === "expired")
    return { ...state, snapshot: null, failure: null, expired: true };
  return { ...state, section: action.section };
}

export function EmbedFrame({
  sessionId,
  hostOrigin,
  parentWindow = window.parent,
  fetcher = defaultFetcher,
}: EmbedFrameProps) {
  const [state, dispatch] = useReducer(frameReducer, {
    section: "configuration",
    snapshot: null,
    failure: null,
    expired: false,
  });
  const container = useRef<HTMLElement>(null);
  const controllerRef = useRef<FrameProtocolController | null>(null);
  const snapshotLoader = useMemo(() => new EmbedSnapshotLoader(), []);

  useEffect(() => {
    const clearAuthority = () => {
      snapshotLoader.cancel();
      dispatch({ type: "clear" });
    };
    const controller = new FrameProtocolController({
      sessionId,
      hostOrigin,
      parentWindow,
      fetchBootstrap: async (id, input) => {
        const response = await fetcher(
          `/v1/administration/embed-sessions/${encodeURIComponent(id)}/bootstrap`,
          {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(input),
          },
        );
        if (!response.ok) throw new Error("Bootstrap failed.");
        return (await response.json()) as BootstrapResult;
      },
      onBootstrapped: (result) => {
        clearAuthority();
        dispatch({ type: "loading" });
        snapshotLoader.load(
          (signal) => loadSnapshot(fetcher, result, signal),
          (snapshot) => {
            dispatch({ type: "loaded", snapshot });
          },
          () => {
            dispatch({
              type: "failed",
              message: "The permitted administration state is not available.",
            });
          },
        );
      },
      onNavigate: (section) => {
        dispatch({ type: "navigate", section });
      },
      onTheme: applyTheme,
      onDispose: () => {
        clearAuthority();
      },
      onExpired: () => {
        clearAuthority();
        dispatch({ type: "expired" });
      },
      onError: (message) => {
        clearAuthority();
        dispatch({ type: "failed", message });
      },
    });
    controllerRef.current = controller;
    const receive = (event: MessageEvent) => {
      void controller.receive(event);
    };
    window.addEventListener("message", receive);
    const cancelStart = scheduleFrameStart(() => {
      controller.start();
    });
    return () => {
      cancelStart();
      window.removeEventListener("message", receive);
      controller.dispose();
      clearAuthority();
      if (controllerRef.current === controller) controllerRef.current = null;
    };
  }, [fetcher, hostOrigin, parentWindow, sessionId, snapshotLoader]);

  useEffect(() => {
    const element = container.current;
    if (element === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry !== undefined)
        controllerRef.current?.heightChanged(
          Math.ceil(entry.contentRect.height),
        );
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, []);

  function navigate(nextSection: EmbedSection) {
    dispatch({ type: "navigate", section: nextSection });
    controllerRef.current?.navigationChanged(nextSection);
  }

  return (
    <main
      ref={container}
      className="embed-frame"
      aria-busy={
        state.snapshot === null && state.failure === null && !state.expired
      }
    >
      <header className="embed-frame-header">
        <div>
          <span className="embed-frame-mark">
            <Icon name="shield" size={18} />
          </span>
          <strong>LLM Router</strong>
          <small>Service administration</small>
        </div>
        {state.snapshot === null ? null : (
          <StatusPill tone="green">Bounded session</StatusPill>
        )}
      </header>
      {state.expired ? (
        <FrameState
          title="Session expired"
          message="Ask the host service to authorize a new administration session."
          error
        />
      ) : state.failure !== null ? (
        <FrameState title="State unavailable" message={state.failure} error />
      ) : state.snapshot === null ? (
        <FrameState
          title="Waiting for authorization"
          message="The host service must complete the secure frame handshake."
        />
      ) : (
        <>
          <section
            className="embed-scope"
            aria-label="Exact administration scope"
          >
            <ScopeValue label="Service" value={state.snapshot.service_id} />
            <ScopeValue
              label="Workspace"
              value={state.snapshot.workspace_id ?? "Service level"}
            />
          </section>
          <nav
            className="embed-navigation"
            aria-label="Administration sections"
          >
            {(Object.keys(sectionLabels) as EmbedSection[]).map((item) => (
              <button
                key={item}
                type="button"
                aria-current={state.section === item ? "page" : undefined}
                onClick={() => {
                  navigate(item);
                }}
              >
                {sectionLabels[item]}
              </button>
            ))}
          </nav>
          <section className="embed-content">
            <PageHeading
              eyebrow="Exact permitted scope"
              title={sectionLabels[state.section]}
            />
            <EmbedSectionView
              section={state.section}
              snapshot={state.snapshot}
            />
          </section>
        </>
      )}
    </main>
  );
}

function ScopeValue({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function FrameState({
  title,
  message,
  error = false,
}: {
  readonly title: string;
  readonly message: string;
  readonly error?: boolean;
}) {
  return (
    <Card className="embed-frame-state" role={error ? "alert" : "status"}>
      <Icon name={error ? "warning" : "lock"} size={24} />
      <div>
        <h1>{title}</h1>
        <p>{message}</p>
      </div>
    </Card>
  );
}

function EmbedSectionView({
  section,
  snapshot,
}: {
  readonly section: EmbedSection;
  readonly snapshot: EmbedSnapshot;
}) {
  if (section === "configuration") {
    const value = snapshot.configuration;
    if (value === undefined)
      return (
        <FrameEmpty message="This grant does not permit configuration reads." />
      );
    return (
      <div className="embed-card-grid">
        <Metric label="Providers" value={value.providers.length} />
        <Metric label="Routes" value={value.routes.length} />
        <Metric label="Assignments" value={value.assignments.length} />
      </div>
    );
  }
  if (section === "assignments") {
    const values = snapshot.configuration?.assignments;
    if (values === undefined)
      return (
        <FrameEmpty message="This grant does not permit assignment reads." />
      );
    return (
      <Card className="embed-list">
        <h2>Ordered fallback chains</h2>
        {values.length === 0 ? (
          <p>No assignment is available.</p>
        ) : (
          values.map((item) => (
            <div key={item.name}>
              <strong>{item.name}</strong>
              <span>{item.candidates.length} ordered candidates</span>
            </div>
          ))
        )}
      </Card>
    );
  }
  if (section === "requests") {
    const values = snapshot.requests;
    if (values === undefined)
      return (
        <FrameEmpty message="This grant does not permit request-status reads." />
      );
    return (
      <Card className="embed-list">
        <h2>Content-free request status</h2>
        {values.length === 0 ? (
          <p>No bounded status is available.</p>
        ) : (
          values.map((item) => (
            <div key={item.request_id}>
              <strong>{item.request_id}</strong>
              <StatusPill tone="slate">{item.state}</StatusPill>
            </div>
          ))
        )}
      </Card>
    );
  }
  const value = snapshot.accounting;
  if (value == null)
    return (
      <FrameEmpty message="This grant does not permit accounting reads." />
    );
  return (
    <div className="embed-card-grid">
      <Metric label="Logical requests" value={value.logical_requests} />
      <Metric label="Provider attempts" value={value.attempts} />
      <Metric label={`Cost (${value.currency})`} value={value.cost} />
    </div>
  );
}

function Metric({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string | number;
}) {
  return (
    <Card className="embed-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </Card>
  );
}

function FrameEmpty({ message }: { readonly message: string }) {
  return (
    <Card className="embed-empty" role="status">
      {message}
    </Card>
  );
}

async function loadSnapshot(
  fetcher: typeof fetch,
  result: BootstrapResult,
  signal: AbortSignal,
): Promise<EmbedSnapshot> {
  const query = new URLSearchParams({ service_id: result.service_id });
  if (result.workspace_id != null)
    query.set("workspace_id", result.workspace_id);
  const response = await fetcher(
    `/v1/embed/administration/snapshot?${query.toString()}`,
    { credentials: "same-origin", cache: "no-store", signal },
  );
  if (!response.ok) throw new Error("The embed state request failed.");
  return (await response.json()) as EmbedSnapshot;
}

function applyTheme(theme: EmbedTheme) {
  const root = document.documentElement;
  root.dataset.embedMode = theme.mode;
  root.dataset.embedDensity = theme.density;
  root.dataset.embedCorners = theme.corner_style;
}

export function InvalidEmbedFrame() {
  return (
    <main className="embed-frame">
      <FrameState
        title="Invalid frame request"
        message="The host service must create a new administration session."
        error
      />
    </main>
  );
}
