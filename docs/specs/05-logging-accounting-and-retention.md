# Logging, accounting, and retention

Status: Accepted sections only. Exact event schemas, storage products, export
formats, and global limit ranges remain open.

## Data classes

LLM Router MUST keep these data classes separate:

- logical request records;
- provider-attempt and external-tool-attempt records;
- token, unit, price, and cost accounting records;
- agent-run and business-tool audit records;
- security and administration audit records;
- operational metrics and diagnostic logs;
- captured prompt, response, search-query, provider-error, and tool content;
- configuration revisions.

One retention or access setting MUST NOT silently apply to all data classes.

## Content capture

Complete content capture MUST be enabled by default for the current
public-data service profile. It can include prompts, model responses, search
queries, provider errors, external-tool input and output, and business-tool
input and output.

Content capture MUST be an explicit effective configuration value. A global
administrator can set the fleet default and limits. A service or workspace can
override the inherited value within those limits. A configuration change MUST
state when it becomes effective and MUST NOT change the content policy of a
request or run that has already been admitted.

Content capture MUST NOT store provider credentials, service bootstrap
secrets, access tokens, session cookies, passkey enrollment secrets,
authorization headers, private key material, or other control-plane secrets.
The router MUST remove these values before content leaves the process that
received them.

Captured content MUST be encrypted in transit and at rest. Reads MUST require
an explicit content-read permission and MUST create an audit event. The
interface MUST show the service, workspace, request or run, capture policy,
and expiry.

Before a service starts to process non-public or regulated data, its owner MUST
review and set the applicable content-capture and retention policy. The router
MUST permit capture to be disabled without disabling accounting or audit.

## Editable retention defaults

The initial fleet defaults are:

- diagnostic logs: 7 days;
- captured content: 7 days;
- raw logical-request and attempt accounting: 90 days;
- daily accounting aggregates: 2 years;
- security and global-administration audit: 2 years;
- configuration revisions: the latest 100 revisions and all revisions from
  the last 2 years.

These values MUST be configuration, not compiled constants. A global
administrator MUST be able to change fleet defaults and global minimum or
maximum limits. A service or workspace MUST be able to select an allowed value
without a deployment. The nearest configured value MUST replace the inherited
value for that data class.

A retention change MUST show its affected data classes and estimated deletion
or storage effect before confirmation. It MUST create an audit event. A longer
new period MUST NOT imply that already deleted data can return.

## Accounting integrity

Each logical request, provider attempt, external-tool attempt, and business
tool call MUST have a stable identity. Accounting ingest MUST be idempotent by
immutable event identity.

The ledger MUST record reported usage for successful, failed, refused,
interrupted, and uncertain provider attempts when usage is available. It MUST
support later price, provider-usage, or invoice reconciliation without changing
the original event.

Accounting aggregates MUST be reproducible from retained canonical accounting
events for the period in which those events remain available.
