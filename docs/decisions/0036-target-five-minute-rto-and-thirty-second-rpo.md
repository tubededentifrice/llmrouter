# Target five-minute RTO and 30-second RPO

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The warm-standby design needs measurable recovery objectives. Zero loss for all
state would require more synchronous work than the expected deployment needs.

## Decision

Target a 5-minute control-plane RTO and a 30-second RPO for general
asynchronously replicated control state in the high-availability profile.

Require zero acknowledged-event loss for admission bindings, ownership and
fencing changes, request terminal and cancellation states, commit-boundary
state, urgent security revocations, and canonical accounting and audit ingest.
Confirm these operations only after a standby or independent durable replay
journal can recover them.

Run automated high-availability tests before release and at least weekly. Run
an operator disaster-recovery exercise at least quarterly. Measure RTO from
loss of primary control writes through fenced client recovery. Measure RPO from
acknowledged commit times in the recovered state.

## Alternatives

- A 15-minute RTO and 5-minute RPO are easier to operate but increase outage
  time and recent-state loss.
- A 1-minute RTO and zero RPO for all state need synchronous quorum or similar
  infrastructure and add latency and operation complexity.

## Consequences

- Promotion, fencing, and replay must complete within a tested five-minute
  target.
- Some recent general control changes can need replay or operator re-entry.
- Critical acknowledged identities, safety state, and ledgers remain
  recoverable.

## Migration effect

Calling services use official client failover and status recovery. They do not
manage control-plane promotion.

## Security effect

Urgent revocations and fencing state use the zero-loss acknowledgment rule.
Recovery tests must not expose credentials or captured content.

## Review conditions

Review this decision if exercises miss either objective or measured business
impact needs a stronger target.
