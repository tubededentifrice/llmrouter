# Use a PostgreSQL, FastAPI, and Vite foundation

## Context

The first implementation needs a transactional store, a Python HTTP framework,
and a frontend build tool. These choices affect data correctness, operations,
and repository structure.

## Accepted choice

Use PostgreSQL as the one logical transactional store for control state,
coordination, request state, the central ledger, audit, and database-backed
worker jobs. Use FastAPI on ASGI for backend HTTP operations. Use Vite for the
React and strict TypeScript frontend build. Pin exact eligible versions during
implementation setup.

## Alternatives

- Separate databases and a message broker could isolate workloads, but they
  would add distributed consistency and operating work.
- Another ASGI framework could reduce framework features, but it would require
  more contract and validation integration.
- Another frontend build tool could work, but it would add no accepted product
  benefit for the first release.

## Good effects

- One transactional authority supports request binding, leases, accounting,
  audit, and configuration invariants.
- FastAPI aligns with the accepted Python backend and OpenAPI contract.
- Vite gives one small build path for both administration modes.

## Bad effects

- PostgreSQL carries several workload types and needs careful isolation.
- Database-backed jobs can need later partitioning at high scale.
- The service depends on Python and Node.js toolchains.

## Migration effect

There is no product data to migrate. The initial schema and deployment must
support primary and standby PostgreSQL roles and exact dependency pins.

## Security effect

Database roles must use least privilege. FastAPI and Vite dependencies remain
subject to exact pins, the dependency-age rule, and security gates.

## Review conditions

Review this decision if measurements show that one store cannot meet accepted
isolation, throughput, availability, or recovery targets.
