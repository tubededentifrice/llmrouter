# Ship only the public-data profile

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The current intended requests use public data. Private and regulated data need
additional provider eligibility, logging, deletion, and incident rules that no
current production use requires.

## Decision

Expose a versioned data-profile field and accept only the public-data profile
in the first release. Reject another profile. Require calling services to keep
protected private content outside this profile.

## Alternatives

- Public, internal, and restricted profiles now prepare for more services but
  add unused policy and test paths.
- No classification keeps the contract smaller but makes a later safe change
  harder.

## Consequences

- The initial privacy contract is small and explicit.
- A service with private data cannot send it to the router under this profile.
- A future profile needs an accepted contract change.

## Migration effect

Calling services classify each request. Xbot protected third-party data stays
outside LLM Router until an applicable profile exists.

## Security effect

The router still removes credentials and enforces encryption, access, capture,
and retention. The public label does not make secrets public.

## Review conditions

Review this decision before the first service needs private, personal,
regulated, or customer-confidential data.
