# Use built-in encrypted credential storage

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The router needs provider and shared-tool credentials. A required external
secret manager would make small deployments harder and would add another
operating dependency.

## Decision

Support only the built-in encrypted credential store in the first release. Use
envelope encryption with a wrapping key supplied as a deployment secret
outside the database and repository. Do not support external credential-manager
references.

Distribute only the credentials required by an active data-plane route. Keep
decrypted values in memory for a bounded time and use the urgent path for
rotation and revocation.

## Alternatives

- A built-in store plus external references is flexible but increases the
  credential contract and test matrix.
- Deployment secrets only are simple but do not support controlled
  administration and scoped distribution.
- A mandatory external secret manager gives strong central custody but makes
  local deployment harder.

## Consequences

- One credential workflow serves all supported deployments.
- The project owns encryption, key rotation, backup, and recovery safety.
- Operators still supply and protect one deployment wrapping key.

## Migration effect

Provider credentials move through a write-only import or rotation operation.
Calling-service credentials do not move until that service migration starts.

## Security effect

Database and object-storage backups do not contain usable plaintext
credentials. Loss of the wrapping key makes encrypted credentials unavailable.

## Review conditions

Review this decision if an operator requires hardware-backed or external key
custody, or if regulatory controls prohibit built-in credential storage.
