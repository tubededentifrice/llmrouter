# Inactive Beads store

Beads is initialized for this repository. It has no work items.

Do not create, claim, update, import, or use a Beads work item until the user
accepts the LLM Router specifications and explicitly asks to enable Beads
planning.

Read-only inspection is permitted. This command must return an empty JSON
array during the specification review:

```bash
bd list --all --json
```

When the user enables planning, update `AGENTS.md`, the `beads` repository
skill, this file, and `scripts/check-repository.sh` in one change.
