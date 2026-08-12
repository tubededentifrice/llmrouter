# Routing, failover, and request lifecycle

Status: Accepted sections only. Admission receipts, status representation,
idempotency retention, and exact error codes remain open.

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

## Error classification and fallback

LLM Router MUST normalize each failed provider attempt into a stable error
class. The classification MUST state the known failure scope: attempt,
provider-model route, provider instance or credential, assignment candidate,
or complete logical request.

Before the streaming or external-effect commit boundary, the router MUST try
the next eligible assignment candidate outside the known scope of a scoped
failure. This includes, when applicable:

- provider credential or provider-account authentication failure;
- provider-specific policy refusal;
- provider-model cost, quota, or budget ineligibility;
- rate limit, timeout, transport, or provider availability failure;
- provider-specific request incompatibility.

Fallback MUST NOT bypass a request-wide failure. The router MUST stop the
logical request for an invalid caller identity, service or workspace denial,
router-wide policy rejection, exhausted logical-request or owning-scope hard
budget, invalid provider-neutral request, cancellation, or passed commit
boundary.

A provider-specific policy refusal MAY fall back only when the next candidate
is allowed by the same effective router and service policy. The router MUST
NOT use fallback to evade a policy that the service requires across all
providers.

A scoped failure MUST make all candidates in its known affected scope
ineligible for the rest of that logical request. For example, a provider
credential failure MUST skip later candidates that use the same provider
instance or credential. It MUST NOT make an unrelated provider instance
ineligible.

Each attempt record MUST include the normalized class, provider code when
safe, affected scope, retry and fallback decision, and assignment revision.
The administration interface MUST show aggregate and recent failure
indicators by provider, provider-model, and assignment when that scope is
known. It MUST distinguish authentication, policy, budget, rate, availability,
and request-compatibility failures. It MUST redact credentials and unsafe
provider content.

## Hierarchical budgets

LLM Router MUST support inherited hard limits and warning thresholds at the
global, service, workspace, and assignment scopes. A child value MUST stay
within all applicable ancestor limits. A request MUST satisfy every applicable
hard limit.

One logical request MUST have one cost budget across all provider attempts,
agent steps, and routed external-tool attempts that the budget includes.
Fallback MUST NOT reset this budget. Before an attempt, the router MUST reserve
a conservative estimated amount from the remaining logical and owning-scope
budgets. It MUST skip a candidate that cannot fit and MAY try a cheaper eligible
candidate.

Final reported usage MUST reconcile the reservation. The ledger MUST preserve
the estimate, reservation, reported usage, applied price version, actual or
corrected cost, and released amount. A provider can report billable usage for
a failed attempt; that usage MUST consume the applicable budget.

A late usage or cost correction MAY put a scope over its hard limit. It MUST
NOT change the result of completed work. The router MUST stop applicable new
admissions until the scope has available budget or an authorized change raises
or resets the limit.

The administration interface MUST show limit, reserved, used, corrected,
remaining, and enforcement state for each scope. It MUST indicate the request
and candidate that caused a budget rejection. Exact cross-node reservation and
outage behavior remains open.
