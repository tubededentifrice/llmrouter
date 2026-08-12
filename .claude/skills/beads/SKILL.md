---
name: beads
description: Inspect or remove inactive Beads data for the LLM Router repository. Beads planning is disabled until the user accepts the specifications and explicitly asks to enable it.
---

# Keep Beads disabled

Use product specifications as the only current planning source. Do not create,
claim, update, import, or use Beads work items. Do not create an implementation
backlog from draft documents.

Permit read-only inspection and user-requested deletion. After a deletion,
verify that `bd list --all --json` returns an empty array. Push the Dolt store
only if its configured workflow requires a push.

Enable Beads only after the user accepts the specification set and explicitly
asks to use Beads for planning. Update `AGENTS.md`, this skill, the repository
check, and `.beads/README.md` in the same change.
