# Use a warm control-plane standby

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

A backup-only recovery takes longer than the desired failover. Multi-primary
writes add conflict handling that the expected scale does not need.

## Decision

Use one writable control plane with asynchronous replication to a warm
standby. Permit automatic promotion only through an external fencing mechanism
that prevents two writers. Use explicit operator reconciliation for failback.

Promotion creates a new control epoch and fences earlier run owners. A promoted
standby does not repeat an external effect with an uncertain result. Central
ingest acknowledges a canonical accounting or audit event only after a standby
or an independent durable journal can recover it.

Keep tested backups because a standby does not protect against every corrupt
or destructive change.

## Alternatives

- Backup and manual restore are simpler but have longer recovery time.
- Multi-primary operation reduces promotion time but adds write conflicts and
  much more operating complexity.

## Consequences

- Control-plane recovery is faster than full restore.
- Recent acknowledged writes can be inside a visible replication-lag window.
- Promotion and fencing become critical operations.
- Canonical event confirmation can use a batched protected replay write.

## Migration effect

Data-plane nodes use their configured primary and standby list. Calling-service
clients do not manage control-plane promotion.

## Security effect

Fencing prevents split-brain writers. Credential ciphertext and security state
need the same protected replication and audit controls as the primary.

## Review conditions

Review this decision when recovery objectives are accepted or measured lag
exceeds them.
