# Use local health circuits with fleet hints

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Routing needs a fast health decision on each node. Independent nodes can also
create a retry storm when one provider fails across the fleet.

## Decision

Let each data-plane node own its closed, open, and half-open health circuits
from local results. Let the control plane publish authenticated, expiring fleet
hints as advisory input. A hint cannot force a route or become a normal-path
dependency.

Keep circuit scope narrow enough to isolate a provider instance, route,
capability, and normalized failure class when they differ. Use bounded,
jittered, accounted half-open probes.

Only provider evidence changes health. Caller, policy, privacy, budget,
cancellation, and request-specific failures do not. A fresh hint can only open
a matching circuit or delay its next probe for at most five minutes.

## Alternatives

- A central health authority gives uniform state but adds a freshness and
  availability dependency to routing.
- Local-only circuits are fast but let many nodes probe one failed provider.

## Consequences

- Normal routing stays local.
- Fleet hints reduce coordinated retry load.
- Operators can see a different local state on different nodes for a short
  period.

## Migration effect

Calling services do not implement provider health circuits. They receive the
router result and can inspect scoped health through administration.

## Security effect

Hints are authenticated and expire. They cannot enable a route that policy,
budget, or credentials make ineligible.

## Review conditions

Review this decision if local state causes material route instability or fleet
hints do not prevent provider retry storms.
