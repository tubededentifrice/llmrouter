# Active Beads store

The user accepted the LLM Router specification set and enabled Beads planning
on 2026-08-13. Beads is the implementation work tracker for this repository.

Use the repository `beads` skill before a work-item change. Product behavior
remains normative in `docs/specs/`. A task must link to its applicable
specification or contract. A specification gap must be a blocker or decision
item and must block affected implementation.

Use these checks after a task-graph change:

```bash
bd dep cycles
bd graph check
bd list --all --json
```

Each main implementation task must have a dependent self-review task. The
self-review task must block the next dependent main task.
