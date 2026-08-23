# LLM Router native API contract

This directory contains the complete version 1 wire contract. The contract is
native to LLM Router. It does not copy a provider API.

`openapi.yaml` is the normative HTTP contract. All request and response object
schemas are closed. A client must not send fields that the schema does not
define.

Service applications use a long-lived service key in the `Authorization`
header. Each runtime request names one workspace. A model request selects one
assignment or one exact provider-model. An exact provider-model call has no
fallback.

The root service has an implicit `default` assignment. If a call names a
missing assignment, the Router creates it and makes it inherit `default`. One
direct assignment chain replaces its inherited chain. The Router tries each
eligible candidate at most once for one call. It does not retry one candidate.
An assignment reports its observed call requirements. A service or global
administrator can remove an observed requirement that is no longer applicable.

Global administrators use the protected browser session. Administrative write
operations also require the CSRF and Origin headers.

Provider connections, models, provider-models, and credentials are global.
Services cannot own or restrict them. Configuration writes validate and apply
the complete submitted value. They do not create revisions, drafts, rollouts,
or rollback records. A price synchronization keeps the last valid price when a
source has no value or fails.

Record lists use bounded cursor pagination. The global administrator can set
the one detailed-log retention duration from 1 through 30 whole days.
`GET /v1/metrics` supplies Prometheus text for deployment monitoring. It has no
application authentication, so the deployment must limit network access to the
monitoring system.

The other normative files are:

- `stream-protocol.md` for server-sent model stream events.
- `errors.md` for the stable error names.
- `contract-policy.yaml` for operation access and conformance fixtures.

`contract-digests.json` is generated from this contract. Run
`./scripts/check-api-contracts.sh` to validate the contract, fixtures, and
digests.
