import {
  useCallback,
  useEffect,
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
  type AdministratorPlaygroundMediaJob,
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
  type Service,
} from "./api.ts";
import {
  adapterFieldPolicy,
  discardDeletedRecord,
  discardConfirmedRecord,
  excludeDeletedRecords,
  includeConfirmedRecords,
  configurationNodeId,
  parseConfigurationNodeId,
  projectConfigurationGraph,
  providerModelPriceFormDefaults,
  pruneAcknowledgedDeletions,
  pruneAcknowledgedRecords,
  retainConfirmedRecord,
  retainDeletedRecord,
  validateAssignmentChain,
  type ConfigurationLoadPhase,
  type ConfigurationRecordKind,
} from "./configurationState.ts";
import {
  credentialFormValue,
  configuredPriceValue,
  parseManualPrice,
} from "./formContracts.ts";
import { PlaygroundModal } from "./PlaygroundModal.tsx";
import {
  assignmentPlaygroundTarget,
  currentPlaygroundTarget,
  mappingPlaygroundTarget,
  playgroundTargetKey,
  updateMediaRecovery,
  type PlaygroundTargetSnapshot,
} from "./playgroundState.ts";

interface ConfigurationGraphProps {
  readonly assignments: readonly Assignment[];
  readonly assignmentPhase?: ConfigurationLoadPhase;
  readonly catalogPhase?: ConfigurationLoadPhase;
  readonly client: AdministrationClient;
  readonly credentials: readonly Credential[];
  readonly csrf: string;
  readonly globalPhase?: ConfigurationLoadPhase;
  readonly models: readonly Model[];
  readonly onAssignmentDirtyChange: (dirty: boolean) => void;
  readonly onAssignmentPendingChange?: (pending: boolean) => void;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefreshAssignments: () => Promise<void>;
  readonly onRefreshGlobal: () => Promise<void>;
  readonly providerModels: readonly ProviderModel[];
  readonly providerPhase?: ConfigurationLoadPhase;
  readonly providers: readonly Provider[];
  readonly selectedService: string;
  readonly services?: readonly Service[];
}

interface Inspector {
  readonly kind: ConfigurationRecordKind;
  readonly apiName: string | null;
  readonly providerApiName?: string;
  readonly modelApiName?: string;
  readonly serviceApiName?: string;
  readonly rungPosition?: number;
}

interface InspectorTransition {
  readonly chainRows?: readonly EditableTableRow<ChainDraft>[];
  readonly inspector: Inspector | null;
  readonly selectedNodeId: string | null;
  readonly trigger: HTMLElement | null;
}

interface DeleteTarget {
  readonly kind:
    | ConfigurationRecordKind
    | "credential"
    | "credential-replace"
    | "requirement"
    | "draft";
  readonly apiName: string;
  readonly impact: string;
  readonly requirement?: ObservedRequirement;
}

interface ChainDraft {
  readonly providerModel: string;
}

function assignmentPositionLabel(position: number): string {
  return position === 1 ? "Primary" : `Fallback ${String(position)}`;
}

function orderChainRows(
  rows: readonly EditableTableRow<ChainDraft>[],
): readonly EditableTableRow<ChainDraft>[] {
  return rows.map((row, index) => ({
    ...row,
    label: assignmentPositionLabel(index + 1),
  }));
}

interface ConfirmedGlobalRecords {
  readonly providers: readonly Provider[];
  readonly models: readonly Model[];
  readonly mappings: readonly ProviderModel[];
}

interface DeletedGlobalRecords {
  readonly credentials: readonly string[];
  readonly providers: readonly string[];
  readonly models: readonly string[];
  readonly mappings: readonly string[];
}

interface ConfirmedAssignmentRecords {
  readonly serviceApiName: string;
  readonly records: readonly Assignment[];
  readonly deleted: readonly string[];
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
  readonly beginPending: (assignmentOperation?: boolean) => boolean;
  readonly finishPending: () => void;
  readonly onNotice: (tone: "success" | "error", message: string) => void;
  readonly onRefreshAssignments: () => Promise<void>;
  readonly onRefreshGlobal: () => Promise<void>;
  readonly openPlayground: (
    target: PlaygroundTargetSnapshot,
    trigger: HTMLElement,
  ) => void;
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
  readonly services: readonly Service[];
  readonly setAssignmentDirty: (value: boolean) => void;
  readonly setChainRows: Dispatch<
    SetStateAction<readonly EditableTableRow<ChainDraft>[]>
  >;
  readonly setDeleteTarget: (value: DeleteTarget | null) => void;
  readonly setImportInput: (value: string) => void;
  readonly setImportPreview: (
    value: OpenRouterModelImportPreview | null,
  ) => void;
  readonly setInspector: (value: Inspector | null) => void;
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
    resetAssignmentInspector: useCallback(() => {
      patch({
        assignmentDirty: false,
        chainRows: [],
        inspector: null,
        selectedNodeId: null,
      });
    }, [patch]),
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

const adapterLabels: Readonly<Record<ProviderAdapter, string>> = {
  openai: "OpenAI",
  openai_compatible: "OpenAI-compatible",
  openrouter: "OpenRouter",
  custom: "Custom",
  wavespeed: "WaveSpeed",
  ollama: "Ollama",
  local_embeddings: "Local embeddings",
  fake: "Fake",
};

const requirementLabels: Readonly<Record<ObservedRequirement, string>> = {
  text_input: "Text input",
  image_input: "Image input",
  text_output: "Text output",
  structured_json_output: "Structured JSON",
  embedding_output: "Embeddings",
  image_output: "Image output",
  video_output: "Video output",
  audio_output: "Audio output",
  tool_calling: "Tool calling",
  streaming: "Streaming",
  reasoning: "Reasoning",
};

const assignmentDefinitionLabels: Readonly<
  Record<Assignment["definition_kind"], string>
> = {
  direct_chain: "Ordered direct chain",
  inherited_assignment: "Inherit another assignment",
  implicit: "Implicit root default",
};

function modelCapabilityLabels(
  model: Pick<Model, "input_modalities" | "output_modalities" | "capabilities">,
): readonly string[] {
  const labels: string[] = [];
  for (const input of model.input_modalities)
    labels.push(input === "text" ? "Text input" : "Image input");
  for (const output of model.output_modalities) {
    const label: Readonly<
      Record<(typeof model.output_modalities)[number], string>
    > = {
      text: "Text output",
      structured_json: "Structured JSON",
      embedding: "Embeddings",
      image: "Image output",
      video: "Video output",
      audio: "Audio output",
    };
    labels.push(label[output]);
  }
  for (const capability of model.capabilities) {
    const label = {
      tool_calling: "Tool calling",
      streaming: "Streaming",
      reasoning: "Reasoning",
    }[capability];
    labels.push(label);
  }
  return labels;
}

interface BoardState {
  readonly available: boolean;
  readonly content?: string;
  readonly state: "disabled" | "enabled" | "unavailable";
  readonly stateLabel: "Disabled" | "Enabled" | "Unavailable";
}

function providerBoardState(
  provider: Provider,
  credentialNames: ReadonlySet<string>,
): {
  readonly available: boolean;
  readonly content?: string;
  readonly state: "disabled" | "ready" | "unavailable";
  readonly stateLabel: "Disabled" | "Ready" | "Unavailable";
} {
  if (!provider.enabled)
    return { available: false, state: "disabled", stateLabel: "Disabled" };
  const policy = adapterFieldPolicy[provider.adapter];
  if (policy.endpoint === "required" && !provider.endpoint?.trim())
    return {
      available: false,
      content: "Add the required endpoint.",
      state: "unavailable",
      stateLabel: "Unavailable",
    };
  if (
    policy.credential === "required" &&
    (provider.credential_api_name === null ||
      provider.credential_api_name === undefined ||
      !credentialNames.has(provider.credential_api_name))
  )
    return {
      available: false,
      content: "Add the required credential.",
      state: "unavailable",
      stateLabel: "Unavailable",
    };
  return { available: true, state: "ready", stateLabel: "Ready" };
}

function routeBoardState(
  route: ProviderModel,
  providerState: ReturnType<typeof providerBoardState> | undefined,
  modelAvailable: boolean,
): BoardState {
  if (!modelAvailable)
    return {
      available: false,
      content: `Unavailable model: ${route.model_api_name}`,
      state: "unavailable",
      stateLabel: "Unavailable",
    };
  if (providerState === undefined)
    return {
      available: false,
      content: `Unavailable provider: ${route.provider_api_name}`,
      state: "unavailable",
      stateLabel: "Unavailable",
    };
  if (!route.enabled)
    return { available: false, state: "disabled", stateLabel: "Disabled" };
  if (route.cooldown)
    return {
      available: false,
      content: `Cooldown until ${route.cooldown.until}`,
      state: "unavailable",
      stateLabel: "Unavailable",
    };
  if (!providerState.available)
    return {
      available: false,
      content:
        providerState.state === "disabled"
          ? `Provider disabled: ${route.provider_api_name}`
          : `Provider unavailable: ${route.provider_api_name}`,
      state: "unavailable",
      stateLabel: "Unavailable",
    };
  return { available: true, state: "enabled", stateLabel: "Enabled" };
}

function routeMeetsRequirement(
  route: ProviderModel,
  requirement: ObservedRequirement,
): boolean {
  if (requirement === "text_input")
    return route.input_modalities.includes("text");
  if (requirement === "image_input")
    return route.input_modalities.includes("image");
  if (requirement === "text_output")
    return route.output_modalities.includes("text");
  if (requirement === "structured_json_output")
    return route.output_modalities.includes("structured_json");
  if (requirement === "embedding_output")
    return route.output_modalities.includes("embedding");
  if (requirement === "image_output")
    return route.output_modalities.includes("image");
  if (requirement === "video_output")
    return route.output_modalities.includes("video");
  if (requirement === "audio_output")
    return route.output_modalities.includes("audio");
  return route.capabilities.includes(requirement);
}

function assignmentSourceLabel(
  assignment: Assignment,
  selectedService: string,
  services: ReadonlyMap<string, Service>,
): string {
  if (assignment.definition_kind === "implicit") return "Implicit root default";
  if (assignment.defined_by_service_api_name === selectedService)
    return "Local definition";
  const source = assignment.defined_by_service_api_name;
  if (!source) return "Implicit root default";
  const service = services.get(source);
  return service
    ? `Inherited from ${service.display_name} (${service.api_name})`
    : `Inherited from unavailable service (${source})`;
}

function assignmentInheritanceLabel(
  assignment: Assignment,
  assignments: ReadonlyMap<string, Assignment>,
): string | null {
  const inheritedName = assignment.inherits_assignment_api_name;
  if (!inheritedName) return null;
  const inherited = assignments.get(inheritedName);
  return inherited
    ? `Inherits ${inherited.display_name} (${inherited.api_name})`
    : `Inherits unavailable assignment (${inheritedName})`;
}

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
  const configuredPrice = configuredPriceValue(
    formValue(form, "price_source"),
    formValue(form, "price_lookup_key"),
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
    ...configuredPrice,
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

function ReferenceRetryAction({
  onRetry,
  pending,
}: {
  readonly onRetry: () => Promise<void>;
  readonly pending: boolean;
}) {
  return (
    <Button
      disabled={pending}
      onClick={(event) => {
        const retryButton = event.currentTarget;
        const context = event.currentTarget.closest<HTMLElement>(
          ".od-relationship-graph-group, .od-relationship-graph-column",
        );
        const returnNodeId = context
          ?.querySelector<HTMLElement>("[data-node-id]")
          ?.getAttribute("data-node-id");
        void onRetry().finally(() => {
          const restoreFocus = () => {
            if (retryButton.isConnected) {
              retryButton.focus({ preventScroll: true });
              return;
            }
            if (returnNodeId === undefined) return;
            const target = [
              ...document.querySelectorAll<HTMLElement>("[data-node-id]"),
            ].find(
              (item) => item.getAttribute("data-node-id") === returnNodeId,
            );
            target?.focus({ preventScroll: true });
          };
          if (typeof requestAnimationFrame === "function")
            requestAnimationFrame(restoreFocus);
          else restoreFocus();
        });
      }}
      variant="quiet"
    >
      Retry
    </Button>
  );
}

function useConfigurationController({
  assignments,
  assignmentPhase = "ready",
  catalogPhase,
  client,
  credentials,
  csrf,
  globalPhase = "ready",
  models,
  onAssignmentDirtyChange,
  onAssignmentPendingChange = () => undefined,
  onNotice,
  onRefreshAssignments,
  onRefreshGlobal,
  providerModels,
  providerPhase,
  providers,
  selectedService,
  services = [],
}: ConfigurationGraphProps) {
  const effectiveCatalogPhase = catalogPhase ?? globalPhase;
  const effectiveProviderPhase = providerPhase ?? globalPhase;
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
    resetAssignmentInspector,
  } = useConfigurationViewState();
  const [confirmedGlobal, setConfirmedGlobal] =
    useState<ConfirmedGlobalRecords>({
      providers: [],
      models: [],
      mappings: [],
    });
  const [deletedGlobal, setDeletedGlobal] = useState<DeletedGlobalRecords>({
    credentials: [],
    providers: [],
    models: [],
    mappings: [],
  });
  const [confirmedAssignments, setConfirmedAssignments] =
    useState<ConfirmedAssignmentRecords>({
      serviceApiName: selectedService,
      records: [],
      deleted: [],
    });
  const [playgroundTarget, setPlaygroundTarget] =
    useState<PlaygroundTargetSnapshot | null>(null);
  const [playgroundMediaRecovery, setPlaygroundMediaRecovery] = useState<
    ReadonlyMap<string, AdministratorPlaygroundMediaJob>
  >(new Map());
  const [
    playgroundUncertainMediaAdmissions,
    setPlaygroundUncertainMediaAdmissions,
  ] = useState<ReadonlySet<string>>(new Set());
  const authoritativeSnapshot = JSON.stringify({
    credentials,
    providers,
    models,
    mappings: providerModels,
  });
  const [previousAuthoritativeSnapshot, setPreviousAuthoritativeSnapshot] =
    useState(authoritativeSnapshot);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const playgroundReturnFocusRef = useRef<HTMLElement | null>(null);
  const pendingRef = useRef(false);
  const pendingAssignmentRef = useRef(false);
  const pendingInspectorTransitionRef = useRef<InspectorTransition | null>(
    null,
  );
  const pendingCredentialReplacementRef = useRef<{
    readonly form: HTMLFormElement;
    readonly name: string;
    readonly secret: string;
  } | null>(null);
  const previousSelectedServiceRef = useRef(selectedService);

  if (authoritativeSnapshot !== previousAuthoritativeSnapshot) {
    setPreviousAuthoritativeSnapshot(authoritativeSnapshot);
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
    setDeletedGlobal((current) => {
      const next = {
        credentials: pruneAcknowledgedDeletions(
          credentials,
          current.credentials,
        ),
        providers: pruneAcknowledgedDeletions(providers, current.providers),
        models: pruneAcknowledgedDeletions(models, current.models),
        mappings: pruneAcknowledgedDeletions(providerModels, current.mappings),
      };
      return next.credentials === current.credentials &&
        next.providers === current.providers &&
        next.models === current.models &&
        next.mappings === current.mappings
        ? current
        : next;
    });
  }

  const assignmentSnapshot = JSON.stringify({
    serviceApiName: selectedService,
    assignments,
  });
  const [previousAssignmentSnapshot, setPreviousAssignmentSnapshot] =
    useState(assignmentSnapshot);
  if (assignmentSnapshot !== previousAssignmentSnapshot) {
    setPreviousAssignmentSnapshot(assignmentSnapshot);
    setConfirmedAssignments((current) => {
      if (current.serviceApiName !== selectedService)
        return { serviceApiName: selectedService, records: [], deleted: [] };
      const nextRecords = pruneAcknowledgedRecords(
        assignments,
        current.records,
      );
      const nextDeleted = pruneAcknowledgedDeletions(
        assignments,
        current.deleted,
      );
      return nextRecords === current.records && nextDeleted === current.deleted
        ? current
        : { ...current, records: nextRecords, deleted: nextDeleted };
    });
  }

  const visibleProviders = useMemo(
    () =>
      excludeDeletedRecords(
        includeConfirmedRecords(providers, confirmedGlobal.providers),
        deletedGlobal.providers,
      ),
    [confirmedGlobal.providers, deletedGlobal.providers, providers],
  );
  const visibleModels = useMemo(
    () =>
      excludeDeletedRecords(
        includeConfirmedRecords(models, confirmedGlobal.models),
        deletedGlobal.models,
      ),
    [confirmedGlobal.models, deletedGlobal.models, models],
  );
  const visibleMappings = useMemo(
    () =>
      excludeDeletedRecords(
        includeConfirmedRecords(providerModels, confirmedGlobal.mappings),
        deletedGlobal.mappings,
      ),
    [confirmedGlobal.mappings, deletedGlobal.mappings, providerModels],
  );
  const visibleCredentials = useMemo(
    () => excludeDeletedRecords(credentials, deletedGlobal.credentials),
    [credentials, deletedGlobal.credentials],
  );
  const visibleAssignments = useMemo(() => {
    const overlay =
      confirmedAssignments.serviceApiName === selectedService
        ? confirmedAssignments
        : { records: [], deleted: [] };
    return excludeDeletedRecords(
      includeConfirmedRecords(assignments, overlay.records),
      overlay.deleted,
    );
  }, [assignments, confirmedAssignments, selectedService]);

  const projection = useMemo(
    () =>
      projectConfigurationGraph(
        visibleProviders,
        visibleModels,
        visibleMappings,
        visibleAssignments,
      ),
    [visibleAssignments, visibleMappings, visibleModels, visibleProviders],
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
    () => new Map(visibleAssignments.map((item) => [item.api_name, item])),
    [visibleAssignments],
  );
  const serviceByName = useMemo(
    () => new Map(services.map((item) => [item.api_name, item])),
    [services],
  );
  const credentialNames = useMemo(
    () => new Set(visibleCredentials.map((item) => item.api_name)),
    [visibleCredentials],
  );
  const providerStates = useMemo(
    () =>
      new Map(
        visibleProviders.map((item) => [
          item.api_name,
          providerBoardState(item, credentialNames),
        ]),
      ),
    [credentialNames, visibleProviders],
  );
  const routeStates = useMemo(
    () =>
      new Map(
        visibleMappings.map((item) => [
          item.api_name,
          routeBoardState(
            item,
            providerStates.get(item.provider_api_name),
            modelByName.has(item.model_api_name),
          ),
        ]),
      ),
    [modelByName, providerStates, visibleMappings],
  );

  useEffect(() => {
    if (previousSelectedServiceRef.current === selectedService) return;
    previousSelectedServiceRef.current = selectedService;
    setConfirmedAssignments({
      serviceApiName: selectedService,
      records: [],
      deleted: [],
    });
    pendingInspectorTransitionRef.current = null;
    if (inspector?.kind !== "assignment") return;
    resetAssignmentInspector();
    onAssignmentDirtyChange(false);
  }, [
    inspector?.kind,
    onAssignmentDirtyChange,
    resetAssignmentInspector,
    selectedService,
  ]);

  function beginPending(assignmentOperation = false): boolean {
    if (pendingRef.current) return false;
    pendingRef.current = true;
    pendingAssignmentRef.current = assignmentOperation;
    setPending(true);
    if (assignmentOperation) onAssignmentPendingChange(true);
    return true;
  }

  function finishPending() {
    if (!pendingRef.current) return;
    const assignmentOperation = pendingAssignmentRef.current;
    pendingRef.current = false;
    pendingAssignmentRef.current = false;
    setPending(false);
    if (assignmentOperation) onAssignmentPendingChange(false);
  }
  function markAssignmentDirty() {
    if (assignmentDirty) return;
    setAssignmentDirty(true);
    onAssignmentDirtyChange(true);
  }

  function applyInspectorTransition(transition: InspectorTransition) {
    pendingInspectorTransitionRef.current = null;
    if (transition.trigger !== null)
      returnFocusRef.current = transition.trigger;
    setSelectedNodeId(transition.selectedNodeId);
    if (transition.chainRows !== undefined) setChainRows(transition.chainRows);
    setInspector(transition.inspector);
    if (transition.inspector?.kind !== "assignment") {
      setAssignmentDirty(false);
      onAssignmentDirtyChange(false);
    }
  }

  function requestInspectorTransition(transition: InspectorTransition) {
    if (pendingRef.current) return;
    if (
      inspector?.kind === "assignment" &&
      assignmentDirty &&
      (transition.inspector?.kind !== "assignment" ||
        transition.inspector.apiName !== inspector.apiName)
    ) {
      pendingInspectorTransitionRef.current = transition;
      setDeleteTarget({
        kind: "draft",
        apiName: inspector.apiName ?? "new assignment",
        impact: `discard unsaved assignment changes for service ${selectedService}`,
      });
      return;
    }
    applyInspectorTransition(transition);
  }

  function closeInspector() {
    if (pendingRef.current) return;
    if (inspector?.kind === "assignment" && assignmentDirty) {
      pendingInspectorTransitionRef.current = null;
      setDeleteTarget({
        kind: "draft",
        apiName: inspector.apiName ?? "new assignment",
        impact: `discard unsaved assignment changes for service ${selectedService}`,
      });
      return;
    }
    applyInspectorTransition({
      inspector: null,
      selectedNodeId,
      trigger: null,
    });
  }

  function activate(context: RelationshipGraphNodeContext) {
    const identity = parseConfigurationNodeId(context.node.id);
    if (identity === null) return;
    if (pendingRef.current) return;
    const inspectorKind =
      identity.kind === "rung" ? "assignment" : identity.kind;
    if (
      inspector?.kind === inspectorKind &&
      inspector.apiName === identity.apiName &&
      inspector.rungPosition === identity.position &&
      (inspectorKind !== "assignment" ||
        inspector.serviceApiName === selectedService)
    ) {
      returnFocusRef.current = context.trigger;
      return;
    }
    const nextInspector: Inspector = {
      kind: inspectorKind,
      apiName: identity.apiName,
      ...(inspectorKind === "assignment"
        ? { serviceApiName: selectedService }
        : {}),
      ...(identity.position === undefined
        ? {}
        : { rungPosition: identity.position }),
    };
    let nextChainRows: readonly EditableTableRow<ChainDraft>[] | undefined;
    if (inspectorKind === "assignment") {
      const assignment = assignmentByName.get(identity.apiName);
      const chain =
        assignment?.direct_chain ?? assignment?.effective_chain ?? [];
      nextChainRows = chain.map((candidate, index) => ({
        id: `chain:${String(index)}:${candidate.provider_model_api_name}`,
        label: assignmentPositionLabel(index + 1),
        draft: { providerModel: candidate.provider_model_api_name },
      }));
    }
    requestInspectorTransition({
      ...(nextChainRows === undefined ? {} : { chainRows: nextChainRows }),
      inspector: nextInspector,
      selectedNodeId: identity.id,
      trigger: context.trigger,
    });
  }

  function openCreate(
    kind: ConfigurationRecordKind,
    trigger: HTMLButtonElement,
  ) {
    requestInspectorTransition({
      ...(kind === "assignment" ? { chainRows: [] } : {}),
      inspector: {
        kind,
        apiName: null,
        ...(kind === "assignment" ? { serviceApiName: selectedService } : {}),
      },
      selectedNodeId: null,
      trigger,
    });
  }

  const assignmentEmptyMessage =
    selectedService === ""
      ? "Select a service to view assignments."
      : assignmentPhase === "loading"
        ? "Loading Assignments."
        : assignmentPhase === "error"
          ? "Unable to load Assignments."
          : assignmentPhase === "partial"
            ? "No assignments are available in the loaded records."
            : "No assignments are configured for this service.";

  const columns: readonly [
    RelationshipGraphColumn,
    RelationshipGraphColumn,
    RelationshipGraphColumn,
  ] = [
    {
      id: "providers",
      label: "Providers",
      countLabel: `${String(visibleProviders.length)} providers`,
      actions: (
        <span className="configuration-column-actions">
          <Button
            disabled={pending}
            onClick={(event) => {
              openCreate("provider", event.currentTarget);
            }}
            variant="secondary"
          >
            Add provider
          </Button>
          {effectiveProviderPhase === "error" ? (
            <>
              <span>Unable to load Providers.</span>
              <ReferenceRetryAction
                onRetry={onRefreshGlobal}
                pending={pending}
              />
            </>
          ) : null}
        </span>
      ),
      emptyState:
        effectiveProviderPhase === "error" ? (
          <div>
            <p>Unable to load Providers.</p>
            <ReferenceRetryAction onRetry={onRefreshGlobal} pending={pending} />
          </div>
        ) : (
          "No providers are configured."
        ),
      ...(effectiveProviderPhase === "partial"
        ? {
            partialResult: {
              label: "Partial",
              action: (
                <Button
                  aria-label="Load more Providers"
                  disabled={pending}
                  onClick={() => void onRefreshGlobal()}
                  variant="quiet"
                >
                  Load more
                </Button>
              ),
            },
          }
        : {}),
      nodes: projection.providerIds.map((id) => {
        const provider = providerByName.get(id.slice("provider:".length));
        if (provider === undefined) throw new Error(`Missing ${id}.`);
        const boardState = providerStates.get(provider.api_name);
        if (boardState === undefined)
          throw new Error(`Missing state for ${id}.`);
        return {
          id,
          label: provider.display_name,
          detail: `Provider ID: ${provider.api_name} · Adapter: ${adapterLabels[provider.adapter]}`,
          searchText: [
            provider.api_name,
            provider.adapter,
            adapterLabels[provider.adapter],
            boardState.stateLabel,
          ],
          state: boardState.state,
          stateLabel: boardState.stateLabel,
          ...(boardState.content === undefined
            ? {}
            : { content: boardState.content }),
        };
      }),
    },
    {
      id: "catalog",
      label: "Canonical models",
      countLabel: `${String(visibleModels.length)} models · ${String(visibleMappings.length)} routes`,
      actions: (
        <span className="configuration-column-actions">
          <Button
            disabled={pending}
            onClick={(event) => {
              openCreate("model", event.currentTarget);
            }}
            variant="secondary"
          >
            Add canonical model
          </Button>
          <Button
            disabled={pending}
            onClick={(event) => {
              openCreate("mapping", event.currentTarget);
            }}
            variant="secondary"
          >
            Add provider route
          </Button>
          {effectiveCatalogPhase === "error" ? (
            <>
              <span>Unable to load Canonical models.</span>
              <ReferenceRetryAction
                onRetry={onRefreshGlobal}
                pending={pending}
              />
            </>
          ) : null}
        </span>
      ),
      emptyState:
        effectiveCatalogPhase === "error" ? (
          <div>
            <p>Unable to load Canonical models.</p>
            <ReferenceRetryAction onRetry={onRefreshGlobal} pending={pending} />
          </div>
        ) : (
          "No canonical models are configured."
        ),
      ...(effectiveCatalogPhase === "partial"
        ? {
            partialResult: {
              label: "Partial",
              action: (
                <Button
                  aria-label="Load more Canonical models"
                  disabled={pending}
                  onClick={() => void onRefreshGlobal()}
                  variant="quiet"
                >
                  Load more
                </Button>
              ),
            },
          }
        : {}),
      nodes: [
        ...projection.catalogIds.flatMap((id) => {
          const identity = parseConfigurationNodeId(id);
          if (identity?.kind !== "model") return [];
          const model = modelByName.get(identity.apiName);
          if (model === undefined) throw new Error(`Missing ${id}.`);
          const routes = projection.catalogIds.flatMap((routeId) => {
            const routeIdentity = parseConfigurationNodeId(routeId);
            if (routeIdentity?.kind !== "mapping") return [];
            const route = mappingByName.get(routeIdentity.apiName);
            return route?.model_api_name === model.api_name ? [route] : [];
          });
          const routeAvailable = routes.some(
            (item) => routeStates.get(item.api_name)?.available === true,
          );
          const modelState =
            effectiveCatalogPhase === "partial"
              ? ("partial" as const)
              : routeAvailable
                ? ("ready" as const)
                : ("unavailable" as const);
          const modelStateLabel =
            effectiveCatalogPhase === "partial"
              ? "Partial"
              : routeAvailable
                ? "Ready"
                : "Unavailable";
          const capabilityLabels = modelCapabilityLabels(model);
          const hasUnavailableProvider = routes.some(
            (route) => !providerByName.has(route.provider_api_name),
          );
          return [
            {
              id,
              label: model.display_name,
              detail: `Model ID: ${model.api_name}`,
              content: capabilityLabels.join(" · "),
              searchText: [
                model.api_name,
                ...capabilityLabels,
                modelStateLabel,
              ],
              state: modelState,
              stateLabel: modelStateLabel,
              rowsLabel: "Provider routes",
              rowsEmptyState: "No provider routes.",
              rowsActions: (
                <span className="configuration-column-actions">
                  <Button
                    disabled={pending}
                    onClick={(event) => {
                      requestInspectorTransition({
                        inspector: {
                          kind: "mapping",
                          apiName: null,
                          modelApiName: model.api_name,
                        },
                        selectedNodeId: null,
                        trigger: event.currentTarget,
                      });
                    }}
                    variant="quiet"
                  >
                    Add provider route
                  </Button>
                  {hasUnavailableProvider ? (
                    <ReferenceRetryAction
                      onRetry={onRefreshGlobal}
                      pending={pending}
                    />
                  ) : null}
                </span>
              ),
              rows: routes.map((route) => {
                const provider = providerByName.get(route.provider_api_name);
                const routeState = routeStates.get(route.api_name);
                if (routeState === undefined)
                  throw new Error(`Missing state for ${route.api_name}.`);
                const routeCapabilities = modelCapabilityLabels(route);
                return {
                  id: configurationNodeId.mapping(route.api_name),
                  label:
                    provider?.display_name ??
                    `Unavailable provider: ${route.provider_api_name}`,
                  detail: `Route ID: ${route.api_name} · Wire model: ${route.provider_model_name}`,
                  content: [routeCapabilities.join(" · "), routeState.content]
                    .filter(Boolean)
                    .join(" · "),
                  searchText: [
                    route.api_name,
                    route.model_api_name,
                    route.provider_api_name,
                    route.provider_model_name,
                    ...routeCapabilities,
                    routeState.stateLabel,
                  ],
                  state: routeState.state,
                  stateLabel: routeState.stateLabel,
                };
              }),
            },
          ];
        }),
        ...(visibleMappings.some(
          (route) => !modelByName.has(route.model_api_name),
        )
          ? [
              {
                id: "group:unavailable-referenced-records",
                label: "Unavailable referenced records",
                headerActionable: false,
                rowsLabel: "Provider routes",
                state: "unavailable" as const,
                stateLabel: "Unavailable",
                rowsActions: (
                  <ReferenceRetryAction
                    onRetry={onRefreshGlobal}
                    pending={pending}
                  />
                ),
                rows: visibleMappings.flatMap((route) => {
                  if (modelByName.has(route.model_api_name)) return [];
                  const provider = providerByName.get(route.provider_api_name);
                  return [
                    {
                      id: configurationNodeId.mapping(route.api_name),
                      label: `Unavailable model: ${route.model_api_name} · Route ID: ${route.api_name}`,
                      detail:
                        provider?.display_name ??
                        `Unavailable provider: ${route.provider_api_name}`,
                      searchText: [
                        route.api_name,
                        route.model_api_name,
                        route.provider_api_name,
                        route.provider_model_name,
                        "Unavailable",
                      ],
                      state: "unavailable" as const,
                      stateLabel: "Unavailable",
                    },
                  ];
                }),
              },
            ]
          : []),
      ],
    },
    {
      id: "assignments",
      label: "Assignments",
      countLabel:
        selectedService === ""
          ? "Select a service"
          : assignmentPhase === "loading"
            ? "Loading"
            : assignmentPhase === "error"
              ? "Unavailable"
              : `${String(visibleAssignments.length)} effective`,
      actions: (
        <span className="configuration-column-actions">
          <Button
            disabled={
              pending || selectedService === "" || assignmentPhase === "loading"
            }
            onClick={(event) => {
              openCreate("assignment", event.currentTarget);
            }}
            variant="secondary"
          >
            Add assignment
          </Button>
          {assignmentPhase === "error" && selectedService !== "" ? (
            <>
              <span>Unable to load Assignments.</span>
              <ReferenceRetryAction
                onRetry={onRefreshAssignments}
                pending={pending}
              />
            </>
          ) : null}
        </span>
      ),
      emptyState: assignmentEmptyMessage,
      ...(globalPhase === "partial" || assignmentPhase === "partial"
        ? {
            partialResult: {
              label: "Partial",
              action: (
                <Button
                  aria-label="Load more Assignments"
                  disabled={pending || selectedService === ""}
                  onClick={() => void onRefreshAssignments()}
                  variant="quiet"
                >
                  Load more
                </Button>
              ),
            },
          }
        : {}),
      nodes: projection.assignmentIds.map((id) => {
        const assignment = assignmentByName.get(id.slice("assignment:".length));
        if (assignment === undefined) throw new Error(`Missing ${id}.`);
        const local =
          assignment.defined_by_service_api_name === selectedService;
        const sourceLabel = assignmentSourceLabel(
          assignment,
          selectedService,
          serviceByName,
        );
        const inheritanceLabel = assignmentInheritanceLabel(
          assignment,
          assignmentByName,
        );
        const requirementLabel =
          assignment.observed_requirements.length === 0
            ? "No observed requirements."
            : assignment.observed_requirements
                .map((item) => requirementLabels[item])
                .join(" · ");
        const rungStates = assignment.effective_chain.map((candidate) => {
          const route = mappingByName.get(candidate.provider_model_api_name);
          return route === undefined
            ? undefined
            : routeStates.get(route.api_name);
        });
        const assignmentAvailable = rungStates.some(
          (state) => state?.available === true,
        );
        const sourceUnavailable =
          assignment.definition_kind !== "implicit" &&
          assignment.defined_by_service_api_name !== selectedService &&
          assignment.defined_by_service_api_name !== null &&
          assignment.defined_by_service_api_name !== undefined &&
          !serviceByName.has(assignment.defined_by_service_api_name);
        const inheritedAssignmentUnavailable =
          assignment.inherits_assignment_api_name !== null &&
          assignment.inherits_assignment_api_name !== undefined &&
          !assignmentByName.has(assignment.inherits_assignment_api_name);
        const rungReferenceUnavailable = assignment.effective_chain.some(
          (candidate) => {
            const route = mappingByName.get(candidate.provider_model_api_name);
            return (
              route === undefined ||
              !providerByName.has(route.provider_api_name) ||
              !modelByName.has(route.model_api_name)
            );
          },
        );
        const hasUnavailableReference =
          sourceUnavailable ||
          inheritedAssignmentUnavailable ||
          rungReferenceUnavailable;
        const assignmentState =
          assignmentPhase === "loading"
            ? ("loading" as const)
            : globalPhase === "partial" || assignmentPhase === "partial"
              ? ("partial" as const)
              : hasUnavailableReference
                ? ("unavailable" as const)
                : assignment.effective_chain.length === 0
                  ? ("empty" as const)
                  : assignmentAvailable
                    ? ("ready" as const)
                    : ("unavailable" as const);
        const assignmentStateLabel =
          assignmentPhase === "loading"
            ? "Loading"
            : globalPhase === "partial" || assignmentPhase === "partial"
              ? "Partial"
              : hasUnavailableReference
                ? "Unavailable"
                : assignment.effective_chain.length === 0
                  ? "Empty"
                  : assignmentAvailable
                    ? "Ready"
                    : "Unavailable";
        return {
          id,
          label: assignment.display_name,
          detail: `Assignment ID: ${assignment.api_name}`,
          content: [
            sourceLabel,
            inheritanceLabel,
            `Last used: ${assignment.last_used_at ?? "Never"}`,
            requirementLabel,
          ]
            .filter(Boolean)
            .join(" · "),
          searchText: [
            assignment.api_name,
            assignment.definition_kind,
            assignment.defined_by_service_api_name ?? "implicit",
            sourceLabel,
            inheritanceLabel ?? "",
            requirementLabel,
            assignmentStateLabel,
            ...assignment.effective_chain.map(
              (item) => item.provider_model_api_name,
            ),
          ],
          state: assignmentState,
          stateLabel: assignmentStateLabel,
          rowsLabel: "Effective provider routes",
          rowsEmptyState: "No effective provider routes.",
          ...(hasUnavailableReference
            ? {
                rowsActions: (
                  <ReferenceRetryAction
                    onRetry={() =>
                      Promise.all([
                        onRefreshGlobal(),
                        onRefreshAssignments(),
                      ]).then(() => undefined)
                    }
                    pending={pending}
                  />
                ),
              }
            : {}),
          rows: assignment.effective_chain.map((candidate, index) => {
            const position = index + 1;
            const positionLabel = assignmentPositionLabel(position);
            const route = mappingByName.get(candidate.provider_model_api_name);
            const routeState =
              route === undefined ? undefined : routeStates.get(route.api_name);
            const provider =
              route === undefined
                ? undefined
                : providerByName.get(route.provider_api_name);
            const routeModel =
              route === undefined
                ? undefined
                : modelByName.get(route.model_api_name);
            const mismatch =
              route !== undefined &&
              assignment.observed_requirements.some(
                (requirement) => !routeMeetsRequirement(route, requirement),
              );
            return {
              id: configurationNodeId.rung(assignment.api_name, position),
              label: positionLabel,
              detail:
                route === undefined
                  ? `Unavailable route: ${candidate.provider_model_api_name}`
                  : `${provider?.display_name ?? `Unavailable provider: ${route.provider_api_name}`} · ${routeModel?.display_name ?? `Unavailable model: ${route.model_api_name}`}`,
              content: [
                route === undefined ? null : `Route ID: ${route.api_name}`,
                local ? null : "Inherited",
                mismatch ? "Does not meet observed requirements" : null,
                routeState?.content,
              ]
                .filter(Boolean)
                .join(" · "),
              searchText: [
                positionLabel,
                candidate.provider_model_api_name,
                provider?.display_name ?? "",
                routeModel?.display_name ?? "",
                routeState?.stateLabel ?? "Unavailable",
                local ? "Local" : "Inherited",
              ],
              state: routeState?.state ?? ("unavailable" as const),
              stateLabel: [
                routeState?.stateLabel ?? "Unavailable",
                local ? null : "Inherited",
              ]
                .filter(Boolean)
                .join(" · "),
            };
          }),
        };
      }),
    },
  ];

  const relationships = projection.relationships.flatMap((relationship) => {
    const source = parseConfigurationNodeId(relationship.sourceId);
    const target = parseConfigurationNodeId(relationship.targetId);
    if (source?.kind === "provider" && target?.kind === "mapping") {
      const provider = providerByName.get(source.apiName);
      const route = mappingByName.get(target.apiName);
      const routeModel =
        route === undefined ? undefined : modelByName.get(route.model_api_name);
      if (
        provider === undefined ||
        route === undefined ||
        routeModel === undefined
      )
        return [];
      return [
        {
          ...relationship,
          label: "Provides",
          accessibleLabel: `${provider.display_name} (Provider ID: ${provider.api_name}) provides Route ID: ${route.api_name} for ${routeModel.display_name} (Model ID: ${routeModel.api_name})`,
        },
      ];
    }
    if (source?.kind === "mapping" && target?.kind === "rung") {
      const route = mappingByName.get(source.apiName);
      const assignment = assignmentByName.get(target.apiName);
      if (
        route === undefined ||
        !modelByName.has(route.model_api_name) ||
        assignment === undefined ||
        target.position === undefined
      )
        return [];
      const label = assignmentPositionLabel(target.position);
      return [
        {
          ...relationship,
          label,
          accessibleLabel: `${label}: Route ID: ${route.api_name} for ${assignment.display_name} (Assignment ID: ${assignment.api_name})`,
        },
      ];
    }
    return [];
  });

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
    if (!beginPending()) return;
    try {
      const saved =
        inspector?.apiName === null
          ? await client.createProvider(value, csrf)
          : await client.putProvider(value.api_name, value, csrf);
      setConfirmedGlobal((current) => ({
        ...current,
        providers: retainConfirmedRecord(current.providers, saved),
      }));
      setDeletedGlobal((current) => ({
        ...current,
        providers: discardDeletedRecord(current.providers, saved.api_name),
      }));
      await onRefreshGlobal();
      onNotice("success", "The global provider connection was saved.");
      setSelectedNodeId(`provider:${value.api_name}`);
      setInspector({ kind: "provider", apiName: value.api_name });
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      finishPending();
    }
  }

  async function saveCredential(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const { apiName: name, secret } = credentialFormValue(form);
    if (visibleCredentials.some((item) => item.api_name === name)) {
      pendingCredentialReplacementRef.current = {
        form: formElement,
        name,
        secret,
      };
      if (document.activeElement instanceof HTMLElement)
        returnFocusRef.current = document.activeElement;
      setDeleteTarget({
        kind: "credential-replace",
        apiName: name,
        impact: `replace the stored secret for credential ${name}; the prior secret stops serving new attempts after commit`,
      });
      return;
    }
    if (!beginPending()) return;
    try {
      await client.createCredential(name, secret, csrf);
      setDeletedGlobal((current) => ({
        ...current,
        credentials: discardDeletedRecord(current.credentials, name),
      }));
      formElement.reset();
      await onRefreshGlobal();
      onNotice("success", "The write-only credential was saved.");
    } catch (error) {
      const secretInput = formElement.elements.namedItem("secret");
      if (secretInput instanceof HTMLInputElement) secretInput.value = "";
      onNotice("error", errorMessage(error));
    } finally {
      finishPending();
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
    if (!beginPending()) return;
    try {
      const saved =
        inspector?.apiName === null
          ? await client.createModel(value, csrf)
          : await client.putModel(value.api_name, value, csrf);
      setConfirmedGlobal((current) => ({
        ...current,
        models: retainConfirmedRecord(current.models, saved),
      }));
      setDeletedGlobal((current) => ({
        ...current,
        models: discardDeletedRecord(current.models, saved.api_name),
      }));
      await onRefreshGlobal();
      onNotice("success", "The global canonical model was saved.");
      setSelectedNodeId(`model:${value.api_name}`);
      setInspector({ kind: "model", apiName: value.api_name });
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      finishPending();
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
    if (!beginPending()) return;
    try {
      const saved =
        inspector?.apiName === null
          ? await client.createProviderModel(value, csrf)
          : await client.putProviderModel(value.api_name, value, csrf);
      setConfirmedGlobal((current) => ({
        ...current,
        mappings: retainConfirmedRecord(current.mappings, saved),
      }));
      setDeletedGlobal((current) => ({
        ...current,
        mappings: discardDeletedRecord(current.mappings, saved.api_name),
      }));
      await onRefreshGlobal();
      onNotice("success", "The global provider-model mapping was saved.");
      setSelectedNodeId(`mapping:${value.api_name}`);
      setInspector({ kind: "mapping", apiName: value.api_name });
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      finishPending();
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
    if (!beginPending(true)) return;
    try {
      const saved = await client.putAssignment(
        selectedService,
        name,
        value,
        csrf,
      );
      setConfirmedAssignments((current) => ({
        serviceApiName: selectedService,
        records: retainConfirmedRecord(
          current.serviceApiName === selectedService ? current.records : [],
          saved,
        ),
        deleted: discardDeletedRecord(
          current.serviceApiName === selectedService ? current.deleted : [],
          saved.api_name,
        ),
      }));
      await onRefreshAssignments();
      setAssignmentDirty(false);
      onAssignmentDirtyChange(false);
      onNotice("success", "The selected service assignment was saved.");
      setSelectedNodeId(`assignment:${name}`);
      setInspector({
        kind: "assignment",
        apiName: name,
        serviceApiName: selectedService,
      });
    } catch (error) {
      onNotice("error", errorMessage(error));
    } finally {
      finishPending();
    }
  }

  async function previewOpenRouter(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!beginPending()) return;
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
      finishPending();
    }
  }

  async function confirmOpenRouter() {
    if (importPreview === null) return;
    const provider_models = importPreview.provider_options.flatMap((option) =>
      selectedImportProviders.has(option.provider_api_name) && option.selectable
        ? [option.provider_model]
        : [],
    );
    if (!beginPending()) return;
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
      setDeletedGlobal((current) => ({
        ...current,
        models: discardDeletedRecord(current.models, result.model.api_name),
        mappings: result.provider_models.reduce(
          (deleted, mapping) => discardDeletedRecord(deleted, mapping.api_name),
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
      finishPending();
    }
  }

  async function deleteRecord() {
    if (deleteTarget === null) return;
    if (deleteTarget.kind === "draft") {
      const transition = pendingInspectorTransitionRef.current;
      pendingInspectorTransitionRef.current = null;
      setAssignmentDirty(false);
      onAssignmentDirtyChange(false);
      setDeleteTarget(null);
      if (transition === null) setInspector(null);
      else applyInspectorTransition(transition);
      return;
    }
    if (deleteTarget.kind === "credential-replace") {
      const replacement = pendingCredentialReplacementRef.current;
      if (replacement === null || !beginPending()) return;
      try {
        await client.replaceCredential(
          replacement.name,
          replacement.secret,
          csrf,
        );
        replacement.form.reset();
        pendingCredentialReplacementRef.current = null;
        await onRefreshGlobal();
        setDeleteTarget(null);
        onNotice("success", "The write-only credential was replaced.");
      } catch (error) {
        const secretInput = replacement.form.elements.namedItem("secret");
        if (secretInput instanceof HTMLInputElement) secretInput.value = "";
        pendingCredentialReplacementRef.current = null;
        setDeleteTarget(null);
        onNotice("error", errorMessage(error));
      } finally {
        finishPending();
      }
      return;
    }
    const assignmentOperation =
      deleteTarget.kind === "assignment" || deleteTarget.kind === "requirement";
    if (!beginPending(assignmentOperation)) return;
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
      const deletedGlobalKey: keyof DeletedGlobalRecords | null =
        deleteTarget.kind === "provider"
          ? "providers"
          : deleteTarget.kind === "model"
            ? "models"
            : deleteTarget.kind === "mapping"
              ? "mappings"
              : deleteTarget.kind === "credential"
                ? "credentials"
                : null;
      if (deletedGlobalKey !== null)
        setDeletedGlobal((current) => ({
          ...current,
          [deletedGlobalKey]: retainDeletedRecord(
            current[deletedGlobalKey],
            deleteTarget.apiName,
          ),
        }));
      if (
        (deleteTarget.kind === "assignment" ||
          deleteTarget.kind === "requirement") &&
        selectedService !== ""
      )
        setConfirmedAssignments((current) => {
          const records =
            current.serviceApiName === selectedService ? current.records : [];
          const deleted =
            current.serviceApiName === selectedService ? current.deleted : [];
          if (deleteTarget.kind === "assignment")
            return {
              serviceApiName: selectedService,
              records: discardConfirmedRecord(records, deleteTarget.apiName),
              deleted: retainDeletedRecord(deleted, deleteTarget.apiName),
            };
          const assignment = assignmentByName.get(deleteTarget.apiName);
          return {
            serviceApiName: selectedService,
            records:
              assignment === undefined || deleteTarget.requirement === undefined
                ? records
                : retainConfirmedRecord(records, {
                    ...assignment,
                    observed_requirements:
                      assignment.observed_requirements.filter(
                        (item) => item !== deleteTarget.requirement,
                      ),
                  }),
            deleted,
          };
        });
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
      finishPending();
    }
  }

  const inspectorContext =
    inspector === null
      ? null
      : ({
          assignmentByName,
          assignmentDirty,
          beginPending,
          chainRows,
          client,
          closeInspector,
          confirmOpenRouter,
          credentials: visibleCredentials,
          csrf,
          importInput,
          importPreview,
          inspector,
          finishPending,
          markAssignmentDirty,
          mappingByName,
          modelByName,
          models: visibleModels,
          onAssignmentDirtyChange,
          onNotice,
          openPlayground: (target, trigger) => {
            if (pendingRef.current) return;
            playgroundReturnFocusRef.current = trigger;
            setPlaygroundTarget(target);
          },
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
          services,
          setAssignmentDirty,
          setChainRows,
          setDeleteTarget,
          setImportInput,
          setImportPreview,
          setInspector,
          setSelectedNodeId,
          setSelectedImportProviders,
        } satisfies ConfigurationInspectorContext);
  const inspectorContent =
    inspectorContext === null ||
    (inspector?.kind === "assignment" &&
      inspector.serviceApiName !== selectedService) ? null : (
      <ConfigurationInspector context={inspectorContext} />
    );
  const auxiliaryInspector =
    inspector?.apiName === null ? inspectorContent : null;
  const selectedNodeInspector =
    inspector?.apiName === null ? null : inspectorContent;
  const emptyCatalogState =
    effectiveProviderPhase === "error" || effectiveCatalogPhase === "error" ? (
      <div>
        <p>
          {effectiveProviderPhase === "error"
            ? "Unable to load Providers."
            : "No providers are configured."}
        </p>
        <p>
          {effectiveCatalogPhase === "error"
            ? "Unable to load Canonical models."
            : "No canonical models are configured."}
        </p>
        <p>{assignmentEmptyMessage}</p>
        <Button
          disabled={pending}
          onClick={() => void onRefreshGlobal()}
          variant="quiet"
        >
          Retry
        </Button>
      </div>
    ) : (
      <div>
        <p>No providers are configured.</p>
        <p>No canonical models are configured.</p>
        <p>{assignmentEmptyMessage}</p>
        <div className="configuration-column-actions">
          <Button
            disabled={pending}
            onClick={(event) => {
              openCreate("provider", event.currentTarget);
            }}
            variant="secondary"
          >
            Add provider
          </Button>
          <Button
            disabled={pending}
            onClick={(event) => {
              openCreate("model", event.currentTarget);
            }}
            variant="secondary"
          >
            Add canonical model
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
    playground:
      playgroundTarget === null ? null : (
        <PlaygroundModal
          client={client}
          csrf={csrf}
          currentTarget={
            globalPhase === "ready" &&
            (playgroundTarget.kind !== "assignment" ||
              playgroundTarget.serviceContext === selectedService)
              ? currentPlaygroundTarget(
                  playgroundTarget,
                  visibleAssignments,
                  visibleMappings,
                  visibleProviders,
                  visibleModels,
                )
              : null
          }
          onClose={() => {
            const returnTarget = playgroundReturnFocusRef.current;
            setPlaygroundTarget(null);
            const restorePlaygroundFocus = () => {
              if (returnTarget?.isConnected) {
                returnTarget.focus({ preventScroll: true });
                return;
              }
              const fallback =
                document.querySelector<HTMLElement>(
                  '.od-relationship-graph-node[data-selected="true"]:not(:disabled)',
                ) ??
                document.querySelector<HTMLElement>(
                  '.od-relationship-graph-node[tabindex="0"]:not(:disabled), .od-relationship-graph-empty button:not(:disabled), .configuration-graph-page button:not(:disabled)',
                );
              fallback?.focus({ preventScroll: true });
            };
            if (typeof requestAnimationFrame === "function")
              requestAnimationFrame(restorePlaygroundFocus);
            else restorePlaygroundFocus();
          }}
          onMediaJobChange={(job) => {
            setPlaygroundMediaRecovery((current) =>
              updateMediaRecovery(current, playgroundTarget, job),
            );
          }}
          onRefreshTarget={async () => {
            await Promise.all([onRefreshGlobal(), onRefreshAssignments()]);
          }}
          onUncertainMediaAdmissionChange={(uncertain) => {
            setPlaygroundUncertainMediaAdmissions((current) => {
              const key = playgroundTargetKey(playgroundTarget);
              const next = new Set(current);
              if (uncertain) next.add(key);
              else next.delete(key);
              return next;
            });
          }}
          retainedMediaJob={
            playgroundMediaRecovery.get(
              playgroundTargetKey(playgroundTarget),
            ) ?? null
          }
          retainedUncertainMediaAdmission={playgroundUncertainMediaAdmissions.has(
            playgroundTargetKey(playgroundTarget),
          )}
          returnFocusRef={playgroundReturnFocusRef}
          target={playgroundTarget}
        />
      ),
    relationships,
    returnFocusRef,
    setDeleteTarget,
    selectedNodeId,
    selectedNodeInspector,
    onSelectionChange: (nodeId: string | null) => {
      if (nodeId !== null) {
        if (pendingRef.current) return;
        if (!(inspector?.kind === "assignment" && assignmentDirty))
          setSelectedNodeId(nodeId);
        return;
      }
      setSelectedNodeId(null);
      if (inspector?.apiName === null) return;
      setInspector(null);
      if (inspector?.kind === "assignment") {
        setAssignmentDirty(false);
        onAssignmentDirtyChange(false);
      }
    },
    cancelDeleteTarget: () => {
      pendingInspectorTransitionRef.current = null;
      const replacement = pendingCredentialReplacementRef.current;
      if (replacement !== null) {
        const secretInput = replacement.form.elements.namedItem("secret");
        if (secretInput instanceof HTMLInputElement) secretInput.value = "";
        pendingCredentialReplacementRef.current = null;
      }
      setDeleteTarget(null);
    },
  };
}

export function ConfigurationGraph(props: ConfigurationGraphProps) {
  const {
    activate,
    auxiliaryInspector,
    cancelDeleteTarget,
    columns,
    deleteRecord,
    deleteTarget,
    globalPhase,
    emptyCatalogState,
    onRefreshGlobal,
    onSelectionChange,
    pending,
    playground,
    relationships,
    returnFocusRef,
    selectedNodeId,
    selectedNodeInspector,
  } = useConfigurationController(props);
  const graphState = (
    <GraphState onRetry={() => void onRefreshGlobal()} phase={globalPhase} />
  );
  const hasSafeRecords = columns.some((column) => column.nodes.length > 0);
  return (
    <section className="configuration-graph-page">
      {globalPhase === "partial" ? (
        <StatePanel kind="empty" title="Partial configuration graph">
          The Router returned a bounded subset. More global records are
          available. This graph does not claim to be complete.
        </StatePanel>
      ) : null}
      {globalPhase === "loading" && !hasSafeRecords ? (
        graphState
      ) : (
        <>
          {globalPhase === "loading" ||
          (globalPhase === "error" && !hasSafeRecords)
            ? graphState
            : null}
          <RelationshipGraph
            aria-label="LLM configuration relationships"
            auxiliaryInspector={auxiliaryInspector}
            columns={columns}
            emptyState={emptyCatalogState}
            inspector={selectedNodeInspector}
            noResultsDescription="Change the search or restore the complete configuration board."
            noResultsTitle="No configuration matches this search."
            onNodeActivate={activate}
            onSelectionChange={onSelectionChange}
            relationships={relationships}
            partialNoResultsDescription="Load more records or change the search to continue."
            partialNoResultsTitle="No matches in loaded records."
            searchLabel="Search configuration"
            selectedNodeId={selectedNodeId}
          />
        </>
      )}
      <ConfirmationDialog
        confirmLabel={
          deleteTarget?.kind === "draft"
            ? "Discard changes"
            : deleteTarget?.kind === "credential-replace"
              ? "Replace credential"
              : deleteTarget?.kind === "requirement"
                ? "Remove requirement"
                : "Delete record"
        }
        description={deleteTarget?.impact ?? "Delete the selected record."}
        {...(deleteTarget === null
          ? {}
          : { impactStatement: deleteTarget.impact })}
        onCancel={() => {
          cancelDeleteTarget();
        }}
        onConfirm={() => void deleteRecord()}
        open={deleteTarget !== null}
        pending={pending}
        {...(deleteTarget?.kind === "draft" ? {} : { returnFocusRef })}
        title={
          deleteTarget?.kind === "draft"
            ? "Discard assignment changes?"
            : deleteTarget?.kind === "credential-replace"
              ? "Replace this credential secret?"
              : deleteTarget?.kind === "requirement"
                ? "Remove this observed requirement?"
                : "Confirm configuration deletion"
        }
      />
      {playground}
    </section>
  );
}

function ConfigurationInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  if (context.inspector.kind === "provider")
    return (
      <ProviderInspector
        context={context}
        key={`provider:${context.inspector.apiName ?? "new"}`}
      />
    );
  if (context.inspector.kind === "model")
    return (
      <ModelInspector
        context={context}
        key={`model:${context.inspector.apiName ?? "new"}`}
      />
    );
  if (context.inspector.kind === "mapping")
    return (
      <MappingInspector
        context={context}
        key={`mapping:${context.inspector.apiName ?? "new"}`}
      />
    );
  return (
    <AssignmentInspector
      context={context}
      key={`assignment:${context.inspector.serviceApiName ?? "none"}:${context.inspector.apiName ?? "new"}:${String(context.inspector.rungPosition ?? "header")}`}
    />
  );
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
  const [selectedCredential, setSelectedCredential] = useState(
    provider?.credential_api_name ?? "",
  );
  const [selectedEndpoint, setSelectedEndpoint] = useState(
    provider?.endpoint ?? "",
  );
  const [selectedEnabled, setSelectedEnabled] = useState(
    provider?.enabled ?? true,
  );
  const fieldPolicy = adapterFieldPolicy[selectedAdapter];
  const boardState =
    provider === undefined
      ? undefined
      : providerBoardState(
          provider,
          new Set(credentials.map((item) => item.api_name)),
        );
  return (
    <GraphInspector
      activationKey={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      eyebrow="Global provider connection"
      {...(pending ? {} : { onClose: closeInspector })}
      returnFocusRef={returnFocusRef}
      title={provider?.display_name ?? "Add provider"}
    >
      {provider === undefined
        ? null
        : recordFacts([
            ["Provider ID", provider.api_name],
            ["Adapter", adapterLabels[provider.adapter]],
            ["Endpoint", provider.endpoint ?? "Router standard endpoint"],
            ["Credential", provider.credential_api_name ?? "None"],
            ["State", boardState?.stateLabel ?? "Unavailable"],
            ...(boardState?.content === undefined
              ? []
              : [["Corrective action", boardState.content] as const]),
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
              const next = event.currentTarget.value as ProviderAdapter;
              setSelectedAdapter(next);
              setSelectedCredential("");
              setSelectedEndpoint("");
            }}
            value={selectedAdapter}
          >
            {providerAdapters.map((item) => (
              <option key={item} value={item}>
                {adapterLabels[item]}
              </option>
            ))}
          </select>
        </label>
        {fieldPolicy.credential === "none" ? (
          <p className="field-note">This adapter does not use a credential.</p>
        ) : (
          <label>
            Applicable credential
            <select
              name="credential_api_name"
              onChange={(event) => {
                setSelectedCredential(event.currentTarget.value);
              }}
              required={fieldPolicy.credential === "required"}
              value={selectedCredential}
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
                name="endpoint"
                onChange={(event) => {
                  setSelectedEndpoint(event.currentTarget.value);
                }}
                placeholder="https://provider.example/v1"
                required
                type="url"
                value={selectedEndpoint}
              />
            </label>
          ) : null}
          <label className="checkbox-field">
            <input
              checked={selectedEnabled}
              name="enabled"
              onChange={(event) => {
                setSelectedEnabled(event.currentTarget.checked);
              }}
              type="checkbox"
            />
            Enabled after validation
          </label>
          {recordFacts([
            [
              "Adapter",
              provider === undefined || provider.adapter === selectedAdapter
                ? adapterLabels[selectedAdapter]
                : `${adapterLabels[provider.adapter]} → ${adapterLabels[selectedAdapter]}`,
            ],
            [
              "Endpoint after save",
              fieldPolicy.endpoint === "inferred"
                ? "Registered standard endpoint and safe defaults"
                : selectedEndpoint || "Required before save",
            ],
            [
              "Credential after save",
              fieldPolicy.credential === "none"
                ? "None; this adapter does not accept one"
                : selectedCredential ||
                  (fieldPolicy.credential === "required"
                    ? "Required before save"
                    : "None"),
            ],
            ["State after save", selectedEnabled ? "Enabled" : "Disabled"],
          ])}
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
                disabled={pending}
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
            disabled={pending}
            onClick={() => {
              setInspector({
                kind: "mapping",
                apiName: null,
                providerApiName: provider.api_name,
              });
            }}
            variant="secondary"
          >
            Add provider route for this provider
          </Button>
          <Button
            disabled={pending}
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
    setImportPreview,
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
    beginPending,
    finishPending,
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
      {...(pending ? {} : { onClose: closeInspector })}
      returnFocusRef={returnFocusRef}
      title={model?.display_name ?? "Add canonical model"}
    >
      {model === undefined
        ? null
        : recordFacts([
            ["Model ID", model.api_name],
            ["Capabilities", modelCapabilityLabels(model).join(", ") || "None"],
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
            disabled={pending}
            onClick={() => {
              setInspector({
                kind: "mapping",
                apiName: null,
                modelApiName: model.api_name,
              });
            }}
            variant="secondary"
          >
            Add provider route for this model
          </Button>
          <Button
            disabled={pending || applicableMappings.length === 0}
            onClick={() => {
              if (!beginPending()) return;
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
                  finishPending();
                });
            }}
            variant="secondary"
          >
            Synchronize applicable prices
          </Button>
          <Button
            disabled={pending}
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
                  setImportPreview(null);
                  setSelectedImportProviders(new Set());
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
    credentials,
    returnFocusRef,
    saveMapping,
    providers,
    models,
    pending,
    beginPending,
    finishPending,
    client,
    csrf,
    onRefreshGlobal,
    onNotice,
    setDeleteTarget,
    mappingByName,
    openPlayground,
    providerModels,
  } = context;
  const mapping =
    inspector.apiName === null
      ? undefined
      : mappingByName.get(inspector.apiName);
  const mappingProvider = providers.find(
    (item) => item.api_name === mapping?.provider_api_name,
  );
  const mappingModel = models.find(
    (item) => item.api_name === mapping?.model_api_name,
  );
  const mappingState =
    mapping === undefined
      ? undefined
      : routeBoardState(
          mapping,
          mappingProvider === undefined
            ? undefined
            : providerBoardState(
                mappingProvider,
                new Set(credentials.map((item) => item.api_name)),
              ),
          mappingModel !== undefined,
        );
  const playgroundTarget =
    mapping === undefined
      ? null
      : mappingPlaygroundTarget(
          mapping.api_name,
          providerModels,
          providers,
          models,
        );
  return (
    <GraphInspector
      activationKey={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      eyebrow="Global provider route"
      {...(pending ? {} : { onClose: closeInspector })}
      returnFocusRef={returnFocusRef}
      title={
        mapping === undefined
          ? "Add provider route"
          : (mappingProvider?.display_name ??
            `Unavailable provider: ${mapping.provider_api_name}`)
      }
    >
      {mapping === undefined
        ? null
        : recordFacts([
            ["Route ID", mapping.api_name],
            [
              "Provider",
              mappingProvider === undefined
                ? `Unavailable provider: ${mapping.provider_api_name}`
                : `${mappingProvider.display_name} (Provider ID: ${mappingProvider.api_name})`,
            ],
            [
              "Canonical model",
              mappingModel === undefined
                ? `Unavailable model: ${mapping.model_api_name}`
                : `${mappingModel.display_name} (Model ID: ${mappingModel.api_name})`,
            ],
            ["Provider wire model", mapping.provider_model_name],
            ["State", mappingState?.stateLabel ?? "Unavailable"],
            ...(mappingState?.content === undefined
              ? []
              : [["State detail", mappingState.content] as const]),
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
          Route API name
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
          Save provider route
        </Button>
      </form>
      {mapping === undefined ? null : (
        <div className="configuration-inspector-actions">
          <Button
            disabled={pending}
            onClick={() => {
              if (!beginPending()) return;
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
                  finishPending();
                });
            }}
            variant="secondary"
          >
            Synchronize price
          </Button>
          <Button
            disabled={pending}
            onClick={() => {
              setDeleteTarget({
                kind: "mapping",
                apiName: mapping.api_name,
                impact: `delete provider-model mapping ${mapping.api_name}`,
              });
            }}
            variant="quiet"
          >
            Delete provider route
          </Button>
          {playgroundTarget === null ? null : (
            <Button
              disabled={pending}
              onClick={(event) => {
                openPlayground(playgroundTarget, event.currentTarget);
              }}
              variant="quiet"
            >
              Play exact route
            </Button>
          )}
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
  const priceDefaults = providerModelPriceFormDefaults(mapping);
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
        <input defaultValue={priceDefaults.source} name="price_source" />
      </label>
      <label>
        Source model identifier
        <input defaultValue={priceDefaults.lookupKey} name="price_lookup_key" />
      </label>
      <label>
        Manual price currency
        <input
          defaultValue={priceDefaults.currency}
          maxLength={3}
          name="currency"
          placeholder="USD"
        />
      </label>
      <label>
        Manual typed unit prices
        <textarea
          defaultValue={priceDefaults.unitPrices}
          name="unit_prices"
          placeholder="input_token=0.001, output_token=0.002"
          rows={3}
        />
      </label>
    </details>
  );
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- This inspector owns one assignment form, ordered rungs, inherited state, and related commands as one boundary.
function AssignmentInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  const {
    inspector,
    assignmentByName,
    selectedService,
    services,
    closeInspector,
    credentials,
    returnFocusRef,
    markAssignmentDirty,
    saveAssignment,
    chainRows,
    setChainRows,
    providerModels,
    assignmentDirty,
    pending,
    setDeleteTarget,
    openPlayground,
    mappingByName,
    modelByName,
    providerByName,
    providers,
  } = context;
  const assignment =
    inspector.apiName === null
      ? undefined
      : assignmentByName.get(inspector.apiName);
  const isLocal = assignment?.defined_by_service_api_name === selectedService;
  const serviceByName = new Map(
    services.map((item) => [item.api_name, item] as const),
  );
  const sourceLabel =
    assignment === undefined
      ? null
      : assignmentSourceLabel(assignment, selectedService, serviceByName);
  const inheritanceLabel =
    assignment === undefined
      ? null
      : assignmentInheritanceLabel(assignment, assignmentByName);
  const sourceServiceApiName = assignment?.defined_by_service_api_name;
  const sourceService =
    sourceServiceApiName === null ||
    sourceServiceApiName === undefined ||
    sourceServiceApiName === selectedService
      ? undefined
      : serviceByName.get(sourceServiceApiName);
  const inheritedAssignmentApiName = assignment?.inherits_assignment_api_name;
  const inheritedAssignment =
    inheritedAssignmentApiName === null ||
    inheritedAssignmentApiName === undefined
      ? undefined
      : assignmentByName.get(inheritedAssignmentApiName);
  const selectedCandidate =
    inspector.rungPosition === undefined
      ? undefined
      : assignment?.effective_chain[inspector.rungPosition - 1];
  const selectedRoute =
    selectedCandidate === undefined
      ? undefined
      : mappingByName.get(selectedCandidate.provider_model_api_name);
  const selectedProvider =
    selectedRoute === undefined
      ? undefined
      : providerByName.get(selectedRoute.provider_api_name);
  const selectedModel =
    selectedRoute === undefined
      ? undefined
      : modelByName.get(selectedRoute.model_api_name);
  const selectedRouteState =
    selectedRoute === undefined
      ? undefined
      : routeBoardState(
          selectedRoute,
          selectedProvider === undefined
            ? undefined
            : providerBoardState(
                selectedProvider,
                new Set(credentials.map((item) => item.api_name)),
              ),
          selectedModel !== undefined,
        );
  const playgroundTarget =
    assignment === undefined
      ? null
      : assignmentPlaygroundTarget(
          assignment.api_name,
          selectedService,
          [...assignmentByName.values()],
          providerModels,
          providers,
        );
  const [definitionMode, setDefinitionMode] = useState(
    assignment?.definition_kind === "inherited_assignment"
      ? "inherit"
      : "direct",
  );
  return (
    <GraphInspector
      activationKey={`${inspector.kind}:${inspector.apiName ?? "new"}:${String(inspector.rungPosition ?? "header")}`}
      eyebrow={
        selectedService === ""
          ? "Select a service"
          : `${serviceByName.get(selectedService)?.display_name ?? selectedService} configuration context`
      }
      {...(pending ? {} : { onClose: closeInspector })}
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
                ["Assignment ID", assignment.api_name],
                ...(inspector.rungPosition === undefined
                  ? []
                  : [
                      [
                        "Selected rung",
                        assignmentPositionLabel(inspector.rungPosition),
                      ] as const,
                      [
                        "Selected route",
                        selectedCandidate?.provider_model_api_name ??
                          "Unavailable",
                      ] as const,
                      [
                        "Provider",
                        selectedRoute === undefined
                          ? "Unavailable"
                          : selectedProvider === undefined
                            ? `Unavailable provider: ${selectedRoute.provider_api_name}`
                            : `${selectedProvider.display_name} (Provider ID: ${selectedProvider.api_name})`,
                      ] as const,
                      [
                        "Canonical model",
                        selectedRoute === undefined
                          ? "Unavailable"
                          : selectedModel === undefined
                            ? `Unavailable model: ${selectedRoute.model_api_name}`
                            : `${selectedModel.display_name} (Model ID: ${selectedModel.api_name})`,
                      ] as const,
                      [
                        "Route state",
                        [
                          selectedRouteState?.stateLabel ?? "Unavailable",
                          selectedRouteState?.content,
                        ]
                          .filter(Boolean)
                          .join(" · "),
                      ] as const,
                    ]),
                [
                  "Definition",
                  assignmentDefinitionLabels[assignment.definition_kind],
                ],
                ["Definition source", sourceLabel ?? "Implicit root default"],
                [
                  "Effective chain",
                  assignment.effective_chain
                    .map((item) => item.provider_model_api_name)
                    .join(" → ") || "Empty",
                ],
                ["Inherited assignment", inheritanceLabel ?? "None"],
                ["Last used", assignment.last_used_at ?? "Never"],
                [
                  "Observed",
                  assignment.observed_requirements
                    .map((item) => requirementLabels[item])
                    .join(", ") || "No observed requirements.",
                ],
              ])}
              {sourceService === undefined ? null : (
                <details className="configuration-advanced">
                  <summary>Inspect source service</summary>
                  {recordFacts([
                    ["Service ID", sourceService.api_name],
                    ["Display name", sourceService.display_name],
                    [
                      "Parent service",
                      sourceService.parent_service_api_name ?? "None",
                    ],
                    ["Created", sourceService.created_at],
                  ])}
                </details>
              )}
              {inheritedAssignment === undefined ? null : (
                <details className="configuration-advanced">
                  <summary>Inspect inherited assignment</summary>
                  {recordFacts([
                    ["Assignment ID", inheritedAssignment.api_name],
                    ["Display name", inheritedAssignment.display_name],
                    [
                      "Definition source",
                      assignmentSourceLabel(
                        inheritedAssignment,
                        selectedService,
                        serviceByName,
                      ),
                    ],
                    [
                      "Effective chain",
                      inheritedAssignment.effective_chain
                        .map((item) => item.provider_model_api_name)
                        .join(" → ") || "Empty",
                    ],
                    ["Last used", inheritedAssignment.last_used_at ?? "Never"],
                  ])}
                </details>
              )}
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
                        <span>{requirementLabels[requirement]}</span>
                        <Button
                          disabled={pending}
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
                name="definition_kind"
                onChange={(event) => {
                  setDefinitionMode(event.currentTarget.value);
                  markAssignmentDirty();
                }}
                value={definitionMode}
              >
                <option value="direct">Ordered direct chain</option>
                <option value="inherit">Inherit another assignment</option>
              </select>
            </label>
            {definitionMode === "inherit" ? (
              <label>
                Inherited assignment
                <input
                  defaultValue={
                    assignment?.inherits_assignment_api_name ?? "default"
                  }
                  name="inherits_assignment_api_name"
                  required
                />
              </label>
            ) : null}
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
            {definitionMode === "direct" ? (
              <AssignmentChainEditor
                onDirty={markAssignmentDirty}
                providerModels={providerModels}
                rows={chainRows}
                setRows={setChainRows}
              />
            ) : null}
            <div className="configuration-inspector-actions">
              <Button disabled={pending} type="submit">
                Save selected service assignment
              </Button>
              {assignmentDirty ? (
                <Button
                  disabled={pending}
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
              disabled={pending}
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
          {playgroundTarget === null ? null : (
            <Button
              disabled={pending}
              onClick={(event) => {
                openPlayground(playgroundTarget, event.currentTarget);
              }}
              variant="quiet"
            >
              Play assignment
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
      header: "Provider route",
      renderRead: ({ row, update }) => (
        <select
          aria-label={`${row.label} provider route`}
          onChange={(event) => {
            update({ providerModel: event.currentTarget.value });
            onDirty();
          }}
          value={row.draft.providerModel}
        >
          <option value="">Select provider route</option>
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
        ariaLabel="Ordered assignment provider-route chain"
        columns={columns}
        density="compact"
        onDelete={(rowId) => {
          setRows((current) =>
            orderChainRows(current.filter((row) => row.id !== rowId)),
          );
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
            setRows(orderChainRows(orderedRows));
            onDirty();
          },
        }}
        rows={rows}
        state={
          rows.length === 0
            ? {
                kind: "empty",
                message: "No direct candidates. Add the first provider route.",
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
              label: assignmentPositionLabel(current.length + 1),
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
                  disabled={pending}
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
              disabled={pending || !option.selectable}
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
