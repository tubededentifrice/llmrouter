---
name: llmrouter-specs
description: Create or update LLM Router product specifications, API contracts, architecture decisions, source-service research, and migration plans. Use when planned behavior changes for providers, models, assignments, inheritance, fallbacks, agents, tools, routing, logs, accounting, administration, security, retention, replication, or failover.
---

# Maintain LLM Router specifications

Keep one source for each requirement. Use normative words only in
`docs/specs/` and formal files in `docs/api/`. Use decision records to explain
why the user accepted a material choice.

## Prepare

1. Read `AGENTS.md`, `README.md`, `docs/specs/README.md`, and
   `docs/decisions/README.md`.
2. Read each affected specification and contract.
3. For a source-service migration, read its instructions and affected code or
   specification. Do not change it unless the user asks for that change.
4. Ask the user about choices that change public behavior, user experience,
   security, legal risk, cost, or stored data.

## Write

- Put product and service behavior in the closest specification.
- Put wire shapes and errors in `docs/api/`.
- Keep provider and infrastructure products out of the public contract.
- Define ownership, scope, success, failure, limits, consistency, retention,
  and audit behavior for each operation.
- Keep a logical request separate from each provider attempt.
- Define inheritance order, override behavior, disablement, cycles, revisions,
  validation, publication, stale-node behavior, and rollback.
- Define retry ownership, idempotency, fallback, hedging, cancellation,
  streaming interruption, budgets, and duplicate accounting.
- Define service, workspace, global administrator, agent, tool, and credential
  isolation.
- Define log content, redaction, sampling, export, and retention separately
  from durable accounting and audit data.
- For hosted views, define origin checks, short-lived grants, permission scope,
  audit behavior, and a headless alternative.

## Align calling services

Move generic behavior to this repository. Keep domain rules in the calling
service. Replace copied generic rules only after the shared contract and
migration plan exist. Do not change Crewday, FJ2, or Xbot during discovery.

## Verify

Search for conflicting terms and behavior without an owner. Check links and
formal files. Use `selfreview`, then run `./scripts/check-repository.sh`.
