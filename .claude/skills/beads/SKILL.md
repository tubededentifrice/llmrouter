---
name: beads
description: Create, inspect, update, link, or remove LLM Router Beads implementation work after specification approval.
---

# Manage the implementation plan

The user accepted the specification set and enabled Beads implementation
planning on 2026-08-13. Use product specifications and formal API contracts as
the source for implementation tasks. Do not treat a working proposal as an
accepted requirement.

## Create work

1. Read `AGENTS.md`, `README.md`, the specification index, and the applicable
   specifications and contracts.
2. Create one bounded main task for one cohesive verified result. Link it to
   the applicable specification with `--spec-id` and add useful acceptance
   criteria.
3. Create smaller child tasks only when they can be completed and verified
   independently.
4. Create one self-review task for each main task. Make it depend on the main
   task, and make the next dependent main task depend on the self-review task.
5. Represent human action, an external prerequisite, or an unaccepted product
   choice as an explicit blocker or decision task. State the responsible actor,
   evidence needed, good effects, bad effects, and the work that it blocks.
6. Add blocking dependencies in execution order. Do not use a related link
   when one item must finish first.

## Verify work

After a graph change, run `bd lint --status all`, `bd dep cycles`,
`bd graph check`, and inspect `bd graph --all --compact`. Check that each main
task has one self-review task, each blocker has a clear owner and exit
condition, and no implementation task depends on a draft product choice.

Export `.beads/issues.jsonl` only when the repository workflow needs a
reviewable interchange file. Push the Dolt store only if its configured
workflow requires a push.
