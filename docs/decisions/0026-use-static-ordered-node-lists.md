# Use static ordered node lists

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Small deployments know their local and remote router locations. A dynamic
registry or DNS layer adds operation and cache behavior that is not required
for the first release.

## Decision

Configure an ordered endpoint list in each official client. Prefer an eligible
loopback node and fail over through healthy endpoints in order. Configure
control-plane primary and standby endpoints explicitly on router nodes.

Do not add DNS discovery or a signed dynamic node registry in the first
release.

## Alternatives

- A signed registry supports central topology changes but needs registry
  expiry, publication, and health behavior.
- DNS is familiar but cached removal and failover are less exact.

## Consequences

- Discovery has no extra service.
- A topology change needs a deployment configuration update.
- Clients still need endpoint identity and health checks.

## Migration effect

Each calling-service deployment supplies its local and remote endpoint order
when it configures the official client.

## Security effect

Static configuration narrows eligible destinations. Endpoint identity checks
remain required. A configured endpoint is not trusted only by its address.

## Review conditions

Review this decision if fleet topology changes become frequent or manual
configuration causes incidents.
