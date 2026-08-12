# Reliability, deployment, and operations

Status: Accepted sections only. Placement, backup, restore, and capacity
details remain open.

## Static node discovery

The official client MUST use an explicit ordered list of data-plane endpoints
from deployment configuration. It SHOULD place an eligible loopback endpoint
first. It MUST NOT require DNS service discovery or a control-plane node
registry.

The client MUST validate each endpoint identity, health-check endpoints, use
the first healthy eligible endpoint, and fail over in configured order. It
MUST apply bounded connection and health timeouts and MUST NOT send a request
to an endpoint whose identity does not match its configured trust policy.

Router nodes MUST use explicit configured lists for control-plane primary and
standby endpoints and for any required peers. A topology change MUST use a
deployment configuration update and controlled reload or restart. Health and
administration interfaces MUST show the configured order, current selection,
last successful contact, and failure state without exposing credentials.

## Provider health circuits

Each data-plane node MUST make provider health-circuit decisions from its local
attempt results. A circuit MUST have closed, open, and half-open states. It MUST
separate at least provider instance, provider-model route, operation or required
capability, and normalized failure class when those scopes differ. A failure in
one credential or route MUST NOT open an unrelated route.

Only a result that gives evidence about the selected provider scope MAY affect
a health circuit. Provider timeout, transport failure, provider server failure,
rate limit, invalid provider response, and provider credential failure MAY
affect their narrow known scopes. A valid compatible provider response MUST be
a success sample. Caller identity, request validation, router or service policy,
privacy, owning budget, cancellation, and request-specific incompatibility
results MUST NOT affect provider health. A provider policy refusal MUST remain
an eligibility result, not a health sample.

Window size, minimum sample count, failure threshold, open duration, probe
limit, and maximum backoff MUST be editable within global safety limits. Half-
open probes MUST use bounded concurrency and jitter. A probe MUST pass normal
policy, budget, rate, and accounting checks. Health probing MUST NOT bypass a
provider restriction or create unaccounted billable work.

The control plane MUST aggregate node health and publish authenticated,
expiring fleet hints. Each hint MUST bind to a provider instance and route
generation, operation or capability, normalized failure class, source sample
window, publication time, and expiry. The default lifetime MUST be 60 seconds
and the configurable maximum MUST be 5 minutes.

A fresh hint MAY open the matching local circuit or delay its half-open probe
until the hint expires. Thus, a hint can only suppress a candidate. It MUST NOT
close a local circuit, make a candidate eligible, change candidate order among
healthy routes, or change a configured threshold. A node MUST combine a hint
with local state by selecting the more restrictive open state and later probe
time. It MUST continue with local circuits when hints are unavailable, invalid,
for a different generation, or expired.

Health and administration interfaces MUST show local circuit state, fleet hint
state, scope, sample period, normalized reason, next probe time, and last state
change. An authorized administrator MAY request a bounded probe or reset local
history. A reset MUST be audited and MUST NOT override policy or eligibility.

## Local service and configuration outage

A healthy application server SHOULD use a local LLM Router data-plane node for
normal requests. A local node MAY fail over to an eligible remote node through
the official client or local traffic layer.

A data-plane node MAY continue to admit requests with its last valid normal
configuration for no more than 24 hours after it received and validated that
configuration revision. After 24 hours without a new valid revision, it MUST
stop admitting affected requests until it receives a valid revision.

Credential revocation, service disablement, workspace disablement, and urgent
security policy changes MUST use a separate high-priority distribution path.
A node MUST apply an authenticated urgent revision before later normal work.
Urgent distribution MUST NOT wait for normal telemetry or accounting batches.

The router MUST expose the active configuration revision, its age, and stale
state in health and administration interfaces. Each admitted request MUST
record the configuration revision that it used.

## Run recovery performance

Normal token streaming MUST NOT depend on synchronous remote replication.
Strong ownership coordination is limited to run admission, ownership transfer,
and recovery actions that need fencing. The implementation MUST measure and
publish the added latency from local durability, lease work, and checkpointing.

## Local accounting spool and central ledger

Each data-plane node MUST have an encrypted, append-only local event spool. It
MUST write the applicable immutable accounting or audit event before it reports
the related durable success state.

The node MUST send spooled events asynchronously to one logical central ledger.
The ledger MUST ingest by immutable event identity and MUST make a repeated
delivery idempotent. A node MUST keep an event until central ingest is
confirmed or an approved repair procedure transfers responsibility.

A bounded spool MUST use editable warning, shedding, and stop-admission
thresholds below its reserved emergency capacity. Each admission MUST reserve
enough spool bytes for the maximum bounded canonical event load of that request
or run. The bound MUST derive from fixed event-size limits, permitted attempt
and tool-step limits, and maximum admitted concurrency. The node MUST reject
work when it cannot make this reservation. It MUST release reserved bytes only
when ingest is confirmed or responsibility is safely transferred.

At the warning threshold, the node MUST alert and increase delivery urgency. At
the shedding threshold, it MUST stop optional diagnostic logs and set content
capture off for each new request that it still admits. It MUST record the
effective admission-time capture state and pressure reason on that request and
write a pressure-policy audit event. It MUST NOT change the capture policy of a
request that is already admitted. It MUST stop new background, batch,
playground, and agent work before normal foreground work.

At the stop-admission threshold, the node MUST reject all new work. It MUST
preserve reserved capacity for already admitted work, cancellation,
reconciliation, security operations, and delivery of canonical events. It MUST
NOT silently discard or overwrite canonical accounting or audit events at any
pressure level. Health and administration interfaces MUST show spool bytes,
age of the oldest event, threshold state, shed classes, delivery error, and
estimated remaining capacity.

Admission MUST remain stopped until configured recovery hysteresis is met. If
reserved emergency capacity is at risk, the node MUST stop optional work from
already admitted runs and MUST NOT start another external effect from any run.
The bounded reservation MUST remain available for the result of an external
effect that is already in progress. The node MUST enter an operator-visible
emergency state and MUST not report a durable success that lacks its required
canonical event. An implementation MAY use an encrypted configured spill
volume, but it MUST apply the same reservation, integrity, and recovery rules.

## Leased budget allowances

The central budget authority MUST allocate bounded, expiring allowance leases
to data-plane nodes for each applicable hard-budget scope. A node MUST consume
all applicable global, service, workspace, and assignment allowances
atomically in its local admission path. Normal admission MUST NOT wait for a
central reservation when sufficient valid local allowance exists.

A node SHOULD renew allowance asynchronously before it is low or near expiry.
During a control-plane outage, it MAY continue within its unexpired allowance.
It MUST stop affected new admissions when an applicable allowance is empty or
expired. It MUST NOT borrow from another scope or issue its own allowance.

The budget authority MUST fence lease generations, bound the total outstanding
allowance, and reclaim an expired lease only after its safety window. Issued
allowance plus centrally reserved and used budget MUST NOT exceed the available
admission budget for a hard-limit scope. Node failure and retry MUST NOT make
the same allowance valid on two owners.

A request that fits its conservative reservation can later cost more than the
reservation. This usage correction is the only permitted hard-limit overage.
The configured reservation margin, allowance size, and expiry MUST give a
documented maximum correction risk for each scope. Accounting reconciliation
MUST correct used and returned allowance without changing completed request
results.

## Warm control-plane standby

The first release MUST use one writable control plane and at least one warm
standby in the high-availability profile. The primary MUST asynchronously
replicate configuration, identity, credential ciphertext, budget authority,
request status, run ownership, ledger, and audit state needed for recovery.

Automatic promotion MUST require a fencing mechanism that prevents the old
primary and promoted standby from accepting writes at the same time. A standby
MUST NOT promote only because it cannot reach the primary. After promotion,
data-plane nodes MUST select the promoted endpoint from their configured static
control-plane list.

Promotion MUST establish a new authoritative control epoch that fences every
earlier run-owner epoch. The promoted standby MUST NOT take over a run until it
has reconciled the last durable ownership checkpoint and each recorded external
effect intent. An effect with no confirmed result MUST stay uncertain and MUST
NOT run again automatically.

A primary MUST NOT confirm central ingest of a canonical accounting or audit
event until a standby or an independent durable replay journal can recover that
event. This confirmation can use batches and MUST NOT add a remote write to the
model token or stream-chunk path. A data-plane node MUST retain an unconfirmed
event in its spool.

The system MUST expose replication lag, last confirmed replay position,
promotion state, and possible data-loss window. Failback MUST be an explicit
operator action after reconciliation. Backups and restore tests remain required
and do not become optional because a warm standby exists.

## Recovery objectives

The high-availability profile MUST target a control-plane recovery time
objective of 5 minutes from loss of the writable primary to fenced availability
of the promoted control plane. It MUST target a recovery point objective of 30
seconds for general asynchronously replicated control state.

An acknowledged admission binding, request terminal or cancellation state,
stream or external-effect commit boundary, fencing or ownership change, urgent
security revocation, and canonical accounting or audit ingest MUST remain
recoverable with zero acknowledged-event loss. The system MUST NOT acknowledge
one of these operations until a standby or independent durable replay journal
can recover it. This rule does not require synchronous replication for tokens,
stream chunks, diagnostics, or normal telemetry.

The project MUST run automated failover tests before each release and at least
weekly in a high-availability test deployment. It MUST run an operator disaster-
recovery exercise at least quarterly. Tests over a rolling year MUST include
primary process loss, primary host loss, control-plane network isolation,
standby restart and replay, and promotion with active requests.

RTO measurement MUST start when the writable primary first cannot accept a
valid control operation and MUST stop when a fenced promoted control plane
accepts that operation and clients can recover status. The test passes RTO only
at 5 minutes or less. RPO measurement MUST compare the recovered general state
with every acknowledged primary commit time. The test passes RPO only when no
missing general commit is older than 30 seconds before the failure. It passes
the zero-loss rule only when every acknowledged critical event listed above is
recoverable and no external effect repeats.

Each exercise MUST record achieved RTO, achieved RPO, lost or uncertain
operations, repeated effects, and manual actions. Administration MUST show the
latest test and exercise results. A deployment that misses an objective MUST
show a degraded high-availability state.

## S3-compatible content and archive storage

LLM Router MUST support S3-compatible object storage for encrypted content
segments, retention exports, and archives. Each immutable object MUST have a
checksum and manifest identity. Export or deletion work MUST be safe across
retry and worker takeover.

Bucket durability, replication, lifecycle, and placement are deployment
configuration within accepted minimums. Object storage MUST NOT be the live
source for request status, idempotency, active run ownership, leases, or
fencing.
