# Architecture working model

Status: Working model. Accepted choices link to decision records. Other choices
remain proposals.

## Main split

The current recommendation is to separate a control plane from replicated data
plane nodes.

The control plane owns configuration revisions, credentials, permissions,
global administration, audit policy, and fleet state. A data plane node serves
application requests, keeps a validated local configuration snapshot, manages
provider health, executes approved tools, and writes local telemetry.

This split gives a fast local path and permits many nodes. It also makes the
consistency rules more complex. The recommended configuration revision is
immutable, authenticated, and safe to use while the control plane is not
available.

## Configuration model

The proposed effective configuration has these ordered layers:

1. router defaults;
2. root service;
3. each child service in one parent chain;
4. workspace overrides that the effective service controls.

For one named assignment, the nearest layer replaces the complete inherited
fallback chain. Partial chain edits are not in the first release. Providers
and models are reusable definitions. Assignments refer to an ordered policy of
model candidates. The specification needs to define
deletion, disablement, conflict, version pinning, validation, and rollback
behavior.

## Request model

A logical request has one client-generated opaque UUIDv7. Before external work,
the router durably binds it to the authenticated service, optional workspace,
and request fingerprint. It can contain multiple provider attempts because of
retry, fallback, or hedging. The ledger keeps the logical request separate from
each attempt. This prevents duplicate accounting and makes failures clear.

Admission uses one strongly serialized create-if-absent operation across the
fleet. The router rejects a first submission whose UUIDv7 time is outside its
configured initial-age window. This prevents reuse after the 24-hour binding
expires without a long-lived idempotency tombstone.

The normal caller selects a named assignment. A playground or another approved
diagnostic operation can select an exact provider-model through a short-lived
permission. The router still applies isolation, policy, budgets, accounting,
and audit.

After admission, the router owns provider retries and fallback. A client uses
the same request identity after an uncertain timeout. Terminal status and its
idempotency binding remain available for 24 hours. Automatic fallback stops
after streamed output or an external effect becomes visible.

Cancellation first records a request and stops new work. It reports cancelled
only after active work is confirmed stopped. It reports uncertain when a
provider or tool effect cannot be confirmed. Cancellation does not undo output
or an external effect that is already visible. Cancellation needs a mutation
permission and writes an immutable audit event.

Provider adapters normalize failures and record the smallest known affected
scope. Before the visible-output boundary, provider credential, provider
policy, candidate budget, compatibility, rate, and availability failures can
move to an eligible candidate outside that scope. Caller identity, router-wide
policy, owning-scope budget, cancellation, and commit-boundary failures stop
the logical request.

Provider health circuits are local to each data-plane node for fast routing.
The control plane publishes authenticated, expiring fleet hints that reduce
retry storms but do not control the request path. Circuits isolate route and
failure scopes and use bounded half-open probes.

Hard budgets form an inherited global, service, workspace, and assignment
chain. One logical budget covers its fallback attempts. Admission reserves an
estimate, and final provider usage reconciles that reservation. Data-plane
nodes consume bounded, expiring allowance leases locally and renew them
asynchronously. Total issued allowance stays inside the available admission
budget. Only a provider usage correction above its conservative reservation
can put a hard-limit scope over its limit.

## Agent and tool boundary

The calling service should own domain prompts, workflow decisions, user
approval, and the allow-list for each run. The router can own provider-neutral
run mechanics, model calls, tool-call loops, budgets, cancellation, timeouts,
and common tool adapters.

This boundary keeps product logic close to the product. It can still remove
duplicate execution and provider code. The first release includes the complete
harness, but each service can use router functions without the harness.

The router also owns approved common external-tool adapters. A service can use
them from the harness or through direct endpoints. Business tools and current
domain authorization stay in the calling service.

One node owns each agent run with a fenced lease and epoch. A new node can
resume durable state after takeover. Remote replication, normal lease renewal,
and token checkpoints stay asynchronous or batched. The token-stream path does
not wait for remote consensus on each chunk.

## Administration surfaces

The accepted design uses one hosted React administration application with two
permission modes:

- global administration for the full fleet;
- service-scoped administration for one service and its workspaces.

A host application can embed the service-scoped view in an isolated frame with
a short-lived, purpose-bound grant. A headless HTTP interface gives the same
permitted functions to hosts that need a native interface. This approach gives
Crewday and FJ2 the same experience without a React dependency in FJ2.

The frame uses the same base security model as the planned Ontology explorer,
but it has an independent protocol namespace and version.

Global interactive administration uses the same shared Pocket ID deployment as
Ontology. Pocket ID owns human accounts, passkeys, enrollment, and recovery.
Each application is a separate OpenID Connect client and keeps its own
administrator grants, server-side sessions, authorization, and audit. Public
sign-up and non-passkey authentication are disabled.

## Availability model

Each application server can use a router node on localhost. The client can use
an ordered set of remote nodes when the local node is not healthy. A node can
continue with its last valid normal configuration for up to 24 hours. Urgent
credential and security revocations use a separate high-priority path.

Official clients discover data-plane nodes from an ordered static endpoint
list. Router nodes use a static primary and standby control-plane list. The
high-availability profile uses one writable control plane and an asynchronously
replicated warm standby with fenced automatic promotion.

The high-availability targets are a 5-minute control-plane RTO and a 30-second
RPO for general replicated state. Acknowledged admission, fencing, urgent
revocation, accounting, and audit events have a zero-loss recovery rule.

The specification still needs to define timeout budgets, client endpoint
health probes, node draining, and recovery from an unconfirmed external effect.
Eventual consistency is acceptable for fleet telemetry and most configuration
distribution. Credential revocation and security policy changes use a separate
urgent distribution path.

## Data classes

Do not use one retention rule for all data.

- Audit records: small, durable, and append-only.
- Accounting records: durable logical request and attempt totals.
- Operational metrics: aggregated and suitable for a telemetry system.
- Diagnostic logs: bounded, sampled, and short-lived.
- Prompt, response, search-query, and tool content: enabled for the current
  `service-data` profile, separately controlled, and short-lived.
- Configuration snapshots: immutable revisions with bounded history.

The initial retention defaults are accepted and remain editable configuration.
The live ledger and coordination storage products remain open. S3-compatible
object storage is accepted for suitable content and archive objects.

Complete content capture is enabled by default for the current `service-data`
profile. It is inherited configuration at global, service, and
workspace levels. Credentials and control-plane secrets are always removed
before storage. Content reads are permission-controlled and audited.

Each node writes canonical events to an encrypted append-only local spool. It
sends them asynchronously to one logical central ledger with idempotent ingest.
S3-compatible object storage holds encrypted content segments, retention
exports, and archives. It is not the live request-status, idempotency, lease,
or fencing store.

Spool pressure uses graduated shedding. Nodes stop optional logs and capture,
then background work, then all new admission before canonical events are at
risk. Reserved capacity remains for admitted work, cancellation, security, and
reconciliation.

The shared model catalog is global. Provider instances and credentials have a
global or service owner and can be inherited by eligible descendants. Pricing
is explicit on each provider-model route. A scheduled or manual refresh uses
one immutable source snapshot, updates only price state, and does not rewrite
past accounting.

Provider and shared-tool credentials use the built-in envelope-encrypted
store. A wrapping key stays outside the database. External credential-manager
references are not in the first release.

Each valid configuration save publishes one atomic immutable revision
immediately. There is no draft, approval, canary, or promotion state. A restore
publishes another revision with validated earlier content.

The first release accepts only the versioned `service-data` request profile.
It can process public or private content that the calling service is authorized
to use. It excludes control secrets. Captured content is a router technical
copy that expires under router retention and is not available in service-
scoped administration. The administration application uses the provider and
assignment graph with side inspectors as its primary workflow and provides an
accessible table alternative.

## Public interfaces and packaging

The accepted design uses a native versioned HTTP and streaming API as the
primary contract. A tested OpenAI-compatible interface supports accepted common
operations and migration without bypassing assignment policy.

Official Python and TypeScript clients manage token exchange, local-first node
selection, request identity, safe admission retry, status recovery, and stream
handling. Provider retry and fallback remain in the router.

One immutable container image runs combined, control-plane, data-plane, or
worker roles. Production Compose is the first stable deployment package.
Kubernetes packaging follows after the first stable release.
