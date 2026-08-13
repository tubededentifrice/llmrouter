# Use source-driven adapters with registered contracts

## Context

Crewday, FJ2, and Xbot have different provider and tool needs. A guessed
adapter list can omit active behavior or add unused work. Open documents can
also make adapter behavior unsafe to validate.

## Accepted choice

Audit the active source integrations first. Present one exact adapter matrix
for user approval before adapter implementation. Use registered, closed,
versioned schemas for adapter settings, requests, results, capabilities, and
business-tool gateway documents.

## Alternatives

- Implement a broad provider list from general popularity.
- Permit open provider-specific dictionaries in the public contract.
- Let each service define an unversioned callback format.

## Good effects

- The first release follows demonstrated service needs.
- Unknown fields and incompatible versions fail early.
- Gateway and adapter changes have explicit compatibility rules.

## Bad effects

- Source review and user approval block adapter implementation.
- Each supported schema needs a registry entry and conformance tests.

## Migration effect

Calling-service integrations must map to the approved registered documents.
Work for a calling-service repository stays in that repository.

## Security effect

Closed schemas reduce injection and confused-deputy risks. Tool grants and
current service authorization remain required.

## Review conditions

Review the matrix when a service adds an active integration. Add a schema major
version when a change is not backward compatible.
