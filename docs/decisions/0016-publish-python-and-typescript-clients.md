# Publish Python and TypeScript clients

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Calling services do not need to copy token exchange, local failover, admission,
status recovery, or stream handling. Current services use Python, and web or
Node integrations need TypeScript.

## Decision

Publish official Python and TypeScript clients in the first release. Separate
the TypeScript server and browser entry points. Never permit a service
credential in browser code.

## Alternatives

- Publish Python only. This lowers release work but leaves TypeScript behavior
  to each integration.
- Publish generated clients only. This covers HTTP shapes but not local-first
  routing and recovery behavior.

## Consequences

- Client releases and compatibility tests become part of the product.
- Calling services use the same safe request lifecycle.
- The browser client has a deliberately smaller authority surface.

## Migration effect

Crewday, FJ2, and Xbot can replace copied transport and failover logic with an
official client in separate, reviewed migrations.

## Security effect

The clients centralize token renewal and retry safety. Browser code cannot use
a service bootstrap secret or an unrestricted service token.

## Review conditions

Review the client set if a supported calling-service language cannot use an
official client, or if release coordination becomes a migration blocker.
