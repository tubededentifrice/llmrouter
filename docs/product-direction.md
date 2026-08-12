# Product direction

Status: Draft for user review.

## Problem

Crewday, FJ2, Xbot, and future services need similar LLM infrastructure. Each
service now has some local provider, model, assignment, fallback, tool, and
administration logic. This duplicates work and can make security, reliability,
cost control, and operational analysis different in each service.

## Outcome

LLM Router will give each calling service a small and stable interface. The
router will centralize shared infrastructure without taking ownership of the
calling service's product logic.

The planned operator outcomes are:

- define providers and models once;
- assign model fallback chains to named work types;
- inherit assignments through an ordered service chain;
- let a service control workspace overrides;
- operate local router nodes and fail over to healthy remote nodes;
- inspect requests, usage, cost, failures, and configuration changes;
- manage credentials and permissions with a global administration identity;
- expose safe service-scoped management views in different web frameworks.

The planned calling-service outcomes are:

- keep its domain workflows and prompt construction in its own code;
- select an assignment, not a provider-specific model, for normal requests;
- register or allow the tools that an agent can use;
- send service and workspace identity with each request;
- receive a stable result and detailed machine-readable failure information;
- use a local router node without losing service when that node fails.

## Initial callers

- Crewday is the reference for the advanced React LLM graph experience.
- FJ2 proves that the shared administration experience cannot require React in
  the host application.
- Xbot provides additional agent and tool integration cases.

The router will not change these repositories during discovery.

## Scope candidates

The shared service can own model routing, provider adapters, health and circuit
state, request policy, common tool adapters, agent-run mechanics, accounting,
retention, and administration surfaces.

The calling service can own domain prompts, domain data, user approval,
business workflow state, end-user permissions, and tool authorization for each
agent or request.

The architecture interview will resolve the exact boundary.

## Quality goals

- Make a local healthy node the normal request path.
- Give a node failure a bounded and observable failover path.
- Give a configuration change a revision, author, validation result, and
  audit record.
- Give a request a stable identity across retries and fallback attempts.
- Do not count one logical request more than once in accounting.
- Keep sensitive content out of logs by default.
- Use different retention for diagnostic logs, audit records, accounting
  records, and optional request content.
- Keep service and workspace data isolated in APIs, storage, logs, and
  administration views.
- Do not make the public interface depend on one provider SDK, agent framework,
  storage product, queue, or UI framework.

## Out of scope until review

- changes to Crewday, FJ2, or Xbot;
- a final implementation language or storage product;
- an accepted wire protocol;
- end-user chat or prompt-management product features;
- global semantic memory or domain knowledge storage;
- Beads implementation planning.
