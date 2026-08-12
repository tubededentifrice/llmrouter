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
- Keep assignment order separate from health and error eligibility.

Crewday stops for budget and invalid-request errors. Decision 0021 supersedes
that broad rule: the shared router falls back when the error affects only one
candidate or provider scope, and stops when it affects the complete request.

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

### Price synchronization

Crewday and FJ2 both keep price-source settings on model or provider-model
records and support a manual pin. Both specify a weekly OpenRouter refresh and
provide an administrator-triggered refresh. FJ2 has the complete scheduled
task in `apps/llm_providers/tasks.py`, the Sunday schedule in
`config/settings/base.py`, a command in
`apps/llm_providers/management/commands/sync_llm_prices.py`, and the main
batch logic in `apps/llm_providers/price_sync.py`.

FJ2 fetches one catalog for each source and reuses it for all applicable rows.
This is the correct batch shape. It also supports dry runs and source filters.
Its optional fixed or image price fields stay unchanged when a new source row
omits them. The shared design must distinguish an explicit zero from an
omitted or invalid value, so an old component does not remain by accident.

Crewday has stronger normalization and administration behavior in
`app/adapters/llm/openrouter.py` and `app/api/admin/llm.py`. It normalizes
token, fixed-call, and audio-duration units. Its bulk and single-row endpoints
return per-row deltas. The React administration interface shows manual and
stale states and can synchronize after a relevant edit.

Crewday's bulk endpoint fetches the OpenRouter catalog separately for each
row. The shared design uses FJ2's one-fetch batch shape. Crewday's specification
says a lookup miss keeps an edited row, while its implementation and tests
roll back the create or edit. The shared design publishes the valid save and
runs the selected-source refresh asynchronously, with an explicit pending,
missing, stale, or failed price state.

The Crewday specification requires a weekly `sync_llm_pricing` job, but
`app/worker/scheduler.py` does not register it. Normal Crewday runtime creation
also does not load the synchronized database prices into its pricing table:
`app/domain/llm/budget.py` returns an empty default, and production client
construction does not supply another table. The administration price and the
runtime cost can therefore differ.

Both services can apply an OpenRouter model price to a different provider's
route through model-level inheritance or sibling lookup. Neither service
stores provider charge corrections, source price revisions, or invoice
reconciliation. Crewday forces failed calls to zero cost, while FJ2 can retain
an estimate for a failed call. A provider can bill failed or refused attempts.

Adapt these rules:

- make price authority and lookup identity explicit on each provider-model
  route;
- fetch one immutable source snapshot for each synchronization run;
- keep the model catalog curated and separate from price refresh;
- preserve precise quantity facts, source values, and price versions;
- use the exact route price in runtime admission and accounting;
- record billable usage for failed or refused attempts;
- append provider-charge and invoice corrections without rewriting the
  original event;
- expose dry-run deltas, partial errors, manual pins, and stale state.

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
8. At the time of this review, the recommendation was to keep content capture
   off by default and give each data class its own retention policy. Decision
   Decision 0038 supersedes the profile scope. Decision 0012 keeps complete
   content capture on by default and configurable for the `service-data`
   profile. Separate data classes and retention policies still apply.
9. Use one hosted React graph with a safe embed and a headless interface unless
   the interview selects a custom element.
10. Provide an OpenAI-compatible migration interface and use the native
    versioned API for the complete service.
