# Use least-privilege grants and global secret custody

## Context

Human administration needs clear delegation. Provider secret changes have a
larger effect than selection of an already eligible credential reference.

## Accepted choice

Use explicit local grants with authority class, scope, operations, expiry, and
revision. Permit delegation only within the grant of the delegating
administrator. Only a global administrator with the applicable grant and
recent authentication can change provider or shared-tool secret material. A
service administrator can select an eligible reference but cannot manage its
secret value.

## Alternatives

- Give each global administrator unrestricted authority.
- Let service administrators create and replace service-owned secrets.
- Depend only on identity-service groups for Router authorization.

## Good effects

- Authority is explicit, local, narrow, and auditable.
- Service work can use eligible credentials without exposing secret custody.
- Pocket ID authentication does not silently grant Router authority.

## Bad effects

- Grant management adds contract and user-interface work.
- Global secret custody can add an operating handoff for service teams.

## Migration effect

The first bootstrap operation must create a local global grant. Each admin
route must declare its required operation and sensitive-action rule.

## Security effect

This choice limits privilege expansion and secret exposure. Grant and secret
changes require recent authentication and immutable audit events.

## Review conditions

Review this choice if operating evidence shows that global secret custody
prevents required service operation without a safe delegated workflow.
