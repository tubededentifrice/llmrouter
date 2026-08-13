# Logging, accounting, and retention

Status: Accepted on 2026-08-13.

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
`service-data` profile. It can include prompts, model responses, search
queries, provider errors, external-tool input and output, and business-tool
input and output.

Content capture MUST be an explicit effective configuration value. A global
administrator can set the fleet default and limits. A service or workspace can
override the inherited value within those limits. A configuration change MUST
state when it becomes effective and MUST NOT change the content policy of a
request or run that has already been admitted.

Spool-pressure shedding is an admission-time safety exception for a new
request. The router MUST show that capture was disabled by pressure before or
in the admission result, store the effective state and reason, and audit the
pressure-policy transition. It MUST NOT disable capture for work that is
already admitted.

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

The first-release service-scoped administration API and hosted service view
MUST NOT expose captured prompt, response, search-query, provider-error, or
tool content. A global administrator with the explicit content-read permission
MAY read captured content. Each read MUST require recent authentication and
MUST create an audit event. A service can inspect its request state,
accounting, capture policy, and capture expiry without reading the captured
content.

A calling-service record deletion MUST NOT delete or shorten retained router
capture in the first release. Router capture MUST expire under the effective
retention rule recorded at request admission. A service-facing result MUST not
claim that source deletion removed the router copy.

An embedding input is captured content under the normal `service-data` rule.
The router MUST set its capture state and expiry at embedding admission. A
later source change or deletion in Ontology or another calling service MUST NOT
delete or shorten that Router capture. The Router copy MUST expire only under
the admission-time retention rule. Input text, input digests, and vectors MUST
NOT enter logs, metrics, accounting, audit details, or safe errors.

A captured-content export is a captured-content read. It MUST require the
explicit global content-read permission and Pocket ID authentication no more
than five minutes old. Creating the export, reading its status, and issuing or
using a result location MUST create audit events. A result location MUST be
one-use, expire in no more than five minutes, and MUST NOT enter a log or a
referrer. The router MUST NOT include captured content in a service-scoped
export.

The result location MUST be a same-origin Router endpoint. It MUST require the
current administrator session, the explicit content-read grant, recent
authentication, and a short-lived one-use redemption token. It MUST send
`Cache-Control: no-store` and a no-referrer policy. It MUST NOT be a direct or
presigned object-store URL.
This rule follows [decision 0049](../decisions/0049-proxy-protected-exports-and-version-operations.md).

## Editable retention defaults

The initial fleet defaults are:

- diagnostic logs: 7 days;
- captured content: 7 days;
- raw logical-request and attempt accounting: 90 days;
- agent-run and business-tool audit: 30 days;
- daily accounting aggregates: 2 years;
- security and global-administration audit: 2 years;
- configuration revisions: the latest 100 revisions and all revisions from
  the last 2 years.

These values MUST be configuration, not compiled constants. A global
administrator MUST be able to change fleet defaults and global minimum or
maximum limits. A service or workspace MUST be able to select an allowed value
without a deployment. The nearest configured value MUST replace the inherited
value for that data class.

Configuration-revision retention MUST use both a minimum revision count and a
time period. A revision MUST remain while either rule keeps it. The effective
retention response MUST show both values and the rule that currently keeps the
oldest retained revision.
This rule follows [decision 0049](../decisions/0049-proxy-protected-exports-and-version-operations.md).

The initial allowed range for agent-run and business-tool audit MUST be 7 to
365 days. A global administrator MAY make this range narrower. It MUST NOT
permit a value outside these safety limits. The 30-day default and this range
follow [decision 0042](../decisions/0042-retain-agent-and-business-tool-audit-for-thirty-days.md).

A retention change MUST show its affected data classes and estimated deletion
or storage effect before confirmation. It MUST create an audit event. A longer
new period MUST NOT imply that already deleted data can return.

## Accounting integrity

Each hard-budget scope MUST use one configured accounting currency. Every
limit, reservation, price, cost event, correction, and aggregate within that
scope MUST use the same currency. The first release MUST NOT convert currencies
or use a live foreign-exchange rate. A route with a different source currency
MUST have an authorized price in the scope currency before it becomes eligible.
A currency change MUST start a new budget and price revision and MUST NOT
rewrite prior accounting.
This rule follows [decision 0047](../decisions/0047-use-one-currency-per-hard-budget-scope.md).

Each logical request, provider attempt, external-tool attempt, and business
tool call MUST have a stable identity. Accounting ingest MUST be idempotent by
immutable event identity.

The ledger MUST record reported usage for successful, failed, refused,
interrupted, and uncertain provider attempts when usage is available. It MUST
support later price, provider-usage, or invoice reconciliation without changing
the original event.

Accounting aggregates MUST be reproducible from retained canonical accounting
events for the period in which those events remain available.

## Provider-model pricing

Price authority MUST be explicit for each provider-model route. A route MUST
select a named source and lookup identifier or pin its prices to manual values.
A model-level source MAY prefill an administration form, but it MUST NOT
silently become the effective price authority for another provider's route. A
manual pin MUST prevent scheduled synchronization until an administrator
removes the pin.

The price model MUST support typed components and units. It MUST NOT assume
that all providers charge only for input and output tokens. It MUST be able to
represent applicable token, cached-token, request, image, audio-duration,
search, tool, and other provider units without changing past accounting.
Stored prices MUST use fixed decimal precision that preserves sub-cent and
low-unit rates. The ledger MUST NOT use binary floating point or whole cents as
its accounting source of truth.

The initial automatic schedule MUST run weekly and MUST be editable. A global
administrator MUST be able to synchronize all or selected provider-model
routes on demand and preview a dry run. A service administrator MUST have the
same operations only for routes owned by that service. A relevant provider-
model create or price-source edit SHOULD start an asynchronous single-route
synchronization.
One upstream fetch SHOULD serve all applicable rows in one synchronization run
when the source supports a catalog response. The run MUST use one immutable
source snapshot and record its fetch time, source revision or content hash,
and HTTP validator when available.

A synchronization MUST update only price components, source metadata, price
version, and synchronization state. It MUST NOT import a new model, change
capabilities, change provider routing, or change an assignment. A missing row,
invalid value, or source failure MUST keep the last accepted price. It MUST
record an error and stale state; it MUST NOT replace an existing price with
zero. Source data MUST distinguish an explicit zero or not-applicable value
from an omitted, unknown, or invalid value. The normalizer MUST reject
non-finite, negative, excessive, malformed, or unsupported prices and units.
Each accepted price version MUST preserve the raw source strings used for its
normalized components.

Each result MUST identify the provider-model route, source, lookup identifier,
old and new typed prices, status, error class, and synchronization time. The
administration interface MUST show manual, current, stale, missing, and failed
states. The initial stale threshold is 14 days and MUST be editable.

A multi-route synchronization MAY have updated, unchanged, skipped, missing,
and failed rows in one result. It MUST commit accepted row changes atomically
and publish one immediate configuration revision after the transaction
commits. A row failure MUST NOT hide successful or failed results for another
row.

Each provider attempt MUST snapshot the price version and typed prices that
were used for admission and initial accounting. A later price synchronization
MUST NOT rewrite that snapshot or the original cost event. Provider usage or
invoice reconciliation MAY append a correction with its source, time, reason,
and delta. Runtime pricing MUST resolve from the exact immutable provider-model
route. It MUST NOT use only a wire model name or an unrelated process-wide
price map.
