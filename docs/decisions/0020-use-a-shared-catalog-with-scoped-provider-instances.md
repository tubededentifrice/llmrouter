# Use a shared catalog with scoped provider instances

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Provider adapters, model identities, and capabilities are common to all
services. Endpoints, credentials, limits, and commercial terms can belong to
the fleet or to one service tree.

## Decision

Keep provider adapter types and canonical model metadata in one shared
catalog. Let global and service scopes own provider instances. Let eligible
global and service scopes also own provider-model routes. Let eligible children
inherit instances, routes, and catalog entries and disable inherited items
without editing their owner.

Do not let workspaces own credentials or provider instances in the first
release. Workspaces select eligible routes through assignments.

## Alternatives

- Service-owned catalogs give strong autonomy but duplicate adapter and model
  metadata.
- A global-only provider catalog is lean but cannot isolate provider accounts
  and commercial settings by service.

## Consequences

- Provider and model metadata has one update path.
- Provider credentials and limits keep an explicit owner.
- Provider-model route settings keep an explicit owner.
- A faulty shared metadata change can affect many services.

## Migration effect

Crewday and FJ2 provider and model rows need mapping to canonical catalog
entries and scoped provider instances before their assignments move.

## Security effect

Inheritance cannot grant access outside the provider instance's eligible
service tree. A child cannot read or change an inherited credential.

## Review conditions

Review this decision if a workspace needs a separately owned provider account,
or if one canonical model identity cannot represent provider differences.
