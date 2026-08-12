# Use normal configuration for up to 24 hours

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Local router nodes must continue useful work during a temporary control-plane
outage. Unlimited stale use can keep revoked or unsafe policy active.

## Decision

Permit a node to admit requests with its last valid normal configuration for a
maximum of 24 hours. Distribute credential revocations and urgent security
changes through a separate high-priority path.

## Alternatives

- Stop immediately when the control plane is unavailable. This gives stronger
  configuration consistency but stops local model work.
- Use the last revision without a limit. This maximizes availability but can
  keep old policy active too long.

## Consequences

- A control-plane outage does not immediately stop local applications.
- Health and request records need revision age and stale-state data.
- Nodes stop affected admission when the 24-hour limit expires.
