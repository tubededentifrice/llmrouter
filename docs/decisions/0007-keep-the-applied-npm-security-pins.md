# Keep the applied npm security pins

- Status: accepted and applied
- Date: 2026-08-13
- Decision owner: user

## Context

The frontend dependency tree needed exact transitive package versions that
contain applicable security fixes.

## Decision

Keep exact npm overrides for `brace-expansion` 5.0.9 and `nanoid` 5.1.16.
Keep the machine-checked dependency exception list empty and keep the complete
npm lock audit active.

## Alternatives

- Removing the overrides can restore vulnerable transitive versions.
- Ignoring advisories weakens the repository security gate.

## Consequences

The lock file has explicit compatible security versions. Normal dependency-age
rules stay active for future changes.

## Review conditions

Review this decision when direct dependency upgrades remove the need for an
override.
