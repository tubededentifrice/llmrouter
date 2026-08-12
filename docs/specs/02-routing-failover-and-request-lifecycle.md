# Routing, failover, and request lifecycle

Status: Accepted sections only. Admission receipts, status representation,
error classes, and idempotency retention remain open.

## Assignment and diagnostic selection

A normal production request MUST select a named assignment. The router MUST
resolve the effective fallback chain from one immutable configuration revision
for each provider attempt.

An approved playground or diagnostic operation MAY select one exact
provider-model. Exact selection MUST require a short-lived diagnostic
permission. It MUST NOT disable service and workspace isolation, provider
eligibility, privacy policy, budget checks, rate controls, accounting, or
audit. The interface MUST make the selected provider and model clear.

## Logical requests and attempts

One logical request can have multiple provider attempts. The router MUST give
the logical request and each provider attempt different stable identities. It
MUST keep request accounting separate from attempt accounting.

After request admission, LLM Router owns provider retry, fallback, health
filtering, and error classification. A calling service MUST NOT start a second
logical request as a retry for an admitted request.

The router MUST expose a safe status operation for an admitted request. An
uncertain client timeout MUST use the same request identity to obtain or
continue the result. The router MAY report that the final state is still
pending. A status read MUST have the same service and workspace isolation as
the original request.

The exact admission event and idempotency-key contract remain open. The final
contract MUST prevent two logical requests when a client cannot tell whether a
submission was admitted.

## Streaming commit boundary

The router MAY retry or move to another provider before it releases model
output or accepts a tool-call continuation with an external effect.

After it releases model output, it MUST NOT automatically restart the request
with another provider. After it commits a tool continuation or other external
effect, it MUST NOT automatically repeat that effect through fallback.

If the active provider fails after one of these commit boundaries, the router
MUST end the stream as interrupted. It MUST return the stable logical request
identity and enough safe state for the calling service to inspect the result.
It MUST NOT describe partial output as a complete response.
