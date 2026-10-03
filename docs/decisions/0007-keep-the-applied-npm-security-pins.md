# Keep the applied npm security pins

- Status: accepted and applied
- Date: 2026-08-13
- Decision owner: user

## Context

The frontend dependency tree needed exact transitive package versions that
contain applicable security fixes.

## Decision

Keep exact npm overrides for `brace-expansion` 5.0.12 and `nanoid` 5.1.16.
Keep the machine-checked dependency exception list empty and keep the complete
npm lock audit active.

## Alternatives

- Removing the overrides can restore vulnerable transitive versions.
- Ignoring advisories weakens the repository security gate.

## Consequences

The lock file has explicit compatible security versions. Normal dependency-age
rules stay active for future changes.

On 2026-10-03, `brace-expansion` changed to 5.0.12 for the newer denial-of-service
fixes. The existing React Doctor CLI and rules stay in use.

## Review conditions

Review this decision when direct dependency upgrades remove the need for an
override.
