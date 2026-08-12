# Retain terminal request recovery for 24 hours

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Idempotency and status records need enough time for client and node recovery.
They do not need the longer retention of accounting and audit records.

## Decision

Keep a request binding and status while it is nonterminal. After it becomes
terminal, keep its status and idempotency binding for 24 hours. Do not let this
short operational retention delete accounting, audit, or captured content
before their own retention rule.

An official client does not automatically submit an expired identity again. A
new intentional request uses a new UUIDv7. The router rejects an unknown UUIDv7
whose embedded time is outside its configured initial-age window. Thus, it does
not accept an expired identity as new work after the binding is removed.

## Alternatives

- Seven days gives a longer incident window but keeps operational lookup state
  longer.
- Ninety days aligns with some accounting data but is excessive for request
  recovery.

## Consequences

- Normal retries and next-day recovery have a bounded lookup window.
- Recovery after 24 hours uses durable accounting and audit evidence, not
  automatic request replay.
- Operators must make the expiry clear in status and support interfaces.
- Initial UUID age checks prevent a delayed replay from creating new work.

## Migration effect

Calling services must keep their own domain record for longer workflows and
must not treat router status as permanent domain storage.

## Security effect

Short lookup retention reduces the period for direct request-status access.
Authorization remains required during the full period.

## Review conditions

Review this decision if real incidents frequently need safe automatic recovery
after 24 hours.
