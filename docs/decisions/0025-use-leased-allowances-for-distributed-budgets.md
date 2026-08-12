# Use leased allowances for distributed budgets

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Every request can need several shared hard-budget checks. A central reservation
on each admission adds network latency and makes a control-plane outage stop
local requests.

## Decision

Allocate bounded, expiring budget allowance leases to data-plane nodes. Let a
node admit locally while all applicable allowances are valid and sufficient.
Renew leases asynchronously and fence each lease generation.

Stop affected admissions when an allowance is empty or expired. Bound and show
the maximum late-correction risk from a provider charge that exceeds its
conservative reservation. Do not issue spendable allowance above the available
admission budget.

## Alternatives

- A central reservation for each request gives strict totals but adds latency
  and a central dependency.
- Eventual local counters give high availability but can exceed a hard limit
  by an uncontrolled amount.

## Consequences

- Normal admission does not wait for the control plane.
- A control-plane outage can continue only within existing allowances.
- Distributed admission cannot spend more than the issued allowance.
- A late provider usage correction can put a scope over its hard limit.

## Migration effect

Calling services send budget scope and read router enforcement state. They do
not implement distributed budget counters.

## Security effect

Authenticated lease identity, expiry, and fencing prevent a node from creating
or replaying allowance.

## Review conditions

Review this decision if usage-correction overage is unacceptable or lease
renewal adds material load.
