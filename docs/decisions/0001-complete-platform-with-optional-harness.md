# Build the complete platform with an optional harness

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The project needs one router, agent harness, external-tool layer, accounting
system, and administration surface. Some calling services can still need to
own their agent loops.

## Decision

Build the complete router and agent harness in the first release. Include
streaming, failover, accounting, shared tools, global administration, and
service-scoped administration.

Make use of the harness optional. A service can use model and tool routing
without using the router's agent loop.

## Alternatives

- Build routing and tools first. This lowers first-release risk but keeps the
  agent harness duplicated for longer.
- Build model routing only. This is smaller but does not meet the shared-tool
  and harness goals.

## Consequences

- The first release has a larger implementation and verification scope.
- The request protocol and accounting model must support harness and direct
  callers without two routing implementations.
- A service can migrate in stages.
