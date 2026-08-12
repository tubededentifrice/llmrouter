# Process authorized service data with normal capture

- Status: accepted
- Date: 2026-08-12
- Decision owner: user
- Supersedes: decision 0028

## Context

Xbot uses LLM Router for its agent harness. Some accepted xbot work needs
private memory, inbox messages, direct messages, or member conversations from
Ontology. The public-only router profile prevents these accepted workflows.
Ontology remains the canonical store, but the model request must process the
selected content.

## Decision

Accept one initial `service-data` profile. It can process public or private
content that the calling service is authorized to use for the exact request.
Use the same complete-content capture default and editable retention rules for
this profile.

Do not permit credentials, access tokens, session cookies, passkey material,
private keys, authorization headers, or other control secrets. Keep calling-
service authorization, data minimization, and provider eligibility in the
calling service.

Treat captured content as a router technical copy. A source deletion does not
start router capture deletion in the first release. The copy expires through
the router retention rule that applied at admission. Do not expose captured
content through the service-scoped administration API or hosted service view.

## Alternatives

- Keep public-only processing. This blocks accepted xbot agent work.
- Add separate public and private profiles now. This adds policy branches that
  the current internal deployment does not need.
- Disable capture for all xbot work. This removes useful router diagnostics
  and is not required for the accepted internal use.

## Consequences

- Xbot can use one router harness for its accepted agent work.
- Ontology stays the canonical xbot data store.
- Router capture can outlive deletion of the source record until normal
  expiry.
- The service-data name does not state that private content is public.

## Migration effect

There is no deployed router data to migrate. Replace planned `public-data`
request values with `service-data` before implementation.

## Security effect

The calling service must authorize and minimize each request. Router secret
redaction, encryption, access control, capture policy, and retention still
apply. Only a global administrator with content-read permission can read
captured content in the first release.

## Review conditions

Review this decision before a regulated deployment, external customer use, a
new data-residency promise, or a service-scoped content-read feature.
