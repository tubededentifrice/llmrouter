# Use client-generated UUIDv7 request identity

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

A caller can lose the submission response and not know if the router admitted
the request. A router-generated identity cannot safely recover a timeout that
happens before the caller receives that identity.

## Decision

The official client creates one opaque UUIDv7 for each intentional logical
request. Before external work starts, the router durably binds it to the
authenticated service, optional workspace, and request fingerprint. The router
returns an admission receipt after that binding exists.

The binding uses a strongly serialized atomic create-if-absent operation across
eligible nodes. The fingerprint uses a versioned canonical request encoding
and SHA-256. A service-only request has no workspace in its scope.

A repeat with the same fingerprint returns the existing request. A repeat with
a different fingerprint fails as an identity conflict.

## Alternatives

- A client idempotency key plus a router ID gives separate roles but makes each
  request and support workflow use two identities.
- A router-only ID is small but cannot recover a timeout before its receipt.

## Consequences

- A client can safely repeat a submission after an uncertain timeout.
- Official clients must create correct UUIDv7 values and keep them with domain
  work.
- Status and binding state must be available across eligible router nodes.
- Each new logical request needs one strongly serialized admission operation.

## Migration effect

Calling services use the official client identity behavior. They keep the
router request identity on related domain records.

## Security effect

The ID contains no domain data and is not an access credential. Status and
repeat submission still require the original service and optional workspace
scope. The fingerprint excludes credentials and transport-only data.

## Review conditions

Review this decision if UUIDv7 support creates a client compatibility problem
or cross-node binding latency is material.
