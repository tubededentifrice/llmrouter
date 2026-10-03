import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
  type SubmitEvent,
} from "react";
import {
  AdvancedFieldsDisclosure,
  FormControls,
  FormField,
  FormGrid,
  FormSection,
  SearchableSelect,
  Button,
  CapabilityTag,
  CheckboxControl,
  CheckboxChipGroup,
  ConfirmationDialog,
  OrderedChoiceList,
  FormActions,
  InlineAlert,
  Icon,
  IconButton,
  Dialog,
  GraphInspectorFact,
  GraphInspectorFacts,
  GraphInspectorNotice,
  GraphInspectorRow,
  GraphInspectorRows,
  RelationshipGraph,
  NumberControl,
  SelectControl,
  StatePanel,
  SwitchControl,
  TextareaControl,
  TextControl,
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
  type ReasoningStrategy,
  type Service,
} from "./api.ts";
import {
  adapterFieldPolicy,
  assignmentChainSource,
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
import {
  configurationCapabilities,
  modelCapabilityKeys,
  modelHasCapability,
} from "./configurationCapabilities.ts";
import { ReasoningFields } from "./ReasoningFields.tsx";
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
  readonly stateContent?: ReactNode;
  readonly toolbar?: {
    readonly leading?: ReactNode;
    readonly actions?: ReactNode;
  };
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
  readonly inspectorError: string | null;
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
  readonly setInspectorError: Dispatch<SetStateAction<string | null>>;
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

function lastUsedTag(value: string | null | undefined) {
  if (!value) return [{ label: "Never used" }];
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return [];
  const days = Math.max(0, Math.floor((Date.now() - timestamp) / 86_400_000));
  return [
    {
      label:
        days === 0
          ? "Today"
          : days === 1
            ? "1 day ago"
            : `${String(days)} days ago`,
      description: `Last used on ${new Date(timestamp).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })} (${value})`,
    },
  ];
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
    reasoning_strategy: (formValue(form, "reasoning_strategy") ||
      "auto") as ReasoningStrategy,
    default_reasoning_level: (formValue(form, "default_reasoning_level") ||
      null) as ReasoningLevel | null,
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

function providerValue(form: FormData, model?: Model): ProviderModelWrite {
  const reasoningLevels = ["none", "low", "medium", "high"] as const;
  const mappedValues = reasoningLevels.map((level) => ({
    level,
    provider_value: formValue(form, `reasoning_mapping_${level}`),
  }));
  const needsMappings =
    (formValue(form, "capabilities") === ""
      ? model?.capabilities
      : commaValues(formValue(form, "capabilities"))
    )?.includes("reasoning") === true ||
    mappedValues.some((item) => item.provider_value !== "");
  const reasoning_mappings = needsMappings
    ? mappedValues.map((item) => ({
        ...item,
        provider_value: item.provider_value || item.level,
      }))
    : [];
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
    reasoning_strategy: (formValue(form, "reasoning_strategy") ||
      null) as ReasoningStrategy | null,
    default_reasoning_level: (formValue(form, "default_reasoning_level") ||
      null) as ReasoningLevel | null,
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
    <GraphInspectorFacts>
      {entries.map(([label, value]) => (
        <GraphInspectorFact key={label} label={label} value={value} />
      ))}
    </GraphInspectorFacts>
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
      <InlineAlert role="status" title="Loading configuration">
        Wait while the Router reads the global catalog.
      </InlineAlert>
    );
  if (phase === "error")
    return (
      <InlineAlert
        tone="error"
        actions={<Button onClick={onRetry}>Retry</Button>}
        title="Configuration unavailable"
      >
        Existing confirmed records remain unchanged. Try the read again.
      </InlineAlert>
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
      variant="primary"
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
  const [capabilityFilter, setCapabilityFilter] = useState<
    keyof typeof configurationCapabilities | null
  >(null);
  const [inspectorError, setInspectorError] = useState<string | null>(null);
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
  function reportInspectorError(message: string) {
    setInspectorError(message);
    onNotice("error", message);
  }

  function applyInspectorTransition(transition: InspectorTransition) {
    setInspectorError(null);
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

  function toggleCapabilityFilter(
    capability: keyof typeof configurationCapabilities,
  ) {
    if (pendingRef.current || assignmentDirty) return;
    setCapabilityFilter((current) =>
      current === capability ? null : capability,
    );
    setSelectedNodeId(null);
  }

  function capabilityTags(model: Model | ProviderModel) {
    return modelCapabilityKeys(model).map((capability) => ({
      ...configurationCapabilities[capability],
      description: `Filter by ${requirementLabels[capability]}`,
      pressed: capabilityFilter === capability,
      onClick: () => {
        toggleCapabilityFilter(capability);
      },
    }));
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
      countLabel: String(visibleProviders.length),
      actions: (
        <span className="configuration-column-actions">
          <IconButton
            aria-label="Add provider"
            title="Add provider"
            icon={<Icon name="plus" />}
            disabled={pending}
            onClick={(event) => {
              openCreate("provider", event.currentTarget);
            }}
          />
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
          detail: adapterLabels[provider.adapter],
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
      countLabel: String(visibleModels.length),
      actions: (
        <span className="configuration-column-actions">
          <IconButton
            aria-label="Add canonical model"
            title="Add canonical model"
            icon={<Icon name="plus" />}
            disabled={pending}
            onClick={(event) => {
              openCreate("model", event.currentTarget);
            }}
          />
          <IconButton
            aria-label="Add provider route"
            title="Add provider route"
            icon={<Icon name="layers" />}
            disabled={pending}
            onClick={(event) => {
              openCreate("mapping", event.currentTarget);
            }}
          />
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
              tags: capabilityTags(model),
              searchText: [
                model.api_name,
                ...capabilityLabels,
                modelStateLabel,
              ],
              state: modelState,
              stateLabel: modelStateLabel,
              rowsLabel: "Provider routes",
              rowsEmptyState: "No provider routes.",
              ...(hasUnavailableProvider
                ? {
                    rowsActions: (
                      <ReferenceRetryAction
                        onRetry={onRefreshGlobal}
                        pending={pending}
                      />
                    ),
                  }
                : {}),
              rows: routes.map((route) => {
                const provider = providerByName.get(route.provider_api_name);
                const routeState = routeStates.get(route.api_name);
                if (routeState === undefined)
                  throw new Error(`Missing state for ${route.api_name}.`);
                const routeCapabilities = modelCapabilityLabels(route);
                return {
                  id: configurationNodeId.mapping(route.api_name),
                  label: route.provider_model_name,
                  tags:
                    modelCapabilityKeys(route).length !==
                    modelCapabilityKeys(model).length
                      ? capabilityTags(route)
                      : undefined,
                  inlineDetail:
                    provider?.display_name ??
                    `Unavailable provider: ${route.provider_api_name}`,
                  content: routeState.content,
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
              : String(visibleAssignments.length),
      actions: (
        <span className="configuration-column-actions">
          <IconButton
            aria-label="Add assignment"
            title="Add assignment"
            icon={<Icon name="plus" />}
            disabled={
              pending || selectedService === "" || assignmentPhase === "loading"
            }
            onClick={(event) => {
              openCreate("assignment", event.currentTarget);
            }}
          />
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
          (!assignmentByName.has(assignment.inherits_assignment_api_name) ||
            assignmentChainSource(assignment, assignmentByName).api_name ===
              assignment.api_name);
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
          tags: [
            ...lastUsedTag(assignment.last_used_at),
            ...(assignment.inherits_assignment_api_name
              ? [
                  {
                    label: `↳ ${assignmentByName.get(assignment.inherits_assignment_api_name)?.display_name ?? assignment.inherits_assignment_api_name}`,
                    description: inheritanceLabel ?? "Inherited assignment",
                  },
                ]
              : !local
                ? [
                    {
                      label: `↳ ${serviceByName.get(assignment.defined_by_service_api_name ?? "")?.display_name ?? "Root"}`,
                      description: sourceLabel,
                    },
                  ]
                : []),
          ],
          content: [
            sourceUnavailable ? sourceLabel : null,
            inheritedAssignmentUnavailable ? inheritanceLabel : null,
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
              label:
                routeModel?.display_name ??
                `Unavailable route: ${candidate.provider_model_api_name}`,
              inlineDetail: provider?.display_name ?? "Unavailable provider",
              content: [
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
              stateLabel: routeState?.stateLabel ?? "Unavailable",
            };
          }),
        };
      }),
    },
  ];

  const assignmentNodeById = new Map(
    columns[2].nodes.map((node) => [node.id, node]),
  );
  const groupedAssignments: RelationshipGraphColumn = {
    ...columns[2],
    nodes: projection.assignmentGroups.flatMap(({ sourceId, inheritedIds }) => {
      const source = assignmentNodeById.get(sourceId);
      if (!source || !("rows" in source)) return [];
      return [
        {
          ...source,
          relatedRowsLabel: "Inherited assignments",
          relatedRows: inheritedIds.flatMap((id) => {
            const child = assignmentNodeById.get(id);
            const record = assignmentByName.get(id.slice("assignment:".length));
            if (!child || !record) return [];
            const mismatch = record.effective_chain.some((candidate) => {
              const route = mappingByName.get(
                candidate.provider_model_api_name,
              );
              return (
                route &&
                record.observed_requirements.some(
                  (requirement) => !routeMeetsRequirement(route, requirement),
                )
              );
            });
            return [
              {
                id: child.id,
                label: child.label,
                tags: [
                  ...lastUsedTag(record.last_used_at),
                  ...(record.defined_by_service_api_name !== selectedService
                    ? [
                        {
                          label: `↳ ${serviceByName.get(record.defined_by_service_api_name ?? "")?.display_name ?? "Root"}`,
                          description: assignmentSourceLabel(
                            record,
                            selectedService,
                            serviceByName,
                          ),
                        },
                      ]
                    : []),
                ],
                state: child.state,
                stateLabel: child.stateLabel,
                content: mismatch ? (
                  <>
                    {child.content}
                    {child.content ? " · " : null}Does not meet observed
                    requirements
                  </>
                ) : (
                  child.content
                ),
                searchText: child.searchText,
                pathSourceId: sourceId,
              },
            ];
          }),
        },
      ];
    }),
  };

  const capabilityNodeIds =
    capabilityFilter === null ? null : new Set<string>();
  if (capabilityNodeIds && capabilityFilter) {
    for (const model of visibleModels)
      if (modelHasCapability(model, capabilityFilter))
        capabilityNodeIds.add(configurationNodeId.model(model.api_name));
    for (const route of visibleMappings) {
      if (!modelHasCapability(route, capabilityFilter)) continue;
      capabilityNodeIds.add(configurationNodeId.mapping(route.api_name));
      capabilityNodeIds.add(configurationNodeId.model(route.model_api_name));
      capabilityNodeIds.add(
        configurationNodeId.provider(route.provider_api_name),
      );
    }
    for (const assignment of visibleAssignments) {
      assignment.effective_chain.forEach((candidate, index) => {
        if (
          !capabilityNodeIds.has(
            configurationNodeId.mapping(candidate.provider_model_api_name),
          )
        )
          return;
        capabilityNodeIds.add(
          configurationNodeId.assignment(assignment.api_name),
        );
        capabilityNodeIds.add(
          configurationNodeId.rung(assignment.api_name, index + 1),
        );
      });
    }
  }

  function filterCapabilities(
    column: RelationshipGraphColumn,
  ): RelationshipGraphColumn {
    if (!capabilityNodeIds) return column;
    const nodes: RelationshipGraphColumn["nodes"] = column.nodes.flatMap(
      (node) => {
        if (!("rows" in node))
          return capabilityNodeIds.has(node.id) ? [node] : [];
        const rows = node.rows.filter((row) => capabilityNodeIds.has(row.id));
        const relatedRows = node.relatedRows?.filter((row) =>
          capabilityNodeIds.has(row.id),
        );
        return capabilityNodeIds.has(node.id) ||
          rows.length ||
          relatedRows?.length
          ? [{ ...node, rows, relatedRows }]
          : [];
      },
    );
    return {
      ...column,
      nodes,
      countLabel: String(
        nodes.length +
          nodes.reduce(
            (count, node) =>
              count + ("rows" in node ? (node.relatedRows?.length ?? 0) : 0),
            0,
          ),
      ),
    };
  }

  function withEditActions(
    column: RelationshipGraphColumn,
  ): RelationshipGraphColumn {
    return {
      ...column,
      nodes: column.nodes.map((node) => {
        function withEdit(item: typeof node, group?: typeof node): typeof node {
          const identity = parseConfigurationNodeId(item.id);
          if (identity === null) return item;
          const label =
            identity.kind === "rung"
              ? `Edit ${group?.label ?? identity.apiName} route ${String(identity.position)}`
              : `Edit ${identity.kind === "mapping" ? "provider route" : identity.kind} ${item.label}`;
          return {
            ...item,
            actions: (
              <ConfigurationEditControl
                label={label}
                context={{
                  column,
                  node: item,
                  ...(group && "rows" in group ? { group } : {}),
                }}
                pending={pending}
                selected={selectedNodeId === item.id}
                onEdit={activate}
              />
            ),
          };
        }
        return "rows" in node
          ? {
              ...withEdit(node),
              rows: node.rows.map((row) => withEdit(row, node)),
              relatedRows: node.relatedRows?.map((row) => withEdit(row, node)),
            }
          : withEdit(node);
      }),
    };
  }
  const editableColumns: typeof columns = [
    withEditActions(filterCapabilities(columns[0])),
    withEditActions(filterCapabilities(columns[1])),
    withEditActions(filterCapabilities(groupedAssignments)),
  ];

  const relationships = projection.relationships.flatMap((relationship) => {
    if (
      capabilityNodeIds &&
      (!capabilityNodeIds.has(relationship.sourceId) ||
        !capabilityNodeIds.has(relationship.targetId))
    )
      return [];
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
    setInspectorError(null);
    const form = new FormData(event.currentTarget);
    const adapter = formValue(form, "adapter") as ProviderAdapter;
    const policy = adapterFieldPolicy[adapter];
    const endpoint = formValue(form, "endpoint");
    const credential = formValue(form, "credential_api_name");
    if (policy.endpoint === "required" && endpoint === "") {
      reportInspectorError("Enter the required custom endpoint.");
      return;
    }
    if (policy.credential === "required" && credential === "") {
      reportInspectorError(
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
      reportInspectorError(errorMessage(error));
    } finally {
      finishPending();
    }
  }

  async function saveCredential(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setInspectorError(null);
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
      reportInspectorError(errorMessage(error));
    } finally {
      finishPending();
    }
  }

  async function saveModel(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setInspectorError(null);
    let value: ModelWrite;
    try {
      value = modelValue(new FormData(event.currentTarget));
    } catch (error) {
      reportInspectorError(
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
      reportInspectorError(errorMessage(error));
    } finally {
      finishPending();
    }
  }

  async function saveMapping(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setInspectorError(null);
    let value: ProviderModelWrite;
    try {
      const form = new FormData(event.currentTarget);
      value = providerValue(
        form,
        modelByName.get(formValue(form, "model_api_name")),
      );
    } catch (error) {
      reportInspectorError(
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
      reportInspectorError(errorMessage(error));
    } finally {
      finishPending();
    }
  }

  async function saveAssignment(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setInspectorError(null);
    if (selectedService === "") return;
    const form = new FormData(event.currentTarget);
    const name = formValue(form, "api_name");
    const mode = formValue(form, "definition_kind");
    const chain = chainRows.map((row) => row.draft.providerModel);
    const validation =
      mode === "direct" ? validateAssignmentChain(chain) : null;
    if (validation !== null) {
      reportInspectorError(validation);
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
      setSelectedNodeId(`assignment:${name}`);
      setInspector(null);
    } catch (error) {
      reportInspectorError(errorMessage(error));
    } finally {
      finishPending();
    }
  }

  async function previewOpenRouter(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setInspectorError(null);
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
      reportInspectorError(errorMessage(error));
    } finally {
      finishPending();
    }
  }

  async function confirmOpenRouter() {
    if (importPreview === null) return;
    setInspectorError(null);
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
      reportInspectorError(errorMessage(error));
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
          inspectorError,
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
          setInspectorError,
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
    auxiliaryInspector,
    capabilityFilterControl:
      capabilityFilter === null ? null : (
        <>
          <CapabilityTag
            {...configurationCapabilities[capabilityFilter]}
            description={`Clear ${requirementLabels[capabilityFilter]} filter`}
            pressed
            onClick={() => {
              toggleCapabilityFilter(capabilityFilter);
            }}
          />
          <IconButton
            aria-label="Clear capability filter"
            title="Clear capability filter"
            icon={<Icon name="close" />}
            onClick={() => {
              toggleCapabilityFilter(capabilityFilter);
            }}
          />
        </>
      ),
    columns: editableColumns,
    deleteRecord,
    deleteTarget,
    globalPhase,
    emptyCatalogState:
      capabilityFilter === null
        ? emptyCatalogState
        : `No configuration matches ${requirementLabels[capabilityFilter]}.`,
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
      if (pendingRef.current) return;
      requestInspectorTransition({
        inspector: null,
        selectedNodeId: null,
        trigger: null,
      });
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
    auxiliaryInspector,
    cancelDeleteTarget,
    capabilityFilterControl,
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
      <RelationshipGraph
        aria-label="Configuration graph workspace"
        viewportLabel="LLM configuration relationships"
        fullPage
        compact
        filterToSelection
        viewportContent={
          props.stateContent !== undefined ||
          globalPhase === "partial" ||
          (globalPhase === "loading" && hasSafeRecords) ? (
            <>
              {props.stateContent}
              {globalPhase === "loading" && hasSafeRecords ? graphState : null}
              {globalPhase === "partial" ? (
                <InlineAlert role="status" title="Partial configuration graph">
                  The Router returned a bounded subset. More global records are
                  available. This graph does not claim to be complete.
                </InlineAlert>
              ) : null}
            </>
          ) : undefined
        }
        columns={
          globalPhase === "loading" && !hasSafeRecords
            ? [
                { ...columns[0], nodes: [] },
                { ...columns[1], nodes: [] },
                { ...columns[2], nodes: [] },
              ]
            : columns
        }
        emptyState={
          (globalPhase === "loading" || globalPhase === "error") &&
          !hasSafeRecords
            ? graphState
            : emptyCatalogState
        }
        noResultsDescription="Change the search or restore the complete configuration board."
        noResultsTitle="No configuration matches this search."
        onSelectionChange={onSelectionChange}
        relationships={relationships}
        partialNoResultsDescription="Load more records or change the search to continue."
        partialNoResultsTitle="No matches in loaded records."
        toolbar={{
          leading: props.toolbar?.leading,
          actions: (
            <>
              {capabilityFilterControl}
              {props.toolbar?.actions}
            </>
          ),
        }}
        searchLabel="Search configuration"
        selectedNodeId={selectedNodeId}
      />
      {auxiliaryInspector ?? selectedNodeInspector}
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
        {...(deleteTarget?.kind === "credential-replace"
          ? { impactStatement: deleteTarget.impact }
          : {})}
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

function InspectorWriteError({ message }: { readonly message: string | null }) {
  const errorRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (message !== null) errorRef.current?.focus();
  }, [message]);
  return message === null ? null : (
    <div ref={errorRef} tabIndex={-1}>
      <GraphInspectorNotice dynamic tone="error">
        {message}
      </GraphInspectorNotice>
    </div>
  );
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- This inspector keeps one provider form, its write-only credential form, and the shared pending boundary together.
function ProviderInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  const {
    credentials,
    inspector,
    inspectorError,
    closeInspector,
    returnFocusRef,
    saveProvider,
    pending,
    saveCredential,
    setDeleteTarget,
    setInspector,
    setInspectorError,
    providerByName,
  } = context;
  const provider =
    inspector.apiName === null
      ? undefined
      : providerByName.get(inspector.apiName);
  const adapter = provider?.adapter ?? "openai";
  const [fields, updateFields] = useReducer(
    (
      current: {
        readonly selectedAdapter: ProviderAdapter;
        readonly selectedCredential: string;
        readonly selectedEndpoint: string;
        readonly selectedEnabled: boolean;
        readonly apiName: string;
        readonly displayName: string;
        readonly credentialApiName: string;
      },
      patch: Partial<typeof current>,
    ) => ({ ...current, ...patch }),
    {
      selectedAdapter: adapter,
      selectedCredential: provider?.credential_api_name ?? "",
      selectedEndpoint: provider?.endpoint ?? "",
      selectedEnabled: provider?.enabled ?? true,
      apiName: provider?.api_name ?? "",
      displayName: provider?.display_name ?? "",
      credentialApiName: "",
    },
  );
  const {
    selectedAdapter,
    selectedCredential,
    selectedEndpoint,
    selectedEnabled,
    apiName,
    displayName,
    credentialApiName,
  } = fields;
  const fieldPolicy = adapterFieldPolicy[selectedAdapter];
  const boardState =
    provider === undefined
      ? undefined
      : providerBoardState(
          provider,
          new Set(credentials.map((item) => item.api_name)),
        );
  const actions =
    provider === undefined ? undefined : (
      <>
        <IconButton
          aria-label="Add route"
          title="Add route"
          icon={<Icon name="plus" />}
          disabled={pending}
          onClick={() => {
            setInspectorError(null);
            setInspector({
              kind: "mapping",
              apiName: null,
              providerApiName: provider.api_name,
            });
          }}
        />
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
      </>
    );
  return (
    <Dialog
      className="configuration-edit-dialog"
      open
      appearance="form"
      closeDisabled={context.pending}
      key={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      actions={
        <FormActions secondaryActions={actions}>
          <Button disabled={pending} onClick={closeInspector} variant="quiet">
            Cancel
          </Button>
          <Button
            disabled={pending}
            form="configuration-provider-form"
            type="submit"
          >
            {pending ? "Saving…" : "Save provider"}
          </Button>
        </FormActions>
      }
      eyebrow="Global provider connection"
      onClose={closeInspector}
      returnFocusRef={returnFocusRef}
      title={provider?.display_name ?? "Add provider"}
    >
      <InspectorWriteError message={inspectorError} />
      <form
        className="configuration-form"
        id="configuration-provider-form"
        onSubmit={(event) => void saveProvider(event)}
      >
        <FormControls disabled={pending}>
          <FormGrid>
            <TextControl
              label="API name"
              data-dialog-initial-focus={
                provider === undefined ? "" : undefined
              }
              name="api_name"
              onChange={(event) => {
                updateFields({ apiName: event.currentTarget.value });
              }}
              readOnly={provider !== undefined}
              requirement="required"
              value={apiName}
            />
            <TextControl
              label="Display name"
              data-dialog-initial-focus={
                provider !== undefined ? "" : undefined
              }
              name="display_name"
              onChange={(event) => {
                updateFields({ displayName: event.currentTarget.value });
              }}
              requirement="required"
              value={displayName}
            />
          </FormGrid>
          <SelectControl
            label="Adapter"
            name="adapter"
            onChange={(event) => {
              const next = event.currentTarget.value as ProviderAdapter;
              updateFields({
                selectedAdapter: next,
                selectedCredential: "",
                selectedEndpoint: "",
              });
            }}
            value={selectedAdapter}
          >
            {providerAdapters.map((item) => (
              <option key={item} value={item}>
                {adapterLabels[item]}
              </option>
            ))}
          </SelectControl>
          {fieldPolicy.credential === "none" ? null : (
            <SelectControl
              label="Applicable credential"
              name="credential_api_name"
              onChange={(event) => {
                updateFields({ selectedCredential: event.currentTarget.value });
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
                <option key={item.api_name} value={item.api_name}>
                  {item.api_name}
                </option>
              ))}
            </SelectControl>
          )}
          {fieldPolicy.endpoint === "required" ? (
            <FormField label="Custom endpoint" requirement="required">
              <input
                className="od-form-control od-text-control-input"
                name="endpoint"
                onChange={(event) => {
                  updateFields({ selectedEndpoint: event.currentTarget.value });
                }}
                placeholder="https://provider.example/v1"
                required
                type="url"
                value={selectedEndpoint}
              />
            </FormField>
          ) : null}
          <SwitchControl
            checked={selectedEnabled}
            label="Enabled after validation"
            name="enabled"
            onChange={(event) => {
              updateFields({ selectedEnabled: event.currentTarget.checked });
            }}
          />
          <AdvancedFieldsDisclosure summary="Review changes">
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
          </AdvancedFieldsDisclosure>
        </FormControls>
      </form>
      {provider === undefined ? null : (
        <AdvancedFieldsDisclosure summary="Current provider details">
          {recordFacts([
            ["Provider ID", provider.api_name],
            ["Adapter", adapterLabels[provider.adapter]],
            ["Endpoint", provider.endpoint ?? "Router standard endpoint"],
            ["Credential", provider.credential_api_name ?? "None"],
            ["State", boardState?.stateLabel ?? "Unavailable"],
            ...(boardState?.content === undefined
              ? []
              : [["Corrective action", boardState.content] as const]),
          ])}
        </AdvancedFieldsDisclosure>
      )}
      <AdvancedFieldsDisclosure summary="Manage credentials">
        <form
          className="configuration-form"
          onReset={() => {
            updateFields({ credentialApiName: "" });
          }}
          onSubmit={(event) => void saveCredential(event)}
        >
          <FormControls disabled={pending}>
            <TextControl
              autoComplete="off"
              label="Credential API name"
              name="credential_api_name"
              onChange={(event) => {
                updateFields({ credentialApiName: event.currentTarget.value });
              }}
              requirement="required"
              value={credentialApiName}
            />
            <FormField label="New secret" requirement="required">
              <input
                autoComplete="new-password"
                className="od-form-control od-text-control-input"
                name="secret"
                required
                type="password"
              />
            </FormField>
            <Button disabled={pending} type="submit">
              Save credential
            </Button>
          </FormControls>
        </form>
        <GraphInspectorRows>
          {credentials.map((credential) => (
            <GraphInspectorRow
              actions={
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
              }
              key={credential.api_name}
              label={credential.api_name}
              value={`Fingerprint ${credential.fingerprint}`}
            />
          ))}
        </GraphInspectorRows>
      </AdvancedFieldsDisclosure>
    </Dialog>
  );
}

function ModelInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  const {
    inspector,
    inspectorError,
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
    setInspectorError,
    setSelectedNodeId,
  } = context;
  const model =
    inspector.apiName === null ? undefined : modelByName.get(inspector.apiName);
  const [modelFields, updateModelFields] = useReducer(
    (
      current: {
        readonly apiName: string;
        readonly displayName: string;
        readonly inputModalities: string;
        readonly outputModalities: string;
        readonly capabilities: string;
      },
      patch: Partial<typeof current>,
    ) => ({ ...current, ...patch }),
    {
      apiName: model?.api_name ?? "",
      displayName: model?.display_name ?? "",
      inputModalities: model?.input_modalities.join(", ") ?? "text",
      outputModalities: model?.output_modalities.join(", ") ?? "text",
      capabilities: model?.capabilities.join(", ") ?? "",
    },
  );
  const {
    apiName,
    displayName,
    inputModalities,
    outputModalities,
    capabilities,
  } = modelFields;
  const applicableMappings = providerModels.filter(
    (item) => item.model_api_name === model?.api_name,
  );
  const actions =
    model === undefined ? undefined : (
      <>
        <IconButton
          aria-label="Add route"
          title="Add route"
          icon={<Icon name="plus" />}
          disabled={pending}
          onClick={() => {
            setInspectorError(null);
            setInspector({
              kind: "mapping",
              apiName: null,
              modelApiName: model.api_name,
            });
          }}
        />
        <IconButton
          aria-label="Sync prices"
          title="Sync prices"
          icon={<Icon name="refresh" />}
          disabled={pending || applicableMappings.length === 0}
          onClick={() => {
            setInspectorError(null);
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
                const message =
                  failures === 0
                    ? "Applicable mapping prices were synchronized."
                    : `${String(failures)} applicable mapping price synchronizations failed.`;
                if (failures !== 0) setInspectorError(message);
                onNotice(failures === 0 ? "success" : "error", message);
              })
              .catch((error: unknown) => {
                const message = errorMessage(error);
                setInspectorError(message);
                onNotice("error", message);
              })
              .finally(() => {
                finishPending();
              });
          }}
        />
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
      </>
    );
  return (
    <Dialog
      className="configuration-edit-dialog"
      open
      appearance="form"
      closeDisabled={context.pending}
      key={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      actions={
        <FormActions secondaryActions={actions}>
          <Button disabled={pending} onClick={closeInspector} variant="quiet">
            Cancel
          </Button>
          <Button
            disabled={pending}
            form="configuration-model-form"
            type="submit"
          >
            {pending ? "Saving…" : "Save model"}
          </Button>
        </FormActions>
      }
      eyebrow="Model"
      onClose={closeInspector}
      returnFocusRef={returnFocusRef}
      title={model?.display_name ?? "Add canonical model"}
    >
      <InspectorWriteError message={inspectorError} />
      <form
        className="configuration-form"
        id="configuration-model-form"
        onSubmit={(event) => void saveModel(event)}
      >
        <FormControls disabled={pending}>
          <FormGrid>
            <TextControl
              label="API name"
              data-dialog-initial-focus={model === undefined ? "" : undefined}
              name="api_name"
              onChange={(event) => {
                updateModelFields({ apiName: event.currentTarget.value });
              }}
              readOnly={model !== undefined}
              requirement="required"
              value={apiName}
            />
            <TextControl
              label="Display name"
              data-dialog-initial-focus={model !== undefined ? "" : undefined}
              name="display_name"
              onChange={(event) => {
                updateModelFields({ displayName: event.currentTarget.value });
              }}
              requirement="required"
              value={displayName}
            />
          </FormGrid>
          <ConfigurationCapabilityFields
            inputModalities={inputModalities}
            outputModalities={outputModalities}
            capabilities={capabilities}
            onChange={updateModelFields}
            requiredModalities
          />
          <ReasoningFields model={model} />
          <ModelAdvancedFields model={model} />
        </FormControls>
      </form>
      {model === undefined ? null : (
        <AdvancedFieldsDisclosure summary="Current model details">
          {recordFacts([
            ["Model ID", model.api_name],
            ["Capabilities", modelCapabilityLabels(model).join(", ") || "None"],
            ["Price source", model.price_source ?? "Manual"],
          ])}
        </AdvancedFieldsDisclosure>
      )}
      {model !== undefined ? null : (
        <AdvancedFieldsDisclosure summary="Import from OpenRouter">
          <form
            className="configuration-form"
            onSubmit={(event) => void previewOpenRouter(event)}
          >
            <FormControls disabled={pending}>
              <TextControl
                label="Exact model ID or supported OpenRouter URL"
                maxLength={512}
                onChange={(event) => {
                  setImportInput(event.currentTarget.value);
                  setImportPreview(null);
                  setSelectedImportProviders(new Set());
                }}
                requirement="required"
                value={importInput}
              />
              <Button disabled={pending} type="submit">
                Preview OpenRouter model
              </Button>
            </FormControls>
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
        </AdvancedFieldsDisclosure>
      )}
    </Dialog>
  );
}

function ConfigurationCapabilityFields({
  inputModalities,
  outputModalities,
  capabilities,
  onChange,
  requiredModalities = false,
}: {
  readonly inputModalities: string;
  readonly outputModalities: string;
  readonly capabilities: string;
  readonly onChange: (
    patch: Partial<{
      inputModalities: string;
      outputModalities: string;
      capabilities: string;
    }>,
  ) => void;
  readonly requiredModalities?: boolean;
}) {
  const groups = [
    {
      key: "inputModalities",
      name: "input_modalities",
      label: "Input",
      value: inputModalities,
      options: [
        ["text", "Text"],
        ["image", "Image"],
      ],
    },
    {
      key: "outputModalities",
      name: "output_modalities",
      label: "Output",
      value: outputModalities,
      options: [
        ["text", "Text"],
        ["structured_json", "Structured JSON"],
        ["embedding", "Embedding"],
        ["image", "Image"],
        ["video", "Video"],
        ["audio", "Audio"],
      ],
    },
    {
      key: "capabilities",
      name: "capabilities",
      label: "Capabilities",
      value: capabilities,
      options: [
        ["tool_calling", "Tool calling"],
        ["streaming", "Streaming"],
        ["reasoning", "Reasoning"],
      ],
    },
  ] as const;
  return (
    <>
      {groups.map((group) => {
        const selected = commaValues(group.value);
        return (
          <FormSection
            columns={2}
            key={group.key}
            legend={group.label}
            variant="plain"
          >
            <input name={group.name} type="hidden" value={group.value} />
            {group.options.map(([value, label], index) => (
              <CheckboxControl
                checked={selected.includes(value)}
                key={value}
                label={label}
                required={
                  requiredModalities &&
                  group.key !== "capabilities" &&
                  selected.length === 0 &&
                  index === 0
                }
                onChange={(event) => {
                  const next = event.currentTarget.checked
                    ? [...selected, value]
                    : selected.filter((item) => item !== value);
                  onChange({ [group.key]: next.join(", ") });
                }}
              />
            ))}
          </FormSection>
        );
      })}
    </>
  );
}

interface ModelAdvancedValues {
  readonly maxContextTokens: number | "";
  readonly maxOutputTokens: number | "";
  readonly embeddingDimensions: string;
  readonly maxInputImages: number | "";
  readonly maxInputImageBytes: number | "";
  readonly maxOutputDurationSeconds: number | "";
  readonly priceSource: string;
  readonly priceLookupKey: string;
  readonly currency: string;
  readonly unitPrices: string;
}

function ModelAdvancedFields({ model }: { readonly model: Model | undefined }) {
  const [values, setValues] = useState<ModelAdvancedValues>({
    maxContextTokens: model?.constraints?.max_context_tokens ?? "",
    maxOutputTokens: model?.constraints?.max_output_tokens ?? "",
    embeddingDimensions:
      model?.constraints?.embedding_dimensions?.join(", ") ?? "",
    maxInputImages: model?.constraints?.max_input_images ?? "",
    maxInputImageBytes: model?.constraints?.max_input_image_bytes ?? "",
    maxOutputDurationSeconds:
      model?.constraints?.max_output_duration_seconds ?? "",
    priceSource: model?.price_source ?? "",
    priceLookupKey: model?.price_lookup_key ?? "",
    currency:
      model?.current_price?.source == null
        ? (model?.current_price?.currency ?? "")
        : "",
    unitPrices:
      model?.current_price?.source == null
        ? (model?.current_price?.unit_prices
            .map((item) => `${item.unit}=${item.amount}`)
            .join(", ") ?? "")
        : "",
  });
  const setTextValue = (
    key: keyof ModelAdvancedValues,
    value: string | number,
  ) => {
    setValues((current) => ({ ...current, [key]: value }));
  };
  return (
    <AdvancedFieldsDisclosure summary="Constraints and price source">
      <FormGrid>
        <NumberControl
          label="Maximum context tokens"
          min={1}
          name="max_context_tokens"
          onChange={(event) => {
            setTextValue(
              "maxContextTokens",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxContextTokens}
        />
        <NumberControl
          label="Maximum output tokens"
          min={1}
          name="max_output_tokens"
          onChange={(event) => {
            setTextValue(
              "maxOutputTokens",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxOutputTokens}
        />
        <TextControl
          label="Embedding dimensions"
          name="embedding_dimensions"
          onChange={(event) => {
            setTextValue("embeddingDimensions", event.currentTarget.value);
          }}
          placeholder="768, 1536"
          value={values.embeddingDimensions}
        />
        <NumberControl
          label="Maximum input images"
          min={1}
          name="max_input_images"
          onChange={(event) => {
            setTextValue(
              "maxInputImages",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxInputImages}
        />
        <NumberControl
          label="Maximum input image bytes"
          min={1}
          name="max_input_image_bytes"
          onChange={(event) => {
            setTextValue(
              "maxInputImageBytes",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxInputImageBytes}
        />
        <NumberControl
          label="Maximum output duration seconds"
          min={1}
          name="max_output_duration_seconds"
          onChange={(event) => {
            setTextValue(
              "maxOutputDurationSeconds",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxOutputDurationSeconds}
        />
        <TextControl
          label="Price source"
          name="price_source"
          onChange={(event) => {
            setTextValue("priceSource", event.currentTarget.value);
          }}
          value={values.priceSource}
        />
        <TextControl
          label="Source model identifier"
          name="price_lookup_key"
          onChange={(event) => {
            setTextValue("priceLookupKey", event.currentTarget.value);
          }}
          value={values.priceLookupKey}
        />
        <TextControl
          label="Manual price currency"
          maxLength={3}
          name="currency"
          onChange={(event) => {
            setTextValue("currency", event.currentTarget.value);
          }}
          placeholder="USD"
          value={values.currency}
        />
        <TextareaControl
          label="Manual typed unit prices"
          name="unit_prices"
          onChange={(event) => {
            setTextValue("unitPrices", event.currentTarget.value);
          }}
          placeholder="input_token=0.001, output_token=0.002"
          rows={3}
          value={values.unitPrices}
        />
      </FormGrid>
    </AdvancedFieldsDisclosure>
  );
}

function MappingInspectorActions({
  context,
  mapping,
}: {
  readonly context: ConfigurationInspectorContext;
  readonly mapping: ProviderModel;
}) {
  return (
    <Button
      disabled={context.pending}
      variant="quiet"
      onClick={() => {
        context.setDeleteTarget({
          kind: "mapping",
          apiName: mapping.api_name,
          impact: `delete provider-model mapping ${mapping.api_name}`,
        });
      }}
    >
      Delete Provider-Model
    </Button>
  );
}

function MappingInspector({
  context,
}: {
  readonly context: ConfigurationInspectorContext;
}) {
  const {
    inspector,
    inspectorError,
    closeInspector,
    credentials,
    returnFocusRef,
    saveMapping,
    providers,
    models,
    pending,
    mappingByName,
  } = context;
  const mapping =
    inspector.apiName === null
      ? undefined
      : mappingByName.get(inspector.apiName);
  const [mappingFields, updateMappingFields] = useReducer(
    (
      current: {
        readonly apiName: string;
        readonly providerApiName: string;
        readonly modelApiName: string;
        readonly providerModelName: string;
        readonly enabled: boolean;
      },
      patch: Partial<typeof current>,
    ) => ({ ...current, ...patch }),
    {
      apiName: mapping?.api_name ?? "",
      providerApiName: mapping?.provider_api_name ?? "",
      modelApiName: mapping?.model_api_name ?? "",
      providerModelName: mapping?.provider_model_name ?? "",
      enabled: mapping?.enabled ?? true,
    },
  );
  const { apiName, providerApiName, modelApiName, providerModelName, enabled } =
    mappingFields;
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
  const actions =
    mapping === undefined ? undefined : (
      <MappingInspectorActions context={context} mapping={mapping} />
    );
  const playgroundTarget =
    mapping === undefined
      ? null
      : mappingPlaygroundTarget(
          mapping.api_name,
          context.providerModels,
          providers,
          models,
        );
  return (
    <Dialog
      className="configuration-edit-dialog"
      open
      appearance="form"
      closeDisabled={context.pending}
      key={`${inspector.kind}:${inspector.apiName ?? "new"}`}
      actions={
        <FormActions secondaryActions={actions}>
          <Button disabled={pending} onClick={closeInspector} variant="quiet">
            Cancel
          </Button>
          <Button
            disabled={pending}
            form="configuration-mapping-form"
            type="submit"
          >
            {pending ? "Saving…" : "Save Provider-Model"}
          </Button>
        </FormActions>
      }
      eyebrow="Provider-Model"
      headerActions={
        playgroundTarget === null ? undefined : (
          <IconButton
            aria-label="Play route"
            title="Play route"
            icon={<Icon name="spark" />}
            disabled={pending}
            onClick={(event) => {
              context.openPlayground(playgroundTarget, event.currentTarget);
            }}
          />
        )
      }
      onClose={closeInspector}
      returnFocusRef={returnFocusRef}
      title={
        mapping === undefined
          ? "Add Provider-Model"
          : (mappingProvider?.display_name ??
            `Unavailable provider: ${mapping.provider_api_name}`)
      }
    >
      <InspectorWriteError message={inspectorError} />
      <form
        className="configuration-form"
        id="configuration-mapping-form"
        onSubmit={(event) => void saveMapping(event)}
      >
        <FormControls disabled={pending}>
          <FormGrid>
            {mapping === undefined ? (
              <TextControl
                label="API name"
                data-dialog-initial-focus=""
                name="api_name"
                onChange={(event) => {
                  updateMappingFields({ apiName: event.currentTarget.value });
                }}
                requirement="required"
                value={apiName}
              />
            ) : (
              <input name="api_name" type="hidden" value={apiName} readOnly />
            )}
            {mapping === undefined &&
            inspector.providerApiName !== undefined ? (
              <>
                <input
                  name="provider_api_name"
                  readOnly
                  type="hidden"
                  value={inspector.providerApiName}
                />
                <FormField label="Provider">
                  <input
                    className="od-form-control od-text-control-input"
                    readOnly
                    value={
                      providers.find(
                        (item) => item.api_name === inspector.providerApiName,
                      )?.display_name ?? inspector.providerApiName
                    }
                  />
                </FormField>
              </>
            ) : (
              <SelectControl
                label="Provider"
                name="provider_api_name"
                onChange={(event) => {
                  updateMappingFields({
                    providerApiName: event.currentTarget.value,
                  });
                }}
                requirement="required"
                value={providerApiName}
              >
                <option value="">Select provider</option>
                {providers.map((item) => (
                  <option key={item.api_name} value={item.api_name}>
                    {item.display_name}
                  </option>
                ))}
              </SelectControl>
            )}
            {mapping !== undefined ? (
              <input
                type="hidden"
                name="model_api_name"
                value={modelApiName}
                readOnly
              />
            ) : inspector.modelApiName !== undefined ? (
              <>
                <input
                  name="model_api_name"
                  readOnly
                  type="hidden"
                  value={inspector.modelApiName}
                />
                <FormField label="Model">
                  <input
                    className="od-form-control od-text-control-input"
                    readOnly
                    value={
                      models.find(
                        (item) => item.api_name === inspector.modelApiName,
                      )?.display_name ?? inspector.modelApiName
                    }
                  />
                </FormField>
              </>
            ) : (
              <SelectControl
                label="Model"
                name="model_api_name"
                onChange={(event) => {
                  updateMappingFields({
                    modelApiName: event.currentTarget.value,
                  });
                }}
                requirement="required"
                value={modelApiName}
              >
                <option value="">Select model</option>
                {models.map((item) => (
                  <option key={item.api_name} value={item.api_name}>
                    {item.display_name}
                  </option>
                ))}
              </SelectControl>
            )}
            <TextControl
              label="Model API name"
              data-dialog-initial-focus={mapping !== undefined ? "" : undefined}
              name="provider_model_name"
              onChange={(event) => {
                updateMappingFields({
                  providerModelName: event.currentTarget.value,
                });
              }}
              requirement="required"
              value={providerModelName}
            />
          </FormGrid>
          <SwitchControl
            checked={enabled}
            label="Enabled"
            name="enabled"
            onChange={(event) => {
              updateMappingFields({ enabled: event.currentTarget.checked });
            }}
          />
          <ReasoningFields mapping={mapping} route />
          <MappingAdvancedFields mapping={mapping} />
        </FormControls>
      </form>
      {mapping === undefined ? null : (
        <AdvancedFieldsDisclosure summary="Current route details">
          {recordFacts([
            ["Route ID", mapping.api_name],
            [
              "Provider",
              mappingProvider === undefined
                ? `Unavailable provider: ${mapping.provider_api_name}`
                : `${mappingProvider.display_name} (Provider ID: ${mappingProvider.api_name})`,
            ],
            [
              "Model",
              mappingModel === undefined
                ? `Unavailable model: ${mapping.model_api_name}`
                : `${mappingModel.display_name} (Model ID: ${mappingModel.api_name})`,
            ],
            ["Model API name", mapping.provider_model_name],
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
        </AdvancedFieldsDisclosure>
      )}
    </Dialog>
  );
}

function MappingAdvancedFields({
  mapping,
}: {
  readonly mapping: ProviderModel | undefined;
}) {
  const priceDefaults = providerModelPriceFormDefaults(mapping);
  const [values, setValues] = useState<
    ModelAdvancedValues & {
      readonly inputModalities: string;
      readonly outputModalities: string;
      readonly capabilities: string;
    }
  >({
    inputModalities: mapping?.input_modalities.join(", ") ?? "",
    outputModalities: mapping?.output_modalities.join(", ") ?? "",
    capabilities: mapping?.capabilities.join(", ") ?? "",
    maxContextTokens: mapping?.constraints?.max_context_tokens ?? "",
    maxOutputTokens: mapping?.constraints?.max_output_tokens ?? "",
    embeddingDimensions:
      mapping?.constraints?.embedding_dimensions?.join(", ") ?? "",
    maxInputImages: mapping?.constraints?.max_input_images ?? "",
    maxInputImageBytes: mapping?.constraints?.max_input_image_bytes ?? "",
    maxOutputDurationSeconds:
      mapping?.constraints?.max_output_duration_seconds ?? "",
    priceSource: priceDefaults.source,
    priceLookupKey: priceDefaults.lookupKey,
    currency: priceDefaults.currency,
    unitPrices: priceDefaults.unitPrices,
  });
  const setValue = (key: keyof typeof values, value: string | number) => {
    setValues((current) => ({ ...current, [key]: value }));
  };
  return (
    <AdvancedFieldsDisclosure summary="Capabilities, constraints, and price">
      <ConfigurationCapabilityFields
        inputModalities={values.inputModalities}
        outputModalities={values.outputModalities}
        capabilities={values.capabilities}
        onChange={(patch) => {
          setValues((current) => ({ ...current, ...patch }));
        }}
      />
      <FormGrid>
        <NumberControl
          label="Maximum context tokens"
          min={1}
          name="max_context_tokens"
          onChange={(event) => {
            setValue(
              "maxContextTokens",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxContextTokens}
        />
        <NumberControl
          label="Maximum output tokens"
          min={1}
          name="max_output_tokens"
          onChange={(event) => {
            setValue(
              "maxOutputTokens",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxOutputTokens}
        />
        <TextControl
          label="Embedding dimensions"
          name="embedding_dimensions"
          onChange={(event) => {
            setValue("embeddingDimensions", event.currentTarget.value);
          }}
          value={values.embeddingDimensions}
        />
        <NumberControl
          label="Maximum input images"
          min={1}
          name="max_input_images"
          onChange={(event) => {
            setValue(
              "maxInputImages",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxInputImages}
        />
        <NumberControl
          label="Maximum input image bytes"
          min={1}
          name="max_input_image_bytes"
          onChange={(event) => {
            setValue(
              "maxInputImageBytes",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxInputImageBytes}
        />
        <NumberControl
          label="Maximum output duration seconds"
          min={1}
          name="max_output_duration_seconds"
          onChange={(event) => {
            setValue(
              "maxOutputDurationSeconds",
              event.currentTarget.value === ""
                ? ""
                : event.currentTarget.valueAsNumber,
            );
          }}
          value={values.maxOutputDurationSeconds}
        />
        <TextControl
          label="Price source"
          name="price_source"
          onChange={(event) => {
            setValue("priceSource", event.currentTarget.value);
          }}
          value={values.priceSource}
        />
        <TextControl
          label="Source model identifier"
          name="price_lookup_key"
          onChange={(event) => {
            setValue("priceLookupKey", event.currentTarget.value);
          }}
          value={values.priceLookupKey}
        />
        <TextControl
          label="Manual price currency"
          maxLength={3}
          name="currency"
          onChange={(event) => {
            setValue("currency", event.currentTarget.value);
          }}
          placeholder="USD"
          value={values.currency}
        />
        <TextareaControl
          label="Manual typed unit prices"
          name="unit_prices"
          onChange={(event) => {
            setValue("unitPrices", event.currentTarget.value);
          }}
          placeholder="input_token=0.001, output_token=0.002"
          rows={3}
          value={values.unitPrices}
        />
      </FormGrid>
    </AdvancedFieldsDisclosure>
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
    inspectorError,
    assignmentByName,
    selectedService,
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
  const [assignmentFields, updateAssignmentFields] = useReducer(
    (
      current: {
        readonly definitionMode: string;
        readonly apiName: string;
        readonly displayName: string;
        readonly inheritedAssignmentName: string;
        readonly reasoningLevel: string;
      },
      patch: Partial<typeof current>,
    ) => ({ ...current, ...patch }),
    {
      definitionMode:
        assignment?.definition_kind === "inherited_assignment"
          ? "inherit"
          : "direct",
      apiName: assignment?.api_name ?? "",
      displayName: assignment?.display_name ?? "",
      inheritedAssignmentName:
        assignment?.inherits_assignment_api_name ?? "default",
      reasoningLevel: assignment?.reasoning_level ?? "",
    },
  );
  const {
    definitionMode,
    apiName,
    displayName,
    inheritedAssignmentName,
    reasoningLevel,
  } = assignmentFields;
  const [choosingInheritance, setChoosingInheritance] = useState(false);
  const [requirements, setRequirements] = useState<
    readonly ObservedRequirement[]
  >(assignment?.observed_requirements ?? []);
  const savedDirectRows = useRef(
    assignment?.definition_kind === "inherited_assignment" ? [] : chainRows,
  );
  async function changeRequirement(
    requirement: ObservedRequirement,
    add: boolean,
  ) {
    if (assignment === undefined || !context.beginPending(true)) return;
    context.setInspectorError(null);
    try {
      if (add)
        await context.client.addRequirement(
          selectedService,
          assignment.api_name,
          requirement,
          context.csrf,
        );
      else
        await context.client.removeRequirement(
          selectedService,
          assignment.api_name,
          requirement,
          context.csrf,
        );
      setRequirements((current) =>
        add
          ? [...current, requirement]
          : current.filter((item) => item !== requirement),
      );
      await context.onRefreshAssignments();
    } catch (error) {
      context.setInspectorError(errorMessage(error));
    } finally {
      context.finishPending();
    }
  }
  const hasActions =
    assignment !== undefined && (isLocal || playgroundTarget !== null);
  const actions =
    selectedService === "" || !hasActions ? undefined : (
      <>
        {isLocal ? (
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
      </>
    );
  return (
    <Dialog
      className="configuration-edit-dialog"
      open
      appearance="form"
      closeDisabled={context.pending}
      key={`${inspector.kind}:${inspector.apiName ?? "new"}:${String(inspector.rungPosition ?? "header")}`}
      actions={
        <FormActions
          secondaryActions={
            <>
              {actions}
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
            </>
          }
        >
          <Button disabled={pending} onClick={closeInspector} variant="quiet">
            Cancel
          </Button>
          <Button
            disabled={pending || selectedService === ""}
            form="configuration-assignment-form"
            type="submit"
          >
            {pending ? "Saving…" : "Save assignment"}
          </Button>
        </FormActions>
      }
      eyebrow={assignment?.api_name}
      headerActions={
        playgroundTarget === null ? undefined : (
          <IconButton
            aria-label="Play assignment"
            title="Play assignment"
            icon={<Icon name="spark" />}
            disabled={pending}
            onClick={(event) => {
              openPlayground(playgroundTarget, event.currentTarget);
            }}
          />
        )
      }
      onClose={closeInspector}
      returnFocusRef={returnFocusRef}
      title={assignment?.display_name ?? "Add assignment"}
    >
      <InspectorWriteError message={inspectorError} />
      {selectedService === "" ? (
        <StatePanel kind="empty" title="Service required">
          Select one service. Providers, models, mappings, credentials, and
          prices stay global.
        </StatePanel>
      ) : (
        <>
          <form
            className="configuration-form"
            id="configuration-assignment-form"
            onChange={markAssignmentDirty}
            onSubmit={(event) => void saveAssignment(event)}
          >
            <FormControls disabled={pending}>
              <FormGrid>
                {assignment === undefined ? (
                  <TextControl
                    label="Assignment API name"
                    data-dialog-initial-focus=""
                    name="api_name"
                    onChange={(event) => {
                      updateAssignmentFields({
                        apiName: event.currentTarget.value,
                      });
                    }}
                    requirement="required"
                    value={apiName}
                  />
                ) : (
                  <input
                    name="api_name"
                    type="hidden"
                    value={apiName}
                    readOnly
                  />
                )}
                <TextControl
                  label="Display name"
                  data-dialog-initial-focus={
                    assignment !== undefined ? "" : undefined
                  }
                  name="display_name"
                  onChange={(event) => {
                    updateAssignmentFields({
                      displayName: event.currentTarget.value,
                    });
                  }}
                  value={displayName}
                />
                <SelectControl
                  label="Reasoning level override"
                  name="reasoning_level"
                  onChange={(event) => {
                    updateAssignmentFields({
                      reasoningLevel: event.currentTarget.value,
                    });
                  }}
                  value={reasoningLevel}
                >
                  <option value="">Model default</option>
                  <option>none</option>
                  <option>low</option>
                  <option>medium</option>
                  <option>high</option>
                </SelectControl>
              </FormGrid>
              <input
                type="hidden"
                name="definition_kind"
                value={definitionMode}
                readOnly
              />
              <input
                type="hidden"
                name="inherits_assignment_api_name"
                value={inheritedAssignmentName}
                readOnly
              />
              <FormActions alignment="start" layout="wrap">
                {definitionMode === "inherit" ? (
                  <>
                    <span>
                      Inherits from{" "}
                      <strong>
                        {assignmentByName.get(inheritedAssignmentName)
                          ?.display_name ?? inheritedAssignmentName}
                      </strong>
                    </span>
                    <Button
                      variant="quiet"
                      onClick={() => {
                        setChoosingInheritance((current) => !current);
                      }}
                    >
                      Change
                    </Button>
                    <Button
                      variant="quiet"
                      onClick={() => {
                        updateAssignmentFields({ definitionMode: "direct" });
                        setChainRows(
                          savedDirectRows.current.length > 0
                            ? savedDirectRows.current
                            : orderChainRows(
                                (
                                  assignmentByName.get(inheritedAssignmentName)
                                    ?.effective_chain ?? []
                                ).map((item, index) => ({
                                  id: `inherited:${String(index)}`,
                                  label: assignmentPositionLabel(index + 1),
                                  draft: {
                                    providerModel: item.provider_model_api_name,
                                  },
                                })),
                              ),
                        );
                        setChoosingInheritance(false);
                        markAssignmentDirty();
                      }}
                    >
                      Stop inheriting
                    </Button>
                  </>
                ) : (
                  <Button
                    variant="quiet"
                    onClick={() => {
                      setChoosingInheritance((current) => !current);
                    }}
                  >
                    Inherit from…
                  </Button>
                )}
              </FormActions>
              {choosingInheritance ? (
                <SearchableSelect
                  label="Inherit from"
                  value=""
                  placeholder="Search assignments"
                  options={[...assignmentByName.values()].flatMap((item) =>
                    item.api_name === apiName
                      ? []
                      : [
                          {
                            value: item.api_name,
                            label: item.display_name,
                            searchText: item.api_name,
                          },
                        ],
                  )}
                  onChange={(value) => {
                    if (definitionMode === "direct")
                      savedDirectRows.current = chainRows;
                    updateAssignmentFields({
                      definitionMode: "inherit",
                      inheritedAssignmentName: value,
                    });
                    setChoosingInheritance(false);
                    markAssignmentDirty();
                  }}
                />
              ) : null}
              {definitionMode === "direct" ? (
                <AssignmentChainEditor
                  modelByName={modelByName}
                  requirements={requirements}
                  pending={pending}
                  onDirty={markAssignmentDirty}
                  providerModels={providerModels}
                  providerByName={providerByName}
                  rows={chainRows}
                  setRows={setChainRows}
                />
              ) : null}
            </FormControls>
          </form>
          {assignment === undefined ? null : (
            <AdvancedFieldsDisclosure summary="Assignment details">
              {recordFacts([
                ["Last used", assignment.last_used_at ?? "Never"],
                ...(inspector.rungPosition === undefined
                  ? []
                  : [
                      [
                        "Selected model",
                        selectedModel?.display_name ??
                          selectedCandidate?.provider_model_api_name ??
                          "Unavailable",
                      ] as const,
                      [
                        "Provider",
                        selectedProvider?.display_name ?? "Unavailable",
                      ] as const,
                      [
                        "State",
                        selectedRouteState?.stateLabel ?? "Unavailable",
                      ] as const,
                    ]),
              ])}
              <FormSection legend="Requirements" variant="plain">
                <FormControls disabled={pending}>
                  <CheckboxChipGroup label="Assignment requirements">
                    {(
                      Object.keys(requirementLabels) as ObservedRequirement[]
                    ).map((requirement) => (
                      <CheckboxControl
                        key={requirement}
                        appearance="chip"
                        label={requirementLabels[requirement]}
                        checked={requirements.includes(requirement)}
                        onChange={(event) =>
                          void changeRequirement(
                            requirement,
                            event.currentTarget.checked,
                          )
                        }
                      />
                    ))}
                  </CheckboxChipGroup>
                </FormControls>
              </FormSection>
            </AdvancedFieldsDisclosure>
          )}
        </>
      )}
    </Dialog>
  );
}

function AssignmentChainEditor({
  onDirty,
  providerModels,
  providerByName,
  modelByName,
  pending,
  requirements,
  rows,
  setRows,
}: {
  readonly onDirty: () => void;
  readonly providerModels: readonly ProviderModel[];
  readonly providerByName: ReadonlyMap<string, Provider>;
  readonly modelByName: ReadonlyMap<string, Model>;
  readonly pending: boolean;
  readonly requirements: readonly ObservedRequirement[];
  readonly rows: readonly EditableTableRow<ChainDraft>[];
  readonly setRows: Dispatch<
    SetStateAction<readonly EditableTableRow<ChainDraft>[]>
  >;
}) {
  const newRowIdPrefix = useId();
  const nextRowIdRef = useRef(0);
  const routes = new Map(
    providerModels.map((route) => [route.api_name, route]),
  );
  return (
    <FormSection legend="Fallback chain" variant="plain">
      <OrderedChoiceList
        label="Ordered assignment provider-route chain"
        addLabel="Add provider route"
        addPlaceholder="Search provider or model"
        disabled={pending}
        maxItems={16}
        options={providerModels.flatMap((route) =>
          requirements.every((requirement) =>
            routeMeetsRequirement(route, requirement),
          )
            ? [
                {
                  value: route.api_name,
                  label: `${route.provider_model_name} · ${providerByName.get(route.provider_api_name)?.display_name ?? route.provider_api_name}`,
                  searchText: `${route.api_name} ${modelByName.get(route.model_api_name)?.display_name ?? route.model_api_name}`,
                  disabled: !route.enabled,
                },
              ]
            : [],
        )}
        items={rows.map((row) => {
          const route = routes.get(row.draft.providerModel);
          return {
            id: row.id,
            value: row.draft.providerModel,
            label:
              route?.provider_model_name ??
              `Unavailable route: ${row.draft.providerModel}`,
            ...(route
              ? {
                  detail: `${providerByName.get(route.provider_api_name)?.display_name ?? route.provider_api_name}${route.enabled ? "" : " · Disabled"}`,
                }
              : {}),
          };
        })}
        onAdd={(value) => {
          nextRowIdRef.current += 1;
          setRows((current) => [
            ...current,
            {
              id: `chain:${newRowIdPrefix}:${String(nextRowIdRef.current)}`,
              label: assignmentPositionLabel(current.length + 1),
              draft: { providerModel: value },
            },
          ]);
          onDirty();
        }}
        onRemove={(id) => {
          setRows((current) =>
            orderChainRows(current.filter((row) => row.id !== id)),
          );
          onDirty();
        }}
        onReorder={(ids) => {
          setRows((current) =>
            orderChainRows(
              ids.flatMap((id) => {
                const row = current.find((item) => item.id === id);
                return row ? [row] : [];
              }),
            ),
          );
          onDirty();
        }}
      />
    </FormSection>
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
          <div
            className="openrouter-provider-option"
            key={option.provider_api_name}
          >
            <CheckboxControl
              checked={selectedProviders.has(option.provider_api_name)}
              disabled={pending || !option.selectable}
              label={
                <span>
                  <strong>{option.provider_display_name}</strong>
                  <small>
                    {option.selectable
                      ? option.provider_model.api_name
                      : option.unavailable_reason}
                  </small>
                </span>
              }
              onChange={(event) => {
                const next = new Set(selectedProviders);
                if (event.currentTarget.checked)
                  next.add(option.provider_api_name);
                else next.delete(option.provider_api_name);
                setSelectedProviders(next);
              }}
            />
          </div>
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
function ConfigurationEditControl({
  context,
  label,
  onEdit,
  pending,
  selected,
}: {
  readonly context: Omit<RelationshipGraphNodeContext, "trigger">;
  readonly label: string;
  readonly onEdit: (context: RelationshipGraphNodeContext) => void;
  readonly pending: boolean;
  readonly selected: boolean;
}) {
  return (
    <IconButton
      aria-label={label}
      title={label}
      icon={<Icon name="edit" />}
      disabled={pending}
      tabIndex={selected ? 0 : -1}
      onClick={(event) => {
        onEdit({ ...context, trigger: event.currentTarget });
      }}
    />
  );
}
