import {
  useCallback,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
  type SubmitEvent,
} from "react";
import {
  Button,
  ConfirmationDialog,
  EditableTable,
  GraphInspector,
  RelationshipGraph,
  StatePanel,
  type EditableTableColumn,
  type EditableTableRow,
  type RelationshipGraphColumn,
  type RelationshipGraphNodeContext,
} from "@opendle/ui";
import {
  errorMessage,
  type AdministrationClient,
  type Assignment,
  type AssignmentWrite,
  type Credential,
  type Model,
  type ModelWrite,
  type OpenRouterModelImportPreview,
  type ObservedRequirement,
  type Provider,
  type ProviderAdapter,
  type ProviderModel,
  type ProviderModelWrite,
  type ReasoningLevel,
} from "./api.js";
import {
  adapterFieldPolicy,
  discardConfirmedRecord,
  includeConfirmedRecords,
  parseConfigurationNodeId,
  projectConfigurationGraph,
  pruneAcknowledgedRecords,
  retainConfirmedRecord,
  validateAssignmentChain,
  type ConfigurationLoadPhase,
  type ConfigurationRecordKind,
} from "./configurationState.js";
import { credentialFormValue, parseManualPrice } from "./formContracts.js";

interface ConfigurationGraphProps {
  readonly assignments: readonly Assignment[];
  readonly client: AdministrationClient;
  readonly credentials: readonly Credential[];
  readonly csrf: string;
  readonly globalPhase?: ConfigurationLoadPhase;
  readonly models: readonly Model[];
  readonly onAssignmentDirtyChange: (dirty: boolean) => void;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefreshAssignments: () => Promise<void>;
  readonly onRefreshGlobal: () => Promise<void>;
  readonly providerModels: readonly ProviderModel[];
  readonly providers: readonly Provider[];
  readonly selectedService: string;
}

interface Inspector {
  readonly kind: ConfigurationRecordKind;
  readonly apiName: string | null;
  readonly providerApiName?: string;
  readonly modelApiName?: string;
}

interface DeleteTarget {
  readonly kind:
    ConfigurationRecordKind | "credential" | "requirement" | "draft";
  readonly apiName: string;
  readonly impact: string;
  readonly requirement?: ObservedRequirement;
}

interface ChainDraft {
  readonly providerModel: string;
}

interface ConfirmedGlobalRecords {
  readonly providers: readonly Provider[];
  readonly models: readonly Model[];
  readonly mappings: readonly ProviderModel[];
}

interface ConfigurationViewState {
  readonly inspector: Inspector | null;
  readonly selectedNodeId: string | null;
  readonly deleteTarget: DeleteTarget | null;
  readonly pending: boolean;
  readonly assignmentDirty: boolean;
  readonly chainRows: readonly EditableTableRow<ChainDraft>[];
  readonly importInput: string;
  readonly importPreview: OpenRouterModelImportPreview | null;
  readonly selectedImportProviders: ReadonlySet<string>;
}

interface ConfigurationInspectorContext {
  readonly assignmentByName: ReadonlyMap<string, Assignment>;
  readonly assignmentDirty: boolean;
  readonly chainRows: readonly EditableTableRow<ChainDraft>[];
  readonly client: AdministrationClient;
  readonly closeInspector: () => void;
  readonly confirmOpenRouter: () => Promise<void>;
  readonly credentials: readonly Credential[];
  readonly csrf: string;
  readonly importInput: string;
  readonly importPreview: OpenRouterModelImportPreview | null;
  readonly inspector: Inspector;
  readonly markAssignmentDirty: () => void;
  readonly mappingByName: ReadonlyMap<string, ProviderModel>;
  readonly modelByName: ReadonlyMap<string, Model>;
  readonly models: readonly Model[];
  readonly onAssignmentDirtyChange: (dirty: boolean) => void;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefreshAssignments: () => Promise<void>;
  readonly onRefreshGlobal: () => Promise<void>;
  readonly pending: boolean;
  readonly previewOpenRouter: (
    event: SubmitEvent<HTMLFormElement>,
  ) => Promise<void>;
  readonly providerByName: ReadonlyMap<string, Provider>;
  readonly providerModels: readonly ProviderModel[];
  readonly providers: readonly Provider[];
  readonly returnFocusRef: RefObject<HTMLElement | null>;
  readonly saveAssignment: (
    event: SubmitEvent<HTMLFormElement>,
  ) => Promise<void>;
  readonly saveCredential: (
    event: SubmitEvent<HTMLFormElement>,
  ) => Promise<void>;
  readonly saveMapping: (event: SubmitEvent<HTMLFormElement>) => Promise<void>;
  readonly saveModel: (event: SubmitEvent<HTMLFormElement>) => Promise<void>;
  readonly saveProvider: (event: SubmitEvent<HTMLFormElement>) => Promise<void>;
  readonly selectedImportProviders: ReadonlySet<string>;
  readonly selectedService: string;
  readonly setAssignmentDirty: (value: boolean) => void;
  readonly setChainRows: Dispatch<
    SetStateAction<readonly EditableTableRow<ChainDraft>[]>
  >;
  readonly setDeleteTarget: (value: DeleteTarget | null) => void;
  readonly setImportInput: (value: string) => void;
  readonly setInspector: (value: Inspector | null) => void;
  readonly setPending: (value: boolean) => void;
  readonly setSelectedNodeId: (value: string | null) => void;
  readonly setSelectedImportProviders: (value: ReadonlySet<string>) => void;
}

type ConfigurationViewAction =
  | { readonly type: "patch"; readonly patch: Partial<ConfigurationViewState> }
  | {
      readonly type: "chain-rows";
      readonly value: SetStateAction<readonly EditableTableRow<ChainDraft>[]>;
    };

function useConfigurationViewState() {
  const [state, dispatch] = useReducer(
    (current: ConfigurationViewState, action: ConfigurationViewAction) =>
      action.type === "patch"
        ? { ...current, ...action.patch }
        : {
            ...current,
            chainRows:
              typeof action.value === "function"
                ? action.value(current.chainRows)
                : action.value,
          },
    {
      inspector: null,
      selectedNodeId: null,
      deleteTarget: null,
      pending: false,
      assignmentDirty: false,
      chainRows: [],
      importInput: "",
      importPreview: null,
      selectedImportProviders: new Set<string>(),
    },
  );
  const patch = useCallback((value: Partial<ConfigurationViewState>) => {
    dispatch({ type: "patch", patch: value });
  }, []);
  return {
    state,
    setInspector: useCallback(
      (value: Inspector | null) => {
        patch({ inspector: value });
      },
      [patch],
    ),
    setSelectedNodeId: useCallback(
      (value: string | null) => {
        patch({ selectedNodeId: value });
      },
      [patch],
    ),
    setDeleteTarget: useCallback(
      (value: DeleteTarget | null) => {
        patch({ deleteTarget: value });
      },
      [patch],
    ),
    setPending: useCallback(
      (value: boolean) => {
        patch({ pending: value });
      },
      [patch],
    ),
    setAssignmentDirty: useCallback(
      (value: boolean) => {
        patch({ assignmentDirty: value });
      },
      [patch],
    ),
    setChainRows: useCallback(
      (value: SetStateAction<readonly EditableTableRow<ChainDraft>[]>) => {
        dispatch({ type: "chain-rows", value });
      },
      [],
    ),
    setImportInput: useCallback(
      (value: string) => {
        patch({ importInput: value });
      },
      [patch],
    ),
    setImportPreview: useCallback(
      (value: OpenRouterModelImportPreview | null) => {
        patch({ importPreview: value });
      },
      [patch],
    ),
    setSelectedImportProviders: useCallback(
      (value: ReadonlySet<string>) => {
        patch({ selectedImportProviders: value });
      },
      [patch],
    ),
  };
}

const providerAdapters: readonly ProviderAdapter[] = [
  "openai",
  "openai_compatible",
  "openrouter",
  "custom",
  "wavespeed",
  "ollama",
  "local_embeddings",
  "fake",
];

function formValue(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function commaValues(value: string): readonly string[] {
  return value.split(",").flatMap((item) => {
    const result = item.trim();
    return result === "" ? [] : [result];
  });
}

function numberValue(form: FormData, name: string): number | undefined {
  const value = formValue(form, name);
  if (value === "") return undefined;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1)
    throw new Error(`${name.replaceAll("_", " ")} must be a positive integer.`);
  return parsed;
}

function numberValues(
  form: FormData,
  name: string,
): readonly number[] | undefined {
  const values = commaValues(formValue(form, name));
  if (values.length === 0) return undefined;
  const result = values.map((value) => Number(value));
  if (result.some((value) => !Number.isSafeInteger(value) || value < 1))
    throw new Error(
      `${name.replaceAll("_", " ")} must contain positive integers.`,
    );
  if (new Set(result).size !== result.length)
    throw new Error(
      `${name.replaceAll("_", " ")} must not contain duplicates.`,
    );
  return result;
}

function constraintValue(form: FormData) {
  const maxContextTokens = numberValue(form, "max_context_tokens");
  const maxOutputTokens = numberValue(form, "max_output_tokens");
  const embeddingDimensions = numberValues(form, "embedding_dimensions");
  const maxInputImages = numberValue(form, "max_input_images");
  const maxInputImageBytes = numberValue(form, "max_input_image_bytes");
  const maxOutputDurationSeconds = numberValue(
    form,
    "max_output_duration_seconds",
  );
  return {
    ...(maxContextTokens === undefined
      ? {}
      : { max_context_tokens: maxContextTokens }),
    ...(maxOutputTokens === undefined
      ? {}
      : { max_output_tokens: maxOutputTokens }),
    ...(embeddingDimensions === undefined
      ? {}
      : { embedding_dimensions: embeddingDimensions }),
    ...(maxInputImages === undefined
      ? {}
      : { max_input_images: maxInputImages }),
    ...(maxInputImageBytes === undefined
      ? {}
      : { max_input_image_bytes: maxInputImageBytes }),
    ...(maxOutputDurationSeconds === undefined
      ? {}
      : { max_output_duration_seconds: maxOutputDurationSeconds }),
  };
}

function modelValue(form: FormData): ModelWrite {
  const constraints = constraintValue(form);
  const manualPrice = parseManualPrice(
    formValue(form, "currency"),
    formValue(form, "unit_prices"),
  );
  return {
    api_name: formValue(form, "api_name"),
    display_name: formValue(form, "display_name"),
    input_modalities: commaValues(formValue(form, "input_modalities")) as (
      "text" | "image"
    )[],
    output_modalities: commaValues(formValue(form, "output_modalities")) as (
      "text" | "structured_json" | "embedding" | "image" | "video" | "audio"
    )[],
    capabilities: commaValues(formValue(form, "capabilities")) as (
      "tool_calling" | "streaming" | "reasoning"
    )[],
    ...(Object.keys(constraints).length === 0 ? {} : { constraints }),
    ...(formValue(form, "price_source") === ""
      ? {}
      : {
          price_source: formValue(form, "price_source"),
          price_lookup_key: formValue(form, "price_lookup_key"),
        }),
    ...(manualPrice === null ? {} : { manual_price: manualPrice }),
  };
}

function providerValue(form: FormData): ProviderModelWrite {
  const supportedReasoningLevels: readonly ReasoningLevel[] = [
    "none",
    "low",
    "medium",
    "high",
  ];
  const reasoning_mappings = commaValues(
    formValue(form, "reasoning_mappings"),
  ).map((entry) => {
    const separator = entry.indexOf("=");
    if (separator < 1 || separator === entry.length - 1)
      throw new Error("Use reasoning mappings such as none=disabled.");
    const level = entry.slice(0, separator).trim();
    if (!supportedReasoningLevels.some((candidate) => candidate === level))
      throw new Error(
        "Use the supported reasoning levels: none, low, medium, or high.",
      );
    return {
      level: level as ReasoningLevel,
      provider_value: entry.slice(separator + 1).trim(),
    };
  });
  if (
    new Set(reasoning_mappings.map((item) => item.level)).size !==
    reasoning_mappings.length
  )
    throw new Error("Enter each reasoning level only once.");
  const constraints = constraintValue(form);
  const manualPrice = parseManualPrice(
    formValue(form, "currency"),
    formValue(form, "unit_prices"),
  );
  return {
    api_name: formValue(form, "api_name"),
    provider_api_name: formValue(form, "provider_api_name"),
    model_api_name: formValue(form, "model_api_name"),
    provider_model_name: formValue(form, "provider_model_name"),
    enabled: form.get("enabled") === "on",
    ...(formValue(form, "input_modalities") === ""
      ? {}
      : {
          input_modalities: commaValues(
            formValue(form, "input_modalities"),
          ) as ("text" | "image")[],
        }),
    ...(formValue(form, "output_modalities") === ""
      ? {}
      : {
          output_modalities: commaValues(
            formValue(form, "output_modalities"),
          ) as Model["output_modalities"],
        }),
    ...(formValue(form, "capabilities") === ""
      ? {}
      : {
          capabilities: commaValues(
            formValue(form, "capabilities"),
          ) as Model["capabilities"],
        }),
    ...(Object.keys(constraints).length === 0 ? {} : { constraints }),
    ...(reasoning_mappings.length === 0 ? {} : { reasoning_mappings }),
    ...(formValue(form, "price_source") === ""
      ? {}
      : {
          price_source: formValue(form, "price_source"),
          price_lookup_key: formValue(form, "price_lookup_key"),
        }),
    ...(manualPrice === null ? {} : { manual_price: manualPrice }),
  };
}

function recordFacts(entries: readonly (readonly [string, string])[]) {
  return (
    <dl className="configuration-facts">
      {entries.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function GraphState({
  phase,
  onRetry,
}: {
  readonly phase: ConfigurationLoadPhase;
  readonly onRetry: () => void;
}) {
  if (phase === "loading")
    return (
      <StatePanel kind="loading" title="Loading configuration">
        Wait while the Router reads the global catalog.
      </StatePanel>
    );
  if (phase === "error")
    return (
      <StatePanel
        kind="error"
        onRetry={onRetry}
        title="Configuration unavailable"
      >
        Existing confirmed records remain unchanged. Try the read again.
      </StatePanel>
    );
  return null;
}

function useConfigurationController({
  assignments,
  client,
  credentials,
  csrf,
  globalPhase = "ready",
  models,
  onAssignmentDirtyChange,
  onNotice,
  onRefreshAssignments,
  onRefreshGlobal,
  providerModels,
  providers,
  selectedService,
}: ConfigurationGraphProps) {
  const {
    state: {
      assignmentDirty,
      chainRows,
      deleteTarget,
      importInput,
      importPreview,
      inspector,
      pending,
      selectedImportProviders,
      selectedNodeId,
    },
    setAssignmentDirty,
    setChainRows,
    setDeleteTarget,
    setImportInput,
    setImportPreview,
    setInspector,
    setPending,
    setSelectedImportProviders,
    setSelectedNodeId,
  } = useConfigurationViewState();
  const [confirmedGlobal, setConfirmedGlobal] =
    useState<ConfirmedGlobalRecords>({
      providers: [],
      models: [],
      mappings: [],
    });
  const authoritativeNames = JSON.stringify({
    providers: providers.map((item) => item.api_name),
    models: models.map((item) => item.api_name),
    mappings: providerModels.map((item) => item.api_name),
  });
  const [previousAuthoritativeNames, setPreviousAuthoritativeNames] =
    useState(authoritativeNames);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  if (authoritativeNames !== previousAuthoritativeNames) {
    setPreviousAuthoritativeNames(authoritativeNames);
    setConfirmedGlobal((current) => {
      const next = {
        providers: pruneAcknowledgedRecords(providers, current.providers),
        models: pruneAcknowledgedRecords(models, current.models),
        mappings: pruneAcknowledgedRecords(providerModels, current.mappings),
      };
      return next.providers === current.providers &&
        next.models === current.models &&
        next.mappings === current.mappings
        ? current
        : next;
    });
  }

  const visibleProviders = useMemo(
    () => includeConfirmedRecords(providers, confirmedGlobal.providers),
    [confirmedGlobal.providers, providers],
  );
  const visibleModels = useMemo(
    () => includeConfirmedRecords(models, confirmedGlobal.models),
    [confirmedGlobal.models, models],
  );
  const visibleMappings = useMemo(
    () => includeConfirmedRecords(providerModels, confirmedGlobal.mappings),
    [confirmedGlobal.mappings, providerModels],
  );

  const projection = useMemo(
    () =>
      projectConfigurationGraph(
        visibleProviders,
        visibleModels,
        visibleMappings,
        assignments,
      ),
    [assignments, visibleMappings, visibleModels, visibleProviders],
  );
  const providerByName = useMemo(
    () => new Map(visibleProviders.map((item) => [item.api_name, item])),
    [visibleProviders],
  );
  const modelByName = useMemo(
    () => new Map(visibleModels.map((item) => [item.api_name, item])),
    [visibleModels],
  );
  const mappingByName = useMemo(
    () => new Map(visibleMappings.map((item) => [item.api_name, item])),
    [visibleMappings],
  );
  const assignmentByName = useMemo(
    () => new Map(assignments.map((item) => [item.api_name, item])),
    [assignments],
  );
  function markAssignmentDirty() {
    if (assignmentDirty) return;
    setAssignmentDirty(true);
    onAssignmentDirtyChange(true);
  }

  function closeInspector() {
    if (pending) return;
    if (inspector?.kind === "assignment" && assignmentDirty) {
      setDeleteTarget({
        kind: "draft",
        apiName: inspector.apiName ?? "new assignment",
        impact: `discard unsaved assignment changes for service ${selectedService}`,
      });
      return;
    }
    setInspector(null);
  }

  function activate(context: RelationshipGraphNodeContext) {
    const identity = parseConfigurationNodeId(context.node.id);
    if (identity === null) return;
    returnFocusRef.current = context.trigger;
    if (identity.kind === "assignment") {
      const assignment = assignmentByName.get(identity.apiName);
      const chain =
        assignment?.direct_chain ?? assignment?.effective_chain ?? [];
      setChainRows(
        chain.map((candidate, index) => ({
          id: `chain:${String(index)}:${candidate.provider_model_api_name}`,
          label: `Fallback ${String(index + 1)}`,
          draft: { providerModel: candidate.provider_model_api_name },
        })),
      );
    }
    setInspector({ kind: identity.kind, apiName: identity.apiName });
  }

  function openCreate(
    kind: ConfigurationRecordKind,
    trigger: HTMLButtonElement,
  ) {
    returnFocusRef.current = trigger;
    setSelectedNodeId(null);
    setAssignmentDirty(false);
    onAssignmentDirtyChange(false);
    if (kind === "assignment") setChainRows([]);
    setInspector({ kind, apiName: null });
  }

  const columns: readonly [
    RelationshipGraphColumn,
    RelationshipGraphColumn,
    RelationshipGraphColumn,
  ] = [
    {
      id: "providers",
      label: "Global providers",
      countLabel: `${String(visibleProviders.length)} global`,
      actions: (
        <Button
          onClick={(event) => {
            openCreate("provider", event.currentTarget);
          }}
          variant="secondary"
        >
          Add provider
        </Button>
      ),
      emptyState: "No global provider connections.",
      nodes: projection.providerIds.map((id) => {
        const provider = providerByName.get(id.slice("provider:".length));
        if (provider === undefined) throw new Error(`Missing ${id}.`);
        return {
          id,
          label: provider.display_name,
          detail: `${provider.api_name} · ${provider.adapter}`,
          searchText: [provider.api_name, provider.adapter],
          state: provider.enabled
            ? ("default" as const)
            : ("disabled" as const),
          stateLabel: provider.enabled
            ? "Global and enabled"
            : "Global and disabled",
        };
      }),
    },
    {
      id: "catalog",
      label: "Global models and mappings",
      countLabel: `${String(visibleModels.length)} models · ${String(visibleMappings.length)} mappings`,
      actions: (
        <span className="configuration-column-actions">
          <Button
            onClick={(event) => {
              openCreate("model", event.currentTarget);
            }}
            variant="secondary"
          >
            Add model
          </Button>
          <Button
            onClick={(event) => {
              openCreate("mapping", event.currentTarget);
            }}
            variant="secondary"
          >
            Add mapping
          </Button>
        </span>
      ),
      emptyState: "No canonical models or provider-model mappings.",
      nodes: projection.catalogIds.map((id) => {
        const identity = parseConfigurationNodeId(id);
        if (identity?.kind === "model") {
          const model = modelByName.get(identity.apiName);
          if (model === undefined) throw new Error(`Missing ${id}.`);
          return {
            id,
            label: model.display_name,
            detail: `${model.api_name} · canonical model`,
            searchText: [model.api_name, ...model.capabilities],
            stateLabel: "Global canonical model",
          };
        }
        const mapping = mappingByName.get(identity?.apiName ?? "");
        if (mapping === undefined) throw new Error(`Missing ${id}.`);
        const model = modelByName.get(mapping.model_api_name);
        return {
          id,
          label: `${model?.display_name ?? mapping.model_api_name} — ${mapping.api_name}`,
          detail: `${mapping.provider_api_name} · ${mapping.provider_model_name}`,
          searchText: [
            mapping.api_name,
            mapping.model_api_name,
            mapping.provider_api_name,
            mapping.provider_model_name,
          ],
          state: mapping.enabled ? ("default" as const) : ("disabled" as const),
          stateLabel: mapping.enabled
            ? "Global provider-model mapping"
            : "Global provider-model mapping, disabled",
        };
      }),
    },
    {
      id: "assignments",
      label:
        selectedService === ""
          ? "Service assignments"
          : `${selectedService} assignments`,
      countLabel:
        selectedService === ""
          ? "Select a service"
          : `${String(assignments.length)} effective`,
      actions: (
        <Button
          disabled={selectedService === ""}
          onClick={(event) => {
            openCreate("assignment", event.currentTarget);
          }}
          variant="secondary"
        >
          Add assignment
        </Button>
      ),
      emptyState:
        selectedService === ""
          ? "Select one service to configure assignments. Providers, models, mappings, credentials, and prices remain global."
          : "This service has no effective assignment records.",
      nodes: projection.assignmentIds.map((id) => {
        const assignment = assignmentByName.get(id.slice("assignment:".length));
        if (assignment === undefined) throw new Error(`Missing ${id}.`);
        const local =
          assignment.defined_by_service_api_name === selectedService;
        return {
          id,
          label: assignment.display_name,
          detail: `${assignment.api_name} · ${local ? "local" : `inherited from ${assignment.defined_by_service_api_name ?? "implicit root"}`}`,
          searchText: [
            assignment.api_name,
            assignment.definition_kind,
            assignment.defined_by_service_api_name ?? "implicit",
            ...assignment.effective_chain.map(
              (item) => item.provider_model_api_name,
            ),
          ],
          stateLabel: local
            ? "Selected service assignment, local definition"
            : "Selected service assignment, inherited definition",
        };
      }),
    },
  ];

  async function saveProvider(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const adapter = formValue(form, "adapter") as ProviderAdapter;
    const policy = adapterFieldPolicy[adapter];
    const endpoint = formValue(form, "endpoint");
    const credential = formValue(form, "credential_api_name");
    if (policy.endpoint === "required" && endpoint === "") {
      onNotice(
        "error",
        "Enter the required custom endpoint in Advanced settings.",
      );
      return;
    }
    if (policy.credential === "required" && credential === "") {
      onNotice(
        "error",
        "Select an applicable credential before you enable this connection.",
      );
      return;
    }
    const value = {
      api_name: formValue(form, "api_name"),
      display_name: formValue(form, "display_name"),
      adapter,
      ...(endpoint === "" ? {} : { endpoint }),
      ...(credential === "" ? {} : { credential_api_name: credential }),
      enabled: form.get("enabled") === "on",
    };
    setPending(true);
    try {
      const created = inspector?.apiName === null;
      const saved = created
        ? await client.createProvider(value, csrf)
        : await client.putProvider(value.api_name, value, csrf);
      if (created)
        setConfirmedGlobal((current) => ({
          ...current,
          providers: retainConfirmedRecord(current.providers, saved),
        }));
      await onRefreshGlobal();
      onNotice("success", "The global provider connection was saved.");
      setSelectedNodeId(`provider:${value.api_name}`);
      setInspector({ kind: "provider", apiName: value.api_name });
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function saveCredential(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const { apiName: name, secret } = credentialFormValue(form);
    setPending(true);
    try {
      if (credentials.some((item) => item.api_name === name))
        await client.replaceCredential(name, secret, csrf);
      else await client.createCredential(name, secret, csrf);
      formElement.reset();
      await onRefreshGlobal();
      onNotice("success", "The write-only credential was saved.");
    } catch (error) {
      const secretInput = formElement.elements.namedItem("secret");
      if (secretInput instanceof HTMLInputElement) secretInput.value = "";
      onNotice("error", errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function saveModel(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    let value: ModelWrite;
    try {
      value = modelValue(new FormData(event.currentTarget));
    } catch (error) {
      onNotice(
        "error",
        error instanceof Error ? error.message : "The model is invalid.",
      );
      return;
    }
    setPending(true);
    try {
      const created = inspector?.apiName === null;
      const saved = created
        ? await client.createModel(value, csrf)
        : await client.putModel(value.api_name, value, csrf);
      if (created)
        setConfirmedGlobal((current) => ({
          ...current,
          models: retainConfirmedRecord(current.models, saved),
        }));
      await onRefreshGlobal();
      onNotice("success", "The global canonical model was saved.");
      setSelectedNodeId(`model:${value.api_name}`);
      setInspector({ kind: "model", apiName: value.api_name });
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function saveMapping(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    let value: ProviderModelWrite;
    try {
      value = providerValue(new FormData(event.currentTarget));
    } catch (error) {
      onNotice(
        "error",
        error instanceof Error ? error.message : "The mapping is invalid.",
      );
      return;
    }
    setPending(true);
    try {
      const created = inspector?.apiName === null;
      const saved = created
        ? await client.createProviderModel(value, csrf)
        : await client.putProviderModel(value.api_name, value, csrf);
      if (created)
        setConfirmedGlobal((current) => ({
          ...current,
          mappings: retainConfirmedRecord(current.mappings, saved),
        }));
      await onRefreshGlobal();
      onNotice("success", "The global provider-model mapping was saved.");
      setSelectedNodeId(`mapping:${value.api_name}`);
      setInspector({ kind: "mapping", apiName: value.api_name });
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function saveAssignment(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (selectedService === "") return;
    const form = new FormData(event.currentTarget);
    const name = formValue(form, "api_name");
    const mode = formValue(form, "definition_kind");
    const chain = chainRows.map((row) => row.draft.providerModel);
    const validation =
      mode === "direct" ? validateAssignmentChain(chain) : null;
    if (validation !== null) {
      onNotice("error", validation);
      return;
    }
    const reasoning = formValue(form, "reasoning_level");
    const value: AssignmentWrite =
      mode === "inherit"
        ? {
            display_name: formValue(form, "display_name"),
            inherits_assignment_api_name: formValue(
              form,
              "inherits_assignment_api_name",
            ),
            ...(reasoning === ""
              ? {}
              : { reasoning_level: reasoning as ReasoningLevel }),
          }
        : {
            display_name: formValue(form, "display_name"),
            direct_chain: chain.map((providerModel) => ({
              provider_model_api_name: providerModel,
            })),
            ...(reasoning === ""
              ? {}
              : { reasoning_level: reasoning as ReasoningLevel }),
          };
    setPending(true);
    try {
      await client.putAssignment(selectedService, name, value, csrf);
      await onRefreshAssignments();
      setAssignmentDirty(false);
      onAssignmentDirtyChange(false);
      onNotice("success", "The selected service assignment was saved.");
      setSelectedNodeId(`assignment:${name}`);
      setInspector({ kind: "assignment", apiName: name });
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function previewOpenRouter(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setImportPreview(null);
    setSelectedImportProviders(new Set());
    try {
      const preview = await client.previewOpenRouterModel(importInput, csrf);
      setImportPreview(preview);
      setSelectedImportProviders(
        new Set(
          preview.provider_options.flatMap((item) =>
            item.selectable ? [item.provider_api_name] : [],
          ),
        ),
      );
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function confirmOpenRouter() {
    if (importPreview === null) return;
    const provider_models = importPreview.provider_options.flatMap((option) =>
      selectedImportProviders.has(option.provider_api_name) && option.selectable
        ? [option.provider_model]
        : [],
    );
    setPending(true);
    try {
      const reviewed = {
        source_model_id: importPreview.source_model_id,
        model: importPreview.model,
        ...(importPreview.reviewed_price === undefined
          ? {}
          : { reviewed_price: importPreview.reviewed_price }),
        provider_models,
      };
      const result = await client.importOpenRouterModel(reviewed, csrf);
      setConfirmedGlobal((current) => ({
        ...current,
        models: retainConfirmedRecord(current.models, result.model),
        mappings: result.provider_models.reduce(
          (records, mapping) => retainConfirmedRecord(records, mapping),
          current.mappings,
        ),
      }));
      await onRefreshGlobal();
      setImportPreview(null);
      setImportInput("");
      setSelectedNodeId(`model:${result.model.api_name}`);
      setInspector({ kind: "model", apiName: result.model.api_name });
      onNotice(
        "success",
        "The reviewed model and mappings were created atomically.",
      );
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function deleteRecord() {
    if (deleteTarget === null) return;
    if (deleteTarget.kind === "draft") {
      setAssignmentDirty(false);
      onAssignmentDirtyChange(false);
      setDeleteTarget(null);
      setInspector(null);
      return;
    }
    setPending(true);
    try {
      if (deleteTarget.kind === "provider")
        await client.deleteProvider(deleteTarget.apiName, csrf);
      else if (deleteTarget.kind === "model")
        await client.deleteModel(deleteTarget.apiName, csrf);
      else if (deleteTarget.kind === "mapping")
        await client.deleteProviderModel(deleteTarget.apiName, csrf);
      else if (deleteTarget.kind === "credential")
        await client.deleteCredential(deleteTarget.apiName, csrf);
      else if (
        deleteTarget.kind === "requirement" &&
        deleteTarget.requirement !== undefined &&
        selectedService !== ""
      )
        await client.removeRequirement(
          selectedService,
          deleteTarget.apiName,
          deleteTarget.requirement,
          csrf,
        );
      else if (selectedService !== "")
        await client.deleteAssignment(
          selectedService,
          deleteTarget.apiName,
          csrf,
        );
      setConfirmedGlobal((current) => ({
        providers:
          deleteTarget.kind === "provider"
            ? discardConfirmedRecord(current.providers, deleteTarget.apiName)
            : current.providers,
        models:
          deleteTarget.kind === "model"
            ? discardConfirmedRecord(current.models, deleteTarget.apiName)
            : current.models,
        mappings:
          deleteTarget.kind === "mapping"
            ? discardConfirmedRecord(current.mappings, deleteTarget.apiName)
            : current.mappings,
      }));
      if (
        deleteTarget.kind === "assignment" ||
        deleteTarget.kind === "requirement"
      )
        await onRefreshAssignments();
      else await onRefreshGlobal();
      setDeleteTarget(null);
      if (deleteTarget.kind !== "requirement") setInspector(null);
      onNotice(
        "success",
        deleteTarget.kind === "requirement"
          ? "The observed requirement was removed."
          : "The selected configuration record was deleted.",
      );
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  const inspectorContext =
    inspector === null
      ? null
      : ({
          assignmentByName,
          assignmentDirty,
          chainRows,
          client,
          closeInspector,
          confirmOpenRouter,
          credentials,
          csrf,
          importInput,
          importPreview,
          inspector,
          markAssignmentDirty,
          mappingByName,
          modelByName,
          models: visibleModels,
          onAssignmentDirtyChange,
          onNotice,
          onRefreshAssignments,
          onRefreshGlobal,
          pending,
          previewOpenRouter,
          providerByName,
          providerModels: visibleMappings,
          providers: visibleProviders,
          returnFocusRef,
          saveAssignment,
          saveCredential,
          saveMapping,
          saveModel,
          saveProvider,
          selectedImportProviders,
          selectedService,
          setAssignmentDirty,
          setChainRows,
          setDeleteTarget,
          setImportInput,
          setInspector,
          setPending,
          setSelectedNodeId,
          setSelectedImportProviders,
        } satisfies ConfigurationInspectorContext);
  const inspectorContent =
    inspectorContext === null ? null : (
      <ConfigurationInspector context={inspectorContext} />
    );
  const auxiliaryInspector =
    inspector?.apiName === null ? inspectorContent : null;
  const selectedNodeInspector =
    inspector?.apiName === null ? null : inspectorContent;
  const emptyCatalogState = (
    <div>
      <p>
        No configuration records are available. Add a global provider or
        canonical model.
      </p>
      <div className="configuration-column-actions">
        <Button
          onClick={(event) => {
            openCreate("provider", event.currentTarget);
          }}
          variant="secondary"
        >
          Add provider
        </Button>
        <Button
          onClick={(event) => {
            openCreate("model", event.currentTarget);
          }}
          variant="secondary"
        >
          Add model
        </Button>
      </div>
    </div>
  );

  return {
    activate,
    auxiliaryInspector,
    columns,
    deleteRecord,
    deleteTarget,
    globalPhase,
    emptyCatalogState,
    onRefreshGlobal,
    pending,
    relationships: projection.relationships,
    returnFocusRef,
    setDeleteTarget,
    selectedNodeId,
    selectedNodeInspector,
    setSelectedNodeId,
  };
}

export function ConfigurationGraph(props: ConfigurationGraphProps) {
  const {
    activate,
    auxiliaryInspector,
    columns,
    deleteRecord,
    deleteTarget,
    globalPhase,
    emptyCatalogState,
    onRefreshGlobal,
    pending,
    relationships,
    returnFocusRef,
    setDeleteTarget,
    selectedNodeId,
    selectedNodeInspector,
    setSelectedNodeId,
  } = useConfigurationController(props);
  const graphState = (
    <GraphState onRetry={() => void onRefreshGlobal()} phase={globalPhase} />
  );
  return (
    <section className="configuration-graph-page">
      {globalPhase === "partial" ? (
        <StatePanel kind="empty" title="Partial configuration graph">
          The Router returned a bounded subset. More global records are
          available. This graph does not claim to be complete.
        </StatePanel>
      ) : null}
      {globalPhase === "loading" || globalPhase === "error" ? (
        graphState
      ) : (
        <RelationshipGraph
          aria-label="Global LLM configuration and selected service assignments"
          auxiliaryInspector={auxiliaryInspector}
          columns={columns}
          emptyState={emptyCatalogState}
          inspector={selectedNodeInspector}
          noResultsDescription="Change the search or restore the complete global configuration graph."
          onNodeActivate={activate}
          onSelectionChange={setSelectedNodeId}
          relationships={relationships}
          searchLabel="Search providers, models, mappings, and assignments"
          selectedNodeId={selectedNodeId}
        />
      )}
      <ConfirmationDialog
        confirmLabel={
          deleteTarget?.kind === "draft"
            ? "Discard changes"
            : deleteTarget?.kind === "requirement"
              ? "Remove requirement"
              : "Delete record"
        }
        description={deleteTarget?.impact ?? "Delete the selected record."}
        {...(deleteTarget === null
          ? {}
          : { impactStatement: deleteTarget.impact })}
        onCancel={() => {
          setDeleteTarget(null);
        }}
        onConfirm={() => void deleteRecord()}
        open={deleteTarget !== null}
        pending={pending}
        {...(deleteTarget?.kind === "draft" ? {} : { returnFocusRef })}
        title={
          deleteTarget?.kind === "draft"
            ? "Discard assignment changes?"
            : deleteTarget?.kind === "requirement"
              ? "Remove this observed requirement?"
              : "Confirm configuration deletion"
        }
      />
    </section>
  );
}

function ConfigurationInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  if (context.inspector.kind === "provider")
    return <ProviderInspector context={context} />;
  if (context.inspector.kind === "model")
    return <ModelInspector context={context} />;
  if (context.inspector.kind === "mapping")
    return <MappingInspector context={context} />;
  return <AssignmentInspector context={context} />;
}

function ProviderInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  const {
    credentials,
    inspector,
    closeInspector,
    returnFocusRef,
    saveProvider,
    pending,
    saveCredential,
    setDeleteTarget,
    setInspector,
    providerByName,
  } = context;
  const provider =
    inspector.apiName === null
      ? undefined
      : providerByName.get(inspector.apiName);
  const adapter = provider?.adapter ?? "openai";
  const [selectedAdapter, setSelectedAdapter] = useState(adapter);
  const fieldPolicy = adapterFieldPolicy[selectedAdapter];
  return (
    <GraphInspector
      activationKey={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      eyebrow="Global provider connection"
      onClose={closeInspector}
      returnFocusRef={returnFocusRef}
      title={provider?.display_name ?? "Add provider"}
    >
      {provider === undefined
        ? null
        : recordFacts([
            ["API name", provider.api_name],
            ["Adapter", provider.adapter],
            ["Endpoint", provider.endpoint ?? "Router standard endpoint"],
            ["Credential", provider.credential_api_name ?? "None"],
            ["State", provider.enabled ? "Enabled" : "Disabled"],
          ])}
      <form
        className="configuration-form"
        onSubmit={(event) => void saveProvider(event)}
      >
        <label>
          API name
          <input
            defaultValue={provider?.api_name}
            readOnly={provider !== undefined}
            name="api_name"
            required
          />
        </label>
        <label>
          Display name
          <input
            defaultValue={provider?.display_name}
            name="display_name"
            required
          />
        </label>
        <label>
          Adapter
          <select
            name="adapter"
            onChange={(event) => {
              setSelectedAdapter(event.currentTarget.value as ProviderAdapter);
            }}
            value={selectedAdapter}
          >
            {providerAdapters.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        {fieldPolicy.credential === "none" ? (
          <p className="field-note">This adapter does not use a credential.</p>
        ) : (
          <label>
            Applicable credential
            <select
              defaultValue={provider?.credential_api_name ?? ""}
              name="credential_api_name"
              required={fieldPolicy.credential === "required"}
            >
              <option value="">
                {fieldPolicy.credential === "required"
                  ? "Select credential"
                  : "No credential"}
              </option>
              {credentials.map((item) => (
                <option key={item.api_name}>{item.api_name}</option>
              ))}
            </select>
          </label>
        )}
        <details className="configuration-advanced">
          <summary>Advanced settings and review</summary>
          <p>
            {fieldPolicy.endpoint === "inferred"
              ? "The Router will use the registered standard endpoint and safe adapter defaults."
              : "This adapter has no registered standard endpoint. Review the explicit endpoint before save."}
          </p>
          {fieldPolicy.endpoint === "required" ? (
            <label>
              Custom endpoint
              <input
                defaultValue={provider?.endpoint ?? ""}
                name="endpoint"
                placeholder="https://provider.example/v1"
                required
                type="url"
              />
            </label>
          ) : null}
          <label className="checkbox-field">
            <input
              defaultChecked={provider?.enabled ?? true}
              name="enabled"
              type="checkbox"
            />
            Enabled after validation
          </label>
        </details>
        <Button disabled={pending} type="submit">
          {pending ? "Saving…" : "Review and save provider"}
        </Button>
      </form>
      <section className="configuration-inspector-section">
        <h3>Write-only credential</h3>
        <p>
          Create or replace one encrypted credential. The Router never returns
          its secret.
        </p>
        <form
          className="configuration-form"
          onSubmit={(event) => void saveCredential(event)}
        >
          <label>
            Credential API name
            <input autoComplete="off" name="credential_api_name" required />
          </label>
          <label>
            New secret
            <input
              autoComplete="new-password"
              name="secret"
              required
              type="password"
            />
          </label>
          <Button disabled={pending} type="submit">
            Save credential
          </Button>
        </form>
        <ul className="configuration-safe-list">
          {credentials.map((credential) => (
            <li key={credential.api_name}>
              <span>
                <strong>{credential.api_name}</strong>
                <small>Fingerprint {credential.fingerprint}</small>
              </span>
              <Button
                onClick={() => {
                  setDeleteTarget({
                    kind: "credential",
                    apiName: credential.api_name,
                    impact: `delete credential ${credential.api_name}`,
                  });
                }}
                variant="quiet"
              >
                Delete
              </Button>
            </li>
          ))}
        </ul>
      </section>
      {provider === undefined ? null : (
        <div className="configuration-inspector-actions">
          <Button
            onClick={() => {
              setInspector({
                kind: "mapping",
                apiName: null,
                providerApiName: provider.api_name,
              });
            }}
            variant="secondary"
          >
            Add mapping for this provider
          </Button>
          <Button
            onClick={() => {
              setDeleteTarget({
                kind: "provider",
                apiName: provider.api_name,
                impact: `delete provider ${provider.api_name}`,
              });
            }}
            variant="quiet"
          >
            Delete provider
          </Button>
        </div>
      )}
    </GraphInspector>
  );
}

function ModelInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  const {
    inspector,
    closeInspector,
    returnFocusRef,
    saveModel,
    pending,
    setDeleteTarget,
    previewOpenRouter,
    importInput,
    setImportInput,
    importPreview,
    confirmOpenRouter,
    selectedImportProviders,
    setSelectedImportProviders,
    modelByName,
    providerModels,
    client,
    csrf,
    onRefreshGlobal,
    onNotice,
    setInspector,
    setPending,
    setSelectedNodeId,
  } = context;
  const model =
    inspector.apiName === null ? undefined : modelByName.get(inspector.apiName);
  const applicableMappings = providerModels.filter(
    (item) => item.model_api_name === model?.api_name,
  );
  return (
    <GraphInspector
      activationKey={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      eyebrow="Global canonical model"
      onClose={closeInspector}
      returnFocusRef={returnFocusRef}
      title={model?.display_name ?? "Add model"}
    >
      {model === undefined
        ? null
        : recordFacts([
            ["API name", model.api_name],
            ["Inputs", model.input_modalities.join(", ")],
            ["Outputs", model.output_modalities.join(", ")],
            ["Capabilities", model.capabilities.join(", ") || "None"],
            ["Price source", model.price_source ?? "Manual"],
          ])}
      <form
        className="configuration-form"
        onSubmit={(event) => void saveModel(event)}
      >
        <label>
          API name
          <input
            defaultValue={model?.api_name}
            readOnly={model !== undefined}
            name="api_name"
            required
          />
        </label>
        <label>
          Display name
          <input
            defaultValue={model?.display_name}
            name="display_name"
            required
          />
        </label>
        <label>
          Input modalities
          <input
            defaultValue={model?.input_modalities.join(", ") ?? "text"}
            name="input_modalities"
            required
          />
        </label>
        <label>
          Output modalities
          <input
            defaultValue={model?.output_modalities.join(", ") ?? "text"}
            name="output_modalities"
            required
          />
        </label>
        <label>
          Capabilities
          <input
            defaultValue={model?.capabilities.join(", ")}
            name="capabilities"
          />
        </label>
        <ModelAdvancedFields model={model} />
        <Button disabled={pending} type="submit">
          Save canonical model
        </Button>
      </form>
      {model !== undefined ? (
        <div className="configuration-inspector-actions">
          <Button
            onClick={() => {
              setInspector({
                kind: "mapping",
                apiName: null,
                modelApiName: model.api_name,
              });
            }}
            variant="secondary"
          >
            Add mapping for this model
          </Button>
          <Button
            disabled={pending || applicableMappings.length === 0}
            onClick={() => {
              setPending(true);
              void client
                .synchronizePrices(
                  applicableMappings.map((item) => item.api_name),
                  csrf,
                )
                .then(async (result) => {
                  await onRefreshGlobal();
                  const failures = result.items.filter(
                    (item) => item.outcome === "failed",
                  ).length;
                  onNotice(
                    failures === 0 ? "success" : "error",
                    failures === 0
                      ? "Applicable mapping prices were synchronized."
                      : `${String(failures)} applicable mapping price synchronizations failed.`,
                  );
                })
                .catch((error: unknown) => {
                  onNotice("error", errorMessage(error));
                })
                .finally(() => {
                  setPending(false);
                });
            }}
            variant="secondary"
          >
            Synchronize applicable prices
          </Button>
          <Button
            onClick={() => {
              setDeleteTarget({
                kind: "model",
                apiName: model.api_name,
                impact: `delete canonical model ${model.api_name}`,
              });
            }}
            variant="quiet"
          >
            Delete model
          </Button>
        </div>
      ) : (
        <section className="configuration-inspector-section">
          <h3>Create from OpenRouter</h3>
          <form
            className="configuration-form"
            onSubmit={(event) => void previewOpenRouter(event)}
          >
            <label>
              Exact model ID or supported OpenRouter URL
              <input
                maxLength={512}
                onChange={(event) => {
                  setImportInput(event.currentTarget.value);
                }}
                required
                value={importInput}
              />
            </label>
            <Button disabled={pending} type="submit">
              Preview OpenRouter model
            </Button>
          </form>
          {importPreview === null ? null : (
            <OpenRouterPreview
              onOpenConflict={(kind, apiName) => {
                const graphKind = kind === "model" ? "model" : "mapping";
                setSelectedNodeId(`${graphKind}:${apiName}`);
                setInspector({ kind: graphKind, apiName });
              }}
              onConfirm={() => void confirmOpenRouter()}
              pending={pending}
              preview={importPreview}
              selectedProviders={selectedImportProviders}
              setSelectedProviders={setSelectedImportProviders}
            />
          )}
        </section>
      )}
    </GraphInspector>
  );
}

function ModelAdvancedFields({ model }: { readonly model: Model | undefined }) {
  return (
    <details className="configuration-advanced">
      <summary>Constraints and price source</summary>
      <label>
        Maximum context tokens
        <input
          defaultValue={model?.constraints?.max_context_tokens ?? ""}
          min="1"
          name="max_context_tokens"
          type="number"
        />
      </label>
      <label>
        Maximum output tokens
        <input
          defaultValue={model?.constraints?.max_output_tokens ?? ""}
          min="1"
          name="max_output_tokens"
          type="number"
        />
      </label>
      <label>
        Embedding dimensions
        <input
          defaultValue={
            model?.constraints?.embedding_dimensions?.join(", ") ?? ""
          }
          name="embedding_dimensions"
          placeholder="768, 1536"
        />
      </label>
      <label>
        Maximum input images
        <input
          defaultValue={model?.constraints?.max_input_images ?? ""}
          min="1"
          name="max_input_images"
          type="number"
        />
      </label>
      <label>
        Maximum input image bytes
        <input
          defaultValue={model?.constraints?.max_input_image_bytes ?? ""}
          min="1"
          name="max_input_image_bytes"
          type="number"
        />
      </label>
      <label>
        Maximum output duration seconds
        <input
          defaultValue={model?.constraints?.max_output_duration_seconds ?? ""}
          min="1"
          name="max_output_duration_seconds"
          type="number"
        />
      </label>
      <label>
        Price source
        <input defaultValue={model?.price_source ?? ""} name="price_source" />
      </label>
      <label>
        Source model identifier
        <input
          defaultValue={model?.price_lookup_key ?? ""}
          name="price_lookup_key"
        />
      </label>
      <label>
        Manual price currency
        <input
          defaultValue={
            model?.current_price?.source == null
              ? model?.current_price?.currency
              : ""
          }
          maxLength={3}
          name="currency"
          placeholder="USD"
        />
      </label>
      <label>
        Manual typed unit prices
        <textarea
          defaultValue={
            model?.current_price?.source == null
              ? model?.current_price?.unit_prices
                  .map((item) => `${item.unit}=${item.amount}`)
                  .join(", ")
              : ""
          }
          name="unit_prices"
          placeholder="input_token=0.001, output_token=0.002"
          rows={3}
        />
      </label>
    </details>
  );
}

function MappingInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  const {
    inspector,
    closeInspector,
    returnFocusRef,
    saveMapping,
    providers,
    models,
    pending,
    setPending,
    client,
    csrf,
    onRefreshGlobal,
    onNotice,
    setDeleteTarget,
    mappingByName,
  } = context;
  const mapping =
    inspector.apiName === null
      ? undefined
      : mappingByName.get(inspector.apiName);
  return (
    <GraphInspector
      activationKey={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      eyebrow="Global provider-model mapping"
      onClose={closeInspector}
      returnFocusRef={returnFocusRef}
      title={mapping?.api_name ?? "Add mapping"}
    >
      {mapping === undefined
        ? null
        : recordFacts([
            ["Provider", mapping.provider_api_name],
            ["Canonical model", mapping.model_api_name],
            ["Provider wire model", mapping.provider_model_name],
            ["State", mapping.enabled ? "Enabled" : "Disabled"],
            [
              "Effective price",
              mapping.effective_price == null
                ? "Unavailable"
                : `${mapping.effective_price.currency} · ${String(mapping.effective_price.unit_prices.length)} typed units`,
            ],
          ])}
      <form
        className="configuration-form"
        onSubmit={(event) => void saveMapping(event)}
      >
        <label>
          Mapping API name
          <input
            defaultValue={mapping?.api_name}
            readOnly={mapping !== undefined}
            name="api_name"
            required
          />
        </label>
        {mapping === undefined && inspector.providerApiName !== undefined ? (
          <>
            <input
              name="provider_api_name"
              readOnly
              type="hidden"
              value={inspector.providerApiName}
            />
            {recordFacts([["Provider", inspector.providerApiName]])}
          </>
        ) : (
          <label>
            Provider
            <select
              defaultValue={mapping?.provider_api_name ?? ""}
              name="provider_api_name"
              required
            >
              <option value="">Select provider</option>
              {providers.map((item) => (
                <option key={item.api_name}>{item.api_name}</option>
              ))}
            </select>
          </label>
        )}
        {mapping === undefined && inspector.modelApiName !== undefined ? (
          <>
            <input
              name="model_api_name"
              readOnly
              type="hidden"
              value={inspector.modelApiName}
            />
            {recordFacts([["Canonical model", inspector.modelApiName]])}
          </>
        ) : (
          <label>
            Canonical model
            <select
              defaultValue={mapping?.model_api_name ?? ""}
              name="model_api_name"
              required
            >
              <option value="">Select model</option>
              {models.map((item) => (
                <option key={item.api_name}>{item.api_name}</option>
              ))}
            </select>
          </label>
        )}
        <label>
          Provider wire model
          <input
            defaultValue={mapping?.provider_model_name}
            name="provider_model_name"
            required
          />
        </label>
        <label className="checkbox-field">
          <input
            defaultChecked={mapping?.enabled ?? true}
            name="enabled"
            type="checkbox"
          />
          Enabled
        </label>
        <MappingAdvancedFields mapping={mapping} />
        <Button disabled={pending} type="submit">
          Save mapping
        </Button>
      </form>
      {mapping === undefined ? null : (
        <div className="configuration-inspector-actions">
          <Button
            disabled={pending}
            onClick={() => {
              setPending(true);
              void client
                .synchronizePrices([mapping.api_name], csrf)
                .then(async (result) => {
                  await onRefreshGlobal();
                  const item = result.items[0];
                  onNotice(
                    item?.outcome === "failed" ? "error" : "success",
                    item?.message ??
                      `Price synchronization: ${item?.outcome ?? "no result"}.`,
                  );
                })
                .catch((error: unknown) => {
                  onNotice("error", errorMessage(error));
                })
                .finally(() => {
                  setPending(false);
                });
            }}
            variant="secondary"
          >
            Synchronize price
          </Button>
          <Button
            onClick={() => {
              setDeleteTarget({
                kind: "mapping",
                apiName: mapping.api_name,
                impact: `delete provider-model mapping ${mapping.api_name}`,
              });
            }}
            variant="quiet"
          >
            Delete mapping
          </Button>
          <Button disabled variant="quiet">
            Playground available after configuration delivery
          </Button>
        </div>
      )}
    </GraphInspector>
  );
}

function MappingAdvancedFields({
  mapping,
}: {
  readonly mapping: ProviderModel | undefined;
}) {
  return (
    <details className="configuration-advanced">
      <summary>Capabilities, reasoning, and price</summary>
      <p>
        Empty capability fields use the canonical model. Enter values only to
        narrow this provider route.
      </p>
      <label>
        Input modalities
        <input
          defaultValue={mapping?.input_modalities.join(", ") ?? ""}
          name="input_modalities"
        />
      </label>
      <label>
        Output modalities
        <input
          defaultValue={mapping?.output_modalities.join(", ") ?? ""}
          name="output_modalities"
        />
      </label>
      <label>
        Capabilities
        <input
          defaultValue={mapping?.capabilities.join(", ") ?? ""}
          name="capabilities"
        />
      </label>
      <label>
        Reasoning mappings
        <input
          defaultValue={
            mapping?.reasoning_mappings
              .map((item) => `${item.level}=${item.provider_value}`)
              .join(", ") ?? ""
          }
          name="reasoning_mappings"
          placeholder="none=disabled, high=high"
        />
      </label>
      <label>
        Maximum context tokens
        <input
          defaultValue={mapping?.constraints?.max_context_tokens ?? ""}
          min="1"
          name="max_context_tokens"
          type="number"
        />
      </label>
      <label>
        Maximum output tokens
        <input
          defaultValue={mapping?.constraints?.max_output_tokens ?? ""}
          min="1"
          name="max_output_tokens"
          type="number"
        />
      </label>
      <label>
        Embedding dimensions
        <input
          defaultValue={
            mapping?.constraints?.embedding_dimensions?.join(", ") ?? ""
          }
          name="embedding_dimensions"
        />
      </label>
      <label>
        Maximum input images
        <input
          defaultValue={mapping?.constraints?.max_input_images ?? ""}
          min="1"
          name="max_input_images"
          type="number"
        />
      </label>
      <label>
        Maximum input image bytes
        <input
          defaultValue={mapping?.constraints?.max_input_image_bytes ?? ""}
          min="1"
          name="max_input_image_bytes"
          type="number"
        />
      </label>
      <label>
        Maximum output duration seconds
        <input
          defaultValue={mapping?.constraints?.max_output_duration_seconds ?? ""}
          min="1"
          name="max_output_duration_seconds"
          type="number"
        />
      </label>
      <label>
        Price source
        <input defaultValue={mapping?.price_source ?? ""} name="price_source" />
      </label>
      <label>
        Source model identifier
        <input
          defaultValue={mapping?.price_lookup_key ?? ""}
          name="price_lookup_key"
        />
      </label>
      <label>
        Manual price currency
        <input
          defaultValue=""
          maxLength={3}
          name="currency"
          placeholder="USD"
        />
      </label>
      <label>
        Manual typed unit prices
        <textarea
          defaultValue=""
          name="unit_prices"
          placeholder="input_token=0.001, output_token=0.002"
          rows={3}
        />
      </label>
    </details>
  );
}

function AssignmentInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  const {
    inspector,
    assignmentByName,
    selectedService,
    closeInspector,
    returnFocusRef,
    markAssignmentDirty,
    saveAssignment,
    chainRows,
    setChainRows,
    providerModels,
    assignmentDirty,
    pending,
    setDeleteTarget,
  } = context;
  const assignment =
    inspector.apiName === null
      ? undefined
      : assignmentByName.get(inspector.apiName);
  const isLocal = assignment?.defined_by_service_api_name === selectedService;
  return (
    <GraphInspector
      activationKey={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      eyebrow={
        selectedService === ""
          ? "Select a service"
          : `${selectedService} configuration context`
      }
      onClose={closeInspector}
      returnFocusRef={returnFocusRef}
      title={assignment?.display_name ?? "Add assignment"}
    >
      {selectedService === "" ? (
        <StatePanel kind="empty" title="Service required">
          Select one service. Providers, models, mappings, credentials, and
          prices stay global.
        </StatePanel>
      ) : (
        <>
          {assignment === undefined ? null : (
            <>
              {recordFacts([
                ["API name", assignment.api_name],
                ["Definition", assignment.definition_kind],
                [
                  "Direct source",
                  assignment.defined_by_service_api_name ?? "Implicit root",
                ],
                [
                  "Effective chain",
                  assignment.effective_chain
                    .map((item) => item.provider_model_api_name)
                    .join(" → ") || "Empty",
                ],
                [
                  "Inherited assignment",
                  assignment.inherits_assignment_api_name ?? "None",
                ],
                ["Last used", assignment.last_used_at ?? "Never"],
                [
                  "Observed",
                  assignment.observed_requirements.join(", ") || "None",
                ],
              ])}
              {assignment.observed_requirements.length === 0 ? null : (
                <section className="configuration-inspector-section">
                  <h3>Observed requirements</h3>
                  <p>
                    Remove a stale observation only after you confirm its exact
                    assignment impact.
                  </p>
                  <ul className="configuration-safe-list">
                    {assignment.observed_requirements.map((requirement) => (
                      <li key={requirement}>
                        <span>{requirement}</span>
                        <Button
                          onClick={() => {
                            setDeleteTarget({
                              kind: "requirement",
                              apiName: assignment.api_name,
                              requirement,
                              impact: `remove observed requirement ${requirement} from assignment ${assignment.api_name} for service ${selectedService}`,
                            });
                          }}
                          variant="quiet"
                        >
                          Remove
                        </Button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
          <form
            className="configuration-form"
            onChange={markAssignmentDirty}
            onSubmit={(event) => void saveAssignment(event)}
          >
            <label>
              Assignment API name
              <input
                defaultValue={assignment?.api_name}
                readOnly={assignment !== undefined}
                name="api_name"
                required
              />
            </label>
            <label>
              Display name
              <input
                defaultValue={assignment?.display_name}
                name="display_name"
              />
            </label>
            <label>
              Definition
              <select
                defaultValue={
                  assignment?.definition_kind === "inherited_assignment"
                    ? "inherit"
                    : "direct"
                }
                name="definition_kind"
              >
                <option value="direct">Ordered direct chain</option>
                <option value="inherit">Inherit another assignment</option>
              </select>
            </label>
            <label>
              Inherited assignment
              <input
                defaultValue={
                  assignment?.inherits_assignment_api_name ?? "default"
                }
                name="inherits_assignment_api_name"
              />
            </label>
            <label>
              Reasoning level
              <select
                defaultValue={assignment?.reasoning_level ?? ""}
                name="reasoning_level"
              >
                <option value="">Model default</option>
                <option>none</option>
                <option>low</option>
                <option>medium</option>
                <option>high</option>
              </select>
            </label>
            <AssignmentChainEditor
              onDirty={markAssignmentDirty}
              providerModels={providerModels}
              rows={chainRows}
              setRows={setChainRows}
            />
            <div className="configuration-inspector-actions">
              <Button disabled={pending} type="submit">
                Save selected service assignment
              </Button>
              {assignmentDirty ? (
                <Button
                  onClick={() => {
                    setDeleteTarget({
                      kind: "draft",
                      apiName: assignment?.api_name ?? "new assignment",
                      impact: `discard unsaved assignment changes for service ${selectedService}`,
                    });
                  }}
                  type="button"
                  variant="quiet"
                >
                  Discard changes
                </Button>
              ) : null}
            </div>
          </form>
          {assignment !== undefined && isLocal ? (
            <Button
              onClick={() => {
                setDeleteTarget({
                  kind: "assignment",
                  apiName: assignment.api_name,
                  impact: `delete local assignment ${assignment.api_name} from ${selectedService}`,
                });
              }}
              variant="quiet"
            >
              Delete local definition
            </Button>
          ) : null}
          {assignment === undefined ? null : (
            <Button disabled variant="quiet">
              Open playground (available in the next configuration delivery)
            </Button>
          )}
        </>
      )}
    </GraphInspector>
  );
}

function AssignmentChainEditor({
  onDirty,
  providerModels,
  rows,
  setRows,
}: {
  readonly onDirty: () => void;
  readonly providerModels: readonly ProviderModel[];
  readonly rows: readonly EditableTableRow<ChainDraft>[];
  readonly setRows: Dispatch<
    SetStateAction<readonly EditableTableRow<ChainDraft>[]>
  >;
}) {
  const nextRowIdRef = useRef(rows.length);
  const columns: readonly EditableTableColumn<ChainDraft>[] = [
    {
      key: "mapping",
      header: "Provider-model mapping",
      renderRead: ({ row, update }) => (
        <select
          aria-label={`${row.label} provider-model mapping`}
          onChange={(event) => {
            update({ providerModel: event.currentTarget.value });
            onDirty();
          }}
          value={row.draft.providerModel}
        >
          <option value="">Select mapping</option>
          {providerModels.map((item) => (
            <option disabled={!item.enabled} key={item.api_name}>
              {item.api_name}
            </option>
          ))}
        </select>
      ),
      renderEdit: ({ row }) => row.draft.providerModel,
    },
  ];
  return (
    <section className="configuration-chain-editor">
      <h3>Ordered fallback chain</h3>
      <EditableTable
        ariaLabel="Ordered assignment provider-model chain"
        columns={columns}
        density="compact"
        onDelete={(rowId) => {
          setRows((current) => current.filter((row) => row.id !== rowId));
          onDirty();
        }}
        onDraftChange={(rowId, patch) => {
          setRows((current) =>
            current.map((row) =>
              row.id === rowId
                ? { ...row, draft: { ...row.draft, ...patch } }
                : row,
            ),
          );
        }}
        reorder={{
          onReorder: ({ orderedRows }) => {
            setRows(orderedRows);
            onDirty();
          },
        }}
        rows={rows}
        state={
          rows.length === 0
            ? {
                kind: "empty",
                message:
                  "No direct candidates. Add the first provider-model mapping.",
              }
            : { kind: "ready" }
        }
      />
      <Button
        disabled={rows.length >= 16}
        onClick={() => {
          nextRowIdRef.current += 1;
          setRows((current) => [
            ...current,
            {
              id: `chain:new:${String(nextRowIdRef.current)}`,
              label: `Fallback ${String(current.length + 1)}`,
              draft: { providerModel: "" },
            },
          ]);
          onDirty();
        }}
        type="button"
        variant="secondary"
      >
        Add fallback
      </Button>
    </section>
  );
}

function OpenRouterPreview({
  onConfirm,
  onOpenConflict,
  pending,
  preview,
  selectedProviders,
  setSelectedProviders,
}: {
  readonly onConfirm: () => void;
  readonly onOpenConflict: (
    kind: "model" | "provider_model",
    apiName: string,
  ) => void;
  readonly pending: boolean;
  readonly preview: OpenRouterModelImportPreview;
  readonly selectedProviders: ReadonlySet<string>;
  readonly setSelectedProviders: (value: ReadonlySet<string>) => void;
}) {
  const price = preview.reviewed_price;
  return (
    <section
      className="openrouter-preview"
      aria-label="Reviewed OpenRouter import"
    >
      <h4>{preview.model.display_name}</h4>
      {recordFacts([
        ["Source ID", preview.source_model_id],
        ["Canonical API name", preview.model.api_name],
        ["Inputs", preview.model.input_modalities.join(", ")],
        ["Outputs", preview.model.output_modalities.join(", ")],
        ["Capabilities", preview.model.capabilities.join(", ") || "None"],
        [
          "Context bound",
          String(
            preview.model.constraints?.max_context_tokens ?? "Unavailable",
          ),
        ],
        [
          "Output bound",
          String(preview.model.constraints?.max_output_tokens ?? "Unavailable"),
        ],
        [
          "Reasoning",
          preview.reasoning.supported
            ? [
                "Supported",
                preview.reasoning.mandatory === true ? "mandatory" : null,
                preview.reasoning.default_effort == null
                  ? null
                  : `default ${preview.reasoning.default_effort}`,
              ]
                .filter((item) => item !== null)
                .join(" · ")
            : "Not supported",
        ],
        [
          "Supported controls",
          preview.supported_constraints.join(", ") || "None reported",
        ],
        [
          "Typed price",
          price == null
            ? "Unavailable"
            : `${price.currency}: ${price.unit_prices.map((item) => `${item.unit}=${item.amount}`).join(", ")}`,
        ],
      ])}
      {preview.issues.length === 0 ? null : (
        <div>
          <h5>Review issues</h5>
          <ul>
            {preview.issues.map((item, index) => (
              <li key={`${item.code}:${String(index)}`}>
                {item.field}: {item.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {preview.conflicts.length === 0 ? null : (
        <div role="alert">
          <h5>Blocking conflicts</h5>
          <ul>
            {preview.conflicts.map((item) => (
              <li key={`${item.kind}:${item.api_name}`}>
                <span>{item.message}</span>{" "}
                <Button
                  onClick={() => {
                    onOpenConflict(item.kind, item.api_name);
                  }}
                  variant="quiet"
                >
                  Open {item.kind === "model" ? "model" : "mapping"} node{" "}
                  {item.api_name}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <fieldset>
        <legend>Select global OpenRouter provider connections</legend>
        {preview.provider_options.map((option) => (
          <label
            className="openrouter-provider-option"
            key={option.provider_api_name}
          >
            <input
              checked={selectedProviders.has(option.provider_api_name)}
              disabled={!option.selectable}
              onChange={(event) => {
                const next = new Set(selectedProviders);
                if (event.currentTarget.checked)
                  next.add(option.provider_api_name);
                else next.delete(option.provider_api_name);
                setSelectedProviders(next);
              }}
              type="checkbox"
            />{" "}
            <span>
              <strong>{option.provider_display_name}</strong>
              <small>
                {option.selectable
                  ? option.provider_model.api_name
                  : option.unavailable_reason}
              </small>
            </span>
          </label>
        ))}
      </fieldset>
      <Button
        disabled={
          pending ||
          !preview.can_confirm ||
          selectedProviders.size === 0 ||
          preview.conflicts.length > 0
        }
        onClick={onConfirm}
      >
        Confirm exact reviewed import
      </Button>
      <p className="field-note">
        Confirmation sends the exact reviewed model, price, and selected mapping
        objects. It does not normalize or refetch them.
      </p>
    </section>
  );
}

export type { ConfigurationGraphProps };
