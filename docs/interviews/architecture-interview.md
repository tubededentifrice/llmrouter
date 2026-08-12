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

Recommendation: A service registers one fixed tool-gateway endpoint. The
router sends a short-lived, run-scoped grant. The service checks the current
user, workspace, approval, and record state when each tool runs.

- Service gateway: It keeps business authority and data local. It requires a
  signed callback contract and service availability.
- Router executes all tools: It centralizes the harness. It moves domain code
  and credentials out of the service.
- Service executes the full loop: It has the smallest trust boundary. It keeps
  duplicate loop and provider code.

Question: Can each calling service expose one private tool-gateway endpoint?

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

Recommendation: Support rotatable hashed service keys for simple deployment
and mutual TLS for higher assurance. Bind each credential to one service,
allowed routes, and optional source networks.

- Keys and mutual TLS: It supports different operations. It makes the security
  contract larger.
- Mutual TLS only: It gives strong machine identity. Certificate operation is
  more difficult.
- Keys only: It is easy to deploy. Key copying and theft are larger risks.

Question: Do the first three services need mutual TLS, or are scoped rotatable
keys sufficient for the first release?

### 14. Content logging

Recommendation: Keep prompts, responses, search queries, and tool input or
output off by default. Permit separate short-lived capture policies for a
service or workspace. Encrypt captured content and audit each read.

- Default off with controlled capture: It reduces privacy and breach risk. It
  can make rare defects more difficult to reproduce.
- Redacted samples by default: It improves diagnostics. Redaction can fail.
- Full content by default: It gives the best debugging data. It has high
  privacy, security, and storage risk.

Question: Which services need content capture, and who can enable or read it?

### 15. Retention targets

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

Question: Do these initial periods match your operational and legal needs?

### 16. Data placement and recovery

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

Question: Can all servers reach one control-plane ledger most of the time, and
can they keep a local encrypted spool during an outage?

## Round 4: compatibility, operation, and open source

### 17. Public API shape

Recommendation: Make a native versioned API the primary contract. Add a small
official client for Python first. Add an OpenAI-compatible endpoint only for
migration and simple callers.

- Native plus compatibility: It supports the full product and easier
  migrations. Two surfaces need tests.
- OpenAI-compatible only: Many clients work quickly. Router-specific controls
  become headers and extensions with weak portability.
- Native only: It is clean. Existing callers need more migration work.

Question: Which client languages need official support in the first release?

### 18. Deployment package

Recommendation: Ship one container image that can run control-plane, data-plane,
worker, or combined roles. Provide Compose first and Kubernetes examples after
the first stable release.

- One image with roles: It reduces release work and supports small or large
  installs. Role configuration needs to be clear.
- Separate images: Each image is small and explicit. Build and version work is
  duplicated.
- One combined process only: It is easiest to start. It limits independent
  scaling and failure isolation.

Question: Does the first release need Kubernetes support, or is production
Compose support sufficient?

### 19. License

Recommendation: Apache License 2.0. It is permissive and includes an explicit
patent grant. It is longer than MIT and can be less familiar to small users.

- Apache-2.0: Good for service and company adoption, with patent terms.
- MIT: Very short and permissive, without an explicit patent grant.
- AGPL-3.0: It requires network users to share modified service source. Some
  companies will not adopt it.

Question: Which open source license do you want?

### 20. Compatibility promise

Recommendation: Keep the native API stable within a major version. Permit
database and internal configuration changes without compatibility guarantees.
Support the current and previous minor client versions during normal upgrades.

- Strong compatibility: It gives callers confidence. It slows interface
  correction.
- Best effort until version 1.0: It permits fast design changes. Migrations can
  disrupt early users.
- Internal-only compatibility: It is easiest for development. It conflicts
  with a useful open source project.

Question: Do you want a stable public contract before version 1.0, or can the
first releases make documented breaking changes?

## Follow-up rounds

After these answers, the next interview will cover provider catalog ownership,
fallback error classes, budgets, privacy labels, secret stores, configuration
publication, node discovery, disaster recovery, UI workflows, and migration
order for Crewday, FJ2, and Xbot.
