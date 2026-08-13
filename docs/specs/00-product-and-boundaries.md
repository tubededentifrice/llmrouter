# Product and boundaries

Status: Accepted on 2026-08-13.

## Complete platform

LLM Router MUST provide all of these shared functions in its first release:

- model-provider and external-tool routing;
- ordered fallback and provider health handling;
- non-streaming and streaming model calls;
- a complete agent harness;
- request, attempt, tool, token, and cost accounting;
- global administration;
- service-scoped administration;
- direct endpoints for shared external tools.

Use of the agent harness MUST be optional for each service and for each
eligible request. A service MUST be able to use routing, streaming, failover,
accounting, tools, and administration without moving its agent loop into LLM
Router.

LLM Router MUST NOT require a calling service to move domain prompts, business
workflow decisions, domain records, user authorization, or business-tool
implementation into the router.

## Initial services

Crewday, FJ2, and Xbot are the initial calling services. This repository MUST
define shared contracts and MUST NOT contain implementation or migration tasks
for a calling-service repository.

Xbot has no production runtime data. Its product specifications MUST align
with the accepted shared contracts before its implementation starts. Crewday
code alignment and FJ2 code and data migration belong to separate work in
their own repositories. FJ2 migration MUST NOT start until the applicable
shared contracts are accepted.

## Initial data profile

The first release MUST expose a versioned request data-profile field. Its only
accepted value MUST be the named `service-data` profile. A caller MAY use this
profile for public, private, personal, or unpublished content that the caller
is authorized to process for the exact request.

The calling service remains responsible for user authorization, workspace
authorization, data minimization, and provider eligibility before submission.
The router MUST apply the configured capture, retention, provider, and access
rules to the complete request. The profile MUST NOT permit a provider route
that the service or workspace policy excludes.

The `service-data` profile MUST NOT contain a provider credential, service
bootstrap secret, access token, session cookie, passkey material, private key,
authorization header, or another control secret in a structured control field.
The router MUST reject a control secret in such a field. It MUST remove a known
authenticated control value before data leaves the receiving process. The
first release MUST NOT classify or reject arbitrary prompt, response, or tool
content only because a broad pattern resembles a secret.
This rule follows [decision 0052](../decisions/0052-use-structured-secret-fields-and-standard-endpoint-trust.md).

Captured content is a retention-bound router technical record. It MUST NOT
become the calling service's canonical domain store. Deletion of a source
record in a calling service does not start capture deletion in the first
release. The captured copy MUST expire under the router retention rule that
applied at admission.
