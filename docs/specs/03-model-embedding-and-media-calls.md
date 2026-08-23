# Model, embedding, and media calls

Status: Accepted on 2026-08-23.

## Common request rules

Each call MUST authenticate one service API key and identify one workspace
that the service owns. The Router MUST reject a missing, deleted, or foreign
workspace before provider work.

A call MUST select either one named assignment or one exact enabled
provider-model. An exact selection MUST use no assignment fallback. Both paths
MUST use the same workspace isolation, tags, logging, usage, cost, and safety
limits.

A request MAY contain 0 through 32 plain string tags. One tag MUST contain 1
through 128 UTF-8 bytes. The complete normalized tag set MUST contain no more
than 2048 UTF-8 bytes. The Router MUST remove duplicate tags and sort the set
by UTF-8 byte order before accounting. Tag order and duplicate input MUST NOT
create different accounting groups.

A deployment MUST configure bounded request bytes, connection timeouts,
provider-attempt timeouts, concurrency, and output sizes. The formal API MAY
set a smaller limit for one operation. It MUST NOT permit an unbounded input,
output, attempt, or job.

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
