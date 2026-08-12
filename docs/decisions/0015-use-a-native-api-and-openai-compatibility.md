# Use a native API and OpenAI compatibility

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The full router needs request identity, assignments, agent runs, tools,
accounting, recovery, and administration. Existing clients also benefit from a
familiar OpenAI-compatible migration surface.

## Decision

Make a native versioned API the primary contract. Also provide a tested
OpenAI-compatible interface for accepted common operations. Let the compatible
`model` field name an assignment by default. Keep exact provider-model use
behind the diagnostic permission.

## Alternatives

- Use only an OpenAI-compatible API. This improves initial client reuse but
  forces router behavior into custom extensions.
- Use only the native API. This is clean but increases migration work.

## Consequences

- Two public surfaces need contract and conformance tests.
- Router-specific behavior has one clear native source contract.
- Common OpenAI clients can migrate without bypassing routing policy.

## Migration effect

Calling services can first use the compatibility interface. They can move to
the native interface when they need router-specific behavior.

## Security effect

Both interfaces use the same identity, isolation, diagnostic permission, and
audit controls. Compatibility does not create a policy bypass.

## Review conditions

Review this decision if compatibility cannot represent common migrations
without unsafe behavior, or if two public surfaces cause excessive defects.
