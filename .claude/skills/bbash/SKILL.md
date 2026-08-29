---
name: bbash
description: Run LLM Router bug bashes that reproduce reports against localhost, compare them with accepted behavior, and create review-gated Beads tasks. Use for interactive defect reporting, not implementation.
---

# Bug-bash LLM Router

Run an interactive reporting session. Reproduce and document defects. Do not
edit product code or implement fixes while this skill is active. After each
report, give the result and invite the user to report the next issue.

## Keep the session within project boundaries

- Use `http://127.0.0.1:5174` for all browser and API checks. The protected
  external site is for user access only.
- Do not bind a service to the public interface.
- Read the accepted specification, API contract, and decision that define the
  expected behavior before you classify an observation as a defect.
- If the expected behavior is not accepted, do not create an implementation
  bug. Create the applicable decision or blocker work under the `beads` skill,
  or use `llmrouter-specs` when the user asks to define the behavior.
- Do not run `./scripts/local-development.sh reset` or `prove` unless the user
  authorizes the data deletion that the command performs.
- Do not call a paid provider to reproduce a defect unless the user explicitly
  authorizes that call. Prefer the fake provider adapter.
- Keep credentials, authorization data, prompts, model responses, tool data,
  and private runtime data out of Beads and chat. Record safe, redacted
  evidence only.
- Use ASD-STE100 Simplified Technical English in task text and reports.

## Prepare the session

1. Read and follow the local `beads` skill before you inspect or change the
   Beads graph.
2. Inspect `git status` and preserve work that other agents own.
3. Run `./scripts/local-development.sh status`. Start the local deployment if
   the reported surface needs it and it is not running.
4. Inspect open and closed Beads for a possible duplicate before you create a
   task.
5. Get the affected route or operation, the observed result, the expected
   result, and any necessary service, workspace, administrator, browser, or
   timing conditions. First try to reproduce the report with the available
   facts. Ask a question only when verification cannot establish a required
   fact.

## Reproduce the report

Use the smallest check that gives reliable evidence:

- Use `curl` for status codes, redirects, API responses, and server errors.
- Use Playwright for layout, interaction, client validation, responsive
  behavior, console errors, and network errors.
- Use focused repository commands for backend, queue, storage, and accounting
  behavior. Do not make an unsafe data change only to obtain evidence.
- Inspect local logs when needed, but do not copy sensitive request content or
  secrets into the report.

For an authenticated administration check, follow the localhost test-session
procedure in the `repository-tooling` skill:

1. Run `./scripts/local-development.sh test-session`.
2. Let the browser process read
   `.local-development/test-administrator-session.json` directly. Never print
   or copy its values.
3. Set the named cookie for `http://127.0.0.1:5174` with the required host-only,
   HttpOnly, SameSite=Lax, path `/`, and `secure: false` attributes.
4. Use the file's CSRF value and exact origin for administrator writes.
5. Always run `./scripts/local-development.sh clear-test-session` after the
   check, including after a failed reproduction attempt.

Record this evidence when it applies:

- exact localhost route or API operation;
- UTC time of the observation;
- service, workspace, administrator role, and other relevant test state,
  without private identifiers;
- numbered reproduction steps;
- observed status, response shape, screen state, console error, or log event;
- expected behavior and its normative source;
- frequency and timing conditions;
- focused command or browser procedure that reproduces the result.

Do not claim a root cause that the evidence does not establish. If the issue
does not reproduce, state what you checked and ask for the missing condition.
If the user wants the issue tracked without a reproducer, create a bounded
investigation task instead of a confirmed bug.

## Classify and split the work

Create one main Bead for one cohesive, independently verifiable result. Split
the report when it has independent acceptance results, rollback boundaries,
or owners. Separate database, contract, runtime, configuration, interface, and
end-to-end work when each result can pass alone.

Add a dependency only when one task cannot start until another task and its
self-review are complete. Do not use a dependency only because two defects are
related.

Use the Beads priority field:

- P0: active security exposure, data loss, or complete service outage;
- P1: a core workflow is unavailable and has no safe workaround;
- P2: a material defect with a workaround or limited scope;
- P3: a minor functional or interface defect;
- P4: a low-impact cosmetic defect.

If an existing task covers the same defect, do not create a duplicate. Add
new, safe evidence to the existing task when it improves the work item.

## Create review-ready Beads

Follow the local `beads` skill for every graph change. A confirmed defect main
task must use type `bug`, use the labels `main` and `repo:llmrouter`, link to
the accepted source with `--spec-id`, and contain these sections. Add the
`entrypoint` label when the task has no prerequisite review or blocker.

```markdown
## Problem
[One precise defect.]

## Normative Source
[Accepted specification, API contract, or decision and the applicable rule.]

## Evidence
[Safe observed evidence. Do not include sensitive model or authorization data.]

## Expected Behavior
[The required result from the normative source.]

## Steps to Reproduce
1. [Prepare a safe local state.]
2. [Perform the operation.]
3. [Observe the defect.]

## Environment
- URL or operation: [localhost route or API operation]
- Identity scope: [safe service, workspace, or administrator description]
- Conditions: [browser, viewport, timing, or other relevant state]

## Test Plan
[An exact focused command or browser procedure and its expected result.]

## Acceptance Criteria
- [ ] [One observable result.]
- [ ] [The focused regression check passes.]

## Dependencies and Human Input
[Required predecessor review or blocker, or state that no input is required.]
```

Create one `selfreview` chore for each main task. Title it
`Self-review: <main task title>`, label it `selfreview`, and set metadata
`{"review_of":"<main-task-id>"}`. Its description must instruct its owner to
run `$selfreview` in autofix mode against the main task result. Make the main
task block its review task.

When a later main task needs an earlier result, make it depend on the earlier
review task, not on the earlier main task. A main task can depend only on a
review task or an explicit blocker. Do not give a dependent main task the
`entrypoint` label.

After every graph change, run:

```bash
bd lint --status all
bd dep cycles
bd graph check
bd graph --all --compact
```

Inspect the graph output. Confirm that each main task has exactly one review
task and that each dependency has a necessary execution order. Export Beads
only when the repository workflow needs an interchange file.

## Continue or end the session

After each report, tell the user:

- whether the defect reproduced;
- the created or existing task IDs;
- the selected priority and normative source;
- any dependency or decision blocker;
- what evidence or user fact is still missing.

Then ask for the next report. Do not start implementation or the Director from
inside the bug-bash session.

When the user ends the session, summarize all confirmed defects,
investigations, duplicates, and decision blockers. Show the dependency graph
when dependencies exist, and run `scripts/agent-next-task.sh` to report the
ready implementation queue.
