# Let the router own retries and stop fallback after visible output

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Duplicated retry logic in calling services can create duplicate charges,
answers, tool effects, and agent runs. A transparent provider restart after
streamed output can also mix or repeat results.

## Decision

After admission, LLM Router owns provider retries and fallback. An uncertain
client uses the same logical request identity and can observe a pending state.

Permit automatic fallback only before output or an external continuation is
visible. After that boundary, end a failed stream as interrupted and return the
logical request identity.

## Alternatives

- Let clients own retries. This keeps the router smaller but duplicates logic
  and weakens accounting.
- Restart a visible stream with another provider. This can complete more calls
  but can repeat or contradict output and effects.
- Buffer the complete response. This permits safe fallback but removes normal
  streaming latency.

## Consequences

- Calling services remove provider retry and fallback logic.
- The router needs request identity, idempotency, status, and attempt records.
- A user can receive partial output with an explicit interrupted state.
