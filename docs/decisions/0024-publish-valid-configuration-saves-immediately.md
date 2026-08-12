# Publish valid configuration saves immediately

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Draft, approval, canary, and promotion stages can reduce change risk, but they
make normal administration slower and more complex than the current services
need.

## Decision

Validate and immediately publish each successful configuration save as one
atomic immutable revision. Reject stale concurrent edits. Keep the active
revision unchanged after validation or storage failure.

Do not add drafts, approvals, canaries, or promotion in the first release.
Restore an earlier state by immediately publishing a new revision with that
validated content.

## Alternatives

- A staged workflow gives controlled rollout and preview but adds several
  administrator states and actions.
- Git-only publication gives review history but weakens direct administration
  and slows urgent changes.

## Consequences

- Normal configuration takes one save action.
- Immutable revisions still give audit, distribution state, and restoration.
- A valid but incorrect save can affect all eligible nodes as it propagates.

## Migration effect

Calling-service administration can map one successful save to one router
revision without adding an approval workflow.

## Security effect

Recent authentication, authorization, validation, concurrency checks, and
audit remain active. Urgent security changes keep their separate path.

## Review conditions

Review this decision if configuration incidents show a need for approval or
canary publication.
