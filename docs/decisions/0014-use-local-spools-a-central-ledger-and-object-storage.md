# Use local spools, a central ledger, and object storage

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Local router nodes need to continue during a central-network outage without
putting remote writes on the request path. The deployment also has inexpensive
S3-compatible storage with configurable bucket durability.

## Decision

Write immutable events to an encrypted append-only local spool before the
applicable success acknowledgement. Send events asynchronously to one central
ledger with idempotent ingest.

Support S3-compatible storage for encrypted content segments, retention
exports, and archives. Let deployments configure bucket durability within
documented minimums. Do not use object storage as the live request-status,
idempotency, lease, or fencing database.

## Alternatives

- Write all events directly to one central database. This is simpler but makes
  local operation depend on the central network and database.
- Use multi-primary databases. This supports local writes but adds conflict and
  operating complexity.

## Consequences

- Nodes need spool encryption, bounds, backpressure, replay, and repair.
- The central ledger needs immutable event IDs and duplicate rejection.
- Object storage needs manifests, checksums, lifecycle rules, and restore
  tests.
