# Keep administrator sessions for seven days

- Status: accepted
- Date: 2026-08-22
- Decision owner: user

## Context

The Router administration cookie is a browser-session cookie. A browser removes
it when the person closes the browser. The Router session also has a 15-minute
idle limit and an eight-hour absolute limit. Thus, a person must sign in again
after each browser restart and at least once every eight hours.

Pocket ID checks the human identity. Router checks the Pocket ID session at
least every five minutes. Sensitive actions need authentication that is no more
than five minutes old. These controls can keep a longer local session safe.

## Decision

Keep one Router administrator session for no more than seven days. Use a
persistent, host-only cookie with the same maximum lifetime. Set both the idle
limit and the absolute limit to seven days. Activity must not extend the
absolute limit.

Keep the existing five-minute Pocket ID session check, refresh-token rotation,
access-token introspection, recent-authentication rule, local revocation, and
explicit logout behavior.

## Alternatives

- Keep a browser-session cookie. This has a smaller persistence risk, but it
  makes a person sign in after each browser restart.
- Keep the eight-hour absolute limit. This reduces the lifetime of a stolen
  local cookie, but it does not give the requested seven-day session.
- Use a sliding seven-day limit without an absolute limit. This reduces sign-in
  work, but activity can keep a stolen session valid without a fixed end.

## Consequences

- A browser restart does not end a valid Router session.
- A person normally signs in once in each seven-day period.
- Explicit logout, local revocation, or Pocket ID rejection can end the session
  before seven days.
- Sensitive actions still need authentication that is no more than five minutes
  old.
- A stolen local cookie has a longer possible lifetime than before.

## Migration effect

The database migration replaces the 15-minute and eight-hour session checks
with seven-day checks. A rollback revokes and shortens a session that does not
fit the old limits.

## Security effect

The persistent cookie increases the time in which theft of the cookie can
matter. The cookie stays `Secure`, `HttpOnly`, `SameSite=Lax`, host-only, and
without a `Domain` attribute. Pocket ID rejection must make the local session
unusable within five minutes. Activity cannot extend the seven-day absolute
limit.

## Review conditions

Review this decision if administrators use shared devices, if session theft
occurs, if Pocket ID checks cannot revoke a local session within five minutes,
or if administrators need a different session duration.
