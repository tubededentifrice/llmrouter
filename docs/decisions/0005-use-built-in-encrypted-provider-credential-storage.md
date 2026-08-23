# Use built-in encrypted provider credential storage

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The Router needs provider credentials. A required external secret manager
would add another dependency for small deployments.

## Decision

Use one built-in envelope-encrypted credential store. Supply the wrapping key
as a deployment secret outside the database and repository. Do not support an
external credential-manager reference in the first release.

## Alternatives

- Deployment-secret-only credentials make administration and rotation harder.
- Required external secret management adds operation work and another failure
  path.
- Both forms increase the first-release contract and test matrix.

## Consequences

The project owns encryption, key rotation, backup safety, and write-only
credential administration. A database backup without the wrapping key cannot
decrypt provider credentials.

## Review conditions

Review this decision if an operator requires hardware-backed or external key
custody.
