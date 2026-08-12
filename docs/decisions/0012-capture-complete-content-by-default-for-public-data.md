# Capture complete content by default for public data

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The current and planned first services operate on public data. Complete
request and tool content gives strong diagnostic information. Future services
can have different privacy needs.

## Decision

Enable complete prompt, response, search-query, provider-error, and tool
content capture by default for the current public-data profile. Make capture an
inherited global, service, and workspace setting that is easy to change.

Always remove credentials, authorization material, passkey enrollment secrets,
and control-plane secrets before storage. Encrypt captured content and audit
each read.

## Alternatives

- Keep content capture off by default. This lowers exposure but gives less
  diagnostic data.
- Store only redacted samples. This is smaller, but redaction can remove useful
  context or fail to remove sensitive content.

## Consequences

- Current operators get complete short-lived diagnostic evidence.
- Content storage can grow quickly and needs strict retention.
- A service that starts to handle non-public data needs a policy review before
  use.
