import type {
  AdministrationClient,
  StatisticsFilters,
  StatisticsResult,
} from "./api.ts";

export const statisticsGroups = [
  { value: "date", label: "Date" },
  { value: "call_actor", label: "Call actor" },
  { value: "service", label: "Service" },
  { value: "workspace", label: "Workspace" },
  { value: "administrator", label: "Administrator" },
  { value: "configuration_service", label: "Assignment configuration service" },
  { value: "assignment", label: "Assignment" },
  { value: "provider_model", label: "Provider route" },
  { value: "outcome", label: "Outcome" },
  { value: "tag", label: "Tag" },
] as const;
const groupLabels = new Map<string, string>(
  statisticsGroups.map((group) => [group.value, group.label]),
);
export const statisticsFilterKeys = [
  "from",
  "through",
  "service",
  "workspace",
  "call_actor",
  "administrator",
  "configuration_service",
  "assignment",
  "provider_model",
  "outcome",
  "tag",
] as const;
export type StatisticsFilterKey = (typeof statisticsFilterKeys)[number];
export type StatisticsDraft = Record<StatisticsFilterKey, string> & {
  group_by: readonly string[];
};
export type StatisticsErrors = Partial<
  Record<StatisticsFilterKey | "group_by", string>
>;
export const exactAssignmentLabel = "Exact provider route calls";
const day = 86_400_000;
export function initialStatisticsDraft(now = Date.now()): StatisticsDraft {
  const through = new Date(now).toISOString().slice(0, 10);
  return {
    from: new Date(Date.parse(`${through}T00:00:00Z`) - 29 * day)
      .toISOString()
      .slice(0, 10),
    through,
    service: "",
    workspace: "",
    call_actor: "",
    administrator: "",
    configuration_service: "",
    assignment: "",
    provider_model: "",
    outcome: "",
    tag: "",
    group_by: [],
  };
}
function dateTime(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000"))
    return null;
  const time = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(time) &&
    new Date(time).toISOString() === `${value}T00:00:00.000Z`
    ? time
    : null;
}
export function validateStatistics(draft: StatisticsDraft): {
  errors: StatisticsErrors;
  query: StatisticsFilters | null;
} {
  const errors: StatisticsErrors = {};
  const from = dateTime(draft.from),
    through = dateTime(draft.through);
  if (from === null) errors.from = "Enter a valid From date.";
  if (through === null) errors.through = "Enter a valid Through date.";
  else if (draft.through === "9999-12-31")
    errors.through = "Through is outside the supported date range.";
  else if (from !== null && through < from)
    errors.through = "Through must be the same as or after From.";
  else if (from !== null && (through - from) / day + 1 > 366)
    errors.through = "Select 366 dates or fewer.";
  const apiName = /^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
  for (const key of [
    "service",
    "workspace",
    "configuration_service",
    "provider_model",
  ] as const) {
    if (draft[key] && !apiName.test(draft[key]))
      errors[key] =
        `Enter a valid ${groupLabels.get(key)?.toLowerCase() ?? key} API name.`;
  }
  if (
    draft.call_actor &&
    draft.call_actor !== "service" &&
    draft.call_actor !== "administrator"
  )
    errors.call_actor = "Select a valid call actor.";
  if (Array.from(draft.administrator).length > 500)
    errors.administrator =
      "Enter an administrator subject of 500 characters or fewer.";
  if (
    draft.assignment &&
    draft.assignment !== exactAssignmentLabel &&
    (!/^[a-z0-9][a-z0-9._-]*$/.test(draft.assignment) ||
      draft.assignment.length > 127)
  )
    errors.assignment =
      "Enter a valid assignment name or select Exact provider route calls.";
  if (
    draft.outcome &&
    draft.outcome !== "succeeded" &&
    draft.outcome !== "failed"
  )
    errors.outcome = "Select a valid outcome.";
  if (new TextEncoder().encode(draft.tag).length > 128)
    errors.tag = "Enter a tag of 128 UTF-8 bytes or fewer.";
  const selectedGroups = new Set(draft.group_by);
  const groups: string[] = [];
  for (const group of statisticsGroups) {
    if (selectedGroups.has(group.value)) groups.push(group.value);
  }
  if (groups.length > 8 || selectedGroups.size !== groups.length)
    errors.group_by = "Select up to 8 groups.";
  if (Object.keys(errors).length || from === null || through === null)
    return { errors, query: null };
  return {
    errors,
    query: {
      from: `${draft.from}T00:00:00Z`,
      to: new Date(through + day).toISOString().replace(".000Z", "Z"),
      ...(draft.service ? { service: draft.service } : {}),
      ...(draft.workspace ? { workspace: draft.workspace } : {}),
      ...(draft.call_actor === "service" || draft.call_actor === "administrator"
        ? { call_actor: draft.call_actor }
        : {}),
      ...(draft.administrator ? { administrator: draft.administrator } : {}),
      ...(draft.configuration_service
        ? { configuration_service: draft.configuration_service }
        : {}),
      ...(draft.assignment
        ? {
            assignment:
              draft.assignment === exactAssignmentLabel
                ? "(exact)"
                : draft.assignment,
          }
        : {}),
      ...(draft.provider_model ? { provider_model: draft.provider_model } : {}),
      ...(draft.outcome === "succeeded" || draft.outcome === "failed"
        ? { outcome: draft.outcome }
        : {}),
      ...(draft.tag ? { tag: draft.tag } : {}),
      ...(groups.length ? { group_by: groups } : {}),
    },
  };
}
export function statisticsActiveCount(draft: StatisticsDraft): number {
  return (
    statisticsFilterKeys.slice(4).filter((key) => draft[key] !== "").length +
    draft.group_by.length
  );
}
export function statisticsDimensionLabel(
  group: string,
  value: string | null,
): string {
  if (value === null) return "Not applicable";
  if (group === "assignment" && value === "(exact)")
    return exactAssignmentLabel;
  if (group === "call_actor")
    return value === "service"
      ? "Service calls"
      : value === "administrator"
        ? "Administrator playground calls"
        : value;
  if (group === "outcome")
    return value === "succeeded"
      ? "Succeeded"
      : value === "failed"
        ? "Failed"
        : value;
  return value;
}
export interface StatisticsState {
  readonly draft: StatisticsDraft;
  readonly errors: StatisticsErrors;
  readonly phase: "unqueried" | "loading" | "ready" | "error";
  readonly result: StatisticsResult | null;
  readonly groups: readonly string[];
  readonly pendingKey: string | null;
  readonly focus: {
    readonly serial: number;
    readonly key: keyof StatisticsErrors;
  } | null;
}
export class StatisticsController {
  private state: StatisticsState;
  private listeners = new Set<() => void>();
  private sequence = 0;
  private focusSerial = 0;
  private pending = new Map<string, Promise<StatisticsResult>>();
  constructor(
    private readonly client: Pick<AdministrationClient, "statistics">,
    now = Date.now(),
  ) {
    this.state = {
      draft: initialStatisticsDraft(now),
      errors: {},
      phase: "unqueried",
      result: null,
      groups: [],
      pendingKey: null,
      focus: null,
    };
  }
  getSnapshot = (): StatisticsState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private update(patch: Partial<StatisticsState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }
  change = (
    key: keyof StatisticsDraft,
    value: string | readonly string[],
  ): void => {
    const draft = { ...this.state.draft, [key]: value } as StatisticsDraft;
    const checked = validateStatistics(draft).errors;
    const errors = Object.fromEntries(
      Object.entries(this.state.errors).filter(
        ([field, message]) =>
          checked[field as keyof StatisticsErrors] === message,
      ),
    );
    this.update({ draft, errors });
  };
  dispose = (): void => {
    ++this.sequence;
    this.update({ pendingKey: null });
  };
  submit = async (): Promise<void> => {
    const { errors, query } = validateStatistics(this.state.draft);
    const key = query ? JSON.stringify(query) : null;
    if (key !== null && key === this.state.pendingKey) return;
    const sequence = ++this.sequence;
    if (!query || !key) {
      const first = [...statisticsFilterKeys, "group_by" as const].find(
        (field) => errors[field],
      );
      this.update({
        errors,
        pendingKey: null,
        phase: this.state.result ? "ready" : "unqueried",
        focus: first ? { serial: ++this.focusSerial, key: first } : null,
      });
      return;
    }
    this.update({
      errors: {},
      phase: "loading",
      result: null,
      groups: query.group_by ?? [],
      pendingKey: key,
      focus: null,
    });
    try {
      let request = this.pending.get(key);
      if (!request) {
        request = this.client.statistics(query);
        this.pending.set(key, request);
      }
      const result = await request;
      if (sequence === this.sequence)
        this.update({ result, phase: "ready", pendingKey: null });
    } catch {
      if (sequence === this.sequence)
        this.update({ phase: "error", pendingKey: null });
    } finally {
      this.pending.delete(key);
    }
  };
}
export function statisticsMessage(state: StatisticsState): string {
  if (state.phase === "loading") return "Loading usage and cost.";
  if (state.phase === "error")
    return "Unable to load usage and cost. Review the filters and try again.";
  if (state.phase === "unqueried")
    return "Choose filters and run the statistics query.";
  if (!state.result?.buckets.length)
    return "No usage or cost matches these filters.";
  const count = state.result.buckets.length;
  return `${String(count)} accounting ${count === 1 ? "group" : "groups"} loaded.`;
}
