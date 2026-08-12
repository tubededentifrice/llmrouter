# Use durable agent runs with asynchronous checkpoints

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The complete agent harness must survive a router-node failure. Per-token remote
coordination would add avoidable latency to every stream.

## Decision

Give each active run one fenced owner and epoch. Store enough durable state for
a new owner to resume the run after takeover.

Keep remote replication, normal lease renewal, token persistence, and ordinary
checkpoint work asynchronous or batched. Do not put remote consensus on each
token or stream chunk. Permit strong coordination at admission and takeover,
and require a local durable intent before an external effect that must not run
twice.

## Alternatives

- Keep runs node-local. This is faster to implement but loses runs on node
  failure.
- Coordinate every transition remotely. This simplifies some recovery cases
  but adds steady-state latency and a remote dependency.
- Let the calling service own recovery. This duplicates harness behavior.

## Consequences

- Steady-state streaming stays local and fast.
- Failover takeover can take longer than a normal run step.
- Recovery needs fencing, effect reconciliation, stream cursors, and tests for
  uncertain provider and tool results.
