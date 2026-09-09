import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useState,
  useSyncExternalStore,
} from "react";
import {
  Button,
  DataTable,
  DateTime,
  FormActions,
  FormSection,
  PageHeading,
  PageSurface,
  SelectControl,
  StatusPill,
  TextControl,
  type DataTableColumn,
} from "@opendle/ui";
import type { AdministrationClient, RequestLogSummary } from "./api.ts";
import { LogsController, logsFilterKeys, type LogsState } from "./logsState.ts";
import { LogsDetail } from "./LogsDetail.tsx";
import {
  requestLogActorLabel,
  requestLogRouteLabel,
  requestLogScopeLabel,
} from "./logPresentation.ts";
const labels = {
  from: "From time (UTC)",
  to: "Before time (UTC)",
  call_actor: "Call actor",
  administrator: "Administrator",
  configuration_service: "Assignment configuration service",
};
function LogsFilters({
  state,
  controller,
}: {
  readonly state: LogsState;
  readonly controller: LogsController;
}) {
  return (
    <form
      className="administration-form logs-filter-form"
      aria-label="Logs filters"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        void controller.apply();
      }}
    >
      <FormSection
        legend="Logs filters"
        columns={2}
        actions={
          <FormActions>
            <Button
              id="logs-apply"
              type="submit"
              aria-busy={state.pendingAction?.startsWith("apply:")}
              aria-disabled={
                !state.bounds || state.pendingAction?.startsWith("apply:")
              }
            >
              Apply Logs filters
            </Button>
            <Button
              id="logs-clear"
              onClick={() => {
                void controller.apply(true);
              }}
              aria-busy={state.pendingAction?.startsWith("clear:")}
              aria-disabled={
                !state.bounds || state.pendingAction?.startsWith("clear:")
              }
            >
              Clear Logs filters
            </Button>
          </FormActions>
        }
      >
        {logsFilterKeys.map((key) => (
          <Fragment key={key}>
            {key === "call_actor" ? (
              <SelectControl
                label={labels[key]}
                error={state.errors[key]}
                id={`logs-filter-${key}`}
                value={state.filters[key]}
                onChange={(event) => {
                  controller.changeFilter(key, event.currentTarget.value);
                }}
              >
                <option value="">All call actors</option>
                <option value="service">Service calls</option>
                <option value="administrator">
                  Administrator playground calls
                </option>
              </SelectControl>
            ) : (
              <TextControl
                label={labels[key]}
                error={state.errors[key]}
                id={`logs-filter-${key}`}
                value={state.filters[key]}
                onChange={(event) => {
                  controller.changeFilter(key, event.currentTarget.value);
                }}
              />
            )}
          </Fragment>
        ))}
      </FormSection>
    </form>
  );
}
function recordsLabel(count: number): string {
  return `Logs loaded: ${String(count)} ${count === 1 ? "record" : "records"}.`;
}
function logsMessage(state: LogsState): string {
  if (state.phase === "loading") return "Loading Logs.";
  if (state.phase === "error") return "Logs are unavailable.";
  if (state.more === "loading") return "Loading more Logs.";
  if (state.more === "error" || state.walk?.stopped)
    return "More Logs are unavailable.";
  if (!state.walk?.rows.length)
    return state.walk?.filtered
      ? "No Logs match these filters."
      : "No Logs are available in the configured retention window.";
  return `${recordsLabel(state.walk.rows.length)} ${state.walk.cursor ? "More Logs are available." : "All Logs in this range are loaded."}`;
}
function logsColumns(
  controller: LogsController,
  state: LogsState,
): readonly DataTableColumn<RequestLogSummary>[] {
  return [
    {
      key: "started",
      header: "Started",
      width: "12rem",
      render: ({ row }) => <DateTime value={row.started_at} />,
    },
    {
      key: "actor",
      header: "Actor",
      width: "8rem",
      render: ({ row }) => requestLogActorLabel(row),
    },
    {
      key: "scope",
      header: "Scope",
      width: "18rem",
      render: ({ row }) => requestLogScopeLabel(row),
    },
    {
      key: "kind",
      header: "Kind",
      width: "7rem",
      render: ({ row }) => row.kind,
    },
    {
      key: "route",
      header: "Route",
      width: "16rem",
      render: ({ row }) => requestLogRouteLabel(row),
    },
    {
      key: "tags",
      header: "Tags",
      width: "16rem",
      render: ({ row }) => (row.tags?.length ? row.tags.join(", ") : "None"),
    },
    {
      key: "outcome",
      header: "Outcome",
      width: "8rem",
      render: ({ row }) => (
        <StatusPill tone={row.outcome === "succeeded" ? "green" : "red"}>
          {row.outcome}
        </StatusPill>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      width: "14rem",
      render: ({ row }) => (
        <Button
          aria-label={`Inspect Logs details for request ${row.id}`}
          aria-controls={
            state.detail?.id === row.id ? "logs-details-region" : undefined
          }
          aria-expanded={state.detail?.id === row.id}
          aria-busy={
            state.detail?.id === row.id && state.detail.phase === "loading"
          }
          aria-disabled={
            state.detail?.id === row.id && state.detail.phase === "loading"
          }
          onClick={(event) => {
            void controller.inspect(row.id, event.currentTarget);
          }}
          variant="quiet"
        >
          Inspect Logs details
        </Button>
      ),
    },
  ];
}
export function LogsPage({
  client,
}: {
  readonly client: AdministrationClient;
}) {
  const [controller] = useState(() => new LogsController(client));
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
  );
  useEffect(() => {
    void controller.refresh("entry");
    return controller.dispose;
  }, [controller]);
  useLayoutEffect(() => {
    const target = state.focus?.target;
    if (!target) return;
    const element =
      target === "logs-more"
        ? document.querySelector<HTMLButtonElement>(
            "#logs-table .od-data-table-load-more button",
          )
        : document.getElementById(target);
    element?.focus();
  }, [state.focus]);
  const message = logsMessage(state);
  const walk = state.walk;
  const rows = state.phase === "ready" ? (walk?.rows ?? []) : [];
  return (
    <PageSurface className="administration-page">
      <PageHeading
        eyebrow="Best-effort diagnostics"
        title="Logs"
        description="Only global administrators can read complete retained model content and media."
        actions={
          <>
            <Button
              id="logs-refresh"
              aria-busy={state.pendingAction === "refresh"}
              aria-disabled={state.pendingAction === "refresh"}
              onClick={() => {
                void controller.refresh();
              }}
            >
              Refresh Logs
            </Button>
            {state.retryVisible ? (
              <Button
                id="logs-retry"
                aria-busy={state.pendingAction === "retry"}
                aria-disabled={state.pendingAction === "retry"}
                onClick={() => {
                  void controller.refresh("retry");
                }}
              >
                Retry Logs
              </Button>
            ) : null}
          </>
        }
      />
      <DataTable
        id="logs-table"
        ariaLabel="Logs"
        className="administration-data-table"
        columns={logsColumns(controller, state)}
        rows={rows}
        selection={{
          mode: "single",
          selectedRowIds: state.detail ? [state.detail.id] : [],
          onChange: (ids) => {
            const id = ids[0];
            if (id)
              void controller.inspect(
                id,
                document.activeElement instanceof HTMLElement
                  ? document.activeElement
                  : undefined,
              );
          },
          getLabel: (row) => `Inspect Logs details for request ${row.id}`,
        }}
        getRowId={(row) => row.id}
        getRowLabel={(row) => `Request ${row.id}`}
        maxRows={Math.max(200, rows.length + 100)}
        minimumWidth="102rem"
        filters={<LogsFilters state={state} controller={controller} />}
        toolbarLabel="Logs filters"
        liveMessage={message}
        state={{
          kind:
            state.phase === "loading"
              ? "loading"
              : state.phase === "error"
                ? "error"
                : !rows.length && !walk?.stopped
                  ? "empty"
                  : "ready",
          message: (
            <span id="logs-ready" tabIndex={-1}>
              {message}
            </span>
          ),
        }}
        {...(state.phase === "ready" &&
        walk &&
        (rows.length > 0 || walk.stopped)
          ? {
              loadMore: {
                hasMore: walk.cursor !== null,
                loading: state.more === "loading",
                loadLabel: "Load more Logs",
                loadingLabel: "Loading more Logs.",
                retryLabel: "Retry loading more Logs",
                completeLabel: walk.stopped
                  ? "More Logs are unavailable."
                  : "All Logs in this range are loaded.",
                loadedLabel: recordsLabel(rows.length),
                ...(state.more === "error"
                  ? { error: "More Logs are unavailable." }
                  : {}),
                onLoadMore: () => {
                  void controller.loadMore();
                },
                onRetry: () => {
                  void controller.loadMore();
                },
              },
            }
          : {})}
      />
      {state.detail ? (
        <LogsDetail selection={state.detail} controller={controller} />
      ) : null}
    </PageSurface>
  );
}
