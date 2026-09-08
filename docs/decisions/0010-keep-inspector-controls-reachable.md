# Keep inspector controls reachable

- Status: accepted
- Date: 2026-09-08
- Decision owner: user

## Context

Two inspector rules prevented access to controls in measured browser cases.
At a `1100 × 800` viewport, wrapped toolbar actions extended below the fixed
overlay start position. The inspector covered two actions. At a `412 × 1000`
viewport and 200% text, a title of 200 `W` characters made the fixed header too
tall. The footer extended outside the inspector.

The user approved both recommended changes on 2026-09-08. The
[shared compact graph inspector specification](../specs/04-authentication-administration-and-shared-ui.md#shared-compact-graph-inspector)
owns the placement, fit, scrolling, focus, and verification requirements.

## Decision

Place the overlay below the rendered toolbar when actions wrap. Keep the
actions directly visible and use the remaining height for the inspector.

When the fixed title leaves insufficient room for content and controls, scroll
the title and details together. Keep Close and footer actions fixed. Keep the
existing fixed header when it fits.

OpenDLE UI owns both behaviors so Router, Ontology, and Xbot use one solution.

## Alternatives

- Keep the fixed overlay start and put toolbar actions in a menu. This keeps
  more inspector height, but adds a step to find and use those actions.
- Keep an extreme title fixed and shorten its visible text. This saves space,
  but makes the complete title harder to read and compare with the details.
- Scroll the complete inspector, including Close and footer actions. This
  makes room for the title, but moves essential controls out of view.

## Consequences

- All toolbar actions remain directly available. A wrapped toolbar leaves less
  height for inspector details and can require more local scrolling.
- The complete title remains available. An extreme title can scroll out of
  view while the user reads details. Close and footer actions stay available.
- The shared implementation measures rendered content and responds to changes
  in text and available size. Consumers do not set local layout exceptions.
- Browser checks cover wrapped actions and extreme text as well as ordinary
  layouts. These changes do not change stored data or service permissions.
