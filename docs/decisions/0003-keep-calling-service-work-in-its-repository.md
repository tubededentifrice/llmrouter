# Keep calling-service work in its repository

- Status: accepted
- Date: 2026-08-23
- Decision owner: user

## Context

Crewday, FJ2, and Xbot must change their local assignment and call behavior.
Those repositories have separate owners, data, release work, and tests.

## Decision

Keep Router specifications and implementation in this repository. Keep each
calling service's code changes and data migration in that calling service's
repository and planning process.

## Alternatives

- One cross-repository plan gives one view but mixes owners and task state.
- Calling-service changes in this repository would bypass their local safety
  and migration rules.

## Consequences

Shared contracts must be accepted before a calling service removes its local
behavior. Cross-repository delivery needs explicit coordination.

## Review conditions

Review this decision only if one atomic cross-repository release becomes
necessary.
