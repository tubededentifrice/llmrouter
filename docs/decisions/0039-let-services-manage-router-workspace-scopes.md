# Let services manage their router workspace scopes

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Xbot owns its user-facing workspace life cycle. Each xbot workspace needs a
matching Ontology workspace and LLM Router workspace scope. A central operator
must not prepare each scope before the xbot workspace can become active.

## Decision

Let a registered service use narrow service-management operations to create,
read, disable, restore, and retire its router workspace scopes. Require a
short-lived token, an idempotency key, and an opaque caller reference. Keep
these permissions separate from request, content, accounting, and
configuration permissions.

Retirement stops new work but does not delete retained router records. Normal
router retention controls their expiry. Never reuse a retired workspace
identity.

## Alternatives

- Require a global administrator for each workspace. This makes xbot setup and
  recovery depend on a separate operator action.
- Let a normal workspace token manage the workspace. This mixes data-plane and
  service-management authority.

## Consequences

- Xbot can supply one workspace creation and retirement flow.
- Partial cross-service provisioning still needs xbot reconciliation.
- Router accounting, audit, and capture can remain after workspace retirement.

## Migration effect

No deployed workspace records need migration.

## Security effect

The management token has no request or data permission. Each operation is
idempotent and audited.

## Review conditions

Review this decision if a service must transfer a workspace to another
service or if legal deletion must remove router technical records before their
normal expiry.
