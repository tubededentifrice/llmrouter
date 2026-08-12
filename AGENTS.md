# Agent instructions

These instructions apply to the complete repository.

## Mission

Build and operate LLM Router. It is a source-available control plane and data
plane for model providers, models, assignments, fallbacks, agent runs, shared
tools, request accounting, and administration.

Keep service-specific product rules in the calling service. Move shared router
behavior here only when two or more services need it or when one control point
is necessary for security, reliability, or accounting.

Do not put secrets, API credentials, private prompts, model responses, private
runtime data, or unpublished third-party data in Git.

## Working rules

1. Read this file, `README.md`, and the applicable document indexes before a
   change.
2. Inspect Git status. Preserve all changes that you do not own.
3. Read the applicable files in `docs/specs/` and `docs/decisions/` before a
   behavior change.
4. Use LSP tools for symbol lookup, definitions, references, callers,
   diagnostics, renames, formatting, and safe edit previews when they apply.
   Use `rg`, Git, and shell tools for broad search and command execution.
5. Before a code behavior change, use LSP to inspect affected symbols and
   callers. After the edit, run LSP diagnostics and repository checks.
6. Make the smallest complete change that solves the request.
7. Ask the user about choices that materially change public behavior, user
   experience, security, legal risk, cost, or stored data.
8. Use ASD-STE100 Simplified Technical English in user reports, pull requests,
   review comments, documentation, and agent-created content.
9. After non-trivial edits, use the repository `selfreview` skill. Review all
   owned changes one more time, then run `./scripts/check-repository.sh`.

## Tooling and durable guidance

Use the `repository-tooling` skill when repeated work, test access, environment
setup, or a missing quality check causes friction. Improve the repository tool,
fixture, skill, or gate in the same change when it is safe and in scope. Do not
leave a repeatable workaround only in chat or weaken authentication to make a
test pass.

Keep root instructions limited to durable policy. Put reusable workflows in a
skill, deterministic operations in `scripts/`, and directory-only rules in a
nested `AGENTS.md`.

## Specification and implementation

Product behavior belongs in `docs/specs/`. Architecture choices belong in
`docs/decisions/`. Keep each requirement in one source document and link to it
from other documents.

The current work defines the service. Do not add product implementation until
the user approves the applicable specifications. Repository maintenance tools,
research notes, interface experiments, and formal contract drafts are
permitted when the user asks for them.

Beads is initialized, but Beads planning is disabled. Do not create, claim,
update, import, or use a Beads work item. The user must state that the
specifications are ready and explicitly ask to enable Beads before this rule
can change.

## Service boundaries

Every runtime request must have a service identity. A workspace request must
also have a workspace identity. A normal service must not read or change
another service's configuration, credentials, requests, accounting data,
agents, tools, or workspaces.

Service configuration uses one ordered parent chain. For one named assignment,
the nearest service or workspace layer replaces the complete inherited
fallback chain. Do not merge chains or add partial chain edits in the first
release.

Global administration uses a separate identity and permission path. Each
global administration action must produce an audit event.

Treat prompts, model responses, tool inputs, tool outputs, and provider
credentials as sensitive data. Do not log their contents unless an approved
specification permits it. Redact secrets before data leaves a process.

Keep provider, storage, queue, telemetry, React, and agent-framework products
out of the public contract. Use versioned, product-neutral interfaces.

## Dependencies and interfaces

Do not select or install a dependency version that is less than 14 complete
days old. Use exact versions and immutable container references. Do not use
`@latest`, floating tags, or unpinned remote install scripts. An exception
needs a user decision and an architecture decision record.

Use `uv` for all Python environments, dependency changes, locking, commands,
and tools. Do not use `pip`, `pipx`, Poetry, or a manually created virtual
environment.

Keep each public contract versioned and product-neutral.
Compatibility APIs, SDKs, hosted administration views, and headless interfaces
must not weaken service or workspace isolation.

If a React application is added, each change must keep React Doctor at score
100 with zero diagnostics. Add and run the React gate with the application.

## Concurrent work and Git

Other agents can change repositories at the same time.

- Do not reset, discard, overwrite, or reformat work that you do not own.
- Do not use broad staging commands in a shared worktree. Stage exact paths.
- Split concurrent work by file or component.
- Reconcile new `origin/main` changes before integration.
- Use focused commits. State why a change exists.
- Never force-push or rewrite shared history.
- If a service or permission failure prevents a push, report it clearly.

## Repository map

- `docs/specs/`: normative product and service behavior after approval.
- `docs/decisions/`: accepted architecture decisions.
- `docs/research/`: time-stamped evidence and source-service review notes.
- `docs/api/`: formal public contracts after the interface review.
- `app/`: service and user-interface code after specification approval.
- `scripts/`: local and CI quality tools.
- `.beads/`: inactive Beads configuration with no work items.
- `.claude/skills/`: repository workflows, exposed through `.agents/skills`
  and `.codex/skills`.

Add a nested `AGENTS.md` only when a directory needs different rules. State
only the differences.
