import type {
  AdministrationClient,
  Page,
  RequestLog,
  RequestLogSummary,
} from "./api.ts";

export const logsFilterKeys = [
  "from",
  "to",
  "call_actor",
  "administrator",
  "configuration_service",
] as const;
export type LogsFilterKey = (typeof logsFilterKeys)[number];
export type LogsFilters = Record<LogsFilterKey, string>;
export type LogsErrors = Partial<LogsFilters>;
export const emptyLogsFilters = (): LogsFilters => ({
  from: "",
  to: "",
  call_actor: "",
  administrator: "",
  configuration_service: "",
});
export interface LogsBounds {
  readonly from: string;
  readonly to: string;
}
export interface LogsQuery extends LogsBounds {
  readonly call_actor?: "service" | "administrator";
  readonly administrator?: string;
  readonly configuration_service?: string;
}
export function logsRetentionBounds(
  days: number,
  now = Date.now(),
): LogsBounds {
  if (!Number.isInteger(days) || days < 1 || days > 30)
    throw Error("Invalid Logs retention.");
  const end = Math.floor(now / 1000) * 1000;
  return {
    from: new Date(end - days * 86_400_000).toISOString().replace(".000Z", "Z"),
    to: new Date(end).toISOString().replace(".000Z", "Z"),
  };
}
function validTime(value: string): boolean {
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(value) ||
    value.startsWith("0000")
  )
    return false;
  const time = new Date(`${value}Z`);
  return (
    Number.isFinite(time.getTime()) && time.toISOString() === `${value}.000Z`
  );
}
export function validateLogsFilters(
  filters: LogsFilters,
  bounds: LogsBounds,
): { errors: LogsErrors; query: LogsQuery } {
  const errors: LogsErrors = {};
  if (filters.from && !validTime(filters.from))
    errors.from = "Enter a valid From time in UTC.";
  if (filters.to && !validTime(filters.to))
    errors.to = "Enter a valid Before time in UTC.";
  const from = filters.from ? `${filters.from}Z` : bounds.from;
  const to = filters.to ? `${filters.to}Z` : bounds.to;
  if (!errors.from && !errors.to) {
    if (from >= to) errors.to = "From time must be before Before time.";
    const outside =
      filters.from && (from < bounds.from || from > bounds.to)
        ? "from"
        : filters.to && (to < bounds.from || to > bounds.to)
          ? "to"
          : null;
    if (outside && !errors[outside])
      errors[outside] =
        "Select times inside the configured Logs retention window.";
  }
  if (
    filters.call_actor &&
    filters.call_actor !== "service" &&
    filters.call_actor !== "administrator"
  )
    errors.call_actor = "Select a valid call actor.";
  if (Array.from(filters.administrator).length > 500)
    errors.administrator =
      "Enter an administrator subject of 500 characters or fewer.";
  if (
    filters.configuration_service &&
    !/^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(filters.configuration_service)
  )
    errors.configuration_service =
      "Enter a valid assignment configuration service API name.";
  return {
    errors,
    query: {
      from,
      to,
      ...(filters.call_actor === "service" ||
      filters.call_actor === "administrator"
        ? { call_actor: filters.call_actor }
        : {}),
      ...(filters.administrator
        ? { administrator: filters.administrator }
        : {}),
      ...(filters.configuration_service
        ? { configuration_service: filters.configuration_service }
        : {}),
    },
  };
}
interface Walk {
  readonly query: LogsQuery;
  readonly filtered: boolean;
  readonly rows: readonly RequestLogSummary[];
  readonly cursor: string | null;
  readonly seen: ReadonlySet<string>;
  readonly stopped: boolean;
}
export interface LogsSelection {
  readonly id: string;
  readonly record: RequestLog | null;
  readonly phase: "loading" | "ready" | "error";
  readonly media: { readonly id: string; readonly url: string } | null;
  readonly mediaPending: boolean;
}
export interface LogsState {
  readonly bounds: LogsBounds | null;
  readonly filters: LogsFilters;
  readonly errors: LogsErrors;
  readonly walk: Walk | null;
  readonly phase: "loading" | "ready" | "error";
  readonly pendingAction: string | null;
  readonly retryVisible: boolean;
  readonly more: "idle" | "loading" | "error";
  readonly detail: LogsSelection | null;
  readonly focus: { readonly serial: number; readonly target: string } | null;
}

/** Router-owned identities keep obsolete list, detail and media reads inert. */
export class LogsController {
  private state: LogsState = {
    bounds: null,
    filters: emptyLogsFilters(),
    errors: {},
    walk: null,
    phase: "loading",
    pendingAction: null,
    retryVisible: false,
    more: "idle",
    detail: null,
    focus: null,
  };
  private listeners = new Set<() => void>();
  private sequence = 0;
  private selection = 0;
  private mediaSequence = 0;
  private focusSerial = 0;
  private confirmed: Walk | null = null;
  private returnFocus: HTMLElement | null = null;
  constructor(
    private readonly client: AdministrationClient,
    private readonly now: () => number = Date.now,
  ) {}
  getSnapshot = (): LogsState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private update(patch: Partial<LogsState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }
  private focus(target: string): LogsState["focus"] {
    return { serial: ++this.focusSerial, target };
  }
  changeFilter = (key: LogsFilterKey, value: string): void => {
    const filters = { ...this.state.filters, [key]: value };
    const checked = this.state.bounds
      ? validateLogsFilters(filters, this.state.bounds).errors
      : {};
    const errors = Object.fromEntries(
      Object.entries(this.state.errors).filter(
        ([field, error]) => checked[field as LogsFilterKey] === error,
      ),
    );
    this.update({ filters, errors });
  };
  private invalidateDetail(close: boolean): void {
    ++this.selection;
    ++this.mediaSequence;
    const detail = this.state.detail;
    if (close && detail?.media) URL.revokeObjectURL(detail.media.url);
    this.update({
      detail: close
        ? null
        : detail
          ? {
              ...detail,
              phase: detail.phase === "loading" ? "error" : detail.phase,
              mediaPending: false,
            }
          : null,
    });
  }
  dispose = (): void => {
    ++this.sequence;
    this.invalidateDetail(true);
    this.update({ pendingAction: null });
  };
  refresh = async (action = "refresh"): Promise<void> => {
    if (this.state.pendingAction === action) return;
    const sequence = ++this.sequence;
    this.invalidateDetail(true);
    this.update({
      phase: "loading",
      pendingAction: action,
      more: "idle",
      errors: {},
      focus: null,
    });
    await this.client
      .retention()
      .then(async (retention) => {
        if (sequence !== this.sequence) return;
        const bounds = logsRetentionBounds(retention.duration_days, this.now());
        this.update({ bounds });
        await this.firstPage(sequence, this.state.filters, bounds);
      })
      .catch(() => {
        if (sequence === this.sequence)
          this.update({
            phase: "error",
            walk: null,
            pendingAction: null,
            retryVisible: true,
          });
      });
  };
  apply = async (clear = false): Promise<void> => {
    const filters = clear ? emptyLogsFilters() : this.state.filters;
    const action = `${clear ? "clear" : "apply"}:${JSON.stringify(filters)}`;
    if (this.state.pendingAction === action || !this.state.bounds) return;
    const sequence = ++this.sequence;
    // Invalid Apply preserves confirmed details, but never a pending detail read.
    this.invalidateDetail(false);
    this.update({ filters, pendingAction: action, more: "idle", focus: null });
    await this.firstPage(sequence, filters, this.state.bounds);
  };
  private async firstPage(
    sequence: number,
    filters: LogsFilters,
    bounds: LogsBounds,
  ): Promise<void> {
    const { query, errors } = validateLogsFilters(filters, bounds);
    const firstError = logsFilterKeys.find((key) => errors[key]);
    if (firstError) {
      this.update({
        errors,
        walk: this.confirmed,
        phase: this.confirmed ? "ready" : "error",
        pendingAction: null,
        focus: this.focus(`logs-filter-${firstError}`),
      });
      return;
    }
    this.invalidateDetail(true);
    this.update({ errors: {}, walk: null, phase: "loading", more: "idle" });
    await this.client
      .requestLogsPage(
        query.from,
        query.to,
        undefined,
        this.optionalFilters(query),
      )
      .then((page) => {
        if (sequence !== this.sequence) return;
        const walk = this.acceptPage(
          {
            query,
            filtered: logsFilterKeys.some((key) => filters[key] !== ""),
            rows: [],
            cursor: null,
            seen: new Set(),
            stopped: false,
          },
          page,
        );
        this.confirmed = walk;
        this.update({
          walk,
          phase: "ready",
          pendingAction: null,
          retryVisible: this.state.pendingAction === "retry",
          ...(walk.stopped ? { focus: this.focus("logs-refresh") } : {}),
        });
      })
      .catch(() => {
        if (sequence === this.sequence)
          this.update({
            phase: "error",
            walk: null,
            pendingAction: null,
            retryVisible: true,
          });
      });
  }
  private optionalFilters(query: LogsQuery) {
    return {
      ...(query.call_actor ? { call_actor: query.call_actor } : {}),
      ...(query.administrator ? { administrator: query.administrator } : {}),
      ...(query.configuration_service
        ? { configuration_service: query.configuration_service }
        : {}),
    };
  }
  private acceptPage(walk: Walk, page: Page<RequestLogSummary>): Walk {
    const ids = new Set(walk.rows.map((row) => row.id));
    const added: RequestLogSummary[] = [];
    for (const row of page.items) {
      if (!ids.has(row.id)) {
        ids.add(row.id);
        added.push(row);
      }
    }
    const rows = [...walk.rows, ...added];
    if (!page.page.has_more)
      return { ...walk, rows, cursor: null, stopped: false };
    const cursor = page.page.next_cursor;
    if (
      added.length === 0 ||
      typeof cursor !== "string" ||
      Array.from(cursor).length < 1 ||
      Array.from(cursor).length > 500 ||
      walk.seen.has(cursor)
    )
      return { ...walk, rows, cursor: null, stopped: true };
    return {
      ...walk,
      rows,
      cursor,
      seen: new Set([...walk.seen, cursor]),
      stopped: false,
    };
  }
  loadMore = async (): Promise<void> => {
    const walk = this.state.walk;
    if (
      this.state.phase !== "ready" ||
      this.state.more === "loading" ||
      !walk?.cursor ||
      walk.stopped
    )
      return;
    const sequence = this.sequence;
    this.update({ more: "loading", focus: null });
    await this.client
      .requestLogsPage(
        walk.query.from,
        walk.query.to,
        walk.cursor,
        this.optionalFilters(walk.query),
      )
      .then((page) => {
        if (sequence !== this.sequence) return;
        const next = this.acceptPage(walk, page);
        this.confirmed = next;
        this.update({
          walk: next,
          more: "idle",
          focus: this.focus(
            next.stopped
              ? "logs-refresh"
              : next.cursor
                ? "logs-more"
                : "logs-ready",
          ),
        });
      })
      .catch(() => {
        if (sequence === this.sequence)
          this.update({ more: "error", focus: this.focus("logs-more") });
      });
  };
  inspect = async (id: string, trigger?: HTMLElement): Promise<void> => {
    if (this.state.detail?.id === id && this.state.detail.phase === "loading")
      return;
    const sequence = this.sequence;
    const selection = ++this.selection;
    ++this.mediaSequence;
    if (this.state.detail?.media)
      URL.revokeObjectURL(this.state.detail.media.url);
    if (trigger) this.returnFocus = trigger;
    const isNew = trigger !== undefined || this.state.detail?.id !== id;
    this.update({
      detail: {
        id,
        record: null,
        phase: "loading",
        media: null,
        mediaPending: false,
      },
      ...(isNew ? { focus: this.focus("logs-detail-heading") } : {}),
    });
    await this.client
      .requestLog(id)
      .then((record) => {
        if (sequence !== this.sequence || selection !== this.selection) return;
        if (record.summary.id !== id) throw Error("Mismatched Logs detail.");
        this.update({
          detail: {
            id,
            record,
            phase: "ready",
            media: null,
            mediaPending: false,
          },
        });
      })
      .catch(() => {
        if (sequence === this.sequence && selection === this.selection)
          this.update({
            detail: {
              id,
              record: null,
              phase: "error",
              media: null,
              mediaPending: false,
            },
          });
      });
  };
  closeDetail = (): void => {
    this.invalidateDetail(true);
    if (this.returnFocus?.isConnected) this.returnFocus.focus();
    else this.update({ focus: this.focus("logs-refresh") });
    this.returnFocus = null;
  };
  prepareMedia = async (id: string): Promise<void> => {
    const detail = this.state.detail;
    if (!detail || detail.mediaPending) return;
    const sequence = this.sequence;
    const selection = this.selection;
    const media = ++this.mediaSequence;
    this.update({ detail: { ...detail, mediaPending: true } });
    await this.client
      .requestLogMedia(detail.id, id)
      .then((blob) => {
        if (
          sequence !== this.sequence ||
          selection !== this.selection ||
          media !== this.mediaSequence
        )
          return;
        if (detail.media) URL.revokeObjectURL(detail.media.url);
        const url = URL.createObjectURL(
          new Blob([blob], { type: "application/octet-stream" }),
        );
        this.update({
          detail: { ...detail, media: { id, url }, mediaPending: false },
        });
      })
      .catch(() => {
        if (
          sequence !== this.sequence ||
          selection !== this.selection ||
          media !== this.mediaSequence
        )
          return;
        if (detail.media) URL.revokeObjectURL(detail.media.url);
        this.update({
          detail: {
            ...detail,
            phase: "error",
            media: null,
            mediaPending: false,
          },
        });
      });
  };
}
