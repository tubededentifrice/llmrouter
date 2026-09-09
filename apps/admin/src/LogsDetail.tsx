import { useLayoutEffect, useRef } from "react";
import {
  Button,
  Panel,
  PanelContent,
  PanelHeader,
  DateTime,
} from "@opendle/ui";
import type { RequestLog } from "./api.ts";
import type { LogsController, LogsSelection } from "./logsState.ts";
import {
  requestLogActorLabel,
  requestLogScopeLabel,
  requestLogRouteLabel,
} from "./logPresentation.ts";
function RouterDateTime({ value }: { readonly value: string }) {
  return <DateTime value={value} />;
}
function LogsContent({
  detail,
  selection,
  controller,
}: {
  readonly detail: RequestLog;
  readonly selection: LogsSelection;
  readonly controller: LogsController;
}) {
  const mediaLink = selection.media;
  return (
    <PanelContent className="log-detail" aria-label="Logs detail content">
      <section>
        <h3>Request facts</h3>
        <dl className="log-detail-facts">
          <div>
            <dt>Logical call</dt>
            <dd>{detail.summary.logical_call_id}</dd>
          </div>
          <div>
            <dt>Actor and scope</dt>
            <dd>
              {requestLogActorLabel(detail.summary)} ·{" "}
              {requestLogScopeLabel(detail.summary)}
            </dd>
          </div>
          <div>
            <dt>Route</dt>
            <dd>{requestLogRouteLabel(detail.summary)}</dd>
          </div>
          <div>
            <dt>Kind and outcome</dt>
            <dd>
              {detail.summary.kind} · {detail.summary.outcome}
            </dd>
          </div>
          <div>
            <dt>Started</dt>
            <dd>
              <RouterDateTime value={detail.summary.started_at} />
            </dd>
          </div>
          <div>
            <dt>Tags</dt>
            <dd>
              {detail.summary.tags === undefined ||
              detail.summary.tags.length === 0
                ? "None"
                : detail.summary.tags.join(", ")}
            </dd>
          </div>
        </dl>
      </section>
      <section>
        <h3>Request content</h3>
        <pre>{detail.request_json}</pre>
      </section>
      <section>
        <h3>Response content</h3>
        <pre>{detail.response_json ?? "Response content is unavailable."}</pre>
      </section>
      <section>
        <h3>Attempts</h3>
        <ol className="log-attempt-list">
          {detail.attempts.map((item, index) => (
            <li key={`${item.provider_model_api_name}-${String(index)}`}>
              <strong>
                {item.provider_model_api_name} · {item.outcome}
              </strong>
              <span>
                <RouterDateTime value={item.started_at} /> through{" "}
                <RouterDateTime value={item.completed_at} />
              </span>
              <span>
                {item.usage === undefined
                  ? "Usage unavailable"
                  : `${item.usage.currency} ${item.usage.cost} · ${item.usage.units
                      .map((unit) => `${unit.unit} ${unit.quantity}`)
                      .join(", ")}`}
              </span>
              <span>
                Applied prices: {item.applied_prices.currency} ·{" "}
                {item.applied_prices.unit_prices
                  .map((price) => `${price.unit} ${price.amount}`)
                  .join(", ")}
                {typeof item.applied_prices.source === "string"
                  ? ` · source ${item.applied_prices.source}`
                  : ""}
                {item.applied_prices.synchronized_at == null ? null : (
                  <>
                    {" "}
                    · synchronized{" "}
                    <RouterDateTime
                      value={item.applied_prices.synchronized_at}
                    />
                  </>
                )}
              </span>
              {item.error == null ? null : (
                <span>
                  {item.error.code}: {item.error.message}
                  {item.error.details?.field === undefined
                    ? ""
                    : ` · field ${item.error.details.field}`}
                  {item.error.details?.reason === undefined
                    ? ""
                    : ` · ${item.error.details.reason}`}
                </span>
              )}
              {item.response_json === undefined ? null : (
                <pre>{item.response_json}</pre>
              )}
            </li>
          ))}
        </ol>
      </section>
      <section>
        <h3>Retained media</h3>
        {detail.media == null || detail.media.length === 0 ? (
          <p>No retained media</p>
        ) : (
          <ul>
            {detail.media.map((item) => (
              <li key={item.id}>
                <span>
                  {item.role} · {item.media_type} · {String(item.size_bytes)}{" "}
                  bytes
                </span>
                <Button
                  onClick={() => {
                    void controller.prepareMedia(item.id);
                  }}
                  variant="quiet"
                  aria-disabled={selection.mediaPending}
                  aria-busy={selection.mediaPending}
                >
                  Prepare retained media download
                </Button>
                {mediaLink?.id === item.id ? (
                  <a
                    download={`request-log-media-${item.id}`}
                    href={mediaLink.url}
                  >
                    Download retained media
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </PanelContent>
  );
}
export function LogsDetail({
  selection,
  controller,
}: {
  readonly selection: LogsSelection;
  readonly controller: LogsController;
}) {
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const heading = root.current?.querySelector("h2");
    if (heading) {
      heading.id = "logs-detail-heading";
      heading.tabIndex = -1;
    }
  }, [selection.id]);
  return (
    <div ref={root}>
      <Panel
        id="logs-details-region"
        aria-label="Logs details"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !event.defaultPrevented) {
            event.preventDefault();
            event.stopPropagation();
            controller.closeDetail();
          }
        }}
      >
        <PanelHeader
          title={`Logs details for request ${selection.id}`}
          actions={
            <>
              <Button
                id="logs-detail-retry"
                aria-disabled={selection.phase === "loading"}
                aria-busy={selection.phase === "loading"}
                onClick={() => {
                  void controller.inspect(selection.id);
                }}
                variant="quiet"
              >
                Retry Logs details
              </Button>
              <Button onClick={controller.closeDetail} variant="quiet">
                Close Logs details
              </Button>
            </>
          }
        />
        <p role="status" aria-live="polite">
          {selection.phase === "loading"
            ? "Loading Logs details."
            : selection.phase === "error"
              ? "Logs details are unavailable."
              : "Logs details loaded."}
        </p>
        {selection.phase === "ready" && selection.record ? (
          <LogsContent
            detail={selection.record}
            selection={selection}
            controller={controller}
          />
        ) : null}
      </Panel>
    </div>
  );
}
