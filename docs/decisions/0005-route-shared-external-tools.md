# Route shared external tools through LLM Router

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

The calling services use external search, extraction, scrape, and screenshot
providers. They need the same routing, failover, accounting, and policy model
as LLM providers.

## Decision

Put shared external-tool adapters and assignments in LLM Router. Provide both
agent-harness use and direct service endpoints. Apply the same routing,
failover, authorization, budgets, redaction, and accounting to both paths.

Keep business tools and their domain authorization in the calling service.

## Alternatives

- Keep all external adapters in calling services. This keeps a smaller router
  but duplicates adapters, safety controls, and accounting.
- Create a separate tool-router service. This gives a clean boundary but adds
  another deployment and failure path.

## Consequences

- The router handles more external credentials and potentially sensitive tool
  inputs.
- Services can make direct search calls without starting an agent run.
- Tool routing needs service, workspace, privacy, budget, and retention rules.
