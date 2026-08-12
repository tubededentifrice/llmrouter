# Reliability, deployment, and operations

Status: Accepted sections only. Placement, recovery objectives, backup,
restore, and capacity details remain open.

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

A bounded spool MUST apply backpressure before disk exhaustion. It MUST NOT
silently discard canonical accounting or audit events. The exact admission
behavior at each pressure level remains open.

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

## S3-compatible content and archive storage

LLM Router MUST support S3-compatible object storage for encrypted content
segments, retention exports, and archives. Each immutable object MUST have a
checksum and manifest identity. Export or deletion work MUST be safe across
retry and worker takeover.

Bucket durability, replication, lifecycle, and placement are deployment
configuration within accepted minimums. Object storage MUST NOT be the live
source for request status, idempotency, active run ownership, leases, or
fencing.
