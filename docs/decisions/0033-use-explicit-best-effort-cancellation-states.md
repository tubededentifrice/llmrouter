# Use explicit best-effort cancellation states

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

A caller can request cancellation after provider or tool work starts. Some
external systems cannot confirm a stop, and a visible effect cannot be undone
by changing the router state.

## Decision

Record `cancel_requested` before stopping new work. Report `cancelled` only
after active work is confirmed stopped. Keep `cancel_requested` while bounded
reconciliation can make progress. Finish as `uncertain` if an active provider
or tool effect cannot be confirmed. Keep partial output, committed effects,
usage, and cost visible.

Do not treat a caller disconnect as confirmed cancellation.

Require an explicit cancel permission. Stop every new provider, fallback,
agent, tool, and external-effect action. Write an immutable audit event for the
request, actor, state change, and adapter stop result.

## Alternatives

- Immediate `cancelled` gives a fast response but can be false while external
  work continues.
- Detach-only behavior is simple but can continue spend after a user asks to
  stop.

## Consequences

- The interface can show an honest cancellation state.
- Adapters need stop and reconciliation behavior when their provider supports
  it.
- Cancellation can finish as uncertain and can still have billable usage.
- Cancellation adds an audited mutation permission and adapter stop contract.

## Migration effect

Calling services must show or map `cancel_requested`, `cancelled`, and
`uncertain` without claiming rollback.

## Security effect

Cancellation and status reads require the same service and workspace scope as
the request. A cancellation cannot be used to inspect another caller's state.

## Review conditions

Review this decision when provider cancellation support or effect
reconciliation becomes reliable enough for stronger guarantees.
