# Accounting, logs, retention, and operations

Status: Accepted on 2026-08-23.

## Attempt and request accounting

The Router MUST keep one logical call record separate from each provider
attempt. It MUST record billable usage from successful and failed attempts
when the provider reports it. Fallback MUST NOT hide or replace a prior
attempt's usage or cost.

Each attempt MUST snapshot its provider connection, provider model, typed
usage, applied typed prices, cost, outcome, start time, end time, and safe
failure class. A later price change MUST NOT change this record or a daily
aggregate.

Raw request and attempt accounting MUST be durable PostgreSQL data until its
scheduled daily rollup succeeds. A scheduled rollup MUST process each closed
UTC day no later than 03:00 UTC on the next day. It MUST be safe to repeat and
MUST NOT count one raw attempt more than once.

Equivalent rows MAY aggregate when all grouping dimensions are equal.
Dimensions MUST include date, service, workspace, assignment or exact-call
marker, provider-model, outcome, normalized tag set, usage unit, and price
currency. Daily aggregates MUST have no automatic expiry.

Deleting a workspace or service MUST delete its raw accounting and daily
aggregates. The public resource MUST be absent before physical cleanup starts.
Internal cleanup MUST finish or report an operator-visible failure within 24
hours. Cleanup state MUST NOT become a public service or workspace state.

## Statistics

A service MUST be able to read accounting statistics only for its own scope.
A global administrator MUST be able to read all scopes.

Statistics MUST support bounded filters and groups for date, service,
workspace, assignment, provider-model, outcome, and tags. Results MUST contain
calls, attempts, typed units, and cost. One query MUST cover no more than 366
days and return no more than 1000 groups. The API MUST use bounded pagination
for record lists. The product MUST NOT provide a general analytics query
language.

## Detailed request logs

The Router MUST keep complete detailed request logs for one global rolling
duration. The default MUST be 7 days. A global administrator MAY configure a
whole-day value from 1 through 30 days. A service or workspace MUST NOT change
this value.

Detailed logs MUST contain applicable model messages, uploaded input images,
tool definitions, tool results, provider responses, generated media, attempt
errors, routing results, tags, usage, cost, and timing. The Router MUST NOT
pattern-scan, classify, redact, or rewrite this model content.

Provider credentials, service API keys, administrator cookies, authorization
headers, object-storage credentials, and CSRF values are control data. They
MUST NOT enter a request-log field. This exclusion is not content redaction.

Detailed logs MUST be best-effort diagnostic data. Cache loss or eviction MAY
delete them before the configured maximum period. The Router MUST NOT claim a
minimum durability period. Only a global administrator MAY read them. A
service API key and a service API MUST NOT expose detailed request logs.

Uploaded input images and generated media MUST use the same rolling maximum
duration. They MAY disappear early after an applicable diagnostic-cache or
object-storage failure. They MUST remain behind Router endpoints while they
exist.

Storage MUST organize detailed data by date and workspace so scheduled
retention and service or workspace deletion are bounded. This organization
MUST NOT be part of the public API.

## Basic activity log

The Router MUST keep a basic activity event for each administrator or service
configuration change. It MUST record actor identity, action, target, time, and
result. It MUST NOT store old values and MUST NOT claim to be a security-grade
immutable audit trail.

The activity log MUST use the same global duration and best-effort retention
posture as detailed request logs. Only a global administrator MAY read the
complete activity log. A service API key MUST NOT query it. A service MAY
receive the result of its own write without receiving a general activity-log
query.

## Deployment

LLM Router MUST be one normal logical web application with PostgreSQL. A
deployment MAY run ordinary identical application replicas behind a load
balancer. The product MUST NOT define separate runtime roles, Router node
discovery, node draining, local spools, fleet hints, leased budgets, standby
promotion, fencing, or a Router replication protocol.

Normal application, load-balancer, PostgreSQL, object-storage, backup, and
deployment tools MUST supply deployment reliability. Deployment owners MUST
select and test a backup and restore policy that fits their needs. The Router
MUST NOT define a fixed backup schedule, zone count, recovery time, recovery
point, backup API resource, or restore workflow.

## Metrics and health

The Router MUST expose Prometheus metrics for request counts, attempt counts,
latency, outcomes, provider-model cooldowns, usage units, cost, media jobs,
database health, and application saturation. Metrics MUST NOT contain model
content, image bytes, generated media, service API keys, provider credentials,
or administrator cookies.

The administration application MUST show a small health summary for the web
application, PostgreSQL, object storage, price synchronization, log retention,
and accounting rollup. It MUST NOT reproduce a complete metrics or operations
product.

Deployment configuration MUST set bounded request size, attempt timeout,
connection timeout, media-job deadline, concurrency, and database-pool limits.
The application MUST reject new work with a safe retryable error when an
applicable concurrency limit is full.
