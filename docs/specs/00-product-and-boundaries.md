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

Crewday, FJ2, and Xbot are the initial calling services. This repository MUST
define shared contracts and MUST NOT contain implementation or migration tasks
for a calling-service repository.

Xbot has no production runtime data. Its product specifications MUST align
with the accepted shared contracts before its implementation starts. Crewday
code alignment and FJ2 code and data migration belong to separate work in
their own repositories. FJ2 migration MUST NOT start until the applicable
shared contracts are accepted.

## Initial data profile

The first release MUST expose a versioned request data-profile field. Its only
accepted value MUST be the named public-data profile. A caller MUST use this
profile only for content that is public, intended for public release, or
explicitly approved as public for the operation.

The router MUST reject another profile or protected private content that it
can identify. The calling service remains responsible for classifying its
domain data before submission. A later private-data profile needs a separate
accepted specification change. It MUST NOT silently change the meaning of the
public-data profile.
