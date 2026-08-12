# Let the nearest scope replace an assignment chain

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

A service inherits from one parent. A workspace inherits from its service.
Partial list merge rules can make the effective fallback order difficult to
understand and review.

## Decision

For one named assignment, use the complete chain from the nearest layer that
defines it. Do not merge it with the inherited chain. Do not support partial
chain edits in the first release.

## Alternatives

- Patch inherited chains. This makes small edits shorter but hides inherited
  behavior and makes deletion and ordering more complex.
- Support replacement and patches. This gives flexibility but increases the
  API, validator, and interface size.

## Consequences

- Effective assignments are deterministic and easy to display.
- A workspace must copy a chain to change one entry.
- A future chain-extension feature needs a new explicit contract.
