# Permit documented breaking changes before version 1

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The public contracts need real Crewday, FJ2, and Xbot migration experience
before they become stable.

## Decision

Permit documented breaking public-interface changes before version 1.0. After
version 1.0, require a new major version for breaking changes. During normal
upgrades, support the current and previous minor official-client versions.

## Alternatives

- Promise stability immediately. This gives early confidence but can preserve
  design errors.
- Make no compatibility promise. This is flexible but unsuitable for a shared
  service.

## Consequences

- Pre-1.0 releases need clear migration notes.
- Version 1.0 is an explicit contract-stability milestone.
- The server and official clients need a published compatibility table.

## Migration effect

Early adopters follow release notes and can need coordinated upgrades.
After version 1.0, normal upgrades can use the current or previous minor
official-client version.

## Security effect

The compatibility promise does not extend support for a release with a known
security defect. A security advisory can require an urgent client or server
upgrade.

## Review conditions

Review this decision before version 1.0 and before the project removes support
for any client version that the compatibility table lists.
