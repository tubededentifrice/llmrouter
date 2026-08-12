# Reliability, deployment, and operations

Status: Accepted sections only. Discovery, placement, backup, restore, and
capacity details remain open.

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

## S3-compatible content and archive storage

LLM Router MUST support S3-compatible object storage for encrypted content
segments, retention exports, and archives. Each immutable object MUST have a
checksum and manifest identity. Export or deletion work MUST be safe across
retry and worker takeover.

Bucket durability, replication, lifecycle, and placement are deployment
configuration within accepted minimums. Object storage MUST NOT be the live
source for request status, idempotency, active run ownership, leases, or
fencing.
