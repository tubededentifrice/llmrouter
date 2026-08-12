---
name: selfreview
description: Review recent LLM Router code, contract, tool, repository, or specification changes with a skeptical pass. Use after non-trivial edits and before commit or push. In autofix mode, fix all defects, missing work, and material risks without asking for triage.
---

# Self-review

Find defects before work leaves the repository. Review against the user
request, applicable specifications, formal contracts, and accepted decisions.

## Gather the change

1. Run `git status --short --branch`.
2. Read working, staged, and applicable recent diffs.
3. Read affected specifications, contracts, decisions, and research evidence.
4. Preserve dirty files that another worker owns.

## Review the change

Check all applicable areas:

- Trace success, failure, empty, retry, fallback, timeout, cancellation,
  stale-configuration, and concurrent paths.
- Check service and workspace isolation at each request, cache key, log,
  accounting row, credential, tool call, and administration action.
- Check inheritance order, replacement, disablement, cycles, validation,
  revisions, publication, propagation, rollback, and stale nodes.
- Check logical request identity, provider attempt identity, idempotency,
  duplicate suppression, retry ownership, fallback, hedging, and budgets.
- Check streaming start, partial output, interruption, retry safety, tool calls,
  cancellation, and final accounting.
- Check provider health, circuit behavior, rate limits, concurrency limits,
  node draining, local-first routing, and remote failover.
- Check prompt, response, tool, credential, and personal-data redaction.
- Check log sampling, retention, export, deletion, audit durability, accounting
  accuracy, and cost correction.
- Check global administrator identity, service-scoped grants, origin checks,
  replay controls, secret display, and audit events.
- Check semantic HTML, keyboard access, focus, phone layout, loading, empty,
  error, stale, and offline states for user-interface changes.
- Confirm that normative behavior is in `docs/specs/` and accepted choices are
  in `docs/decisions/`.
- Find contradictions, undefined terms, duplicate requirements, and behavior
  without a clear owner.

Use `BUGS`, `MISSING`, `RISKY`, and `NITPICKS` as finding severities. Each
finding must name a file and line, state the trigger, and give a direct fix. Do
not invent findings.

## Fix and verify

In autofix mode, fix all `BUGS`, `MISSING`, and `RISKY` findings. Fix a
`NITPICK` only when the edit is small and safe.

Run LSP diagnostics and narrow tests for code edits. Then run:

```bash
./scripts/check-repository.sh
```

Return findings, fixes, checks, and any verification limit.
