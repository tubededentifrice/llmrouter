# Use graph and inspector administration

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Provider, model, route, assignment, inheritance, fallback, health, and error
relationships are difficult to understand across separate forms. Crewday has
the most advanced current graph reference.

## Decision

Use a searchable graph with side inspectors as the primary registry and
assignment administration workflow. Show inheritance, status, failures, and
fallback directly. Provide an accessible table with the same actions and data.

## Alternatives

- Tables first make bulk work direct but hide relationship context.
- Separate management pages are simple but make fallback and inheritance hard
  to inspect.

## Consequences

- One interface works for global and service-scoped administration.
- Graph layout and accessible non-visual operation both need tests.
- The table prevents graph interaction from becoming an access barrier.

## Migration effect

Services can embed the hosted view or use the headless API. A host does not
need React.

## Security effect

The graph and inspector use the same service, workspace, and administrator
authorization as the headless API.

## Review conditions

Review this decision if operators cannot complete common work quickly or the
graph becomes unusable at the supported catalog size.
