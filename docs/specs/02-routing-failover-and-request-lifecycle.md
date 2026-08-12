# Routing, failover, and request lifecycle

Status: Accepted sections only. Exact error codes, the complete status-
transition table, and the cancellation reconciliation time limit remain open.

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

The official client MUST create one opaque UUIDv7 for each intentional logical
request. The UUID MUST use cryptographically strong random bits and MUST NOT
contain service, workspace, user, assignment, or content data. The router MUST
scope the identity to the authenticated service and, when supplied, workspace.

For a first submission, the router MUST validate the UUIDv7 timestamp against
a configured maximum initial age and future clock-skew limit. The safe defaults
MUST be 15 minutes of initial age and 5 minutes of future clock skew. A repeat
for an existing binding remains valid during its retention period even after
the initial-age window. The router MUST reject an unknown UUIDv7 outside the
initial-age window. It MUST NOT admit it as a new request.

Before provider work, shared-tool work, or another external effect starts, the
router MUST durably bind that identity to a collision-resistant fingerprint of
the complete provider-neutral request and its admission-relevant context. A
successful binding is the admission event. The router MUST return an admission
receipt that contains the logical request identity, admission time, current
state, and status location.

The binding MUST use one strongly serialized, atomic create-if-absent operation
across all eligible data-plane nodes. Only the operation that creates or finds
the matching binding can continue. This admission operation MUST finish before
external work starts. It is not required on each token, stream chunk, or
provider attempt.

The formal API contract MUST mark each request field as fingerprinted or
transient. The fingerprint MUST include the operation and API versions,
authenticated service and optional workspace, data profile, assignment or
approved exact route, input, tool definitions and allow-list, model and agent
limits, budget and timeout controls, output controls, and each attachment
identity and content digest. It MUST include any later field that can change
execution, cost, tools, or result meaning.

The fingerprint MUST exclude credentials, transport-only headers, trace
fields, node identity, receipt time, current status, and another field that the
formal contract marks as transient. Its first version MUST use RFC 8785 JSON
canonicalization, immutable attachment digests, and SHA-256. The binding MUST
store the fingerprint version and digest. Canonical request bytes remain
subject to their content retention rule and MUST NOT be stored only to support
idempotency.

A repeat submission with the same identity and fingerprint MUST return the
existing admission and MUST NOT create another logical request. A repeat with
the same scoped identity and a different fingerprint MUST fail as an identity
conflict and MUST NOT expose the earlier request content. The binding and
status MUST be available through another eligible router node.

The router MUST expose an authenticated status operation for an admitted
request. An uncertain client timeout MUST use the same request identity to
submit the same request again or obtain its status. It MUST NOT create a second
logical request. The router MAY report a nonterminal state while work or
reconciliation continues. A status read MUST enforce the original service and
workspace isolation.

The router MUST retain a nonterminal binding and status until the request
becomes terminal. It MUST retain a terminal status and its idempotency binding
for 24 hours after the terminal transition. This operational retention MUST
NOT shorten accounting, audit, or captured-content retention. The official
client MUST NOT automatically resubmit an expired request identity. The server
MUST use the initial-age check to reject that identity after its binding
expires. An intentional new logical request MUST use a new UUIDv7.

## Cancellation

Cancellation MUST require an explicit cancel permission for the request's
authenticated service and, when supplied, workspace. A cancel operation MUST
be idempotent. For a nonterminal request, it MUST first record
`cancel_requested`. It MUST stop new provider attempts, retries, fallback,
agent steps, shared-tool attempts, business-tool calls, and other external
effects. It MUST request cancellation of active provider or tool work when the
adapter supports it.

The router MUST report `cancelled` only after it confirms that no active work
can continue. Cancellation does not undo output or an external effect that is
already visible. The result MUST identify partial output and committed effects
when they exist. If the router cannot confirm whether active work or an effect
stopped, it MUST keep `cancel_requested` while bounded reconciliation can make
progress. If that reconciliation ends without proof, it MUST report the
terminal state `uncertain`. It MUST NOT report `cancelled` only because the
caller disconnected.

If a request was already terminal, cancellation MUST preserve that terminal
state and report that the request was too late to cancel. Provider usage that
arrives during or after cancellation MUST still enter accounting and budget
reconciliation.

Each cancel operation MUST write an immutable audit event with the actor,
request scope, time, prior state, permission result, adapter stop results, and
final state when known. It MUST NOT include a credential or unsafe provider
content.

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
