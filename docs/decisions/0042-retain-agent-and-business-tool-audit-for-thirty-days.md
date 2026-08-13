# Retain agent and business-tool audit for 30 days

## Context

Agent runs and business-tool calls need audit evidence for investigation and
effect reconciliation. This evidence is separate from security audit, raw
accounting, and captured content. The retention model needs an initial default
and a safe editable range for this data class.

## Accepted choice

The user accepted an initial default of 30 days for agent-run and business-tool
audit records. A global administrator can set an allowed range within the hard
safety limits of 7 to 365 days. A service or workspace can select a value in
the effective allowed range without a deployment.

## Alternatives

- Seven days lowers storage but gives less time to investigate an effect.
- Ninety days aligns with raw accounting but keeps detailed tool audit for
  longer than the initial need.
- Two years aligns with security audit but is excessive for normal run and
  tool investigation.

## Good effects

- Administrators have 30 days to investigate a run or reconcile a tool effect.
- The data class has its own visible and editable retention control.
- A global range prevents an unexpectedly short or long value.

## Bad effects

- Detailed run and tool audit consumes storage for 30 days.
- An investigation after expiry must use other retained evidence.

## Migration effect

There is no runtime migration. The implementation must create this separate
retention class before it stores first-release agent-run or business-tool audit
records.

## Security effect

The change does not permit captured-content access. Authorization, encryption,
audit-read controls, and deletion workers continue to apply.

## Review conditions

Review this choice if investigations often need more than 30 days, if storage
use is excessive, or if a legal requirement sets a different minimum or
maximum.
