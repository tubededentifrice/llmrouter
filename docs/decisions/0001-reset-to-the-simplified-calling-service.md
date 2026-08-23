# Reset to the simplified calling service

- Status: accepted
- Date: 2026-08-23
- Decision owner: user

## Context

The earlier design combined model routing, agent runs, shared tools, durable
request recovery, hosted service administration, and distributed control. It
created many states and failure modes that the current calling services do not
need.

## Decision

Reset LLM Router to the product in the accepted
[service simplification source](../interviews/service-simplification-2026-08-23.md).
Use that source as the only product input for the replacement specifications.

Make LLM Router one normal model, embedding, and media calling service. Keep
the shared harness in OpenDLE Lib and reusable React components in OpenDLE UI.
Remove Router-hosted agents, shared external tools, service frames, token
exchange, durable model-request recovery, OpenAI compatibility, resource
revisions, and Router-owned distributed coordination.

## Alternatives

- Keep the earlier platform. This preserves implemented work but keeps the
  unwanted states and operation cost.
- Remove only selected features. This leaves overlapping contracts and makes
  the product boundary difficult to explain.

## Consequences

- The specification, API, Beads, and implementation plans need replacement.
- Calling services run the shared harness and their tools in their processes.
- Normal deployment tools supply application and database reliability.
- Git history remains the source for old design evidence.

## Review conditions

Review this decision only when a calling service has evidence that one removed
system is necessary as shared Router behavior.
