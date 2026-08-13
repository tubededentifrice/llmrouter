# Use bounded attempt timeouts and node draining

## Context

Fallback and shutdown need fixed bounds. An unbounded attempt or drain can hold
work, budget, leases, and deployment progress indefinitely.

## Accepted choice

Permit no more than eight provider candidates and attempts. Limit one attempt
to 120 seconds and one logical execution to 15 minutes. Shorten or skip a late
attempt when needed. Use a 15-minute default drain and a 30-minute maximum.
Transfer work only through fenced ownership and keep an unconfirmed effect
uncertain.

## Alternatives

- Use only one logical deadline without an attempt limit.
- Let each provider set an unbounded timeout.
- Stop a node immediately or wait without a drain limit.

## Good effects

- Capacity and spool reservations have deterministic limits.
- Fallback cannot extend execution without a fixed ceiling.
- Deployments have a bounded safe-stop procedure.

## Bad effects

- A provider operation longer than 120 seconds cannot finish in one attempt.
- Some work can become uncertain at the drain limit.

## Migration effect

Assignment validation, request contracts, clients, workers, and operational
interfaces must use the accepted attempt, logical, and drain limits.

## Security effect

Fixed limits reduce resource exhaustion. Fenced transfer prevents two owners
from repeating one external effect.

## Review conditions

Review these limits only with measured provider and deployment evidence and a
new bound that preserves reservation and recovery guarantees.
