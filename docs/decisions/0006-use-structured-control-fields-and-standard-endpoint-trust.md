# Use structured control fields and standard endpoint trust

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Broad secret-pattern scans can reject valid model content and cannot prove
that arbitrary text has no secret. Custom provider endpoints also need a clear
trust rule.

## Decision

Keep credentials and authorization values in structured control fields that
never enter request-log content. Do not pattern-scan or rewrite arbitrary
model content.

For non-loopback HTTPS endpoints, use normal certificate-authority validation
and exact-hostname checks. Permit plain HTTP only on an explicit loopback
endpoint.

## Alternatives

- Broad content scanning has false results and changes trusted internal model
  data.
- Disabled certificate checks make endpoint identity unsafe.
- A required private trust system adds deployment work that current endpoints
  do not need.

## Consequences

Callers must keep control secrets out of model content. Endpoint configuration
cannot bypass standard transport identity checks.

## Review conditions

Review this decision if a supported provider needs a different authenticated
transport profile.
