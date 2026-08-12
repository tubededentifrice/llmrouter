# Use hierarchical budgets with synchronized prices

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

One logical request can create several provider attempts and tool calls.
Provider prices can change independently of a software release. A fallback
must not reset cost limits or rewrite old accounting.

## Decision

Use inherited hard limits and warning thresholds at global, service,
workspace, and assignment scopes. Reserve estimated cost before each attempt,
share one logical budget across fallback, and reconcile against reported
usage.

Make price authority explicit for each provider-model route. Synchronize typed
prices on a configurable weekly schedule and on demand. Support manual pins,
dry-run deltas, per-row errors, and stale indicators. Keep the last accepted
price on a synchronization failure. Snapshot the applied price version on
every attempt and append later corrections.

## Alternatives

- Alert-only budgets avoid admission coordination but permit uncontrolled
  spend.
- Service-only hard limits are simpler but one workspace or assignment can
  consume the complete service allowance.
- Mutable current-price accounting is small but changes historical cost when
  a price refresh occurs.

## Consequences

- Fallback cannot multiply the logical request's budget.
- Admission needs a conservative estimate and a reservation mechanism.
- Pricing supports non-token units and historical price evidence.

## Migration effect

FJ2 and Crewday price-source fields and manual pins can map to router
provider-model routes. Their historical usage remains unchanged during
migration.

## Security effect

Budget reads and changes follow service and workspace isolation. A price-source
response is untrusted configuration input and needs validation and bounds.

## Review conditions

Review this decision after invoice reconciliation measures estimation error,
or if cross-node reservations add unacceptable request latency.
