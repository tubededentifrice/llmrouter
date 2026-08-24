# Model, embedding, and media calls

Status: Accepted on 2026-08-23. The administrator playground amendment was
accepted on 2026-08-24.

## Common request rules

Each service call MUST authenticate one service API key and identify one
workspace that the service owns. The Router MUST reject a missing, deleted,
or foreign workspace before provider work.

A service call MUST select either one named assignment or one exact enabled
provider-model. An exact selection MUST use no assignment fallback. Both
service paths MUST use the same service and workspace isolation, tags,
logging, usage, cost, and safety limits.

A request MAY contain 0 through 32 plain string tags. One tag MUST contain 1
through 128 UTF-8 bytes. The complete normalized tag set MUST contain no more
than 2048 UTF-8 bytes. The Router MUST remove duplicate tags and sort the set
by UTF-8 byte order before accounting. Tag order and duplicate input MUST NOT
create different accounting groups.

A deployment MUST configure bounded request bytes, connection timeouts,
provider-attempt timeouts, concurrency, and output sizes. The formal API MAY
set a smaller limit for one operation. It MUST NOT permit an unbounded input,
output, attempt, or job.

## Administrator playground calls

An allowlisted administrator session MUST be able to make synchronous and
streaming model calls, synchronous embedding calls, and image, video, and
audio media-job calls from the global playground. These operations MUST use
the same native messages, tools, structured JSON, image input, embedding,
media input, temperature, output limit, tag, request-size, timeout,
concurrency, capability, and output bounds as the applicable service call.
The administrator contract MUST NOT contain a service API key or a workspace
identity. The browser MUST NOT fetch, store, or send a service API key.

An exact administrator call MUST name one enabled global provider-model. It
MUST omit service context and MUST use no assignment fallback. An assignment
administrator call MUST name one existing assignment and exactly one service
as its configuration context. The Router MUST resolve the assignment through
that service's current parent chain. This service name MUST NOT authorize,
own, expose, or delete the call or any record that it creates. The Router MUST
reject an assignment request with no service context and an exact request
with a service context before provider work.

An administrator assignment call MUST use a read-only assignment snapshot. It
MUST NOT create a missing assignment, change last-use evidence, or add an
observed requirement. A missing service or assignment MUST return `not_found`.
An empty or ineligible effective chain and a disabled or ineligible exact
provider-model MUST return `provider_unavailable`. The Router MUST apply the
normal candidate eligibility, one-attempt, cooldown, fallback, structured-
output validation, and visible-output rules to the snapshot.

Each administrator call MUST require a current administrator session, the
session-bound CSRF token, and the exact allowed `Origin` at admission. A
missing or expired session MUST return `authentication_required`. A missing
or incorrect CSRF token or Origin MUST return `permission_denied`. Polling one
administrator media job and reading its content MUST require a current
administrator session. These reads MUST NOT accept a service key. Session
expiry after admission MUST NOT change the selected route or cancel provider
work that the Router already admitted. A later poll or content read MUST
authenticate again.

The Router application MUST be the only database-transaction owner for an
administrator call. After authentication and complete validation, it MUST
create one logical call and its immutable administrator actor and selection
snapshot in one transaction before provider work. A rollback of this
transaction MUST start no provider work. Provider I/O MUST occur outside a
database transaction. Each completed provider attempt MUST commit its raw
accounting once before the Router starts a fallback attempt, returns a
non-streaming success, sends a stream `completed` event, or makes a media job
terminal. If that commit fails, the Router MUST report `internal_error`, MUST
NOT start another candidate, MUST NOT report success or stream completion,
and MUST NOT repeat the completed provider attempt. After visible stream
output, this dependency failure MUST use the terminal `error` event. A media
worker MAY retry the accounting or terminal-state transaction, but it MUST
use the existing logical-call and attempt identities and MUST NOT repeat
provider work.

Validation that reads a service, assignment, inherited chain, provider-model,
or other current configuration MUST occur in the call-creation transaction or
MUST be repeated in that transaction. A concurrent change MUST be wholly
before or wholly after the admitted snapshot. It MUST NOT produce a snapshot
from parts of two configuration states.

The immutable selection snapshot MUST keep the admitted assignment chain,
provider-model configuration, and call controls. It MUST NOT preserve a
deleted or replaced provider credential for an attempt that has not started.
The credential rules in
[Providers, models, prices, and configuration](02-providers-models-prices-and-configuration.md#provider-connections-and-credentials)
MUST apply when each attempt gets its credential.

A model or embedding response MUST return the logical call identity, elapsed
milliseconds, selected provider-model, usage, and cost that are available for
the successful result. It MUST also return each completed attempt in order
with its route, outcome, elapsed time, reported usage and cost, and safe error.
The interface MUST NOT hide failed-attempt cost, add costs in different
currencies, or claim an unavailable usage value is zero. The administrator
stream MUST identify the logical call before visible output and MUST return
elapsed time, selector, route, attempts, usage, and cost on completion. It MUST
use the native interruption error when failure occurs after visible output. A
caller disconnect MUST use the normal model-call rules and MUST NOT create a
replay or result operation.

A result-level or job-level usage and cost value MUST describe only the
selected provider result. It MUST NOT add values from the complete attempt
list. A caller that totals attempts MUST keep different currencies separate.

A successful model, embedding, stream, or media result MUST contain exactly
one succeeded attempt. That attempt MUST be the last attempt. After the Router
creates the logical call, a failed model or embedding response and a stream
`error` event MUST return the logical call identity, selector, elapsed time,
completed attempts, and safe error. An error before logical-call creation MUST
use the basic error envelope and MUST NOT invent a logical call identity. An
attempt MUST omit usage and cost when they are unavailable. It MUST NOT use a
zero value to represent missing provider data.

Each successful administrator playground response and stream MUST use
`Cache-Control: no-store`. The stream response MUST also return the logical
call identity in its declared correlation header. These headers MUST NOT
contain authority or a secret.

Administrator media operations MUST contain only create, get, and retained-
content reads. They MUST NOT add list, cancel, replay, or delete operations.
The create transaction MUST create the logical call and media job before it
returns acceptance or starts provider work. Each later state change MUST use
one Router-owned transaction. A response MUST return the logical call
identity, current or final selected provider-model, elapsed time when known,
and reported usage and cost when known. It MUST return media bytes only from
the authenticated Router content operation. A job response MUST return its
completed attempts in order. A pending or running job MAY have no completed
attempt. A succeeded job MUST have at least one succeeded attempt.

An administrator call MUST use only safe native errors. An error, response
header, stream event, job, accounting row, log index, and activity detail MUST
NOT contain a provider credential, service API key, administrator cookie,
CSRF token, authorization header, object-store credential, or internal object
location.

Administrator playground operations MUST use the stable errors as follows:

- `authentication_required` for no current administrator session, including
  when the request supplies only a service key;
- `permission_denied` for a missing or incorrect CSRF token or Origin;
- `invalid_request` for a closed-schema error, contradictory selector, missing
  assignment service context, invalid input, or exceeded request bound;
- `not_found` for a missing service, assignment, or administrator media job;
- `provider_unavailable` when selection or capability filtering leaves no
  eligible provider-model;
- `upstream_failed` when one exact attempt or all permitted assignment
  attempts fail before a result;
- `content_unavailable` when media is not ready or retained bytes no longer
  exist;
- `rate_limited` when an applicable rate or concurrency admission bound is
  full; and
- `internal_error` for a transaction or required dependency failure that has
  no safe, more specific error.

`conflict` and `assignment_cycle` MUST NOT report a normal playground call
failure. A configuration write can return these errors through its own
operation. A concurrent configuration change after call admission MUST NOT
change the immutable selection snapshot. A logical call identity MUST be a
correlation value only. It MUST NOT authorize a log, job, media, accounting,
or result read.

## Attempts and fallback

One assignment call MUST have one ordered list of provider-model candidates.
The Router MUST try each eligible candidate no more than once. It MUST NOT
apply a per-candidate retry count, retry delay, or retry backoff.

An assignment model call MAY exclude 0 through 16 unique provider-model API
names. The Router MUST skip a matching candidate before eligibility checks and
provider work. A name that is not in the current assignment chain MUST have no
effect. A skipped candidate MUST NOT count as an attempt or provider failure.
An exact provider-model call MUST NOT contain an exclusion list.

Before visible model output, a provider authentication, rate, timeout,
transport, availability, refusal, incompatibility, or invalid-response failure
MUST move the call to the next eligible candidate. A service, workspace,
input-validation, Router-wide safety, or empty-chain failure MUST stop the
call before another candidate.

After output becomes visible, a provider failure MUST end the call or stream.
The Router MUST NOT start another candidate. A stream failure after visible
output MUST report a stable provider-neutral interruption error before the
connection closes when transport still permits it.

One assignment chain MUST contain no more than 16 candidates. A normal model
or embedding provider attempt MUST have a deployment timeout from 1 through
600 seconds. A complete model or embedding connection MUST have a deployment
timeout no greater than 15 minutes.

## Provider cooldown

Three applicable provider-model failures in a rolling 60-second window MUST
put that provider-model into a 60-second cooldown in the application cache.
Authentication, rate, timeout, transport, availability, and invalid-response
failures MUST count. Caller validation, service isolation, and structured-
output validation failures MUST NOT count unless the provider response caused
the validation failure.

A call MUST skip a candidate while its known cooldown is active. The
administration application MUST show each known current cooldown and its last
failure class. Cooldown state MUST be best-effort cache data. A cache loss or
restart MAY clear it. The product MUST NOT have half-open probes, durable
health history, fleet hints, or an editable circuit-breaker state machine.

## Model calls

A model call MUST support multi-turn messages with text content. Input MAY
also contain uploaded JPEG, PNG, or WebP bytes. The Router MUST NOT fetch an
image URL and MUST NOT accept a caller object-store reference.

One uploaded input image MUST contain no more than 20 MiB. One request MUST
contain no more than 8 images and no more than 50 MiB of uploaded image bytes.
The complete provider-neutral JSON body, excluding uploaded image bytes, MUST
contain no more than 2 MiB. A deployment MAY use smaller limits.

The Router MUST store uploaded input images in Router-controlled object
storage for the detailed-log retention period. Only an authenticated global
administrator MAY read them, and the read MUST use a Router endpoint. A
response MUST NOT expose an object-store bucket, key, credential, direct URL,
or presigned URL.

A caller MAY provide tool definitions and tool-result messages. A model MAY
return one or more tool calls. The Router MUST NOT execute them.

Normal output MUST be text or tool calls. A caller MAY instead request one
JSON Schema. The Router MUST use only candidates that support structured
output and MUST validate the returned JSON before success. An invalid
provider value before visible output MUST use normal fallback.

A caller MAY set temperature from 0 through 2 and an output limit from 1
through 1000000 provider-neutral output units. A deployment or model
capability MAY set a smaller maximum. An assignment or candidate MUST NOT
store defaults for these values.

A non-streaming response MUST finish on the same HTTP connection. A streaming
response MUST use the native stream contract. Model calls MUST NOT have a
durable admission receipt, status route, cancellation route, replay route,
resume token, idempotency binding, or result route after disconnect.

A caller disconnect MUST NOT create a public cancellation or recovery state.
The Router MAY ask the active provider to stop when the adapter supports it.
It MUST still record available attempt usage, cost, outcome, and detailed log
data. The caller MUST NOT receive the later result through another operation.

## Embedding calls

An embedding request MUST contain a bounded batch of 1 through 32 text items.
One item MUST contain 1 through 32768 UTF-8 bytes. The complete batch MUST
contain no more than 262144 UTF-8 bytes. A deployment MAY use smaller limits.

The Router MUST send the complete batch on one selected provider connection.
Fallback MUST repeat the complete batch. A successful response MUST return one
finite vector for each input item in input order. It MUST return no vectors
when one item fails or a provider returns the wrong count or dimension.

An embedding call MUST finish on the same HTTP connection. It MUST NOT use a
media job or have durable admission, status, cancellation, replay, or result
recovery.

## Media generation

The Router MUST support image, video, and audio generation. Image and video
generation MUST accept one text prompt and MAY accept uploaded JPEG, PNG, or
WebP image inputs under the model-call image limits. Audio generation MUST
accept text input. The first release MUST NOT accept audio or video input for
editing, extension, conversion, or remix.

Media generation MUST use a job with only `pending`, `running`, `succeeded`,
or `failed` state. A state change MUST move forward and MUST NOT return to an
earlier state. `succeeded` and `failed` MUST be terminal.

A create response MUST return the job identity and current state. The owning
service MUST be able to read its job and download a successful result through
Router endpoints. The product MUST NOT provide cancellation, detailed progress
phases, worker ownership, general request recovery, or direct object-store
access.

One media job MUST have a deployment deadline no greater than 24 hours. A
provider failure before a provider result MUST use the assignment candidate
order and one-attempt rule. A failed job MUST expose only a safe corrective
error. An uncertain create response MAY result in more than one provider job
if the caller submits a new create request; the API MUST state this limit.

Generated media MUST stay in Router-controlled object storage for the detailed
log retention duration. A media job MAY remain visible after its result
expires, but it MUST report that the retained result is unavailable. A
service or workspace delete MUST make its jobs and media unavailable and MUST
start their deletion. If a deleted scope had a `pending` or `running` job, the
Router MUST keep that job and any late provider result unavailable. It MUST
discard or delete the late result. This internal cleanup MUST NOT add a public
job cancellation or cleanup state.
