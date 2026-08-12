# Architecture interview

Status: In progress. The accepted answers are recorded below. Unanswered items
remain open.

This interview covers choices that change visible behavior, operating risk,
security, cost, or stored data. The recommended answer is first in each item.

## Round 1: product boundary and visible behavior

### 1. First release boundary

Accepted answer: Full platform. The first release includes the router, complete
optional agent harness, streaming, provider failover, accounting, shared tools,
global administration, and service-scoped administration. A service can use
the router without using the harness.

Recommendation: Include model routing, streaming, provider failover, usage
accounting, common external tools, the global administration application, and
the service-scoped graph. Include a small agent loop only if Crewday and FJ2 can
use one run protocol without moving their business tools.

- Full router and agent harness: It removes more duplicate work. It also makes
  the first release larger and makes run ownership and tool callbacks critical.
- Router and tools first: It gives earlier value and lower migration risk. The
  services keep duplicate agent-loop code for longer.
- Model router only: It is the smallest release. It does not meet the stated
  goal for shared search tools and an agent harness.

Resolved: Full platform with an optional harness.

### 2. Assignment override behavior

Accepted answer: The nearest scope replaces the complete fallback chain for
one named assignment. Partial inheritance-chain edits are not in the first
release.

Recommendation: The nearest scope replaces the complete fallback chain for one
named assignment. Add an explicit `extend` operation later only if a real use
case needs it.

- Replace: It is easy to read, validate, display, and roll back. A workspace
  needs to copy a chain when it wants one small change.
- Patch or merge: It makes small overrides concise. It can hide inherited
  changes and makes order, deletion, and review more difficult.
- Support both now: It is flexible. It increases the contract and UI size at
  the start.

Resolved: Replace the complete chain.

### 3. Service tree control

Accepted answer: Global administrators create services and parent links. A
service administrator controls its own assignments and workspaces.

Related accepted answer: Interactive global administration uses passkeys only.
There is no public sign-up, password, email sign-in link, or social sign-in.
An operator CLI creates a short-lived, one-use initial enrollment or recovery
URL. This follows the Ontology administration security model.

Recommendation: Global administrators create services and parent links. A
service administrator can change its own assignments and its workspaces, but
cannot create a new child service unless global policy delegates that action.

- Global tree control: It gives the clearest isolation and audit model. It adds
  work for the global administrator.
- Delegated child creation: It gives services more independence. A bad change
  can create unexpected scope and cost.
- Service-controlled tree: It is flexible. It weakens global governance.

Resolved: Global administrators.

### 4. Model selection escape hatch

Accepted answer: Normal production requests use assignments. A playground or
other approved diagnostic operation can select an exact provider-model through
a short-lived diagnostic permission. The router audits and accounts for the
request.

Recommendation: Normal production calls select an assignment. Permit an exact
provider-model only for a short-lived diagnostic permission and record it in
the audit and accounting data.

- Assignment only: It keeps policy and failover consistent. Diagnostics can be
  slower.
- Controlled exact model: It helps tests and provider evaluation. It can evade
  assignment policy if permissions are weak.
- Any caller can select a model: It is simple for callers. It removes much of
  the control-plane value.

Resolved: Permit controlled exact-model testing.

### 5. Shared administration interface

Accepted answer: Use the same base integration model as the planned Ontology
explorer. LLM Router hosts one React application. A service embeds its scoped
view in an isolated cross-origin frame through a one-use bootstrap handshake.
A headless API provides the same permitted functions.

Recommendation: Host one React application in LLM Router. Embed the
service-scoped view in an isolated frame. Also provide a headless API. Use a
short-lived grant and an exact origin allow-list.

- Hosted frame: One release serves React and non-React hosts. Isolation and
  release control are strong. Theme and host navigation integration are less
  direct.
- Custom element: It integrates better with the host page. CSS isolation,
  dependency packaging, and upgrades are more difficult.
- Separate implementations: Each host has full control. The interfaces will
  drift and duplicate work.

Resolved: Yes. Use the Ontology explorer base model.

## Round 2: request and failure behavior

### 6. Retry ownership

Accepted answer: After admission, LLM Router owns provider retries and
fallback. A calling service does not duplicate this logic. An uncertain client
timeout uses the same logical request identity and can remain pending until the
client obtains its state.

Recommendation: The official client retries only connection failures that
prove the router did not admit the request. After admission, the router owns
provider retries and fallback. An uncertain client timeout returns or queries
the same request ID; it does not start a second request.

- Router ownership: It reduces duplicate provider charges and agent runs. It
  needs request status and idempotency storage.
- Client ownership: It keeps router nodes simpler. Clients can duplicate work
  after uncertain failures.
- At-least-once blind retry: It has high availability. It can create duplicate
  side effects and charges.

Resolved: The router owns retries after admission. A pending uncertain state is
acceptable.

### 7. Streaming failover

Accepted answer: Fallback stops after model output or a tool continuation
becomes visible. The stream ends as interrupted and returns the stable request
identity.

Recommendation: Permit fallback only before the router releases model output
or accepts a tool-call continuation. After visible output, return an interrupted
stream with its request ID. Let the calling service or user decide how to
continue.

- Stop after output: It avoids mixed or duplicate answers. A user can see a
  partial answer.
- Restart transparently: It can complete more calls. It can repeat text, change
  meaning, or repeat tool effects.
- Buffer the complete response: It permits safe fallback. It removes useful
  streaming latency.

Resolved: Stop automatic fallback after visible output or effects.

### 8. Stale configuration

Accepted answer: A node can use its last valid normal configuration for up to
24 hours. Credential and security revocations use a separate urgent
distribution path.

Recommendation: A node can serve its last valid configuration during a control
plane outage for a service-defined time. Expired security revocations use a
separate urgent channel and can stop affected calls.

- Bounded stale service: It keeps local applications available. A recent route
  or price change can take time to apply.
- Fail closed when disconnected: It gives the strongest configuration
  consistency. A control-plane failure stops model work.
- Serve without a limit: It gives maximum availability. It can use revoked
  credentials or policy for too long.

Resolved: Use a 24-hour maximum.

### 9. Agent-run ownership

Accepted answer: Use durable resumable runs with one fenced owner. Keep remote
replication, lease renewal, and token checkpoint work asynchronous and off the
normal token-stream path. Strong coordination is permitted for run admission
and takeover. Local durability is permitted before an external effect that
must not run twice.

Recommendation: One node owns an agent run through a fenced lease and epoch. A
new node can resume durable state only after it fences the old owner.

- Durable resumable runs: They survive node failure. They need stronger
  coordination and durable run state.
- Node-local runs: They are much simpler. A node failure loses the run.
- Calling-service ownership: It keeps the router stateless. It leaves agent
  harness duplication in each service.

Resolved: Runs survive node failure without a material steady-state latency
cost.

## Round 3: tools, security, and data

### 10. Business tool execution

Accepted answer: Each service registers one fixed private tool-gateway
endpoint. LLM Router sends a short-lived, run-scoped grant. The service checks
the current user, workspace, approval, tool permission, and record state when
the tool runs. A request cannot supply an arbitrary callback URL.

Recommendation: A service registers one fixed tool-gateway endpoint. The
router sends a short-lived, run-scoped grant. The service checks the current
user, workspace, approval, and record state when each tool runs.

- Service gateway: It keeps business authority and data local. It requires a
  signed callback contract and service availability.
- Router executes all tools: It centralizes the harness. It moves domain code
  and credentials out of the service.
- Service executes the full loop: It has the smallest trust boundary. It keeps
  duplicate loop and provider code.

Resolved: Use one registered private tool gateway for each service.

### 11. Common external tools

Accepted answer: LLM Router owns shared adapters and routing for Brave,
ScrapingDog, Serper, SearXNG, and similar external tools. Services can use them
through the agent harness or call direct tool endpoints. Both paths use the
same routing, failover, authorization, budgets, and accounting.

Recommendation: The router owns adapters for Brave, ScrapingDog, Serper,
SearXNG, and similar external infrastructure tools. A service defines named
tool assignments, permitted operations, budgets, privacy classes, and
workspace overrides.

- Shared adapters with service policy: This is dry and auditable. The router
  handles more secret and personal data.
- Service-owned adapters: Service boundaries stay small. Adapter, retry,
  accounting, and security work stays duplicated.
- A separate tool-router service: It gives a clean future boundary. It adds an
  extra service and operational path now.

Resolved: Include shared external tools in LLM Router and provide direct
service endpoints.

### 12. Global administrator authentication

Accepted answer: LLM Router owns passkey-only global administrator identities.
There is no public sign-up or alternative interactive sign-in. A trusted
server CLI creates a short-lived, one-use enrollment URL for initial access or
recovery. Keep the operator flow aligned with Ontology.

Recommendation: Use a separate global-administrator origin and audience. Use
passkeys, recent authentication for sensitive changes, and no normal service
credential on this plane.

- Passkey only: It strongly resists phishing and avoids passwords. Recovery
  needs careful design.
- Existing identity provider plus passkey or hardware-key policy: It can use
  current staff lifecycle controls. It adds identity-provider integration.
- Password and TOTP: It is familiar. It is weaker against phishing.

Resolved: LLM Router owns passkey-only administrator identities.

### 13. Service authentication

Accepted answer: An administrator creates a show-once bootstrap secret. A
service exchanges it for short-lived, scoped access tokens. Mutual TLS is an
optional additional control for deployments that need it.

Recommendation: Support rotatable hashed service keys for simple deployment
and mutual TLS for higher assurance. Bind each credential to one service,
allowed routes, and optional source networks.

- Keys and mutual TLS: It supports different operations. It makes the security
  contract larger.
- Mutual TLS only: It gives strong machine identity. Certificate operation is
  more difficult.
- Keys only: It is easy to deploy. Key copying and theft are larger risks.

Resolved: Use short-lived scoped tokens with optional mutual TLS.

### 14. Content logging

Accepted answer: Capture complete prompt, response, search-query, and tool
content by default for the current public-data services. The setting is easy to
change by global, service, or workspace configuration. Continue to remove
credentials, authorization values, enrollment secrets, session tokens, and
other control-plane secrets before storage.

Recommendation: Keep prompts, responses, search queries, and tool input or
output off by default. Permit separate short-lived capture policies for a
service or workspace. Encrypt captured content and audit each read.

- Default off with controlled capture: It reduces privacy and breach risk. It
  can make rare defects more difficult to reproduce.
- Redacted samples by default: It improves diagnostics. Redaction can fail.
- Full content by default: It gives the best debugging data. It has high
  privacy, security, and storage risk.

Resolved: Full content capture is on by default for now and is configurable.

### 15. Retention targets

Accepted answer: Use the recommended periods as editable defaults. A global
administrator can set fleet defaults. A service and workspace can select an
approved shorter or longer value within global limits.

Recommendation for initial limits:

- diagnostic logs: 7 days;
- optional captured content: 7 days or less;
- raw request and provider-attempt accounting: 90 days;
- daily accounting aggregates: 2 years;
- security and global administration audit: 2 years;
- configuration revisions: the latest 100 revisions and all revisions from
  the last 2 years.

Short limits reduce storage and exposure. Long limits improve investigations,
cost analysis, and compliance evidence. Legal and customer contracts can
require different periods.

Resolved: Use the recommended editable defaults.

### 16. Data placement and recovery

Accepted answer: Each node uses an encrypted append-only local spool and sends
immutable events asynchronously to a central ledger with idempotent ingest.
S3-compatible storage is available for content segments, retention exports,
and archives. Bucket durability can be deployment configuration. Object
storage does not replace the live status, idempotency, lease, or fencing store.

Recommendation: Each node writes an append-only local spool before it reports
success. It sends immutable events to one control-plane ledger with idempotent
ingest. The control plane creates aggregates and retention exports. The local
spool is not the only durable copy after acknowledgement.

- Local spool plus central ledger: It supports offline nodes and recovery. It
  needs reconciliation and disk-pressure behavior.
- Shared central database for all nodes: It is simpler and more consistent. A
  network or database failure stops local operation.
- Multi-primary databases: They support local writes. Conflict and operation
  cost are high for the expected scale.

Resolved: Use a local spool, central ledger, and S3-compatible object storage
for suitable large or retained objects.

## Round 4: compatibility, operation, and source licensing

### 17. Public API shape

Accepted answer: Use a native versioned API as the primary contract. Also
provide an OpenAI-compatible interface for common model calls and migration.
The compatibility `model` value selects an assignment by default. Exact
provider-model selection remains a controlled diagnostic operation.

Recommendation: Make a native versioned API the primary contract. Add a small
official client for Python first. Add an OpenAI-compatible endpoint only for
migration and simple callers.

- Native plus compatibility: It supports the full product and easier
  migrations. Two surfaces need tests.
- OpenAI-compatible only: Many clients work quickly. Router-specific controls
  become headers and extensions with weak portability.
- Native only: It is clean. Existing callers need more migration work.

Resolved: Use a native primary API and an OpenAI-compatible interface.

### 17A. Official clients

Accepted answer: Provide official Python and TypeScript clients in the first
release. They own token exchange, local-first node selection, request identity,
safe retry, status recovery, streaming, and compatibility checks. Browser
TypeScript does not receive a service credential.

Resolved: Provide Python and TypeScript clients.

### 18. Deployment package

Accepted answer: Ship one immutable image that can run combined,
control-plane, data-plane, or worker roles. Provide production Compose first.
Add Kubernetes manifests or a Helm chart after the first stable release.

Recommendation: Ship one container image that can run control-plane, data-plane,
worker, or combined roles. Provide Compose first and Kubernetes examples after
the first stable release.

- One image with roles: It reduces release work and supports small or large
  installs. Role configuration needs to be clear.
- Separate images: Each image is small and explicit. Build and version work is
  duplicated.
- One combined process only: It is easiest to start. It limits independent
  scaling and failure isolation.

Resolved: One image with roles, production Compose first, and Kubernetes after
the first stable release.

### 19. License

Accepted answer: Use the Functional Source License, Version 1.1, ALv2 Future
License (`FSL-1.1-ALv2`) for LLM Router and Ontology. Each version becomes
available under Apache License 2.0 on the second anniversary of the date that
version is made available. The exact copyright and licensor notice is pending
the user's legal-name confirmation.

Recommendation: Apache License 2.0. It is permissive and includes an explicit
patent grant. It is longer than MIT and can be less familiar to small users.

- Apache-2.0: Good for service and company adoption, with patent terms.
- MIT: Very short and permissive, without an explicit patent grant.
- AGPL-3.0: It requires network users to share modified service source. Some
  companies will not adopt it.

Resolved choice: `FSL-1.1-ALv2`. License-file application is pending the exact
licensor name.

### 20. Compatibility promise

Accepted answer: Permit documented breaking changes before version 1.0. After
version 1.0, keep public interfaces compatible within a major version and
support the current and previous minor versions of the official clients during
normal upgrades.

Recommendation: Keep the native API stable within a major version. Permit
database and internal configuration changes without compatibility guarantees.
Support the current and previous minor client versions during normal upgrades.

- Strong compatibility: It gives callers confidence. It slows interface
  correction.
- Best effort until version 1.0: It permits fast design changes. Migrations can
  disrupt early users.
- Internal-only compatibility: It is easiest for development. It conflicts
  with a useful shared source-available project.

Resolved: Permit documented breaking changes before version 1.0, then use the
accepted major-version compatibility policy.

## Round 5: catalog, failures, budgets, credentials, and publication

### 21. Provider catalog ownership

Accepted answer: Use one shared provider-adapter and model catalog. Let global
or service scopes own provider instances, credentials, endpoints, and limits.
Eligible children inherit these items and can disable them without editing
their owner. Workspaces select eligible provider-model routes through
assignments.

Recommendation: Use the shared catalog with scoped provider instances. It
keeps common metadata dry and keeps provider-account ownership explicit.

- Shared catalog with scoped instances: It centralizes adapter and model
  updates. A faulty catalog change can affect many services.
- Service-owned catalogs: They give strong autonomy. They duplicate adapter,
  capability, and model metadata.
- Global instances only: They are lean. They cannot isolate provider accounts
  or commercial terms by service.

Resolved: Use a shared catalog with global and service provider instances.

### 22. Fallback error policy

Accepted answer: Normalize each failure and record its smallest known affected
scope. Before visible output or an external effect, provider credential,
provider-specific policy, candidate budget, compatibility, rate, and
availability failures use the next eligible fallback outside that scope.
Caller identity, router-wide policy, owning-scope hard budget, validation,
cancellation, and commit-boundary failures stop the logical request.

The router records the smallest known affected scope. A provider credential
failure skips later candidates that use the same credential but does not stop
an unrelated provider instance.

The administration graph shows separate authentication, policy, budget, rate,
availability, and request-compatibility indicators for a provider-model route
and assignment when the router knows those scopes. It also shows whether the
router retried, fell back, or stopped.

Recommendation: Use normalized defaults with assignment restrictions. This
keeps a provider-specific defect from stopping a healthy fallback without
letting fallback bypass a request-wide control.

- Candidate-scoped fallback: It improves availability. Error normalization and
  adapter tests become critical.
- Stop for all authentication, policy, and budget errors: It is simple. One
  broken candidate can stop the complete assignment.
- Try every candidate for every error: It maximizes attempts. It can evade
  policy, hide defects, and increase cost.

Resolved: Fall back for candidate-scoped authentication, policy, and budget
failures, with clear administration indicators. Stop for request-wide
failures.

### 23. Budgets and pricing

Accepted answer: Use inherited hard limits and warnings at global, service,
workspace, and assignment scopes. One logical request shares one budget across
all fallback attempts. Reserve an estimate before an attempt and reconcile it
with provider-reported usage and later corrections.

The FJ2 and Crewday review adds these price rules: price authority is explicit
for each provider-model route; manual prices can be pinned; scheduled and
on-demand refresh use one source snapshot; failures keep the last price;
administration shows per-row deltas and stale state; and each attempt stores an
immutable price version.

Recommendation: Use hierarchical limits and reservations. They prevent one
workspace or fallback chain from consuming an uncontrolled amount. The exact
cross-node reservation design needs a separate availability choice.

- Hierarchical hard limits: They give control at each ownership level. They
  need reservation and reconciliation.
- Alerts only: They are easy to operate. They do not stop uncontrolled spend.
- Service limits only: They are smaller. One workspace or assignment can
  consume the complete service allowance.

Resolved: Use hierarchical budgets and synchronized provider-model prices.

### 24. Credential storage

Accepted answer: Support only the built-in encrypted provider and shared-tool
credential store in the first release. Use envelope encryption and keep the
wrapping key outside the database and repository. Do not add external
credential-manager references.

Recommendation: A built-in store gives small deployments complete
administration. External references add a second custody and failure model.

- Built-in store only: It gives one workflow. The project owns encryption,
  wrapping-key rotation, backup, and recovery safety.
- Built-in and external stores: They support more operators. They increase the
  contract and test matrix.
- Deployment secrets only: They are simple. They do not support scoped central
  administration well.

Resolved: Use only the encrypted built-in store.

### 25. Configuration publication

Accepted answer: Validate and immediately publish each successful save as one
atomic immutable revision. Reject stale concurrent edits. Do not use drafts,
approvals, canaries, or promotion. Restore an earlier state by publishing a new
validated revision.

Recommendation before the decision: Use draft, validation, atomic publication,
staged rollout, and rollback. This reduces fleet-wide change risk but adds
several states and actions.

- Immediate validated publication: It takes one save. A valid but incorrect
  change can reach all eligible nodes.
- Staged publication: It supports canaries and approval. It makes normal
  administration more complex.
- Git-only publication: It gives review history. It weakens direct
  administration and slows urgent changes.

Resolved: Publish each valid save immediately and keep immutable revision
history.

## Follow-up rounds

The next interview covers cross-node budget enforcement, node discovery,
disaster recovery, privacy labels, UI workflows, and migration order for
Crewday, FJ2, and Xbot.
