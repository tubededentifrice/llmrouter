# Accounting, logs, retention, and operations

Status: Accepted on 2026-08-23. The graph-first UI and administrator
playground amendments were accepted on 2026-08-24. The administration
statistics-filter, Logs-view, and Configuration activity Result-alignment
amendments were accepted on 2026-08-29.

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
date. `Through` MUST include the selected date. Each submitted value MUST use
`YYYY-MM-DD` and MUST identify a real Gregorian calendar date. The view MUST
NOT normalize an invalid value, such as `2026-02-29` or `2026-04-31`, to a
different date.

On first open, `Through` MUST be the current UTC date and `From` MUST be 29
UTC calendar dates before it. This default selects 30 dates. The current date
MUST come from the same instant in every browser time zone.

For a valid `From` date, the view MUST send `from` as that date at
`00:00:00Z`, inclusive. For a valid `Through` date, it MUST send `to` as the
next calendar date at `00:00:00Z`, exclusive. It MUST calculate the next date
with UTC calendar-date arithmetic. It MUST NOT add a local-time day or use a
local UTC offset. These examples are normative:

| Case      | `From`       | `Through`    | API `from`             | API `to`               |
| --------- | ------------ | ------------ | ---------------------- | ---------------------- |
| One date  | `2026-08-29` | `2026-08-29` | `2026-08-29T00:00:00Z` | `2026-08-30T00:00:00Z` |
| One month | `2026-04-01` | `2026-04-30` | `2026-04-01T00:00:00Z` | `2026-05-01T00:00:00Z` |
| Leap date | `2028-02-29` | `2028-02-29` | `2028-02-29T00:00:00Z` | `2028-03-01T00:00:00Z` |
| Year end  | `2026-12-31` | `2026-12-31` | `2026-12-31T00:00:00Z` | `2027-01-01T00:00:00Z` |

The selected range MUST contain from 1 through 366 UTC calendar dates. A
selection from `2028-01-01` through `2028-12-31` MUST be valid and MUST send
`to=2029-01-01T00:00:00Z`. A selection of 367 dates MUST be invalid. A
`Through` date before `From` MUST be invalid. If the next calendar date after
`Through` cannot be represented as a valid API timestamp, the selection MUST
be invalid. For example, `9999-12-31` MUST be invalid as `Through` because its
exclusive next date is outside the API timestamp format. An invalid selection
MUST NOT send an API request.

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
value MUST remove its obsolete error. The invalid-order, over-limit, and
exclusive-next-date errors MUST belong to the `Through` control. The view MUST
show the non-obvious UTC effect with the visible text `UTC dates. From and
Through include the selected dates.`

The basic filter surface MUST show `From`, `Through`, `Service`, and
`Workspace`, with `Run statistics` outside any disclosure. One disclosure
labelled `Advanced filters` MUST contain `Call actor`, `Administrator`,
`Assignment configuration service`, `Assignment`, `Provider route`, `Outcome`,
`Tag`, and `Group results`. The disclosure MUST be closed by default. When an
advanced filter or group is active, its summary MUST show `Advanced filters
({count} active)`. With no active value, it MUST show `Advanced filters`. The
count MUST add one for each non-empty advanced filter and one for each selected
group. Closing it MUST keep all values. An error in it MUST open it before
focus moves to the invalid control.

The view MUST use these exact human labels for API filters and group values:

| API parameter or group value      | Filter label                       | Group label                        |
| --------------------------------- | ---------------------------------- | ---------------------------------- |
| `from`, `to`; `date` for grouping | `From` and `Through`               | `Date`                             |
| `call_actor`                      | `Call actor`                       | `Call actor`                       |
| `service`                         | `Service`                          | `Service`                          |
| `workspace`                       | `Workspace`                        | `Workspace`                        |
| `administrator`                   | `Administrator`                    | `Administrator`                    |
| `configuration_service`           | `Assignment configuration service` | `Assignment configuration service` |
| `assignment`                      | `Assignment`                       | `Assignment`                       |
| `provider_model`                  | `Provider route`                   | `Provider route`                   |
| `outcome`                         | `Outcome`                          | `Outcome`                          |
| `tag`                             | `Tag`                              | `Tag`                              |

The group selector and each corresponding result dimension MUST use the
applicable `Group label`. A result MUST identify each dimension label and
value in the requested group order.

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
MUST determine the displayed state and result. A response from an earlier
query MUST NOT change the displayed state, result, live message, or notice. A
successful query with no result buckets MUST show `No usage or cost matches
these filters.` A failed query MUST keep the form values and show the
corrective error `Unable to load usage and cost. Review the filters and try
again.` A successful query MUST render calls, attempts, typed units, cost,
currency, and dimensions with the shared data-table behavior. Loading, empty,
error, and ready changes MUST use a live region. A valid submission MUST keep
the current focus.

This view mapping MUST NOT change the native statistics API. The API MUST keep
the required timestamp query parameters `from` and `to`, with an inclusive
lower bound and exclusive upper bound. It MUST NOT add a date-only API
parameter. This rule applies only to the Usage and cost view. It does not
define the Detailed logs view, its filters, or its record-loading behavior.

Unit tests MUST cover one date, the month boundary, the year boundary, the
leap date, exactly 366 dates, 367 dates, invalid order, missing and invalid
date syntax, invalid calendar dates, and exclusive-next-date overflow. Browser
time-zone tests MUST run the same date selections in `UTC`,
`Pacific/Kiritimati`, `Pacific/Pago_Pago`, and `America/New_York` during a
daylight-saving transition. They MUST also test the default range at one fixed
instant near a UTC date boundary. Each case MUST produce the same exact UTC
timestamps or the same client error.

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

### Administration Logs view

All visible navigation, page, region, state, and action text that names this
administration resource MUST use the exact product name `Logs`. It MUST NOT use
`Detailed logs`, `Detailed request logs`, `Request logs`, `Load logs`, or
another destination name. The navigation label, page heading, and list region
accessible name MUST each be `Logs`. The filter region MUST be `Logs filters`.

On page entry, the view MUST automatically read the current configured
retention duration from `/v1/admin/settings/log-retention` and load the newest
100 retained records from `/v1/admin/request-logs`. After it reads a valid
`duration_days` value, it MUST capture one current instant `T`, truncate its
fractional seconds, express it in UTC, and start a new cursor walk. For a
configured duration of `D` whole days, it MUST use `R = T - D * 24 hours`. The
initial list request MUST send these exact query values:

- `from={R in YYYY-MM-DDTHH:mm:ssZ}`;
- `to={T in YYYY-MM-DDTHH:mm:ssZ}`;
- `limit=100`;
- no `cursor`.

The lower bound MUST be inclusive and the upper bound MUST be exclusive. A
record created at or after `T` MUST first be eligible after `Refresh Logs`.
The view MUST NOT make the initial list request until it has a valid configured
duration. If that read fails, the view MUST show `Logs are unavailable.` and
`Retry Logs`.

The list MUST use stable newest-first order by `started_at` descending and
then `id` descending. The first page MUST contain zero through 100 records. If
more records are available, the view MUST show `Load more Logs`. Each load-more
request MUST send `limit=100`, the next server cursor, and the same exact
`from`, `to`, and active filters as the first page in that cursor walk. It MUST
append only older records in the server order. It MUST NOT calculate or change
a cursor from visible data.

The view MUST add a record at most once for one cursor walk, identified by its
request-log `id`. A repeated record MUST NOT change the first copy or its
position. `has_more=false` MUST end the walk even if the response contains a
next cursor. A repeated cursor, a cursor that does not satisfy the public
`Cursor` schema, a missing next cursor when `has_more` is true, or a cursor
that produces no progress MUST stop the walk and show `More Logs are
unavailable.` It MUST keep the safe loaded records and offer `Refresh Logs`.
A failed load-more request MUST also keep the safe records and show `Retry
loading more Logs` for the same cursor.

`Refresh Logs` MUST close selected details and start a new first-page sequence.
That sequence MUST read the current configured duration again, capture a new
`T`, truncate its fractional seconds, express it in UTC, calculate a new `R`,
preserve all active filter values, and validate those values against the new
bounds. If they are valid, it MUST clear the cursor walk and start a new
first-page request. If they are invalid, it MUST use the invalid-filter
behavior below, keep the old list and cursor, and keep details closed. `Retry
Logs` MUST repeat this refresh sequence. Applying a valid filter or clearing
filters MUST start a new first-page sequence with the current `R` and `T`,
clear the cursor walk, and close selected details. These filter actions MUST
NOT capture a new `T`.

Each first-page sequence, cursor walk, and detail selection MUST have an
identity. Starting a new first-page sequence MUST immediately make all older
list, load-more, and detail responses stale. Only the current sequence and its
cursor walk MAY change the records, cursor, list state, list live message, list
notice, or list focus. A sequence that rejects an invalid filter MUST restore
the last valid cursor walk as current, but it MUST NOT restore an invalidated
request or closed detail selection. Only the current detail selection in the
current cursor walk MAY change the detail state, detail live message, detail
notice, or detail focus. A new detail selection MUST invalidate all requests
for the old selection. A load-more request in the current walk MUST NOT
invalidate its current detail selection. `Close Logs details` and Escape MUST
invalidate the current detail selection and its pending requests. A stale
response, error, or notice MUST NOT change visible state or focus. Repeated
activation of the same pending action MUST NOT send a duplicate request.

The default retention range MUST require no filter input. One optional region
named `Logs filters` MUST provide controls in this order: `From time (UTC)`,
`Before time (UTC)`, `Call actor`, `Administrator`, and `Assignment
configuration service`. Each time value MUST use `YYYY-MM-DDTHH:mm:ss` and
identify a real Gregorian UTC date and time. `From time (UTC)` replaces `R` as
the inclusive lower bound. `Before time (UTC)` replaces `T` as the exclusive
upper bound. An empty time control MUST use its automatic retention bound. The
view MUST append `Z` directly and MUST NOT apply the browser time zone or a
local UTC offset.

`Call actor` MUST send `call_actor` with the API value `service` or
`administrator`. Its visible values MUST be `Service calls` and `Administrator
playground calls`, and its empty option MUST be `All call actors`.
`Administrator` MUST send `administrator` with an administrator subject that
has a maximum of 500 characters. `Assignment configuration service` MUST send
`configuration_service` with a value that satisfies the public `ApiName`
schema. An empty optional filter MUST have no corresponding API query
parameter. Each first-page request after entry MUST send the effective `from`
and `to`, `limit=100`, no `cursor`, each active non-time optional filter, and
no inactive optional filter.

For each new cursor walk, the effective filter range MUST satisfy
`R <= from < to <= T`. The view MUST validate preserved time filters against
the new `R` and `T` after a refresh. An invalid filter MUST not send a list
request. An invalid `Apply Logs filters` action MUST keep the current list,
cursor, and selected details for the last valid range. A refresh that finds an
invalid preserved filter MUST use the refresh behavior above. The view MUST
use `aria-invalid`, reference its corrective error, announce the error, and
move focus to the first invalid control in the order above. It MUST use these
exact errors:

- `Enter a valid From time in UTC.`;
- `Enter a valid Before time in UTC.`;
- `From time must be before Before time.`;
- `Select times inside the configured Logs retention window.`;
- `Select a valid call actor.`;
- `Enter an administrator subject of 500 characters or fewer.`;
- `Enter a valid assignment configuration service API name.`

A syntax or value error MUST belong to its control. The range-order error MUST
belong to `Before time (UTC)`. The retention-window error MUST belong to the
first custom time control that puts the range outside the window. `Apply Logs
filters` MUST start a new first-page request only after a valid submission.
`Clear Logs filters` MUST clear all five controls and load the automatic
retention range. A filtered state is a state in which one or more of these
controls has a non-empty submitted value.

The list MUST use these visible states and actions:

- initial, refresh, and valid filter state: `Loading Logs.`;
- unfiltered empty state: `No Logs are available in the configured retention
window.`;
- filtered empty state: `No Logs match these filters.`;
- first-page error state: `Logs are unavailable.` and `Retry Logs`;
- ready state: `Logs loaded: 1 record.` or `Logs loaded: {count} records.`,
  with `More Logs are available.` or `All Logs in this range are loaded.`;
- load-more pending state: `Loading more Logs.`;
- load-more error state: `More Logs are unavailable.` with `Retry loading more
Logs` or `Refresh Logs` as applicable.

Each list state change MUST use a live region. A pending first-page request
MUST not show an empty state. Refresh and filter failures MUST keep the
submitted filter values. A first-page failure MUST NOT show stale records as
current. Load-more completion MUST keep focus on `Load more Logs` when that
action remains available. When a completed load-more request reports that all
records are loaded, removes that action, and does not trigger a stopped-walk
error, focus MUST move to the ready status. A load-more request failure MUST
move focus to `Retry loading more Logs`. A stopped cursor walk MUST move focus
to `Refresh Logs`. A refresh action MUST keep focus on the action that started
it unless validation moves focus to an invalid control. A valid filter action
that sends a first-page request MUST keep focus on the action that started it.

Each visible row action MUST be `Inspect Logs details`. Its accessible name
MUST be `Inspect Logs details for request {id}`. It MUST read the selected
record from `/v1/admin/request-logs/{request_log_id}` and MUST NOT treat the
list summary as complete detail. The detail region accessible name MUST be
`Logs details`, and its heading MUST be `Logs details for request {id}`.
Opening it MUST move focus to its heading. `Close Logs details` and Escape MUST
close it and return focus to the row action. If that action no longer exists,
focus MUST move to `Refresh Logs`.

Only the latest detail selection MUST control the detail region. A load-more
request MUST NOT change the selection. While detail loads, the region MUST
show `Loading Logs details.` If the record expires, disappears, or fails to
load, the list MUST stay usable and the region MUST show `Logs details are
unavailable.` with `Retry Logs details` and `Close Logs details`. It MUST NOT
select a different record. `Retry Logs details` MUST request the same selected
record and MUST keep the selection. Detail loading, ready, and unavailable
changes MUST use a live region. Retained media that disappears MUST follow the
unavailable behavior below without closing the detail region.

The list and selected detail MUST use the shared data-table page grid and the
complete available width. On a desktop, the detail region MUST stay visually
associated with the selected row. On a phone, it MUST follow the list, keep
its heading and close action reachable, and use a bounded local scroll region
for long content. Both layouts MUST keep all columns, row actions, filters,
states, and detail content usable without page-level horizontal overflow.
Enter or Space on a focused row action MUST open the same detail as a pointer
action.

This view rule MUST NOT change the public request-log API. The list API MUST
keep required timestamp parameters `from` and `to`, cursor pagination, and a
`limit` from 1 through 200. The administration view uses 100 for each page,
but an API client MAY use another valid limit. The technical API paths and
schema names MAY continue to use `request-log`; the visible administration
application MUST use `Logs`. The date-only `From` and `Through` rules for the
Usage and cost view MUST NOT apply to these optional UTC date-and-time filters.

API tests MUST cover zero, fewer than 100, exactly 100, and more than 100
retained records. They MUST confirm exact `from`, `to`, and `limit=100` values,
no initial cursor, inclusion at `R`, exclusion at `T`, stable `started_at` and
`id` order, the next cursor, the same bounds and filters on load more, detail
success, detail `not_found`, and `limit` compatibility at 1 and 200. Client and
browser tests MUST cover entry, retention-read failure, refresh, a changed
retention duration with preserved filters, each optional filter, exact active
filter query values, inactive-filter omission, clear, filter validation and
focus, duplicate records, repeated, missing, and invalid cursors,
duplicate-action prevention, stale cursor-walk and detail responses,
concurrent load-more and detail responses, load-more failure and retry, and
selected-detail focus and expiry. Fixed-instant client tests with 1-day and
30-day durations in `UTC`, `Pacific/Kiritimati`, `Pacific/Pago_Pago`, and
`America/New_York` MUST produce the same exact `R` and `T` request values and
MUST confirm fractional-second truncation.

Browser tests MUST run at desktop and phone widths. They MUST cover every
specified `Logs` label, loading, empty, error, ready, load-more, and detail
state; keyboard entry, Escape, and focus return; local scrolling; and no
page-level horizontal overflow. The entry, filter, table, and detail states
MUST pass Axe at both widths. The React application MUST keep React Doctor at
score 100 with zero diagnostics.

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

### Configuration activity result presentation

The Configuration activity `Result` column MUST use the shared center column
alignment in
[Authentication, administration, and shared UI](04-authentication-administration-and-shared-ui.md#shared-datatable-alignment-and-status-pill-sizing).
On a desktop, the `Result` header and status pill MUST be centered in their
applicable table cell content regions. On a phone, the status pill MUST be
centered in the `Result` value region. It MUST NOT be centered across the
complete label-and-value row.

The Router MUST set the existing `DataTableColumn.align` value to `center` for
this column. It MUST supply the activity result text and tone. It MUST NOT add
a wrapper, asymmetric host margin, translated position, Router-only alignment
class, or status-specific shared API. This requirement MUST NOT change the
placement of a health-row status pill, a cooldown status pill, or another
outcome or status column.

Focused Router component tests MUST cover the activity result values
`succeeded` and `failed` and one long synthetic result label. At desktop and
phone widths, they MUST prove at least the shared `0.75rem` default clearance
from each applicable outer inline edge. They MUST also prove that the two free
inline spaces in the desktop cell or phone value region differ by no more than
one CSS pixel. They MUST confirm the centered `Result` header, desktop cell
placement, phone value-region placement, equal pill inline padding, and
unchanged health-row pill placement. At 200% text size, the long label MUST
wrap or break safely. It MUST NOT clip text, hide the status dot, or cause
page-level horizontal overflow. Tests MUST preserve semantic desktop header and
cell associations and the phone `Result` term-to-value association. The
activity table at desktop and phone widths MUST
pass Axe and have reviewed screenshots. Each React change MUST keep React
Doctor at score 100 with zero diagnostics. Browser tests, when applicable, MUST
use `http://127.0.0.1:5174` and the local test-session workflow.

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
