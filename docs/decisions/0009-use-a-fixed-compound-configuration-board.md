# Use a fixed compound configuration board

- Status: accepted
- Date: 2026-08-29
- Decision owner: user

## Context

Provider-to-model-to-assignment configuration has a fixed relationship. The
earlier flat presentation showed canonical models and provider-model mappings
as peer nodes. This made one canonical model look like several unrelated
records. It also made the provider route for an assignment rung difficult to
identify.

Crewday gives the accepted behavior and organization baseline. It groups
provider routes in one canonical-model card. FJ2 gives a compact reference for
the provider, model, and assignment order. Router still has its own native
identities, inheritance rules, states, and mutations.

## Decision

Use a fixed three-column relationship board for providers, canonical models,
and assignments. Use one compound canonical-model card with nested provider
route rows. Connect each assignment fallback position to the exact nested
route row.

Do not use a freeform draggable canvas and do not store visual positions. Keep
readable names primary and technical identities secondary. Keep the reusable
compound-board, relationship, keyboard, search, focus, and responsive behavior
in OpenDLE UI. Keep Router record projection, labels, state, inheritance,
permissions, and mutations in this repository.

## Alternatives

- Keep canonical models and provider routes as peer nodes. This hides their
  parent relationship and repeats model information.
- Use a freeform draggable canvas. This adds position work without adding
  configuration meaning.
- Replace the board with separate record pages. This removes the complete
  provider-to-route-to-assignment view.

## Consequences

- The shared relationship engine must support compound groups and connector
  endpoints on nested rows.
- The Router projection must give each assignment position an exact route
  endpoint and complete accessible relationship text.
- Wide and phone layouts keep one fixed information order. They do not need a
  saved layout model.
- Existing Router lifecycle and deletion rules do not change because of this
  presentation decision.

## Review conditions

Review this decision if configuration gains a second relationship shape that
cannot use the fixed columns, or if a user task requires meaningful manual
positioning.
