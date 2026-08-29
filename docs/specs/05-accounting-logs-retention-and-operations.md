# Accounting, logs, retention, and operations

Status: Accepted on 2026-08-23. The graph-first UI and administrator
playground amendments were accepted on 2026-08-24. The administration
statistics-filter amendment was accepted on 2026-08-29.

## Attempt and request accounting

The Router MUST keep one logical call record separate from each provider
attempt. It MUST record billable usage from successful and failed attempts
when the provider reports it. Fallback MUST NOT hide or replace a prior
attempt's usage or cost.

Each attempt MUST snapshot its provider connection, provider model, applied
typed prices, outcome, start time, end time, and safe failure class. It MUST
also snapshot typed usage and cost when enough provider data is available. A
later price change MUST NOT change this record or a daily aggregate. Usage and
cost MUST be absent when the provider does not report enough data to calculate
them. An absent value MUST NOT become zero.

Each logical call and attempt MUST identify its immutable call actor as
`service` or `administrator`. A service call MUST keep its service and
workspace ownership. An administrator playground call MUST keep the immutable
administrator subject and MUST have no service or workspace owner. An
administrator assignment call MUST also snapshot its configuration service
name. That name MUST remain context only and MUST NOT make the record visible
to that service.

The logical call identity MUST link the call response, raw request and attempt
accounting, detailed log, and applicable media job. Detailed-log or retained-
object loss MUST NOT delete or change durable accounting. A daily rollup MAY
combine logical calls only when their immutable grouping dimensions are
equal. Rollup MUST include administrator playground rows and MUST keep their
call actor, administrator, and configuration-service dimensions. A playground
call MUST NOT create a basic activity event because it is not a configuration
change.

Raw request and attempt accounting MUST be durable PostgreSQL data until its
scheduled daily rollup succeeds. A scheduled rollup MUST process each closed
UTC day no later than 03:00 UTC on the next day. It MUST be safe to repeat and
MUST NOT count one raw attempt more than once. A completed attempt that arrives
after a day was rolled up MUST cause an atomic replacement rollup for that day.
The replacement MUST include the late attempt and MUST NOT duplicate an
earlier attempt.

Equivalent rows MAY aggregate when all grouping dimensions are equal.
Dimensions MUST include date, call actor, service, workspace, administrator,
assignment or exact-call marker, assignment configuration service,
provider-model, outcome, normalized tag set, usage unit, and price currency.
A dimension that does not apply to one call actor MUST have a null value. Daily
aggregates MUST have no automatic expiry.

Statistics call totals MUST include an admitted logical call that has no
provider attempt. Its attempt count MUST be zero, its typed-unit list MUST be
empty, its cost MUST be zero, and its currency MUST be null. An attempt-only
provider-model or outcome dimension MUST be null for this call. A non-null
provider-model or outcome filter MUST exclude it. When one or more attempts in
a statistics bucket have unavailable usage or cost, the bucket MUST contain
only the reported typed units and MUST use null cost. It MUST NOT present a
partial cost as a complete total.

Deleting a workspace or service MUST delete its raw accounting and daily
aggregates. The public resource MUST be absent before physical cleanup starts.
Internal cleanup MUST finish or report an operator-visible failure within 24
hours. Cleanup state MUST NOT become a public service or workspace state.
This deletion MUST NOT delete an administrator playground record that names
the deleted service only as assignment configuration context.

## Statistics

A service MUST be able to read accounting statistics only for its own
records. A global administrator MUST be able to read all accounting records.

Statistics MUST support bounded filters and groups for date, service,
workspace, call actor, administrator, assignment configuration service,
assignment, provider-model, outcome, and tags. Results MUST contain calls,
attempts, typed units, and cost. A service statistics operation MUST return
only service-call records for its authenticated service. It MUST NOT return
an administrator playground record. One query MUST cover no more than 366 days
and return no more than 1000 groups. The API MUST use bounded pagination for
record lists. The product MUST NOT provide a general analytics query language.
Each statistics bucket MUST have one dimension value for each requested group
in the same order. Its dimension count MUST equal the `group_by` count.

The administration statistics view MUST use the shared OpenDLE UI data-table
behavior. Its filter controls and results MUST use the same page gutter and
complete available width. Long dimensions, tags, usage values, and costs MUST
wrap or scroll inside their bounded cells without causing page-level
horizontal overflow.

### Administration statistics filters

The administration Usage and cost view MUST use date-only controls labelled
`From` and `Through`. It MUST identify the dates as UTC and MUST NOT show a
time or use the browser's local time zone. `From` MUST include the selected
date. `Through` MUST include the selected date.

On first open, `Through` MUST be the current UTC date and `From` MUST be 29
UTC calendar dates before it. This default selects 30 dates. The current date
MUST come from the same instant in every browser time zone.

For a valid `From` date, the view MUST send `from` as that date at
`00:00:00Z`, inclusive. For a valid `Through` date, it MUST send `to` as the
next calendar date at `00:00:00Z`, exclusive. It MUST calculate the next date
with UTC calendar-date arithmetic. It MUST NOT add a local-time day or use a
local UTC offset. These examples are normative:

| Case | `From` | `Through` | API `from` | API `to` |
| --- | --- | --- | --- | --- |
| One date | `2026-08-29` | `2026-08-29` | `2026-08-29T00:00:00Z` | `2026-08-30T00:00:00Z` |
| One month | `2026-04-01` | `2026-04-30` | `2026-04-01T00:00:00Z` | `2026-05-01T00:00:00Z` |
| Leap date | `2028-02-29` | `2028-02-29` | `2028-02-29T00:00:00Z` | `2028-03-01T00:00:00Z` |
| Year end | `2026-12-31` | `2026-12-31` | `2026-12-31T00:00:00Z` | `2027-01-01T00:00:00Z` |

The selected range MUST contain from 1 through 366 UTC calendar dates. A
selection from `2028-01-01` through `2028-12-31` MUST be valid and MUST send
`to=2029-01-01T00:00:00Z`. A selection of 367 dates MUST be invalid. A
`Through` date before `From` MUST be invalid. If the next calendar date after
`Through` cannot be represented as a valid API timestamp, the selection MUST
be invalid. An invalid selection MUST NOT send an API request.

The form MUST show these corrective errors next to the applicable control and
in one live error summary:

- `Enter a valid From date.` when `From` is missing or invalid;
- `Enter a valid Through date.` when `Through` is missing or invalid;
- `Through must be the same as or after From.` for an invalid order;
- `Select 366 dates or fewer.` for an over-limit range;
- `Through is outside the supported date range.` when the exclusive next date
  cannot be represented.

The first invalid control MUST have focus after submission. Each invalid
control MUST use `aria-invalid` and MUST reference its error. Correcting a
value MUST remove its obsolete error. The view MUST show the non-obvious UTC
effect with the visible text `UTC dates. From and Through include the selected
dates.`

The basic filter surface MUST show `From`, `Through`, `Service`, and
`Workspace`, with `Run statistics` outside any disclosure. One disclosure
labelled `Advanced filters` MUST contain `Call actor`, `Administrator`,
`Assignment configuration service`, `Assignment`, `Provider route`, `Outcome`,
`Tag`, and `Group results`. The disclosure MUST be closed by default. When an
advanced filter or group is active, its summary MUST show the active count.
The count MUST add one for each non-empty advanced filter and one for each
selected group. Closing it MUST keep all values. An error in it MUST open it
before focus moves to the invalid control.

The view MUST use these exact human labels for API filters and group values:

| API name | Filter label | Group label |
| --- | --- | --- |
| `date` | `From` and `Through` | `Date` |
| `call_actor` | `Call actor` | `Call actor` |
| `service` | `Service` | `Service` |
| `workspace` | `Workspace` | `Workspace` |
| `administrator` | `Administrator` | `Administrator` |
| `configuration_service` | `Assignment configuration service` | `Assignment configuration service` |
| `assignment` | `Assignment` | `Assignment` |
| `provider_model` | `Provider route` | `Provider route` |
| `outcome` | `Outcome` | `Outcome` |
| `tag` | `Tag` | `Tag` |

The visible call-actor values MUST be `Service calls` and `Administrator
playground calls`. The visible exact-assignment value for the API value
`(exact)` MUST be `Exact provider route calls`. The visible outcome values
MUST be `Succeeded` and `Failed`. An unset filter MUST have no API query
parameter. An all-values select option MUST use `All call actors`, `All
services`, `All workspaces`, `All administrators`, `All assignment
configuration services`, `All assignments`, `All provider routes`, `All
outcomes`, or `All tags`, as applicable. A technical API name such as
`provider_model`, `configuration_service`, or `call_actor` MUST NOT be a
visible label.

OpenDLE UI MUST own a reusable compact checkbox-group pattern for `Group
results`. The shared pattern MUST accept host-supplied values and human labels
and MUST NOT contain Router dimension names. It MUST use a semantic fieldset,
show `Group results ({count} selected)` in its compact summary, and keep its
checkbox options hidden until the administrator opens it. It MUST submit
selected values in the displayed order, not the selection order. The Router
MUST display group options in the table order above and MUST enforce the API
maximum of eight unique groups. At the limit, it MUST keep selected groups
enabled, prevent another selection, and announce `Select up to 8 groups.`

Enter or Space on the compact summary MUST open or close it. Tab and Shift+Tab
MUST move through its checkboxes in displayed order. Space MUST change the
focused checkbox. Escape MUST close the open group and return focus to its
summary. Pointer selection of a checkbox label MUST have the same result. A
phone presentation MUST keep the summary, options, labels, focus indicator,
and selected count usable without page-level horizontal overflow. A long open
option list MUST use a bounded local scroll region.

Submitting a valid form MUST keep all submitted values visible. It MUST show
the results table as loading and announce `Loading usage and cost.` It MUST
prevent a duplicate submission for the same pending query. A later valid query
MUST own the result, and an earlier response MUST NOT replace it. A successful
query with no result buckets MUST show `No usage or cost matches these
filters.` A failed query MUST keep the form values and show the corrective
error `Unable to load usage and cost. Review the filters and try again.` A
successful query MUST render calls, attempts, typed units, cost, currency, and
dimensions with the shared data-table behavior. Loading, empty, error, and
ready changes MUST use a live region. A valid submission MUST keep the current
focus.

This view mapping MUST NOT change the native statistics API. The API MUST keep
the required timestamp query parameters `from` and `to`, with an inclusive
lower bound and exclusive upper bound. It MUST NOT add a date-only API
parameter. This rule applies only to the Usage and cost view. It does not
define the Detailed logs view, its filters, or its record-loading behavior.

Unit tests MUST cover one date, the month boundary, the year boundary, the
leap date, exactly 366 dates, 367 dates, invalid order, missing and invalid
dates, and exclusive-next-date overflow. Browser time-zone tests MUST run the
same date selections in `UTC`, `Pacific/Kiritimati`, `Pacific/Pago_Pago`, and
`America/New_York` during a daylight-saving transition. They MUST also test the
default range at one fixed instant near a UTC date boundary. Each case MUST
produce the same exact UTC timestamps or the same client error.

API-request tests MUST confirm the exact `from` and `to` timestamps, every
filter value, that each active filter is sent, that each inactive filter is
omitted, no more than eight unique `group_by` values, and group order. Browser
tests at desktop and phone widths MUST cover the basic and advanced hierarchy,
every human label, group-summary counts, the eight-group limit, keyboard
operation, focus after validation, focus during valid state changes, loading,
empty, API-error, and populated-table states. They MUST check no page-level
horizontal overflow. They MUST run Axe for the basic, advanced, error, and
populated states at both widths. The React application MUST keep React Doctor
at score 100 with zero diagnostics.

## Detailed request logs

The Router MUST keep complete detailed request logs for one global rolling
duration. The default MUST be 7 days. A global administrator MAY configure a
whole-day value from 1 through 30 days. A service or workspace MUST NOT change
this value.

Detailed logs MUST contain applicable model messages, uploaded input images,
tool definitions, tool results, provider responses, generated media, attempt
errors, routing results, tags, usage, cost, and timing. The Router MUST NOT
pattern-scan, classify, redact, or rewrite this model content.

An administrator playground detailed log MUST link to the same logical call
identity as its raw accounting and media job. Its summary MUST identify the
administrator call actor and immutable administrator subject. An assignment
call summary MUST include the configuration service snapshot. It MUST have no
service or workspace owner. A service or workspace delete MUST NOT delete it.
An exact-call summary MUST contain the exact provider-model and MUST omit the
assignment and configuration service. An assignment-call summary MUST contain
the assignment and configuration service together.

Provider credentials, service API keys, administrator cookies, authorization
headers, object-storage credentials, and CSRF values are control data. They
MUST NOT enter a request-log field. This exclusion is not content redaction.

Detailed logs MUST be best-effort diagnostic data. Cache loss or eviction MAY
delete them before the configured maximum period. The Router MUST NOT claim a
minimum durability period. Only a global administrator MAY read them. A
service API key and a service API MUST NOT expose detailed request logs.

The administration log view MUST use the shared OpenDLE UI data-table
behavior for the bounded record list. Filters, loading state, error state,
empty state, result rows, incremental loading, and selected-log details MUST
use one full-width page grid and one gutter. Selecting a row MUST reveal the
complete retained detail without navigating to a second list page. Long model
content, tool data, errors, tags, usage, prices, and route data MUST stay in a
bounded detail region with usable wrapping or local scrolling. The view MUST
NOT let a table or detail value create page-level horizontal overflow.

The log view MUST treat model messages, tool data, provider responses, and
errors as untrusted content. It MUST render text as text and MUST NOT execute
markup, scripts, links, or embedded active content from a log. Retained images
and media MUST load only through authenticated Router endpoints. If a selected
log or media object expires or disappears, the list MUST remain usable and the
detail region MUST show an unavailable state. It MUST NOT silently select a
different log.

Uploaded input images and generated media MUST use the same rolling maximum
duration. They MAY disappear early after an applicable diagnostic-cache or
object-storage failure. They MUST remain behind Router endpoints while they
exist.

Administrator playground input images and generated media MUST use this same
retention and authenticated global-administrator read path. The scheduled
retention sweep MUST delete an administrator media job after its applicable
detailed-log and object-retention cutoff. It MUST NOT delete a pending or
running job before its deployment deadline makes the job terminal. A missing
job MUST return `not_found`. A job whose result is not ready or whose retained
bytes disappeared MUST return `content_unavailable` from its content read.
A deleted assignment, configuration service, provider, model, or provider-
model MUST NOT expose or transfer the job. It MUST remain an administrator-
only record and any late result MUST use the admitted immutable selection
snapshot. A late result MUST be discarded if the administrator call or job no
longer exists when it arrives.

Storage MUST organize detailed data by date and by its owning workspace or
administrator-record partition so scheduled retention and service or
workspace deletion are bounded. This organization MUST NOT be part of the
public API.

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
