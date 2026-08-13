# Embedding protocol version 1

## Contract pin

A caller MUST require the runtime capability `embedding_requests_v1` and major
version 1 of the `embedding_protocol` artifact. The runtime artifact digest is
build provenance. A compatible minor deployment does not need the caller's
whole-file digest at runtime.

The create operation is
`POST /v1/services/{service_id}/workspaces/{workspace_id}/embedding-requests`.
The status operation is
`GET /v1/services/{service_id}/workspaces/{workspace_id}/embedding-requests/{request_id}`.
Both operations require the exact service and workspace token scope. A hidden
or wrong scope returns the normal non-disclosing error.

## Admission and input

Create uses a caller-issued UUIDv7 in `X-Logical-Request-Id`. The closed body
contains the API version, `service-data` profile, assignment, `input_policy_id`,
opaque `model_space_id`, exact dimensions, ordered inputs, fixed 120000 ms
logical timeout, and optional `max_cost`.

Each input contains a caller-issued opaque `input_id`, lowercase SHA-256 of the
exact UTF-8 text bytes, and the text. The Router MUST calculate SHA-256 from
the received UTF-8 bytes and compare it with the declared value before
admission. A mismatch returns `invalid_request`. The Router MUST NOT normalize,
trim, or re-encode text before this check.

Input identities MUST be unique in one batch. The caller's input-policy
identity states which stable normalization rules created the text. Router
fingerprints it but does not interpret it.

## Fixed bounds

- A batch contains from 1 through 32 inputs.
- One input contains from 1 through 32768 UTF-8 bytes.
- A batch contains no more than 262144 UTF-8 input bytes.
- A dimension is from 1 through 4096.
- One provider attempt stops after 30 seconds.
- One logical batch has no more than four provider attempts.
- One logical batch stops after 120 seconds.
- An uncompressed status JSON response contains no more than 8388608 bytes.

A deployment can lower input, batch, or dimension limits. It cannot raise a
fixed limit.

## Model space and fallback

Each candidate declares the `embedding` capability, one opaque Router model-
space identity, and one dimension. All candidates in an embedding assignment
MUST have the same identity and dimension. Publication rejects a mixed chain.
Admission rejects a request that does not match the complete effective chain
with `embedding_space_mismatch` before provider work.

Fallback repeats the complete batch. It cannot change model space or dimension.
The Router returns no vector unless all items succeed. A successful result has
one result for each input, in request order. Each vector has the exact requested
dimension.
The Router rejects a non-finite vector value or a vector with a wrong
dimension, count, input identity, or order as an invalid provider response. It
returns no vector for that batch.

## Cost, data, and disclosure

The exact workspace must have an effective hard budget. The Router rejects
admission when this budget is absent, exhausted, or cannot reserve the
conservative batch estimate. An optional `max_cost` can add a lower request
limit. It does not replace the workspace budget. Billable failed attempts
remain in accounting.

Input text uses normal `service-data` capture. Capture state and expiry are set
at admission. Later source deletion in Ontology or another caller does not
delete or shorten Router capture. The Router copy expires under its recorded
admission-time retention rule.

Logs, metrics, accounting, audit details, and safe errors MUST NOT contain
input text, input SHA-256 values, vectors, or the stored request fingerprint.
The public result MUST NOT contain a provider, provider model, provider route,
credential, or fallback path.
