# Source-service review

Date: 2026-08-12

Status: Research. This document is not normative.

## Method

The review inspected Ontology, Crewday, FJ2, and Xbot. It did not change those
repositories. File paths are relative to each source repository.

## Ontology

Ontology has no product implementation. It is a useful repository and contract
reference.

Adapt these ideas:

- specification-first work and inactive Beads controls from `AGENTS.md`;
- concise indexes for specifications and decisions;
- shared skills through `.claude/skills/`, `.agents/skills`, and
  `.codex/skills`;
- one repository quality gate in `scripts/check-repository.sh`;
- a hosted, isolated view with a versioned message contract and a headless API
  from `docs/specs/04-shared-view-and-host-integration.md`;
- separate global control-plane identity and audit rules from
  `docs/specs/05-administration.md`;
- bounded work, health checks, staged rollout, backup, restore, and degraded
  operation tests from `docs/specs/07-scale-reliability-and-operations.md`.

Do not adapt the ontology data model, multiple-parent additive inheritance, or
the fixed YugabyteDB, OpenSearch, and S3 design. LLM Router needs ordered
replacement and a smaller local-node topology.

## Crewday

### Registry and routing

The main registry models are in `app/adapters/db/llm/models.py`. The runtime
resolver is in `app/domain/llm/router.py`. It has useful provider, model,
provider-model, assignment, priority, capability, and inheritance concepts.

Keep these rules:

- A child direct assignment chain replaces its inherited capability chain.
- Validate an inherited model against the requested child capability.
- Keep assignment order as policy and health as temporary eligibility.
- Retry temporary provider errors and stop for budget or invalid-request
  errors.

Do not copy the live adapter path. `app/api/factory.py` selects one provider
adapter at process start. `app/domain/llm/client.py` sends all resolved rungs
through that adapter. A graph entry can therefore name one provider while the
live path uses a different transport.

The recommended shared router resolves an immutable attempt route. It includes
the provider, endpoint, secret reference, provider model ID, wire model name,
timeouts, limits, capability changes, parameters, assignment source, and
configuration revision.

### Graph interface

The advanced React graph starts at
`app/web/src/pages/admin/llm/LlmPage.tsx`. The provider, model, and assignment
columns, registry modals, assignment editor, edge measurement, stable layout,
search, and tests are good design references.

The components depend on Crewday page, query, event, route, and style systems.
A direct package extraction will have high coupling. Host the common graph in
LLM Router and give each service a scoped embedded view and a headless API.

### Agent and tool safety

The agent loop in `app/domain/agent/runtime.py` has time and step limits,
bounded history, approval pauses, and tool audit. The dispatcher in
`app/agent/dispatcher.py` derives tools from reviewed service operations and
checks current user permissions.

Keep tool definitions, domain authorization, approval, and execution in the
calling service. Let the router enforce run limits and the signed effective
tool allow-list. Do not move business routes into the router.

### Accounting and retention

The usage ledger and budget logic are useful references. They record attempts,
tokens, estimated cost, latency, assignment, fallback count, actor, and
correlation.

Correct these issues in the shared design:

- store stable provider-model identity separately from the wire name;
- record reported usage for failed or refused calls because a provider can
  bill them;
- separate logical requests from provider attempts;
- support late invoice or provider-usage correction;
- publish configuration changes after commit through one revisioned stream;
- do not append old rows to one local gzip file from many replicas.

## FJ2

### Registry and fallback

`apps/llm_providers/models.py` defines provider, model, provider-model,
assignment, usage, and search-provider data. The main fallback loop is in
`apps/llm_providers/clients/factory.py`.

FJ2 proves that many domain callers can use one compatibility function. A
future migration should replace that function with a small LLM Router client.
It should not add router logic to about 50 calling files.

Keep the rule that a paused tool-call continuation stays on the exact provider
and model. Do not use blind fallback after a tool call, released stream output,
or another visible side effect.

FJ2 inheritance has special cases and does not match the requested service
chain. Do not copy it. The closest scope should replace one named assignment.
If list extension is necessary, define it as a separate explicit operation.

### Search and extraction

Search adapters are in `apps/autopublish/services/web_search.py`. ScrapingDog
scrape and screenshot operations are in
`apps/autopublish/services/scrapingdog.py`.

Useful behavior includes capability-specific chains, deadlines, rate limits,
cost estimates, shared correlation IDs, quota circuits, and server-side request
forgery protection.

The shared design needs to correct these issues:

- Search routing needs service, workspace, assignment, and tool-policy scope.
- Do not make an untracked external DuckDuckGo call after a database failure.
- Send configuration invalidation to all replicas through a revision.
- Keep full search queries out of durable logs by default.

### Agent harness

The privileged harness in `apps/admin_agent/runtime.py` has durable turns,
leases, cancellation, context compaction, and limits for turns, tools, tokens,
result size, time, and cost. Tool contracts and catalogs are in
`apps/admin_agent/tool_domains/`.

Keep these principles:

- The service owns its business tools and current authorization.
- The router owns the provider-neutral run protocol and limits.
- Each tool is checked again when it runs.
- A tool result is untrusted and bounded.
- Durable run state is different from the model conversation.

Use a registered service tool gateway. Do not accept an arbitrary callback URL
in each request.

### Logs and interface

FJ2 keeps provider attempts, request correlation, price snapshots, daily
summaries, and short-lived debug content. It also shows why accounting,
diagnostic content, search text, provider errors, and audit events need separate
retention and access rules.

Its graph uses Django templates, Alpine, JavaScript, SVG, and CSS. A shared
hosted interface can avoid a second graph implementation. If embed isolation
does not meet the user experience, a custom element is the next option.

## Xbot

Xbot has specifications and React mocks, but no router backend. Its architecture
keeps agent control, workspaces, memory, social operations, and policy in the
service. This is the correct boundary.

Important requirements are in these files:

- `docs/specs/01-agent-control.md`: fresh tool access checks, durable handoffs,
  mutation plans, pause and stop, and tool audit;
- `docs/specs/04-web-app-and-authentication.md`: passkey user access and recent
  authentication for sensitive changes;
- `docs/specs/05-platforms-operations-and-measurement.md`: workspace, provider,
  and tool-group budgets;
- `docs/specs/06-security-privacy-and-quality.md`: provider approval for each
  workspace and privacy class, raw-model-log retention, and audit retention.

LLM Router should not become Xbot's business-data or memory store. It can
enforce route, cost, provider, privacy, and tool policies that Xbot supplies.

## Main conclusions

1. Use one control plane and local data plane nodes with authenticated,
   versioned configuration snapshots.
2. Use the nearest scope replacement rule for each named assignment.
3. Resolve one immutable route snapshot for a provider attempt.
4. Do not use eventual consistency for active request or agent-run ownership.
5. Use stable request, attempt, run, and accounting event identities.
6. Keep business tools and their authorization in calling services.
7. Separate global administration from service-scoped administration.
8. Keep content capture off by default and give each data class its own
   retention policy.
9. Use one hosted React graph with a safe embed and a headless interface unless
   the interview selects a custom element.
10. Add an OpenAI-compatible endpoint only as a migration interface. Use a
    native versioned API for the complete service.
