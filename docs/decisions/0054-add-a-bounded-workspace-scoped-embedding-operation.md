# Add a bounded workspace-scoped embedding operation

- Status: accepted
- Date: 2026-08-13
- Decision owner: user

## Context

Ontology needs embeddings for derived vector search. LLM Router already owns
provider selection, credentials, fallback, accounting, and cost control. The
Router contract does not have an embedding operation.

The integration needs one stable model space across fallback candidates. It
also needs clear data retention and cost rules. The user accepted normal
`service-data` capture and Router retention after Ontology source deletion. The
user also accepted strict model-space and dimension equality and workspace
budget enforcement without a required per-request cost limit.

## Accepted choice

Add asynchronous embedding create and status operations for one service and
one explicit workspace. Use a named assignment. Require all fallback
candidates to have the same opaque Router model-space identity and exact
dimension. Reject a mixed assignment before publication and reject a request
that does not match the effective chain.

Use fixed first-release batch, input, time, dimension, and response limits.
Return a batch only when all items succeed. Apply the normal workspace hard
budget. Permit, but do not require, a per-request `max_cost`.

Treat input text as normal captured service data. Set retention at admission.
A later source deletion in Ontology does not delete or shorten the retained
Router copy.

## Alternatives

- Let Ontology call embedding providers directly.
- Permit fallback between models that have different spaces or dimensions.
- Require `max_cost` on each embedding request.
- Delete Router capture when Ontology deletes its source value.

## Good effects

- Provider credentials and fallback policy stay in LLM Router.
- Ontology can store one stable opaque model-space identity with each vector.
- Atomic batches cannot mix vector spaces or partial results.
- Workspace policy controls cost without extra work for each request.

## Bad effects

- Ontology deletion does not immediately remove the retained Router copy.
- A model-space change needs a new assignment and an Ontology reindex.
- Strict fallback rules can reduce the eligible provider set.
- A request without `max_cost` can use the available workspace budget.

## Migration effect

There is no product data migration. Router implementations and official server
clients must add embedding create and status support. Ontology must pin the
`embedding_requests_v1` capability and major version 1 of the
`embedding_protocol` artifact before it starts embedding work.

## Security effect

Service and workspace scope apply to admission and status reads. Provider
secrets stay in Router. Logs, metrics, accounting, audit, and safe errors do
not contain input text, input digests, or vectors. Captured input stays under
the existing encrypted captured-content access controls.

## Review conditions

Review this decision if a calling service needs immediate cross-service
capture deletion, a different batch atomicity rule, another model-space
compatibility rule, or a required request cost limit. Also review it if the
fixed bounds prevent supported embedding models from operating safely.
