import { Button, Card, PageHeading, StatusPill } from "@opendle/ui";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  createHostApi,
  type ContextAction,
  type HostContext,
} from "./hostApi.js";
import { HostProtocolController, type HostView } from "./hostProtocol.js";

const initialView: HostView = {
  phase: "loading",
  message: "The example host is loading its server-side context.",
  frame: null,
  height: 420,
  section: "configuration",
};

export function App() {
  const [context, setContext] = useState<HostContext | null>(null);
  const [view, setView] = useState<HostView>(initialView);
  const [changing, setChanging] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const controller = useRef<HostProtocolController | null>(null);
  const api = useRef(createHostApi());

  useEffect(() => {
    const frameEnvironment = import.meta.env as {
      readonly VITE_LLMROUTER_FRAME_ORIGIN?: string;
    };
    const current = new HostProtocolController({
      api: api.current,
      hostOrigin: window.location.origin,
      routerOrigin:
        frameEnvironment.VITE_LLMROUTER_FRAME_ORIGIN ?? "http://127.0.0.1:5175",
      frameWindow: () => frame.current?.contentWindow ?? null,
      onView: setView,
    });
    controller.current = current;
    const abort = new AbortController();
    const receive = (event: MessageEvent) => void current.receive(event);
    window.addEventListener("message", receive);
    void api.current
      .context(abort.signal)
      .then((next) => {
        setContext(next);
        return current.replaceContext(next);
      })
      .catch(() => {
        setView({
          ...initialView,
          phase: "error",
          message: "The example host context is not available.",
        });
      });
    return () => {
      abort.abort();
      window.removeEventListener("message", receive);
      controller.current = null;
      void current.stop();
    };
  }, []);

  const changeContext = useCallback(async (action: ContextAction) => {
    setChanging(true);
    try {
      const next = await api.current.changeContext(action);
      setContext(next);
      await controller.current?.replaceContext(next);
    } catch {
      setView((current) => ({
        ...current,
        phase: "error",
        message: "The example host could not change its server-side context.",
        frame: null,
      }));
    } finally {
      setChanging(false);
    }
  }, []);

  return (
    <main className="example-host">
      <PageHeading
        eyebrow="Localhost proof"
        title="Service administration embed"
        description="This example keeps host authority on the server and gives the browser one short-lived frame session."
      />
      <Card className="example-context" aria-labelledby="context-heading">
        <div className="example-context-heading">
          <div>
            <h2 id="context-heading">Current host authorization</h2>
            <p>
              Each change disposes and revokes the old Router session first.
            </p>
          </div>
          <StatusPill tone={context?.membership ? "green" : "red"}>
            {context?.membership ? "Member" : "No membership"}
          </StatusPill>
        </div>
        <dl>
          <div>
            <dt>User</dt>
            <dd>{context?.host_user_subject ?? "Loading"}</dd>
          </div>
          <div>
            <dt>Workspace</dt>
            <dd>{context?.workspace_id ?? "Loading"}</dd>
          </div>
          <div>
            <dt>Permissions</dt>
            <dd>{context?.permissions.join(", ") ?? "Loading"}</dd>
          </div>
        </dl>
        <div className="example-actions" aria-label="Host context changes">
          <Button
            variant="secondary"
            disabled={changing}
            onClick={() => void changeContext("switch_user")}
          >
            Switch user
          </Button>
          <Button
            variant="secondary"
            disabled={changing}
            onClick={() => void changeContext("switch_workspace")}
          >
            Switch workspace
          </Button>
          <Button
            variant="secondary"
            disabled={changing}
            onClick={() => void changeContext("change_permissions")}
          >
            Change permissions
          </Button>
          <Button
            variant="secondary"
            disabled={changing}
            onClick={() =>
              void changeContext(
                context?.membership ? "lose_membership" : "restore_membership",
              )
            }
          >
            {context?.membership ? "Remove membership" : "Restore membership"}
          </Button>
          <Button
            variant="quiet"
            disabled={changing || !context?.membership}
            onClick={() => void controller.current?.renew()}
          >
            Renew session
          </Button>
        </div>
      </Card>
      <section className="example-embed" aria-labelledby="embed-heading">
        <div className="example-embed-heading">
          <div>
            <h2 id="embed-heading">Embedded Router view</h2>
            <p role={view.phase === "error" ? "alert" : "status"}>
              {view.message}
            </p>
          </div>
          <StatusPill
            tone={
              view.phase === "active"
                ? "green"
                : view.phase === "error"
                  ? "red"
                  : "blue"
            }
          >
            {view.phase}
          </StatusPill>
        </div>
        {view.frame === null ? (
          <Card className="example-frame-state" role="status">
            <p>No old service or workspace frame is present.</p>
          </Card>
        ) : (
          <iframe
            ref={frame}
            key={view.frame.sessionId}
            className="example-frame"
            src={view.frame.frameUrl}
            title={view.frame.title}
            height={view.height}
            data-section={view.section}
            sandbox="allow-scripts allow-same-origin"
            referrerPolicy="no-referrer"
          />
        )}
      </section>
    </main>
  );
}
