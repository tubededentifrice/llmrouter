# Publish one image with runtime roles

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Small deployments need one combined service. Larger deployments need separate
control-plane, data-plane, and worker scaling without separate build pipelines.

## Decision

Publish one immutable image that can run combined, control-plane, data-plane,
or worker roles. Provide production Compose first. Add Kubernetes manifests or
a Helm chart after the first stable release.

## Alternatives

- Publish separate images. This gives explicit artifacts but duplicates build
  and release work.
- Support a combined process only. This is simple but limits scaling and
  failure isolation.

## Consequences

- One release version applies to all roles.
- Startup validation rejects incompatible role configuration.
- Kubernetes packaging does not block the first stable release.

## Migration effect

The first deployments can use the combined role. Operators can split roles
without changing the artifact or public contract.

## Security effect

Each role disables routes and credentials that it does not need. One image
does not give each running role all configured authority.

## Review conditions

Review this decision if role isolation needs different build artifacts, or if
one image causes unacceptable size or patching risk.
