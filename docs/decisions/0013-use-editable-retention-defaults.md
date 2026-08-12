# Use editable retention defaults

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Diagnostic, content, accounting, audit, and configuration data have different
storage and investigation value. Their useful periods can change without a
software release.

## Decision

Use initial defaults of 7 days for diagnostics and captured content, 90 days
for raw accounting, 2 years for daily aggregates and security or global audit,
and the latest 100 plus 2 years for configuration revisions.

Make these values inherited configuration. Let global policy set limits and
let services or workspaces select allowed values without deployment.

## Alternatives

- Use shorter fixed periods. This lowers storage but weakens investigations.
- Permit unlimited service values. This is flexible but weakens global cost
  and lifecycle control.

## Consequences

- Operators can tune cost and investigation depth.
- The administration interface needs impact previews and audit.
- Retention workers need safe behavior when values change.
