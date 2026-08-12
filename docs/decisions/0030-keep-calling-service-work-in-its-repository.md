# Keep calling-service work in its repository

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Crewday and Xbot have no production runtime-data migration. FJ2 has existing
data and is the only eventual data migration. Shared-router planning must not
create implementation tasks that belong to another repository.

## Decision

Keep LLM Router specifications and implementation work in this repository.
Keep each calling service's code changes and migration work in that service's
repository and planning process.

Align Xbot product specifications with the accepted router boundary now.
Handle Crewday code alignment and FJ2 code and data migration separately. Do
not add their implementation or migration tasks to LLM Router.

## Alternatives

- One cross-repository migration plan gives one view but mixes owners and task
  lifecycles.
- Migrating FJ2 during router specification work gives early feedback but
  changes production-facing code before the shared contract is accepted.

## Consequences

- Repository ownership stays clear.
- Shared contracts can stabilize before a caller implementation starts.
- Cross-repository delivery needs explicit coordination later.

## Migration effect

FJ2 owns its eventual existing-data migration. Crewday and Xbot can align code
without a production data move.

## Security effect

Credentials and data do not move during specification alignment. Each later
migration uses its own repository safety controls.

## Review conditions

Review this decision only if one atomic cross-repository release becomes
necessary.
