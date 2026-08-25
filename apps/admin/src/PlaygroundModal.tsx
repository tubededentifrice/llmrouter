import { useEffect, useMemo, useReducer, useRef, type RefObject } from "react";
import {
  Button,
  Dialog,
  OperationPlayground,
  type PlaygroundInputImage,
  type PlaygroundRequestValue,
  type PlaygroundResult,
  type PlaygroundRunState,
} from "@opendle/ui";
import {
  AdministrationApiError,
  errorMessage,
  type AdministrationClient,
  type AdministratorPlaygroundAttempt,
  type AdministratorPlaygroundMediaJob,
  type AdministratorPlaygroundModelRequest,
  type RuntimeInputImage,
  type Usage,
} from "./api.js";
import {
  parseTags,
  parseToolDefinitions,
  nonBlankInputLines,
  pollMediaJob,
  targetUnavailableMessage,
  type PlaygroundTargetSnapshot,
} from "./playgroundState.js";
import { createInputImageSelectionQueue } from "./formContracts.js";

interface PlaygroundModalProps {
  readonly client: AdministrationClient;
  readonly csrf: string;
  readonly currentTarget: PlaygroundTargetSnapshot | null;
  readonly onClose: () => void;
  readonly onMediaJobChange: (
    job: AdministratorPlaygroundMediaJob | null,
  ) => void;
  readonly onRefreshTarget: () => Promise<void>;
  readonly onUncertainMediaAdmissionChange: (uncertain: boolean) => void;
  readonly retainedMediaJob: AdministratorPlaygroundMediaJob | null;
  readonly retainedUncertainMediaAdmission: boolean;
  readonly returnFocusRef: RefObject<HTMLElement | null>;
  readonly target: PlaygroundTargetSnapshot;
}

type AdministratorPlaygroundClient = Required<
  Pick<
    AdministrationClient,
    | "playgroundModel"
    | "playgroundModelStream"
    | "playgroundEmbedding"
    | "playgroundCreateMedia"
    | "playgroundMediaJob"
    | "playgroundMediaContent"
  >
>;

interface InputImageRecord extends PlaygroundInputImage {
  readonly input: RuntimeInputImage;
  readonly sizeBytes: number;
}

interface PlaygroundDiagnostics {
  readonly logicalCallId: string | null;
  readonly attempts: readonly AdministratorPlaygroundAttempt[];
  readonly job?: {
    readonly id: string;
    readonly state: AdministratorPlaygroundMediaJob["state"];
    readonly usage?: Usage;
    readonly error?: AdministratorPlaygroundMediaJob["error"];
  };
}

interface PlaygroundModalState {
  readonly value: PlaygroundRequestValue;
  readonly runState: PlaygroundRunState;
  readonly diagnostics: PlaygroundDiagnostics | null;
  readonly inputImages: readonly InputImageRecord[];
  readonly refreshing: boolean;
  readonly stream: boolean;
  readonly structured: boolean;
  readonly schema: string;
  readonly tools: string;
  readonly tags: string;
}

function mediaDiagnostics(
  job: AdministratorPlaygroundMediaJob,
): PlaygroundDiagnostics {
  return {
    logicalCallId: job.logical_call_id,
    attempts: job.attempts,
    job: {
      id: job.id,
      state: job.state,
      ...(job.usage === undefined ? {} : { usage: job.usage }),
      ...(job.error === undefined ? {} : { error: job.error }),
    },
  };
}

const initialRunState: PlaygroundRunState = {
  status: "empty",
  message: "Run this fixed target to see its global administrator result.",
};

function initialValue(
  target: PlaygroundTargetSnapshot,
): PlaygroundRequestValue {
  return {
    operation: target.operations[0]?.operation ?? "model",
    input: "",
    systemPrompt: "",
    temperature: null,
    outputLimit: null,
  };
}

function correctiveError(error: unknown): PlaygroundRunState {
  const code = error instanceof AdministrationApiError ? error.code : undefined;
  const corrections: Record<string, string> = {
    authentication_required: "Sign in again, then reopen this playground.",
    permission_denied: "Refresh the page to get current browser controls.",
    invalid_request: "Correct the marked request data and run it again.",
    not_found: "Close the playground and review the current graph.",
    provider_unavailable:
      "Enable one compatible route or change the selected target configuration.",
    upstream_failed:
      "Review the safe attempt details and provider connection, then try again.",
    content_unavailable:
      "Poll the same job again. Do not create a replacement job.",
    rate_limited: "Wait for capacity, then run this request again.",
    client_timeout:
      "Check detailed logs or the same media job before you submit the work again.",
    invalid_response: "Check Router health and detailed logs before you retry.",
  };
  return {
    status: "error",
    error: {
      title: "The playground operation did not complete",
      message:
        error instanceof Error && !(error instanceof AdministrationApiError)
          ? error.message
          : errorMessage(error),
      correction:
        (code === undefined ? undefined : corrections[code]) ??
        "Review Router health and try the operation again.",
      ...(code === undefined ? {} : { code }),
    },
  };
}

function usageItems(usage: Usage) {
  return usage.units.map((item, index) => ({
    id: `${item.unit}:${String(index)}`,
    label: item.unit.replaceAll("_", " "),
    value: item.quantity,
  }));
}

function textContent(
  content: readonly (
    | { readonly type: "text"; readonly text: string }
    | {
        readonly type: "tool_call";
        readonly id: string;
        readonly name: string;
        readonly arguments_json: string;
      }
  )[],
): string {
  return content
    .map((part) => {
      if (part.type === "text") return part.text;
      let argumentsValue: unknown;
      try {
        argumentsValue = JSON.parse(part.arguments_json) as unknown;
      } catch {
        throw new AdministrationApiError(
          502,
          "invalid_response",
          "The Router returned invalid tool-call arguments.",
        );
      }
      return JSON.stringify(
        {
          type: "tool_call",
          id: part.id,
          name: part.name,
          arguments: argumentsValue,
        },
        null,
        2,
      );
    })
    .join("\n\n");
}

function resultFacts(
  providerModel: string,
  elapsedMs: number | null,
  usage: Usage | undefined,
  output: PlaygroundResult["output"],
  attempts: readonly AdministratorPlaygroundAttempt[],
): PlaygroundResult {
  return {
    output,
    selectedRoute: {
      label: providerModel,
      detail: `${String(attempts.length)} attempt${attempts.length === 1 ? "" : "s"}${attempts.length > 1 ? " · fallback used" : ""}`,
    },
    latencyMs: elapsedMs,
    usage: usage === undefined ? [] : usageItems(usage),
    cost:
      usage === undefined
        ? null
        : { amount: usage.cost, currency: usage.currency },
  };
}

function requirePlaygroundClient(
  client: AdministrationClient,
): AdministratorPlaygroundClient {
  if (
    client.playgroundModel === undefined ||
    client.playgroundModelStream === undefined ||
    client.playgroundEmbedding === undefined ||
    client.playgroundCreateMedia === undefined ||
    client.playgroundMediaJob === undefined ||
    client.playgroundMediaContent === undefined
  )
    throw new AdministrationApiError(
      500,
      "internal_error",
      "The administrator playground client is unavailable.",
    );
  return client as AdministratorPlaygroundClient;
}

function fileBase64(file: File): Promise<string> {
  return file.arrayBuffer().then((buffer) => {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 8192)
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    return btoa(binary);
  });
}

function inputMediaType(file: File): RuntimeInputImage["media_type"] {
  if (
    file.type === "image/jpeg" ||
    file.type === "image/png" ||
    file.type === "image/webp"
  )
    return file.type;
  throw new Error(`${file.name} must be a JPEG, PNG, or WebP image.`);
}

function PlaygroundDiagnosticsView({
  diagnostics,
}: {
  readonly diagnostics: PlaygroundDiagnostics | null;
}) {
  if (diagnostics === null) return null;
  return (
    <section
      aria-label="Playground route attempts"
      className="playground-diagnostics"
    >
      <h3>Route details</h3>
      <dl>
        <div>
          <dt>Logical call</dt>
          <dd>{diagnostics.logicalCallId ?? "Not admitted"}</dd>
        </div>
        {diagnostics.job === undefined ? null : (
          <>
            <div>
              <dt>Media job</dt>
              <dd>
                {diagnostics.job.id} · {diagnostics.job.state}
              </dd>
            </div>
            {diagnostics.job.usage === undefined ? null : (
              <div>
                <dt>Media job usage</dt>
                <dd>
                  {diagnostics.job.usage.units
                    .map((item) => `${item.quantity} ${item.unit}`)
                    .join(", ") || "No typed usage"}
                  {` · ${diagnostics.job.usage.cost} ${diagnostics.job.usage.currency}`}
                </dd>
              </div>
            )}
            {diagnostics.job.error === undefined ? null : (
              <div>
                <dt>Media job error</dt>
                <dd>
                  {diagnostics.job.error.code}: {diagnostics.job.error.message}
                </dd>
              </div>
            )}
          </>
        )}
      </dl>
      {diagnostics.attempts.length === 0 ? (
        <p>No provider attempt completed.</p>
      ) : (
        <ol>
          {diagnostics.attempts.map((attempt, index) => (
            <li key={`${attempt.provider_model_api_name}:${String(index)}`}>
              <strong>{attempt.provider_model_api_name}</strong>
              <span>
                {attempt.outcome} · {String(attempt.elapsed_ms)} ms
              </span>
              {attempt.usage === undefined ? (
                <span>Usage and cost not reported</span>
              ) : (
                <span>
                  {attempt.usage.units
                    .map((item) => `${item.quantity} ${item.unit}`)
                    .join(", ") || "No typed usage"}
                  {` · ${attempt.usage.cost} ${attempt.usage.currency}`}
                </span>
              )}
              {attempt.error === undefined ? null : (
                <span>
                  {attempt.error.code}: {attempt.error.message}
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- This host coordinator owns one abortable native call lifecycle. Its visual sections are separate shared components.
export function PlaygroundModal({
  client,
  csrf,
  currentTarget,
  onClose,
  onMediaJobChange,
  onRefreshTarget,
  onUncertainMediaAdmissionChange,
  retainedMediaJob,
  retainedUncertainMediaAdmission,
  returnFocusRef,
  target,
}: PlaygroundModalProps) {
  const [state, patchState] = useReducer(
    (
      current: PlaygroundModalState,
      patch: Partial<PlaygroundModalState>,
    ): PlaygroundModalState => ({ ...current, ...patch }),
    {
      value: initialValue(target),
      runState:
        retainedMediaJob !== null
          ? {
              status: "empty",
              message: `Resume admitted media job ${retainedMediaJob.id}. Do not submit a replacement.`,
            }
          : retainedUncertainMediaAdmission
            ? {
                status: "empty",
                message:
                  "The last media admission result is uncertain. Check detailed logs before you allow another submission.",
              }
            : initialRunState,
      diagnostics:
        retainedMediaJob === null ? null : mediaDiagnostics(retainedMediaJob),
      inputImages: [],
      refreshing: false,
      stream: false,
      structured: target.requiresStructuredOutput,
      schema: '{"type":"object","additionalProperties":false,"properties":{}}',
      tools: "",
      tags: "",
    },
  );
  const {
    diagnostics,
    inputImages,
    refreshing,
    runState,
    schema,
    stream,
    structured,
    tags,
    tools,
    value,
  } = state;
  const setValue = (next: PlaygroundRequestValue) => {
    patchState({ value: next });
  };
  const setRunState = (next: PlaygroundRunState) => {
    patchState({ runState: next });
  };
  const setDiagnostics = (next: PlaygroundDiagnostics | null) => {
    patchState({ diagnostics: next });
  };
  const setStream = (next: boolean) => {
    patchState({ stream: next });
  };
  const setStructured = (next: boolean) => {
    patchState({ structured: next });
  };
  const setSchema = (next: string) => {
    patchState({ schema: next });
  };
  const setTools = (next: string) => {
    patchState({ tools: next });
  };
  const setTags = (next: string) => {
    patchState({ tags: next });
  };
  const requestRef = useRef<{ id: number; controller: AbortController } | null>(
    null,
  );
  const nextRequestIdRef = useRef(0);
  const nextImageIdRef = useRef(0);
  const objectUrlRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  const imageQueue = useMemo(
    () =>
      createInputImageSelectionQueue<InputImageRecord>([], (images) => {
        patchState({ inputImages: images });
      }),
    [],
  );
  const targetUnavailable = targetUnavailableMessage(target, currentTarget);
  const unavailable =
    targetUnavailable ??
    (retainedMediaJob !== null
      ? `Media job ${retainedMediaJob.id} is already admitted. Resume or dismiss that job before you submit new work.`
      : retainedUncertainMediaAdmission
        ? "The prior media admission can still have created a job. Check detailed logs, then acknowledge the warning before you submit more work."
        : null);
  const running = runState.status === "loading";
  const modelOperation = value.operation === "model";

  useEffect(
    () => () => {
      mountedRef.current = false;
      imageQueue.dispose();
      requestRef.current?.controller.abort();
      if (objectUrlRef.current !== null)
        URL.revokeObjectURL(objectUrlRef.current);
    },
    [imageQueue],
  );

  function beginRequest(resumingMedia = false): {
    readonly id: number;
    readonly signal: AbortSignal;
  } | null {
    if (
      requestRef.current !== null ||
      (!resumingMedia &&
        (targetUnavailable !== null ||
          retainedMediaJob !== null ||
          retainedUncertainMediaAdmission))
    )
      return null;
    const controller = new AbortController();
    const id = nextRequestIdRef.current + 1;
    nextRequestIdRef.current = id;
    requestRef.current = { id, controller };
    return { id, signal: controller.signal };
  }

  function isCurrent(id: number): boolean {
    return requestRef.current?.id === id;
  }

  function finishRequest(id: number): void {
    if (isCurrent(id)) requestRef.current = null;
  }

  async function resolveAdmittedMediaJob(
    playgroundClient: AdministratorPlaygroundClient,
    initial: AdministratorPlaygroundMediaJob,
    active: { readonly id: number; readonly signal: AbortSignal },
  ): Promise<void> {
    let completed = initial;
    if (completed.state === "pending" || completed.state === "running") {
      try {
        // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a late poll result.
        completed = await pollMediaJob(
          playgroundClient,
          completed,
          active.signal,
          (job) => {
            completed = job;
            onMediaJobChange(job);
            if (isCurrent(active.id)) setDiagnostics(mediaDiagnostics(job));
          },
        );
      } catch (error) {
        if (isCurrent(active.id)) {
          if (error instanceof AdministrationApiError)
            setDiagnostics({
              ...mediaDiagnostics(completed),
              logicalCallId:
                error.context?.logical_call_id ?? completed.logical_call_id,
              attempts: error.context?.attempts ?? completed.attempts,
            });
          else setDiagnostics(mediaDiagnostics(completed));
        }
        throw error;
      }
      if (!isCurrent(active.id)) return;
    }
    onMediaJobChange(completed);
    setDiagnostics(mediaDiagnostics(completed));
    if (completed.state === "failed")
      throw new AdministrationApiError(
        502,
        completed.error?.code ?? "upstream_failed",
        completed.error?.message ?? "The media job failed.",
        completed.error?.details ?? undefined,
        {
          logical_call_id: completed.logical_call_id,
          selector: completed.selector,
          ...(completed.elapsed_ms === undefined
            ? {}
            : { elapsed_ms: completed.elapsed_ms }),
          attempts: completed.attempts,
        },
      );
    if (completed.content === undefined)
      throw new AdministrationApiError(
        502,
        "invalid_response",
        "The succeeded media job has no content facts.",
      );
    // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects late retained content.
    const blob = await playgroundClient.playgroundMediaContent(
      completed.id,
      active.signal,
    );
    if (!isCurrent(active.id)) return;
    if (
      blob.size !== completed.content.size_bytes ||
      blob.type.split(";", 1)[0]?.trim().toLowerCase() !==
        completed.content.media_type.toLowerCase()
    )
      throw new AdministrationApiError(
        502,
        "invalid_response",
        "The media content does not match its admitted job facts.",
      );
    const objectUrl = URL.createObjectURL(blob);
    objectUrlRef.current = objectUrl;
    setRunState({
      status: "success",
      result: resultFacts(
        completed.provider_model_api_name,
        completed.elapsed_ms ?? null,
        completed.usage,
        {
          kind: completed.kind,
          objectUrl,
          label: `${target.label} ${completed.kind} result`,
          mediaType: completed.content.media_type,
        },
        completed.attempts,
      ),
    });
    onMediaJobChange(null);
  }

  async function resumeMediaJob(): Promise<void> {
    if (retainedMediaJob === null || retainedMediaJob.state === "failed")
      return;
    const active = beginRequest(true);
    if (active === null) return;
    setRunState({
      status: "loading",
      message: `The Router is resuming media job ${retainedMediaJob.id}.`,
    });
    try {
      await resolveAdmittedMediaJob(
        requirePlaygroundClient(client),
        retainedMediaJob,
        active,
      );
    } catch (error) {
      if (!isCurrent(active.id)) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      setRunState(correctiveError(error));
    } finally {
      finishRequest(active.id);
    }
  }

  async function run(requestValue: PlaygroundRequestValue): Promise<void> {
    const active = beginRequest();
    if (active === null) return;
    setRunState({
      status: "loading",
      message: "The Router is running this administrator call.",
    });
    setDiagnostics(null);
    if (objectUrlRef.current !== null) {
      URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
    }
    let admittedMediaJob: PlaygroundDiagnostics["job"];
    try {
      const playgroundClient = requirePlaygroundClient(client);
      const parsedTags = parseTags(tags);
      if (requestValue.operation === "embedding") {
        // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a late response from this awaited call.
        const result = await playgroundClient.playgroundEmbedding(
          {
            selector: target.selector,
            inputs: nonBlankInputLines(requestValue.input),
            ...(parsedTags.length === 0 ? {} : { tags: parsedTags }),
          },
          csrf,
          active.signal,
        );
        if (!isCurrent(active.id)) return;
        const first = result.result.embeddings[0];
        setRunState({
          status: "success",
          result: resultFacts(
            result.result.provider_model_api_name,
            result.elapsed_ms,
            result.result.usage,
            {
              kind: "embedding",
              vectorCount: result.result.embeddings.length,
              dimensions: first?.values.length ?? 0,
              ...(first === undefined
                ? {}
                : { preview: first.values.slice(0, 12) }),
            },
            result.attempts,
          ),
        });
        setDiagnostics({
          logicalCallId: result.logical_call_id,
          attempts: result.attempts,
        });
        return;
      }
      if (requestValue.operation !== "model") {
        const mediaRequest =
          requestValue.operation === "audio"
            ? {
                selector: target.selector,
                kind: requestValue.operation,
                prompt: requestValue.input,
                ...(parsedTags.length === 0 ? {} : { tags: parsedTags }),
              }
            : {
                selector: target.selector,
                kind: requestValue.operation,
                prompt: requestValue.input,
                ...(inputImages.length === 0
                  ? {}
                  : {
                      input_images: inputImages.map((image) => ({
                        type: "image" as const,
                        ...image.input,
                      })),
                    }),
                ...(parsedTags.length === 0 ? {} : { tags: parsedTags }),
              };
        onUncertainMediaAdmissionChange(true);
        // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a late response from this awaited call.
        const created = await playgroundClient.playgroundCreateMedia(
          mediaRequest,
          csrf,
          active.signal,
        );
        if (!isCurrent(active.id)) return;
        onUncertainMediaAdmissionChange(false);
        setDiagnostics(mediaDiagnostics(created));
        admittedMediaJob = mediaDiagnostics(created).job;
        onMediaJobChange(created);
        await resolveAdmittedMediaJob(playgroundClient, created, active);
        return;
      }

      let outputFormat: AdministratorPlaygroundModelRequest["output_format"];
      if (structured) {
        JSON.parse(schema);
        outputFormat = { type: "json_schema", schema_json: schema };
      }
      const toolDefinitions = parseToolDefinitions(tools);
      const modelRequest: AdministratorPlaygroundModelRequest = {
        selector: target.selector,
        messages: [
          ...(requestValue.systemPrompt === ""
            ? []
            : [
                { role: "system" as const, content: requestValue.systemPrompt },
              ]),
          {
            role: "user" as const,
            content: [
              { type: "text" as const, text: requestValue.input },
              ...inputImages.map((image) => ({
                type: "image" as const,
                ...image.input,
              })),
            ],
          },
        ],
        ...(toolDefinitions === undefined ? {} : { tools: toolDefinitions }),
        ...(outputFormat === undefined ? {} : { output_format: outputFormat }),
        ...(requestValue.outputLimit === null
          ? {}
          : { output_limit: requestValue.outputLimit }),
        ...(requestValue.temperature === null
          ? {}
          : { temperature: requestValue.temperature }),
        ...(parsedTags.length === 0 ? {} : { tags: parsedTags }),
      };
      if (stream && !structured) {
        // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a late stream result.
        const result = await playgroundClient.playgroundModelStream(
          modelRequest,
          csrf,
          active.signal,
        );
        if (!isCurrent(active.id)) return;
        setRunState({
          status: "success",
          result: resultFacts(
            result.provider_model_api_name,
            result.elapsed_ms,
            result.usage,
            { kind: "text", content: textContent(result.content) },
            result.attempts,
          ),
        });
        setDiagnostics({
          logicalCallId: result.logical_call_id,
          attempts: result.attempts,
        });
      } else {
        // react-doctor-disable-next-line react-doctor/async-defer-await -- The next guard rejects a late model result.
        const result = await playgroundClient.playgroundModel(
          modelRequest,
          csrf,
          active.signal,
        );
        if (!isCurrent(active.id)) return;
        const output =
          result.result.output_type === "structured_json"
            ? {
                kind: "json" as const,
                content: result.result.structured_output_json,
              }
            : {
                kind: "text" as const,
                content: textContent(result.result.content),
              };
        setRunState({
          status: "success",
          result: resultFacts(
            result.result.provider_model_api_name,
            result.elapsed_ms,
            result.result.usage,
            output,
            result.attempts,
          ),
        });
        setDiagnostics({
          logicalCallId: result.logical_call_id,
          attempts: result.attempts,
        });
      }
    } catch (error) {
      if (!isCurrent(active.id)) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      if (
        value.operation !== "model" &&
        value.operation !== "embedding" &&
        error instanceof AdministrationApiError &&
        error.code !== "client_timeout" &&
        error.code !== "invalid_response" &&
        error.context?.logical_call_id === undefined
      )
        onUncertainMediaAdmissionChange(false);
      if (error instanceof SyntaxError)
        setRunState(
          correctiveError(
            new AdministrationApiError(
              400,
              "invalid_request",
              "The JSON Schema or tool definition JSON is invalid.",
              { reason: error.message },
            ),
          ),
        );
      else setRunState(correctiveError(error));
      if (
        error instanceof AdministrationApiError &&
        admittedMediaJob === undefined
      )
        setDiagnostics({
          logicalCallId: error.context?.logical_call_id ?? null,
          attempts: error.context?.attempts ?? [],
        });
    } finally {
      finishRequest(active.id);
    }
  }

  async function addImages(files: readonly File[]): Promise<void> {
    try {
      await imageQueue.add(files, async (file) => {
        nextImageIdRef.current += 1;
        const id = `playground-image-${String(nextImageIdRef.current)}`;
        const input = {
          media_type: inputMediaType(file),
          data_base64: await fileBase64(file),
        };
        return {
          id,
          name: file.name,
          detail: `${String(Math.ceil(file.size / 1024))} KiB`,
          sizeBytes: file.size,
          input,
        };
      });
    } catch (error) {
      if (!mountedRef.current) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      setRunState(
        correctiveError(
          new AdministrationApiError(
            400,
            "invalid_request",
            error instanceof Error ? error.message : "The image is invalid.",
          ),
        ),
      );
    }
  }

  function close(): void {
    mountedRef.current = false;
    imageQueue.dispose();
    nextRequestIdRef.current += 1;
    requestRef.current?.controller.abort();
    requestRef.current = null;
    onClose();
  }

  async function refreshTarget(): Promise<void> {
    if (running || refreshing) return;
    patchState({ refreshing: true });
    try {
      await onRefreshTarget();
    } finally {
      if (mountedRef.current) patchState({ refreshing: false });
    }
  }

  return (
    <Dialog
      actions={
        <Button
          disabled={running || refreshing}
          onClick={() => void refreshTarget()}
          variant="secondary"
        >
          {refreshing ? "Refreshing target…" : "Refresh target"}
        </Button>
      }
      bodyClassName="configuration-playground-dialog-body"
      closeDisabled={false}
      description="Run one unrestricted global administrator call. Assignment service context selects configuration only."
      eyebrow="Global administrator playground"
      onClose={close}
      open
      returnFocusRef={returnFocusRef}
      size="wide"
      title={`Play ${target.label}`}
    >
      {modelOperation ? (
        <details className="configuration-playground-options">
          <summary>Advanced native request</summary>
          <div>
            {target.supportsStreaming ? (
              <label className="checkbox-field">
                <input
                  checked={stream}
                  disabled={running || structured}
                  onChange={(event) => {
                    setStream(event.currentTarget.checked);
                  }}
                  type="checkbox"
                />
                Stream model output
              </label>
            ) : null}
            {target.supportsStructuredOutput ? (
              <label className="checkbox-field">
                <input
                  checked={structured}
                  disabled={running || target.requiresStructuredOutput}
                  onChange={(event) => {
                    setStructured(event.currentTarget.checked);
                    if (event.currentTarget.checked) setStream(false);
                  }}
                  type="checkbox"
                />
                Validate structured JSON output
              </label>
            ) : null}
            {structured ? (
              <label>
                JSON Schema
                <textarea
                  disabled={running}
                  onChange={(event) => {
                    setSchema(event.currentTarget.value);
                  }}
                  rows={6}
                  value={schema}
                />
              </label>
            ) : null}
            {target.supportsTools ? (
              <label>
                Tool definitions JSON
                <textarea
                  disabled={running}
                  onChange={(event) => {
                    setTools(event.currentTarget.value);
                  }}
                  placeholder='[{"name":"lookup","description":"Find a value","input_schema_json":"{\\"type\\":\\"object\\"}"}]'
                  rows={6}
                  value={tools}
                />
              </label>
            ) : null}
          </div>
        </details>
      ) : null}
      <label className="configuration-playground-tags">
        Tags
        <input
          disabled={running}
          onChange={(event) => {
            setTags(event.currentTarget.value);
          }}
          placeholder="manual, diagnostic"
          value={tags}
        />
      </label>
      <OperationPlayground
        fixedTarget={{
          selection: { kind: target.kind, id: target.id },
          label: target.label,
          detail: target.detail,
          ...(target.serviceContext === undefined
            ? {}
            : {
                context: {
                  label: "Service configuration context",
                  value: target.serviceContext,
                },
              }),
          operations: target.operations,
          ...(unavailable === null
            ? {}
            : {
                state: {
                  status: "unavailable" as const,
                  message: unavailable,
                },
              }),
        }}
        id="configuration-playground"
        inputImages={inputImages}
        onAddInputImages={(files) => void addImages(files)}
        onRemoveInputImage={(imageId) => {
          void imageQueue.remove((image) => image.id === imageId);
        }}
        onReset={() => {
          if (running) return;
          setValue(initialValue(target));
          imageQueue.clear();
          setStream(false);
          setStructured(target.requiresStructuredOutput);
          setTools("");
          setTags("");
          setRunState(initialRunState);
          setDiagnostics(null);
          if (objectUrlRef.current !== null) {
            URL.revokeObjectURL(objectUrlRef.current);
            objectUrlRef.current = null;
          }
        }}
        onRun={(requestValue) => void run(requestValue)}
        onValueChange={setValue}
        runLabel={running ? "Running operation" : "Run operation"}
        runState={runState}
        title="Request and result"
        value={value}
      />
      {retainedMediaJob === null ? null : (
        <section aria-label="Admitted media job recovery">
          <p>
            Media job {retainedMediaJob.id} is {retainedMediaJob.state}. The
            Router will not submit a replacement job.
          </p>
          {retainedMediaJob.state === "failed" ? (
            <Button
              disabled={running}
              onClick={() => {
                onMediaJobChange(null);
                setRunState(initialRunState);
              }}
              variant="secondary"
            >
              Dismiss failed media job
            </Button>
          ) : (
            <Button
              disabled={running}
              onClick={() => void resumeMediaJob()}
              variant="secondary"
            >
              Resume media job
            </Button>
          )}
        </section>
      )}
      {!retainedUncertainMediaAdmission || retainedMediaJob !== null ? null : (
        <section aria-label="Uncertain media admission">
          <p>
            The browser did not receive a trustworthy media admission result.
            The Router can still have created a job. Check detailed logs before
            you allow another media submission for this target.
          </p>
          <Button
            disabled={running}
            onClick={() => {
              onUncertainMediaAdmissionChange(false);
              setRunState(initialRunState);
            }}
            variant="secondary"
          >
            I checked; allow a new submission
          </Button>
        </section>
      )}
      <PlaygroundDiagnosticsView diagnostics={diagnostics} />
    </Dialog>
  );
}

export type { PlaygroundModalProps };
