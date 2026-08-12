# Use fallback after candidate-scoped failures

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

A provider-model candidate can fail because of its credential, policy, price,
quota, compatibility, or availability. Another assignment candidate can still
complete the request. A caller or router-wide denial applies to all candidates
and cannot be solved by fallback.

## Decision

Normalize failures and record the smallest known affected scope, from one
attempt through a provider route, instance, credential, or assignment
candidate, up to the complete request.
Before visible output or an external effect, try the next eligible candidate
after a candidate-scoped authentication, policy, budget, rate, availability,
or compatibility failure.

Stop after a request-wide identity, authorization, policy, hard-budget,
validation, cancellation, or commit-boundary failure. Show normalized recent
errors and fallback decisions for providers and assignments in administration.

## Alternatives

- Stop for all authentication, policy, or budget errors. This is simple but
  loses service when only one candidate is affected.
- Try every candidate after every failure. This maximizes attempts but can
  evade request-wide controls and hide configuration defects.

## Consequences

- A broken provider credential does not stop a healthy fallback.
- Later candidates that share that broken credential are skipped.
- Error classification becomes part of adapter conformance testing.
- More attempts can increase latency and billable failed usage.

## Migration effect

Calling services remove local fallback decisions after admission. Their error
messages map to the router's normalized classes.

## Security effect

Fallback cannot bypass caller authorization or an effective policy that
applies to all providers. Provider error content remains redacted.

## Review conditions

Review this decision when a provider adds an ambiguous refusal class, or when
fallback after a provider policy refusal conflicts with a service policy.
