# Use a registered service tool gateway

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The router runs the shared agent protocol, but each calling service owns its
business tools, records, user decisions, and current authorization.

## Decision

Let each service register one fixed private tool-gateway endpoint. Send a
short-lived, one-use, run-scoped grant for each call. Make the service check
current authorization and state at execution time. Do not accept arbitrary
callback URLs in requests.

## Alternatives

- Move business tools into LLM Router. This centralizes execution but moves
  domain authority and credentials out of the service.
- Let each service run the full agent loop. This avoids callbacks but keeps the
  harness duplicated.

## Consequences

- Business data and final domain authority stay in the calling service.
- The router needs a signed gateway contract, bounds, retry rules, and audit.
- Service availability can affect an active tool-calling run.
