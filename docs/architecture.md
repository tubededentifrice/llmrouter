# Architecture working model

Status: Working model for discussion. No choice in this document is accepted.

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

Each layer can replace an assignment. The current recommendation is to make
providers and models reusable definitions, and to make assignments refer to an
ordered policy of model candidates. The specification needs to define
deletion, disablement, conflict, version pinning, validation, and rollback
behavior.

## Request model

A logical request has one stable request ID. It can contain multiple provider
attempts because of retry, fallback, or hedging. The recommended ledger keeps
the logical request separate from each attempt. This prevents duplicate
accounting and makes failures clear.

The normal caller selects a named assignment. An explicit model request can be
an administrator or diagnostic capability. The exact exception policy needs
user review.

## Agent and tool boundary

The calling service should own domain prompts, workflow decisions, user
approval, and the allow-list for each run. The router can own provider-neutral
run mechanics, model calls, tool-call loops, budgets, cancellation, timeouts,
and common tool adapters.

This boundary keeps product logic close to the product. It can still remove
duplicate execution and provider code. The interview will decide if the first
release includes full agent loops or only model routing and tool execution.

## Administration surfaces

The current recommendation is one hosted React administration application with
two permission modes:

- global administration for the full fleet;
- service-scoped administration for one service and its workspaces.

A host application can embed the service-scoped view in an isolated frame with
a short-lived, purpose-bound grant. A headless HTTP interface gives the same
permitted functions to hosts that need a native interface. This approach gives
Crewday and FJ2 the same experience without a React dependency in FJ2.

## Availability model

Each application server can use a router node on localhost. The client can use
an ordered set of remote nodes when the local node is not healthy. A node can
continue with its last valid configuration for a bounded time.

The specification needs to define duplicate suppression, retry ownership,
timeout budgets, stream interruption behavior, health probes, node draining,
and safe behavior when configuration is stale. Eventual consistency is
acceptable for fleet telemetry and most configuration distribution. Credential
revocation and security policy changes can need a stronger path.

## Data classes

Do not use one retention rule for all data.

- Audit records: small, durable, and append-only.
- Accounting records: durable logical request and attempt totals.
- Operational metrics: aggregated and suitable for a telemetry system.
- Diagnostic logs: bounded, sampled, and short-lived.
- Prompt, response, and tool content: disabled by default and separately
  controlled when enabled.
- Configuration snapshots: immutable revisions with bounded history.

The storage products and exact retention periods need user review.

## Public interfaces

The current recommendation is a native versioned HTTP interface with streaming
support and a formal contract. A small client library can manage identity,
timeouts, retries, local-first node selection, and stream handling. An optional
OpenAI-compatible endpoint can help migration, but it should not become the only
contract because it cannot express all assignment, agent, accounting, and
administration behavior cleanly.
