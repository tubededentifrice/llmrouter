# Use graduated spool-pressure shedding

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

A central ledger outage can fill a node's durable event spool. Continuing all
work can exhaust storage and put canonical accounting and audit records at
risk.

## Decision

Use warning, shedding, and stop-admission thresholds with recovery hysteresis.
Stop optional diagnostics and capture first. Then stop new background, batch,
playground, and agent work. Reject all new work before reserved emergency
capacity or canonical events are at risk.

Keep reserved capacity for admitted work, cancellation, reconciliation,
security operations, and canonical event delivery. Never silently drop or
overwrite a canonical accounting or audit event.

Reserve the maximum bounded canonical event load at admission. Under pressure,
disable optional capture only for new admissions and record and audit that
effective state. Do not change the capture policy of admitted work.

## Alternatives

- Immediate rejection at the first warning protects storage but reduces
  availability too early.
- Dropping only diagnostics preserves more work but can still exhaust storage
  during a long outage.

## Consequences

- Foreground work continues through early pressure.
- Optional content capture can be absent during a storage incident.
- A prolonged ledger outage stops all new requests before data integrity fails.

## Migration effect

Official clients must handle a retryable spool-pressure rejection and can fail
over to another eligible node.

## Security effect

Emergency capacity protects security audit and revocation work. Shed content
is marked, not silently absent.

## Review conditions

Review this decision if pressure incidents stop foreground traffic too early
or reserved capacity is not sufficient for safe shutdown and reconciliation.
