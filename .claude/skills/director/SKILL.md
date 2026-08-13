---
name: director
description: Operate autonomous LLM Router delivery across ready Beads work. Use when the user invokes $director, asks the main agent to complete the Beads graph, or asks for coordinated implementation, independent review, verification, commits, and pushes while the main agent stays the operator.
---

# Direct Beads delivery

Keep the Director as the only operator. The Director selects and claims work,
changes the Beads graph, assigns bounded work, accepts reports, closes tasks,
commits exact files, pushes to `origin/main`, and selects the next ready task.
Workers must not select more work, change task state or dependencies, commit,
push, or start a second task.

## Select bounded work

1. Inspect `git status`, current in-progress tasks, and ready self-review
   tasks. Resume one valid in-progress task first. Otherwise, complete a ready
   self-review before a new main task.
2. If no self-review is ready, run `scripts/agent-next-task.sh` and select one
   ready main task.
3. Fetch `origin`, and record `HEAD` and `origin/main`. Reconcile a new
   upstream commit without a reset, rebase, or history rewrite.
4. Read the task's normative sources and inspect its dependency path.
5. Split the task before implementation when it has more than one independent
   acceptance result, rollback boundary, or owner. In particular, separate a
   database foundation, a public contract, runtime behavior, configuration
   integration, and an end-to-end verification when each can pass alone.
6. Keep one primary component owner and one independently verifiable result in
   each main task. Link each new main task to one self-review task.
7. Record explicit human, external, or product-decision blockers. Do not let a
   worker expand the active task to solve an unrelated gap.
8. Do not claim a new task while another valid task is in progress. Claim the
   selected task before a worker starts.

## Delegate bounded work

Give one implementation worker the task identity, owned paths, normative
sources, acceptance criteria, and focused test commands. Require the worker to:

- preserve all files outside its ownership;
- use LSP inspection and diagnostics when applicable;
- run focused checks while it edits;
- use the `selfreview` skill in autofix mode after implementation;
- run `./scripts/check-repository.sh` after its last edit;
- leave affected broad suites to the Director unless the Director assigns one;
- send one concise status at each long-command boundary;
- stop after the assigned result and report immediately.

Do not run an implementation worker and a review worker on shared paths at the
same time. The Director can inspect status and diffs while a worker runs, but it
must not edit the worker's owned paths.

## Use staged verification

Run the narrowest applicable check after each edit. The Director owns broad
suite execution by default. Do not restart a complete suite when no changed
path is in that suite's scope.

After the last material implementation edit:

1. Run the focused tests for the changed component.
2. Run each affected broad suite once.
3. Confirm the worker's final `./scripts/check-repository.sh` result. Run it
   again only if a later edit or integration changed its scope.
4. If a check fails, assign the failure to the implementation worker. Require
   the worker to repeat its focused check, self-review, and final repository
   check. Run the broad final check again only after the last fix that can
   affect it.

The Director must not edit implementation files. If the Director finds a
defect during its diff review, it must return the defect to the worker and
repeat the applicable checks.

Record each command and result in the report. A completed check with no later
relevant edit remains valid.

## Integrate each task

The Director reviews the complete owned diff and confirms that no unrelated
file is present. It also confirms that the worker stopped and released its
execution slot. The Director then closes the main Bead, stages exact paths,
creates one focused commit that states why the change exists, and pushes it to
`origin/main`. Do this before the self-review Bead starts.

Claim the dependent self-review Bead and assign it to an independent worker
that did not implement the main task. Give the reviewer the exact
implementation commit, owned paths, normative sources, and focused checks.
Require the reviewer to use the `selfreview` skill in autofix mode and to make
no Beads or Git changes. After the review:

1. Inspect all fixes and verification evidence.
2. Run each broad suite that a review fix can affect. Do not rerun an
   unaffected suite.
3. If a check fails, assign the failure to the reviewer. Require the reviewer
   to repeat its focused check and final repository check after its last fix.
4. Close the self-review Bead.
5. Commit only the review fixes and Beads records. A no-code review still gets
   a focused verification commit when the task record changed.
6. Push to `origin/main`.
7. Run `scripts/agent-next-task.sh` again because the closure can unlock work.

If a direct push does not use the latest `origin/main`, fetch and integrate the
new upstream commit without a reset, rebase, force-push, or shared-history
rewrite. Rerun checks that the integration can affect, then push again.

Never collect several completed tasks in one worktree commit.

## End idle waits

When a worker's final process ends, request its result once. If it does not
reply at the next agent-message boundary, interrupt it and request its final
report. Do not use repeated empty polling cycles. A worker must finish after
its report so its execution slot is released.

Return to the user only when no autonomous ready task remains or when a real
human or external blocker needs action.
