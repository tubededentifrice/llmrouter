# Agent harness and tools

Status: Accepted sections only. Run ownership, tool-call concurrency, and
approval details remain open.

## Optional agent harness

LLM Router MUST provide a complete provider-neutral agent harness. The harness
MUST support model calls, streaming, bounded tool-call loops, cancellation,
budgets, request accounting, tool accounting, and provider failover where the
request state permits failover.

A service MAY use the harness. It MAY instead run its own agent loop and use
the router for model calls and shared tools. Both modes MUST use the same
provider registry, assignments, fallback policy, accounting rules, and service
and workspace isolation.

The calling service owns its business tools, domain authorization, user
approval, and domain records. LLM Router MUST NOT make a business tool eligible
only because the model requested it.

## Shared external tools

LLM Router MUST provide shared adapters for approved external search,
extraction, scrape, screenshot, and related infrastructure providers. The
initial provider set can include Brave, ScrapingDog, Serper, SearXNG, and other
providers that an accepted provider specification adds.

Shared tools MUST use named assignments with ordered provider fallbacks. Their
requests MUST have service scope and, when supplied, workspace scope. The
router MUST apply the effective permission, privacy, budget, rate, and provider
policy before an external call.

The router MUST provide direct service endpoints for eligible shared tools. A
service can call these endpoints without starting an agent run. Direct and
agent-originated tool calls MUST use the same routing, failover, accounting,
redaction, and audit rules.

The router MUST NOT make an untracked external call when configuration is
missing or invalid.
