# Exchange service secrets for short-lived tokens

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Long-lived service secrets are high-value credentials. Sending them on each
normal request increases their network and log surface.

## Decision

Create a show-once bootstrap secret and store only its verifier. Exchange the
secret for short-lived tokens with exact service, workspace, operation,
audience, expiry, and credential-generation claims. Support optional mutual
TLS as an additional deployment control.

## Alternatives

- Require mutual TLS only. This gives strong machine identity but increases
  certificate operation work.
- Send a long-lived API key on each call. This is simple but exposes the secret
  more often.

## Consequences

- Clients need token caching, renewal, rotation, and revocation behavior.
- A stolen normal token has bounded scope and lifetime.
- Mutual TLS does not replace application scope checks.
