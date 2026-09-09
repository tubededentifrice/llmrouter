import {
  useEffect,
  useLayoutEffect,
  useState,
  useSyncExternalStore,
} from "react";
import {
  AdvancedFieldsDisclosure,
  Button,
  CompactCheckboxGroup,
  DataTable,
  FieldHelp,
  FormActions,
  FormField,
  PageHeading,
  PageSurface,
  RadioGroup,
  SelectControl,
  TextControl,
  type DataTableColumn,
} from "@opendle/ui";
import type { AdministrationClient, Service, StatisticsBucket } from "./api.ts";
import {
  StatisticsController,
  exactAssignmentLabel,
  statisticsActiveCount,
  statisticsDimensionLabel,
  statisticsFilterKeys,
  statisticsGroups,
  statisticsMessage,
  validateStatistics,
  type StatisticsFilterKey,
  type StatisticsState,
} from "./statisticsState.ts";
const STATISTICS_GROUP_MAXIMUM = 1_000;
const labels = {
  from: "From",
  through: "Through",
  service: "Service",
  workspace: "Workspace",
  call_actor: "Call actor",
  administrator: "Administrator",
  configuration_service: "Assignment configuration service",
  assignment: "Assignment",
  provider_model: "Provider route",
  outcome: "Outcome",
  tag: "Tag",
};
function StatisticsFilter({
  field,
  state,
  controller,
  services,
}: {
  readonly field: StatisticsFilterKey;
  readonly state: StatisticsState;
  readonly controller: StatisticsController;
  readonly services: readonly Service[];
}) {
  const props = {
    id: `statistics-filter-${field}`,
    name: field,
    label: labels[field],
    value: state.draft[field],
    error: state.errors[field],
    onChange: (event: { currentTarget: { value: string } }) => {
      controller.change(field, event.currentTarget.value);
    },
  };
  if (field === "service")
    return (
      <SelectControl {...props}>
        <option value="">All services</option>
        {services.map((service) => (
          <option key={service.api_name} value={service.api_name}>
            {service.display_name}
          </option>
        ))}
      </SelectControl>
    );
  if (field === "call_actor" || field === "outcome")
    return (
      <RadioGroup
        {...props}
        options={
          field === "call_actor"
            ? [
                { value: "", label: "All call actors" },
                { value: "service", label: "Service calls" },
                {
                  value: "administrator",
                  label: "Administrator playground calls",
                },
              ]
            : [
                { value: "", label: "All outcomes" },
                { value: "succeeded", label: "Succeeded" },
                { value: "failed", label: "Failed" },
              ]
        }
        onChange={(value) => {
          controller.change(field, value);
        }}
      />
    );
  if (field === "from" || field === "through")
    return (
      <FormField label={props.label} error={props.error}>
        <input
          className="od-form-control"
          id={props.id}
          name={props.name}
          value={props.value}
          onChange={props.onChange}
          type="date"
          min="0001-01-01"
          max="9999-12-31"
          aria-describedby="statistics-utc"
        />
      </FormField>
    );
  return (
    <TextControl
      {...props}
      {...(field === "assignment"
        ? { list: "statistics-assignment-values" }
        : {})}
    />
  );
}
function StatisticsForm({
  state,
  controller,
  services,
}: {
  readonly state: StatisticsState;
  readonly controller: StatisticsController;
  readonly services: readonly Service[];
}) {
  const active = statisticsActiveCount(state.draft);
  const checked = validateStatistics(state.draft);
  const samePending =
    checked.query !== null &&
    JSON.stringify(checked.query) === state.pendingKey;
  return (
    <form
      className="administration-form statistics-form"
      aria-label="Usage and cost filters"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        void controller.submit();
      }}
    >
      <FieldHelp id="statistics-utc">
        UTC dates. From and Through include the selected dates.
      </FieldHelp>
      <div role="alert" aria-atomic="true">
        {Object.values(state.errors).join(" ")}
      </div>
      {statisticsFilterKeys.slice(0, 4).map((field) => (
        <StatisticsFilter
          key={field}
          field={field}
          state={state}
          controller={controller}
          services={services}
        />
      ))}
      <AdvancedFieldsDisclosure
        id="statistics-advanced"
        summary={
          active
            ? `Advanced filters (${String(active)} active)`
            : "Advanced filters"
        }
      >
        {statisticsFilterKeys.slice(4).map((field) => (
          <StatisticsFilter
            key={field}
            field={field}
            state={state}
            controller={controller}
            services={services}
          />
        ))}
        <datalist id="statistics-assignment-values">
          <option value={exactAssignmentLabel} />
        </datalist>
        <CompactCheckboxGroup
          id="statistics-filter-group_by"
          label="Group results"
          name="group_by"
          value={state.draft.group_by}
          options={statisticsGroups.map((group) => ({
            ...group,
            disabled:
              state.draft.group_by.length >= 8 &&
              !state.draft.group_by.includes(group.value),
          }))}
          onChange={(value) => {
            if (value.length <= 8) controller.change("group_by", value);
          }}
          aria-invalid={Boolean(state.errors.group_by)}
          aria-describedby="statistics-group-limit"
        />
        <FieldHelp id="statistics-group-limit" role="status">
          {state.errors.group_by ??
            (state.draft.group_by.length >= 8 ? "Select up to 8 groups." : "")}
        </FieldHelp>
      </AdvancedFieldsDisclosure>
      <FormActions>
        <Button
          id="statistics-run"
          type="submit"
          aria-disabled={samePending}
          aria-busy={state.phase === "loading"}
        >
          Run statistics
        </Button>
      </FormActions>
    </form>
  );
}
function columns(
  groups: readonly string[],
): readonly DataTableColumn<StatisticsBucket>[] {
  return [
    {
      key: "dimensions",
      header: "Dimensions",
      width: "32%",
      render: ({ row }) =>
        row.dimensions.length
          ? row.dimensions
              .map((value, index) => {
                const group = groups[index] ?? "";
                return `${statisticsGroups.find((item) => item.value === group)?.label ?? "Dimension"}: ${statisticsDimensionLabel(group, value)}`;
              })
              .join(" / ")
          : "Total",
    },
    {
      key: "calls",
      header: "Calls",
      align: "end",
      width: "7rem",
      render: ({ row }) => row.calls,
    },
    {
      key: "attempts",
      header: "Attempts",
      align: "end",
      width: "7rem",
      render: ({ row }) => row.attempts,
    },
    {
      key: "usage",
      header: "Typed usage",
      width: "32%",
      render: ({ row }) =>
        row.units.map((unit) => `${unit.unit} ${unit.quantity}`).join(", ") ||
        "None",
    },
    {
      key: "cost",
      header: "Cost",
      align: "end",
      width: "10rem",
      render: ({ row }) =>
        row.cost === null
          ? "Unavailable"
          : row.currency === null
            ? `${row.cost} (no currency)`
            : `${row.currency} ${row.cost}`,
    },
  ];
}
export function StatisticsPage({
  client,
  services,
}: {
  readonly client: AdministrationClient;
  readonly services: readonly Service[];
}) {
  const [controller] = useState(() => new StatisticsController(client));
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  );
  useEffect(() => controller.dispose, [controller]);
  useLayoutEffect(() => {
    if (!state.focus) return;
    const key = state.focus.key;
    if (!["from", "through", "service", "workspace"].includes(key)) {
      const advanced = document.getElementById("statistics-advanced");
      if (advanced instanceof HTMLDetailsElement) advanced.open = true;
    }
    const control = document.getElementById(`statistics-filter-${key}`);
    if (key === "group_by") control?.querySelector("summary")?.focus();
    else if (key === "call_actor" || key === "outcome") {
      const radio =
        control?.querySelector<HTMLInputElement>(
          'input[type="radio"]:checked:enabled',
        ) ??
        control?.querySelector<HTMLInputElement>('input[type="radio"]:enabled');
      radio?.focus();
    } else control?.focus();
  }, [state.focus]);
  const message = statisticsMessage(state);
  const rows = state.result?.buckets ?? [];
  return (
    <PageSurface className="administration-page">
      <PageHeading
        eyebrow="Durable accounting"
        title="Usage and cost statistics"
      />
      <StatisticsForm
        state={state}
        controller={controller}
        services={services}
      />
      <DataTable
        ariaLabel="Usage and cost statistics"
        className="administration-data-table"
        columns={columns(state.groups)}
        getRowId={(row, index) =>
          `${String(index)}:${JSON.stringify([row.dimensions, row.currency])}`
        }
        getRowLabel={(_row, index) => `Statistics group ${String(index + 1)}`}
        liveMessage={message}
        maxRows={STATISTICS_GROUP_MAXIMUM}
        minimumWidth="56rem"
        rows={rows}
        state={{
          kind:
            state.phase === "loading"
              ? "loading"
              : state.phase === "error"
                ? "error"
                : rows.length
                  ? "ready"
                  : "empty",
          message,
        }}
      />
    </PageSurface>
  );
}
