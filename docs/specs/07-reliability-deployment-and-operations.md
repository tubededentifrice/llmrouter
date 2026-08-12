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
