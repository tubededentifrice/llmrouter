# Authorize administrator playground calls

- Status: accepted
- Date: 2026-08-24
- Decision owner: user

## Context

The global administration graph opens a playground from one assignment or
exact provider-model. A service API key is a backend credential and cannot
enter the browser application. Requiring a service workspace would also make
an administrator diagnostic call look like service-owned product traffic.

Each allowlisted administrator already has unrestricted Router authority. The
product has no fine-grained administrator permission scopes.

## Decision

Let an allowlisted administrator session execute model, embedding, image,
video, and audio playground calls. Require the session-bound CSRF token and
exact allowed Origin for call admission. Do not use a service API key or
workspace.

An exact call uses one enabled global provider-model. An assignment call
requires one selected service only to resolve that service's assignment and
inheritance configuration. The service does not authorize, own, expose, or
delete the call record.

Keep administrator playground accounting, detailed logs, media jobs, and
retained objects as global administrator-only records. Identify them with an
immutable administrator call actor and subject. Use the normal call core for
capability checks, attempt accounting, fallback before visible output, and no
fallback after visible output.

## Alternatives

- Put a service key in the administration browser. This exposes a backend
  credential and gives the call false service authority.
- Require a service and workspace. This mixes global diagnostics with service
  accounting and deletion rules.
- Keep the run action disabled. This prevents the administrator from testing
  the global configuration in its management context.
- Add a special administrator permission scope. This conflicts with the one
  unrestricted administrator role.

## Consequences

- The native API needs administrator-only playground call operations.
- Administrator records need an actor type and no service or workspace owner.
- Assignment calls need a service configuration snapshot. Exact calls do not.
- Service APIs and service deletion cannot expose or remove administrator
  playground data.

## Review conditions

Review this decision if the product adds fine-grained administrator authority
or if administrator diagnostics must be charged to an external billing owner.
