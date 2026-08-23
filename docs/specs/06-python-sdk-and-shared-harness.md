# Python SDK and shared harness

Status: Accepted on 2026-08-23.

## Location and boundary

The first shared SDK and complete multi-turn harness MUST support Python. A
TypeScript server SDK MUST NOT be part of the first delivery.

Framework-neutral SDK and harness code MUST live in `../opendle-lib`. Calling
services MUST use that shared implementation and MUST NOT copy its generic
loop behavior. The Router service MUST NOT run the harness.

The FJ2 harness MUST be the primary behavior reference. Only behavior in these
specifications is accepted.

## Conversation ownership

The harness MUST accept current conversation state and return updated state.
It MUST NOT own a durable conversation database. A caller MUST own durable
storage, deletion, user authorization, and domain links.

The harness MAY accept small caller-provided load and save callbacks. The
shared library MAY provide an in-memory store for tests and short-lived
processes. A caller MUST NOT depend on that store for durable state.

## Tool loop

The harness MUST support service-provided tools. The caller MUST supply the
eligible tools and the complete executor. The harness MUST execute multiple
tool calls from one model turn sequentially in model order by default.

A caller MAY replace the complete tool executor for a special need. The
harness MUST NOT expose parallel tool execution as a normal mode. Tool input,
authorization, effects, recovery, and durable results MUST remain caller
responsibilities.

## Sticky model route

After the first successful workflow model call, the harness MUST try that
exact provider connection and wire model first on later turns. If the route is
no longer enabled, no longer supports the call, or fails before visible
output, the SDK MUST continue through the current named assignment fallback
chain. The failed sticky route MUST NOT run a second time in that harness turn
if it also occurs in the current chain. The next successful route MUST become
sticky.

A sticky attempt and each fallback attempt MUST be separate Router attempts
for logging, usage, cost, and errors. The same workspace and caller tags MUST
apply.

## Compaction and pruning

The harness MUST support automatic conversation compaction or message
pruning. The caller MUST select the method and its bounded limits.

Model-based compaction MUST pin the exact provider connection and wire model
that handled the preceding successful workflow call. It MUST NOT resolve the
assignment again and MUST NOT use fallback. If the exact route cannot compact
the conversation, that compaction call MUST fail.

The caller MUST select one bounded response to compaction failure: stop the
workflow with the compaction error or use deterministic message pruning under
the configured message and byte limits. The harness MUST NOT retry compaction
on another provider.

The harness MUST preserve the compatible conversation and tool prefix when it
asks the model to compact context. Compaction MUST be a separate Router model
call with the same workspace and caller tags. It MUST have its own detailed
log, usage, cost, and failure result.

## Connection failure

The SDK MUST NOT add durable admission, status, cancellation, replay, or
idempotency behavior to a model or embedding call. A connection loss after the
Router can have started provider work has an uncertain result. The SDK MUST
return that uncertainty and MUST NOT automatically create a replacement call.

The SDK MAY retry a connection setup only when it has proof that the Router did
not accept request bytes and provider work could not start. Provider fallback
after request acceptance MUST remain Router behavior.
