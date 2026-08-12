# Product and boundaries

Status: Accepted sections only. Other product behavior remains open.

## Complete platform

LLM Router MUST provide all of these shared functions in its first release:

- model-provider and external-tool routing;
- ordered fallback and provider health handling;
- non-streaming and streaming model calls;
- a complete agent harness;
- request, attempt, tool, token, and cost accounting;
- global administration;
- service-scoped administration;
- direct endpoints for shared external tools.

Use of the agent harness MUST be optional for each service and for each
eligible request. A service MUST be able to use routing, streaming, failover,
accounting, tools, and administration without moving its agent loop into LLM
Router.

LLM Router MUST NOT require a calling service to move domain prompts, business
workflow decisions, domain records, user authorization, or business-tool
implementation into the router.

## Initial services

Crewday, FJ2, and Xbot are the initial calling services. Their migration MUST
not start until the applicable shared contracts and migration plans are
accepted.
