# Centralize the service tree and use passkey-only administration

- Status: accepted; the human identity part is superseded by decision 0037
- Date: 2026-08-12
- Decision owner: user

## Context

The global administrator controls all services, parent links, credentials, and
fleet policy. This authority must not be available to a service identity.

## Decision

Only global administrators create services and parent links. Service
administrators control their own assignments and workspaces.

Use passkeys as the only interactive global administrator authentication. Do
not provide public sign-up or another interactive sign-in method. Use a trusted
server CLI to create a short-lived, one-use initial enrollment or recovery
URL. Keep this pattern aligned with Ontology while each service keeps separate
origins, identities, sessions, and audit records.

## Alternatives

- Delegate child-service creation. This gives services more independence but
  increases scope and cost risk.
- Use an identity provider or passwords. This adds authentication methods that
  are not needed for the intended operator group.

## Consequences

- Service-tree changes have one clear authority and audit path.
- Deployment and disaster-recovery procedures must provide the CLI enrollment
  path.
- Loss of all operator access needs a secure server-console procedure.
