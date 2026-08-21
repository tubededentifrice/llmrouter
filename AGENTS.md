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

## Shared OpenDLE code

Before adding backend behavior, check `../opendle-lib` and the sibling
projects. If the behavior has credible reuse in more than one OpenDLE project,
put the framework-neutral code and its tests in `../opendle-lib`, then use the
direct Git `main` dependency here. Keep Router policy, routes, data models,
and service adapters local. Do not copy shared code between projects. One
tested implementation makes fixes and security updates apply to all consumers.

Before adding a reusable frontend component, token, or interaction pattern,
check `../opendle-ui` and put the shared item there first. Keep only
Router-specific data, copy, routes, domain state, and host composition here.

## Development site access

The administration development site runs at `http://127.0.0.1:5174` and is
also available at `https://llmrouter.opendle.dev` through the protected
Pangolin and Traefik route. Agents MUST use the localhost URL for browser
tests, API checks, and local verification. The external URL is for the user
and for protected access only. Do not bypass the protection or add public host
port bindings.

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
10. Use the repository `director` skill for autonomous Beads execution. The
    main Director agent is the only operator for task selection, Beads, Git,
    and pushes. Delegated workers edit and verify only their assigned files,
    report their results, and stop.

## Tooling and durable guidance

Use the `repository-tooling` skill when repeated work, test access, environment
setup, or a missing quality check causes friction. Improve the repository tool,
fixture, skill, or gate in the same change when it is safe and in scope. Do not
leave a repeatable workaround only in chat or weaken authentication to make a
test pass.

Keep root instructions limited to durable policy. Put reusable workflows in a
skill, deterministic operations in `scripts/`, and directory-only rules in a
nested `AGENTS.md`.

Agents may improve these instructions, skills, scripts, and checks when they
find repeated friction or a missing guard. Use progressive disclosure: keep
durable rules here, workflows in skills, and detailed references in linked
files. Test new tools for success, expected failure, and unsafe input. Do not
weaken a check to make a task pass. Agents may delegate independent inspection
or validation to subagents; the owner agent reviews the complete diff and owns
Git actions.

## Specification and implementation

Product behavior belongs in `docs/specs/`. Architecture choices belong in
`docs/decisions/`. Keep each requirement in one source document and link to it
from other documents.

The current work defines the service. Do not add product implementation until
the user approves the applicable specifications. Repository maintenance tools,
research notes, interface experiments, and formal contract drafts are
permitted when the user asks for them.

The user accepted the specification set and enabled Beads planning on
2026-08-13. Use the repository `beads` skill for implementation planning and
work-item changes. Keep product gaps as explicit blocker or decision items.

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
Use the shared [OpenDLE UI](https://github.com/tubededentifrice/opendle-ui)
package in `../opendle-ui` as the shared design system. The React app uses the
Git dependency
`git+https://github.com/tubededentifrice/opendle-ui.git#main`. This dependency
always uses the current shared `main` branch and is exempt from the 14-day age
and exact-version checks. The package includes built files for clean installs.

Build the Router interface on the existing mocks in `apps/admin/`. Before you
change or add a frontend surface, inspect the applicable mocks and implemented
UI patterns in `../llmrouter`, `../ontology`, and `../xbot`. Use them as the
design baseline so the three projects keep one consistent visual language and
interaction model. Do not replace the existing design with a new local design.

Before you invent a component, token, typography rule, layout pattern,
interaction pattern, or design principle, inspect the source and public exports
in `../opendle-ui`. Put an item in `../opendle-ui` when it has any reasonable
potential for reuse, even if no second project uses it yet. Add the shared item
there first, and update this repository to use it in the same change. Keep
shared design principles, tokens, typography, layout rules, interaction
patterns, accessibility conventions, and reusable components in
`../opendle-ui`. Keep only Router-specific data, copy, routes, domain state, and
host composition here. Do not copy a shared item into this repository. If a
local item gains reuse potential, move it to `../opendle-ui` before you
continue.

Use only semantic CSS class names that state the component, element, role, or
state. Do not use helper or utility classes unless the specific case has a
strong documented justification.

## Concurrent work and Git

Other agents can change repositories at the same time.

- Do not reset, discard, overwrite, or reformat work that you do not own.
- Do not use broad staging commands in a shared worktree. Stage exact paths.
- Split concurrent work by file or component.
- Reconcile new `origin/main` changes before integration.
- Use focused commits. State why a change exists.
- Outside a Director workflow, after each task is complete and all checks
  pass, commit only the files that you own and push the commit directly to
  `origin/main`.
- In a Director workflow, only the Director commits the exact task files and
  pushes the commit directly to `origin/main`.
- Do not leave completed work only in the worktree or in a local commit.
- Never force-push or rewrite shared history.
- If a service or permission failure prevents a push, report it clearly.

## Repository map

- `docs/specs/`: normative product and service behavior after approval.
- `docs/decisions/`: accepted architecture decisions.
- `docs/research/`: time-stamped evidence and source-service review notes.
- `docs/api/`: formal public contracts after the interface review.
- `app/`: service and user-interface code after specification approval.
- `scripts/`: local and CI quality tools.
- `.beads/`: active Beads implementation plan and configuration.
- `.claude/skills/`: repository workflows, exposed through `.agents/skills`
  and `.codex/skills`.

Add a nested `AGENTS.md` only when a directory needs different rules. State
only the differences.
