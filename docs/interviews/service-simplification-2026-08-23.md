# Service simplification decisions

Status: Accepted decision source.

Date: 2026-08-23

Approved: 2026-08-23

## Purpose

This record collects the product decisions for a restart of LLM Router. The
new design will keep only necessary external behavior. Simplicity, few failure
modes, and shared code are primary goals.

The current specifications are input to the review. They do not stay accepted
unless this record or a later answer keeps them.

## Decisions stated by the user

### Product purpose

- LLM Router is a shared model, embedding, and media-generation calling
  service for the LLM workflows of other services.
- Its primary runtime function is to call the model assignment that a service
  selects. It uses the configured ordered fallback models and providers.
- The Router also supplies an embedding operation with assignment fallback.
- The product must do only the functions that the user requests.

### Services and workspaces

- A service can inherit from one other service.
- A service owns its workspaces and has all permissions for them.
- A workspace belongs to exactly one service.
- A service API key is a backend-only credential. It gives the service all
  permissions for its own Router scope and workspaces.
- Each model, embedding, and media-generation request must identify exactly one
  workspace.
- A service can create workspaces and associate requests with the workspaces
  that it owns.
- A workspace is currently an accounting label only.
- Workspace cost limits are a possible future function. They are out of scope
  now.
- Each service and workspace has a stable, readable `apiName`.
- A service or workspace exists or it is deleted. There is no disabled,
  retired, restored, deleting, or cleanup-progress public state.
- A service or workspace has no public state revision or version.
- A service cannot be deleted while a child service inherits from it. The
  administrator must first move or delete each child.
- Deleting a service cascades through its keys, workspaces, assignments, raw
  request logs, raw accounting, daily aggregates, media jobs, and retained
  media.

### Assignments and administration

- The Router administration application lets the global administrator
  configure services and their assignment graphs.
- A reusable OpenDLE UI component lets a host configure the assignment graph
  at service scope.
- The Router uses that component in its administration application.
- Calling services can also use that component in their own applications.
- The Router supplies API endpoints for calling-service administration. It
  does not host the calling service's user interface.
- The Crewday assignment interface is the preferred behavior reference. FJ2
  can supply other useful functions, but its non-React implementation is not
  a component source.
- Reusable React components and interaction patterns belong in
  `../opendle-ui`.
- The Router hosts only the global administration application.
- The Router does not host a service-user application or a cross-origin
  service administration frame.
- A calling service renders the shared assignment component in its own user
  interface. Its backend calls the Router API with the service API key.
- The Router does not authenticate or authorize the calling service's human
  users. The calling service owns those checks.
- A child service inherits a named assignment through its single parent chain.
- The nearest service definition replaces the complete inherited fallback
  chain for that assignment.
- A child cannot add, remove, or reorder individual candidates in an inherited
  chain without defining its complete replacement chain.
- A workspace does not define or override assignments. It is only an
  accounting scope.
- Assignments are the central model-routing object.
- Each named assignment represents a service use case.
- An assignment contains the exact ordered provider-model candidates to try.
- Crewday and FJ2 will replace their local assignment and fallback calling
  behavior with this Router API and the shared SDK.
- Each root service has an implicit named `default` assignment. The assignment
  exists even when it has no configured model chain.
- A service can configure its own `default` chain at any level of the service
  parent tree.
- The nearest service definition of `default` replaces the complete parent
  chain. More distant service definitions are ignored.
- A child with no local `default` definition inherits its parent's effective
  `default`.
- One named assignment can inherit from one other named assignment.
- Assignment inheritance must reject a missing parent or cycle.
- A direct fallback chain replaces the inherited assignment chain.
- If a service calls an assignment that has no effective record, the Router
  creates that assignment for the service and makes it inherit `default`.
- An automatically created assignment is immediately usable through its
  inherited `default` chain.
- A call fails before provider work when the effective `default` chain is empty
  or no candidate supports the required call shape.
- The administration application clearly identifies assignments that still
  inherit `default` and have no direct chain. This makes new and unconfigured
  use cases easy to find.
- Each assignment stores when it was last used so that an administrator can
  find stale assignments.
- A normal first call to a new assignment does not require a separate setup
  or registration request.

### Provider connections and credentials

- Only the global administrator can create and manage provider connections,
  model availability, and provider credentials.
- Every globally enabled provider-model is available to every service through
  its assignment configuration surface.
- A service API key cannot read or manage provider credential values.
- The product does not support service-owned provider connections or
  service-owned provider credentials.
- The first simplified adapter set includes OpenAI, OpenAI-compatible
  endpoints, OpenRouter, custom endpoints, WaveSpeed, Ollama, local
  embeddings, and a fake test adapter.
- The first adapter set excludes native Anthropic, Z.AI, and ChatGPT or Codex
  subscription adapters.
- A dormant provider branch that has no selectable current provider type and
  no active caller is not migrated only because old code exists.
- The product does not have a service or service-tree provider-model allowlist.

### Model prices

- Each canonical model can select a price source and a price-source model ID.
- Each provider-model can override the canonical model's price source and
  lookup ID.
- No price source means manual pricing.
- OpenRouter is a normal price source. Other registered sources, such as
  WaveSpeed, can supply media-model prices.
- A price synchronization fetches each selected source once and updates the
  mapped provider-model prices.
- The global administrator can start a synchronization and can see updated,
  unchanged, missing, and failed results.
- The Router also runs one fixed daily price synchronization. The product does
  not have an editable synchronization schedule.
- A source failure or missing source row keeps the last accepted price.
- The Router uses the price that applies to the provider-model usage to
  calculate request and aggregate cost.
- Each attempt snapshots the price that it used. A later price change does not
  rewrite prior raw accounting or daily aggregates.

### Model catalog changes

- The global administrator can create a model and provider-model mapping
  manually.
- The administration application also provides an explicit on-demand import
  preview from registered catalogs such as OpenRouter.
- The administrator selects which previewed entries to import.
- The Router does not automatically add or change catalog entries in the
  background.

### Configuration changes

- A configuration change is validated and then applied directly to the current
  state.
- A validation failure leaves the current state unchanged.
- The product does not keep configuration drafts or configuration revisions.
- The product does not have publication, rollout, or rollback resources or
  workflows.

### Shared harness and SDK

- A common multi-turn harness belongs in `../opendle-lib`.
- Calling services use the harness in their own processes. They must not copy
  its generic loop behavior.
- The FJ2 harness is the primary behavior reference. Only the behavior stated
  in this record is accepted.
- The harness supports service-provided tools.
- The harness supports automatic conversation compaction or message pruning.
  The calling service selects the method.
- A simple SDK makes Router calls and harness use easy for calling services.
- Reusable framework-neutral behavior belongs in `../opendle-lib`.
- The Router does not run the harness. The calling service runs it through the
  shared library and SDK.
- The first shared SDK and complete multi-turn harness support Python.
- A TypeScript server SDK is not part of the first delivery.
- The harness does not own a durable conversation database.
- The harness accepts the current conversation state and returns updated
  state. It can use small caller-provided load and save callbacks.
- The calling service owns durable conversation storage, deletion, user
  authorization, and domain links.
- The shared library can supply an in-memory store for tests and short-lived
  processes, but callers do not depend on it for durable state.
- The harness executes multiple tool calls from one model turn sequentially in
  model order by default.
- A caller can replace the complete tool executor for a special need. The
  harness does not expose parallel execution as a normal mode.
- Model-based conversation compaction pins the exact provider connection and
  wire model that handled the preceding active workflow call.
- Compaction does not resolve the assignment again and does not use fallback.
- If that exact route cannot compact the context, the model-based compaction
  attempt fails and the harness uses its selected bounded failure behavior.
- The shared harness preserves the compatible conversation and tool prefix
  when it asks the model to compact context. This maximizes provider prompt
  caching and can reduce cost.
- Compaction remains a separate Router call for request logs, usage, cost, and
  failure handling. It uses the same workspace and the caller's tags.
- After the first successful workflow call, the harness tries that exact
  provider-model route first on later turns.
- If the sticky route fails before output becomes visible, the SDK continues
  through the current assignment fallback chain. The next successful route
  becomes sticky.

### Model and embedding data

- Model calls support multi-turn text messages.
- Model input can contain text and images.
- A service supplies image input as bounded bytes through the Router API.
- The Router does not fetch a caller-supplied image URL and does not accept a
  caller object-store reference.
- Uploaded input images are stored in Router-controlled object storage for the
  detailed-log retention period so that a global administrator can inspect the
  complete call.
- A service can provide tool definitions and tool-result messages. The model
  can return tool calls. The service executes each tool outside the Router.
- Normal model output is text or a tool call.
- A caller can instead request schema-validated JSON output with one JSON
  Schema.
- The Router uses only a candidate that supports the requested structured
  output and validates the returned value before it reports success.
- The Router also supports image generation, video generation, and audio
  generation.
- Image and video generation accept a text prompt and optional uploaded image
  inputs.
- Audio generation accepts text input.
- The first delivery does not accept audio or video input for media editing,
  extension, conversion, or remix.
- Embedding input is text.
- An embedding request contains a bounded batch of one or more texts and
  returns all vectors on the same connection.
- Embeddings do not use media jobs or another asynchronous request path.
- Each model declares its supported input modalities, output modalities, tool
  calling, streaming, embedding, and other call-shape capabilities.
- Each assignment records the union of capabilities and modalities observed in
  its calls.
- An administrator can remove an observed capability or modality that is no
  longer applicable.
- For each call, the Router filters the effective assignment chain against the
  actual modalities and capabilities that the current call requires. It does
  not use the complete observed union as the runtime filter.
- If no candidate matches, the Router fails before it calls a provider.
- Image, video, and audio generation use minimal jobs because provider work can
  take longer than one normal HTTP request.
- A media job has only `pending`, `running`, `succeeded`, or `failed` state.
- A media job has no cancellation, detailed progress phases, worker-ownership
  protocol, or general request-recovery state machine.
- Generated media is stored in Router-controlled S3-compatible storage for the
  same rolling duration as detailed request logs.
- The owning service downloads its job result through the Router API. A caller
  never receives an S3 bucket, key, credential, direct URL, or presigned URL.
- Detailed administrator logs can show the generated media through a Router
  endpoint.
- Storage uses date and workspace organization so that retention deletion is
  direct and bounded. This organization is not part of the caller contract.

### Runtime boundary

- The Router hosts model calls, embedding calls, and media-generation jobs.
- The Router does not host agent runs.
- The Router does not host shared external-tool adapters.
- The Router does not call a service business-tool gateway.
- The Router has no durable agent-run state, tool-run state, tool callback, or
  tool recovery system.
- A calling service supplies and runs its tools in its own process through the
  common harness.
- Caller tools can cause later model calls through the same Router model-call
  API.

### Deployment boundary

- The Router is one normal logical web application with PostgreSQL.
- A deployment can run ordinary identical application replicas when needed.
- The product does not have separate control-plane, data-plane, worker, or
  combined runtime roles.
- The product does not have Router node discovery, local event spools, fleet
  health hints, leased budget allowances, Router-controlled standby
  promotion, or fenced run ownership.
- Normal application, load balancer, PostgreSQL, backup, and deployment tools
  can provide deployment reliability without a separate Router product
  protocol.

### Request lifetime

- A model call is a normal synchronous or streaming connection-lifetime
  operation.
- Automatic fallback can occur only before model output becomes visible to
  the caller.
- For model and embedding calls, the API does not provide durable request
  admission, request status, cancellation, resume, replay recovery,
  asynchronous jobs, or results after a disconnected call.
- The client does not need a UUID binding or a 24-hour idempotency record for
  a model or embedding call.
- Detailed request logs and accounting records stay durable after a call
  finishes or fails.

### Fallback attempts

- An assignment has one ordered list of provider-model candidates.
- Before output becomes visible, the Router tries each eligible candidate no
  more than once.
- A provider failure moves the call to the next eligible candidate.
- The product does not have per-candidate retry counts, retry delay policy, or
  retry backoff.
- After output becomes visible, a provider failure ends the call or stream. It
  does not start another candidate.

### Provider cooldown

- Repeated recent provider-model failures can put that provider-model in a
  short cooldown.
- A call skips a provider-model while its cooldown is active and continues
  with the next assignment candidate.
- The administration application shows the current cooldown and its reason.
- Cooldown counters and expiry are best-effort cache data. A cache loss or
  restart can clear them.
- The product does not have half-open probes, fleet health hints, durable
  health history, or configurable circuit-breaker state machines.

### Public API style

- The Router publishes one native, versioned, provider-neutral API.
- The Router does not publish an OpenAI-compatible API.
- The shared SDK is the normal integration surface for calling services.
- The repository publishes an OpenAPI contract for the HTTP API.
- Public errors use stable names and concise corrective details. They do not
  expose provider credentials or internal storage details.

### Assignment and exact model selection

- A normal call can select a named assignment and use its ordered fallback
  chain.
- A service can also select one exact provider-model that is available to the
  service. This path is useful for a playground and model evaluation.
- Exact selection does not use assignment fallback.
- Exact and assignment calls use the same service ownership, workspace,
  logging, tagging, usage, and cost accounting rules.
- A reusable React playground component belongs in `../opendle-ui`.
- The Router administration application and calling-service applications can
  use the shared playground component.
- The component can test an exact provider-model or an assignment. It shows
  applicable request controls, text or media input, output, selected route,
  latency, usage, cost, and a corrective error.

### Call controls

- A caller can optionally set an output limit and temperature on a call.
- The Router does not store output-limit or temperature defaults on an
  assignment or assignment candidate.
- An assignment can optionally set one reasoning level for its complete
  effective fallback chain. Disabling reasoning is an important supported
  value because it can reduce token use.
- A model defaults to reasoning enabled when it supports reasoning.
- Provider-model configuration maps the common reasoning setting to the
  provider's supported request shape.

### Service authentication

- A backend sends a revocable long-lived service API key directly with each
  Router API request.
- The product has no service-token exchange, short-lived service token,
  operation scope, audience, embed session, or host action grant.
- One service can have several named active keys.
- Key replacement is create, deploy, and revoke. There is no timed rotation
  state or overlap workflow.
- Each key gives full authority for its service configuration, workspaces,
  model calls, embeddings, media jobs, and accounting, except for detailed
  request logs and global provider or credential management.

### Global administration identity

- The administration application uses the shared Pocket ID service for human
  sign-in.
- Deployment configuration contains an allowlist of Pocket ID subject IDs.
- Each allowed administrator has unrestricted Router administration
  authority.
- The Router does not have administrator tenants, grants, delegated scopes,
  or fine-grained administrator permissions.
- The Router uses one normal local server session with configurable expiry and
  logout.
- The Router does not require recurring identity-provider session checks,
  refresh-token rotation, or recent-authentication workflows.

### Basic activity log

- The Router keeps a bounded basic activity log for administrator and service
  configuration changes.
- It records the actor, action, target, time, and result.
- It does not store old values and is not a security-grade immutable audit
  trail.
- It uses the same global rolling duration and best-effort retention posture as
  detailed request logs.

### Operations boundary

- The Router exposes Prometheus metrics for Grafana.
- The administration application shows only a small useful health summary. It
  does not reproduce a complete metrics or operations product.
- The product has no fixed multi-zone availability, recovery-time, or
  recovery-point promise.
- Deployment-wide configuration supplies bounded request sizes, timeouts,
  concurrency, and similar safety limits.

### Accounting, tags, and request logs

- A calling service can send an array of plain string tags with a request.
- Tags are indexed accounting labels. Examples include `article:123` and
  `reason:write`.
- Statistics use bounded filtering and grouping. Filters and groups can use
  date, service, workspace, assignment, provider-model, outcome, and tags.
- Statistics results contain calls, attempts, units, and cost.
- The product does not provide a general analytics query language.
- Equivalent accounting rows can aggregate by day when their dimensions are
  equal. Dimensions include the tag set, model, workspace, and other necessary
  accounting fields.
- The Router keeps complete detailed request logs for a configured rolling
  period, such as seven days.
- The Router does not redact those request logs. It is for trusted internal
  services.
- Complete log content includes model messages, uploaded input images, tool
  definitions, tool results, provider responses, generated media, attempt
  errors, routing results, tags, usage, cost, and timing when applicable.
- Provider credentials, service API keys, administrator cookies,
  authorization headers, and object-storage credentials are control data, not
  model content. They never enter a request-log field.
- The Router does not pattern-scan, classify, redact, or rewrite prompt,
  response, or tool content.
- One global rolling retention duration applies to all detailed request logs.
- Detailed logs are best-effort cache data. They do not have a strong
  durability guarantee and can disappear before the configured maximum
  retention time after cache loss or eviction.
- Only the global administrator can read detailed request logs.
- A service can read its accounting statistics within its own scope. It can
  use the supported filters and groups for its tags, workspaces, assignments,
  provider-models, outcomes, and dates. It cannot read detailed request logs
  through the service API.
- Raw request and attempt accounting is durable PostgreSQL data until its
  scheduled daily rollup succeeds. It is not stored only in the best-effort
  diagnostic cache.
- A scheduled rollup processes closed days, such as on the next day, and adds
  the raw usage and cost to durable daily aggregates.
- The rollup is safe to repeat and does not count the same raw attempt twice.
- Daily accounting aggregates have no automatic time expiry.
- Deleting a workspace or service deletes its aggregates.
- A workspace deletion deletes that workspace's detailed request logs, raw
  usage rows, and daily accounting aggregates. This is the selected simple
  behavior because the user has no need to keep deleted-workspace history.
- Generated media uses the same retention duration. Media remains in
  Router-controlled object storage and is visible through a surviving detailed
  log or its owning media job until expiry.
- Tag order and duplicate input do not create different accounting groups. The
  Router stores one normalized set of tags for each request.

## Explicitly out of scope now

- Workspace cost limits.
- A Router-hosted user interface for a calling service.
- Calling-service domain rules and domain user interfaces.
- Local copies of reusable backend code or React components.
- Router-hosted agent execution and tool execution.
- Split control-plane and data-plane operation.
- Router-owned high-availability coordination.
- Durable status, cancellation, resume, and asynchronous results for normal
  model and embedding calls. Minimal media-generation jobs stay in scope.
- Hosted or embedded service administration pages.
- Service-owned provider connections and provider credentials.
- An OpenAI-compatible API.
- Fine-grained service or administrator permissions.
- Token exchange, mutual TLS product behavior, and host browser grants.
- Service and workspace disable, retire, restore, and cleanup workflows.

## Supersession rule

After the user approves all decisions, the project will replace the current
specification set and decision index with a small, consistent set. It will
then replace the Beads plan. Implementation must not start before that
approval.
