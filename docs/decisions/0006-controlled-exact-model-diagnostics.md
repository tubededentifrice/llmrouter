# Permit controlled exact-model diagnostics

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Normal calls need assignment policy and fallback. Administrators and service
developers also need to test one provider-model in a playground.

## Decision

Use assignments for normal production calls. Permit exact provider-model
selection through a short-lived diagnostic permission. Keep isolation,
privacy, budgets, rate controls, accounting, and audit active.

## Alternatives

- Permit assignments only. This is simpler but makes provider evaluation less
  useful.
- Let any caller select a model. This is flexible but bypasses routing policy.

## Consequences

- The playground can test a specific route.
- The permission and interface must clearly distinguish diagnostic calls.
- Diagnostic usage remains visible in cost and audit records.
