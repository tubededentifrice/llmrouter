# Allow a 24-hour service-secret rotation overlap

## Context

A service bootstrap-secret rotation must support a staged application
deployment. A short overlap can make a normal deployment fail. A long overlap
keeps the prior secret valid after the new secret is available.

## Accepted choice

The user accepted a maximum and default overlap of 24 hours. An administrator
can select a value from zero to 24 hours and can end the overlap early.
Revocation has no overlap. A short-lived token cannot outlive its credential
generation.

## Alternatives

- A 1-hour overlap reduces exposure but requires a fast deployment.
- A 7-day overlap supports slow deployments but keeps the prior secret valid
  for too long.
- No overlap requires an atomic service deployment.

## Good effects

- A normal staged deployment can rotate without avoidable downtime.
- The interface can show one exact deadline.
- An administrator can use a shorter overlap or stop it early.

## Bad effects

- The prior secret can still exchange tokens during the selected overlap.
- Operations must protect two valid generations during rotation.

## Migration effect

There is no data migration. Service clients must deploy the new secret before
the overlap ends.

## Security effect

Each generation has separate audit records and revocation. Scope expansion
still needs recent administrator authentication. Revocation stops exchange
immediately.

## Review conditions

Review this choice if service deployments normally need more than 24 hours or
if incident evidence shows that the overlap creates unacceptable exposure.
