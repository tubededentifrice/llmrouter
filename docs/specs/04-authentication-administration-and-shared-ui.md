# Authentication, administration, and shared UI

Status: Accepted on 2026-08-23. The graph-first UI and administrator
playground amendments were accepted on 2026-08-24. The fixed compound-board
and administration-content amendments were accepted on 2026-08-29.

## Service API keys

A service API key MUST be a backend-only, revocable, long-lived bearer
credential. A backend MUST send it directly on each Router request. The
product MUST NOT exchange it for another token.

One service MAY have several named active keys. A new key MUST contain at
least 256 random bits and MUST be shown only once. The Router MUST store only
a verifier that is sufficient to authenticate it. It MUST NOT put the key in
a URL, browser code, log, activity detail, or response after its creation.

An authenticated service key MUST have all authority for its service's
assignments, workspaces, model calls, embedding calls, media jobs, key
management, and accounting. It MUST NOT read detailed request logs or manage
global services, provider connections, model availability, provider
credentials, or global prices.

A service or global administrator MUST be able to create and revoke a key for
that service. Revocation MUST reject each later request with that key. Key
replacement MUST use create, deploy, and revoke. The product MUST NOT have a
timed rotation or overlap state.

## Global administrator identity

The global administration application MUST use the shared Pocket ID service
through standard OpenID Connect. Deployment configuration MUST contain an
allowlist of Pocket ID subject identities. Only an allowlisted subject MAY
receive a Router administrator session. The Router MUST reject every other
subject.

Each allowed administrator MUST have unrestricted Router administration
authority. The Router MUST NOT have administrator tenants, grants, delegated
scopes, or fine-grained administrator permissions.

The Router MUST use the OpenID Connect authorization code flow with Proof Key
for Code Exchange. It MUST validate the issuer, audience, signature, expiry,
state, nonce, and exact redirect URI. It MUST use the immutable issuer and
subject pair as the human identity. It MUST NOT use an email address or display
name as authority.

An administration return target MUST be one local absolute path. It MUST NOT
be a network-path reference or an absolute URL.

After sign-in, the Router MUST use one server-side local session with logout
and a configurable expiry from 1 hour through 30 days. Activity MUST NOT
extend the absolute expiry. The product MUST NOT require recurring
identity-provider session checks, refresh-token rotation, or a
recent-authentication workflow.

The session cookie MUST be `Secure`, `HttpOnly`, host-only, and
`SameSite=Lax`. A browser write MUST require a session-bound CSRF token and an
exact allowed origin. Logout MUST invalidate the local session.

## Global administration application

The Router MUST host only one global administration application. It MUST let
an administrator:

- create, move, inspect, and delete services;
- create, inspect, and delete workspaces in the selected-service inspector;
- create and revoke service API keys in the selected-service inspector;
- manage provider connections, credentials, models, capabilities, prices, and
  service assignments in one configuration board;
- preview and import model catalog entries from the model-create workflow;
- start price synchronization and inspect its results;
- open a contextual model, embedding, and media playground from an assignment
  or exact provider-route row;
- inspect accounting, detailed request logs, media, activity, cooldowns, and
  the small health summary.

The sidebar MUST NOT contain separate workspace-and-key, provider, model,
assignment, or playground destinations. The service tree MUST own service,
workspace, and key administration. The three-column configuration board MUST
own provider, model, provider-model, assignment, and contextual playground
administration. Direct application paths for removed destinations MAY redirect
to the applicable retained board, but MUST NOT keep a second configuration
page.

### Administration content

Visible static helper text includes page and section descriptions, field
notes, and instructional paragraphs. It does not include headings, labels,
actions, record values, corrective errors, state messages, live-region
messages, or accessible names. One exact keep rule applies: visible static
helper text MUST explain a required action, a non-obvious effect, a security
boundary, a retention effect, a loading, empty, unavailable, or failure state,
a destructive impact, or an accessibility state. The application MUST remove
visible static helper text that does not meet this rule.

Corrective errors, required actions, non-obvious effects, security and
retention facts, loading, empty, unavailable, and failure states, destructive
impacts, and accessibility states MUST remain available. This rule MUST NOT
remove a live region or an accessible name. It MUST NOT use helper text in
place of a required accessible name or state message.

A health-component message is optional. When it is absent, the application
MUST omit the message and MUST NOT show `No corrective message` or another
placeholder. When it is present, the application MUST show it if it gives a
corrective error, required action, non-obvious effect, or applicable state.

The activity panel MUST NOT show `This is a basic activity record. It is not
immutable configuration history.` or a replacement disclaimer with the same
purpose. This removal does not change the basic-activity requirements in
[Accounting, logs, retention, and operations](05-accounting-logs-retention-and-operations.md#basic-activity-log).

The content inventory MUST cover these retained routes:

| Path | View | Content that the inventory MUST check |
| --- | --- | --- |
| `/overview` | overview | totals, health, and cooldown summaries |
| `/services` | service administration | the service tree, inspectors, workspaces, and keys |
| `/configuration` | configuration administration | all three board columns, inspectors, forms, and playground entry points |
| `/logs` | retained-log inspection | filters, retained records, selected details, media, and retention states |
| `/statistics` | accounting statistics | filters, query states, and accounting results |
| `/operations` | operations | health, retention, cooldowns, and activity |

For each route, the test inventory MUST list each static helper text item, its
expected presence or absence, and one allowed keep reason for each retained
item. An automated browser test MUST compare the rendered text with this
reviewed inventory at desktop and phone widths. The test inventory MUST
include fixtures for corrective errors, required actions, non-obvious effects,
security and retention facts, loading, empty, unavailable, failure,
destructive, and accessibility states. It MUST confirm the two named removals,
live regions, and accessible names. Each route MUST pass Axe at both widths and
MUST have a reviewed snapshot at both widths. Focused snapshots MUST also cover
each conditional state that is not present in the route's normal snapshot.

This content rule MUST NOT remove or hide a retained page heading. A change to
the height or visibility of a graph page heading needs its own accepted
requirement.

The application shell and each retained page MUST use the complete available
width after the sidebar. They MUST use one responsive gutter system for page
headings, filters, panels, graphs, and tables. A page MUST NOT use one maximum
width for its controls and another width for its result, and the page container
MUST NOT set a smaller maximum width. On a phone, the shell MUST use the
complete viewport width with the phone gutter and MUST prevent page-level
horizontal overflow. A graph or dense data region MAY scroll in its own
labelled viewport when its content cannot reflow. Its heading, filters, and
primary actions MUST remain outside that scrolling region.

The service tree MUST use nodes and inspectors as its complete interaction
surface. The three-column configuration board MUST use compound cards, nested
rows, and inspectors as its complete interaction surface. A graph or board
toolbar MUST NOT show a visible surface title such as `Service tree` or repeat
the page title. The page heading and the graph or board accessible name MUST
provide the necessary context. A tree that is smaller than its viewport MUST
be centered in the available graph stage. A larger tree MUST keep its layout
origin and MUST be reachable with bounded graph-viewport scrolling.

Each actionable node, compound-card header, nested row, and assignment rung
MUST be a semantic control in the browser accessibility tree. Its accessible
name MUST identify the record and its important state, column or tree level,
and relationship to other records. A compound card MUST expose its header and
nested rows as one labelled group without making one control contain another
control. Decorative edges MUST be hidden from assistive technology only when
the connected control descriptions provide the same relationship. The
application MUST NOT render a duplicate service, provider, model, route, or
assignment table or list. The nodes and rows MUST provide the complete
accessible record and action surface.

Each graph or board MUST have one active keyboard tab stop. When no inspector
or modal is open, Tab MUST enter at the selected control, or at the first
control when there is no selection, and the next Tab MUST leave the surface.
In the service tree, Up and Down MUST move through the visible nodes, Right
MUST move to the first child, Left MUST move to the parent, and Home and End
MUST move to the first and last visible nodes. If the tree permits branch
collapse, Right MUST first expand a collapsed node and Left MUST first collapse
an expanded node.

In the configuration board, Up and Down MUST move through every visible
actionable control in the current column. The rendered order of a compound
card MUST put its header before its nested rows. The rendered order of an
assignment card MUST put its header before its ordered rungs. Left and Right
MUST move only through an actual relationship to the nearest connected control
in rendered order in the adjacent column. A provider connection MUST connect
to its provider-route rows. A provider-route row MUST connect to its provider
on the left and its exact assignment rungs on the right. An assignment rung
MUST connect to its exact provider route on the left. Home and End MUST move to
the first and last visible actionable control in the current column.

A key that has no valid target MUST keep focus on the current control. A focus
change MUST scroll the focused control into the labelled local viewport. Enter
or Space MUST open the same inspector or modal as a pointer action. An
assignment rung action MUST open its assignment inspector and identify that
rung. Escape MUST close the inspector or modal and return focus to the opening
control. If that control no longer exists, focus MUST follow the
unavailable-record rule below.

The graph or board MUST expose selected, expanded, inherited, disabled, empty,
loading, error, partial, and unavailable state without color alone. A refresh
MUST keep focus and selection when the record still exists. If the record no
longer exists, the surface MUST close its inspector, move focus to the first
available control or its empty-state action, and announce that the record is
unavailable.

OpenDLE UI MUST own the host-neutral relationship engine. This engine MUST own
the fixed three-column layout, compound groups, nested actionable rows,
connector geometry, connector endpoints on nested rows, contextual dimming,
search-result context, bounded local viewports, the one-tab-stop model,
arrow-key movement, selection, focus return, state presentation, live
announcements, phone stacking, and responsive inspector primitives. It MUST
accept host-supplied records, relationships, labels, actions, and state. It
MUST NOT contain a Router provider, canonical-model, provider-route,
assignment, service, credential, capability, or mutation type.

The Router MUST own the service-tree and configuration-board composition. It
MUST own record projection, technical identities, provider readiness,
canonical and route capabilities, assignment inheritance, fallback positions,
domain relationship labels, search fields, permissions, API calls, mutations,
inspector content, playground context, and corrective errors. The Router MUST
use the shared OpenDLE UI engine and inspector primitives. It MUST NOT copy or
fork that reusable behavior in this repository.

The Router configuration board MUST use the exact domain content, state,
relationships, search behavior, empty states, and focused verification in
[Providers, models, prices, and configuration](02-providers-models-prices-and-configuration.md#fixed-configuration-board).
Selecting a provider, canonical model, provider route, or assignment MUST open
its details and applicable actions without navigation. Create actions MUST use
the current column and selected control as context so the form does not ask for
known references.

OpenDLE UI component tests MUST cover compound-group semantics, nested-row
connector endpoints, shared routes, the one-tab-stop model, all specified
keyboard keys, search-result context, focus return, partial, unavailable,
empty, loading, and error states, fixed wide-screen columns, phone stacking,
local scrolling, and responsive inspectors. Router tests MUST supply the
domain assertions defined in the linked configuration-board specification. A
consumer test MUST prove that the shared engine renders host data without
importing Router domain types.

Each graph or board inspector MUST use a right-side panel on a wide screen and
a bottom sheet on a phone. Its content MUST use a local scroll region when
necessary.
Opening an inspector MUST move focus to its heading. The heading MUST be a
programmatic focus target, and Tab MUST move to its first applicable control.
On a wide screen, the side panel MUST let focus leave it. Tab and Shift+Tab MUST
let the user move between the panel and the rest of the page. On a phone, the
bottom sheet MUST make the background inactive and MUST keep focus in the sheet
until it closes.
Opening or closing an inspector MUST NOT change the page width or hide the
focused control outside the reachable local viewport. A failed create,
change, or delete MUST keep the applicable inspector open, keep the entered
non-secret values, and show a corrective error. It MUST NOT show success or
change the graph or board until the server confirms the write.

The playground MUST open as a modal from an applicable provider-route row or
assignment card. It MUST infer the exact provider-model or assignment target,
the available operations, and the controls that apply to its capabilities. It
MUST show the inferred target instead of asking the administrator to select it
again. It MUST show input, output, selected route, latency, usage, cost, media,
and a corrective safe error. The modal MUST fit the available desktop or phone
viewport, keep its long content in a local scroll region, move focus into the
dialog, and make the background inactive. Escape and the close action MUST
close it and restore focus to the row, card, or action that opened it.

The playground modal is a global administration presentation and is not a
workspace-owned navigation page. An allowlisted administrator session MUST
have unrestricted authority to execute its operations. The modal MUST NOT
ask for, fetch, store, or send a service API key or workspace identity. It
MUST NOT show a permission-scope control because the product has no
fine-grained administrator permissions.

An exact provider-route row MUST supply its global provider-model identity
and no service context. An assignment card MUST supply its assignment name
and the currently selected service as configuration context. The modal MUST
show that service context but MUST NOT describe it as authorization or
accounting ownership. Its run action MUST require the normal administrator
session, CSRF, and exact-Origin controls. Administrator playground accounting,
logs, and media MUST remain visible only to an administrator through the
global administration interfaces.

If the target record becomes disabled, deleted, or unavailable while the modal
is open, the modal MUST keep the reviewed input, prevent execution, and show
the target state. It MUST NOT silently change to a different assignment or
provider route.

The application MUST use reusable components, layout, tokens, and interaction
patterns from `../opendle-ui`. Router-specific data and actions MUST stay in
this repository.

OpenDLE UI MUST provide a shared `DataTable` and `EditableTable` family based
on Crewday's refined inline table. It MUST support read-only data tables and
editable tables from one consistent interaction system. The shared behavior
MUST include semantic table structure, caller-defined columns and widths,
responsive phone labels, long-value handling, row selection, keyboard
movement, visible focus, focus after create or delete, inline create and edit,
explicit, automatic, or batch save, validation, pending and error state, safe
delete confirmation, search and filters, initial and bounded incremental
loading, empty and unavailable state, optional detail rows, and accessible
live status.
Optional tree and reorder behavior MAY be included when a host needs it. Host
applications MUST own records, permissions, API calls, domain copy, and
mutation policy.

The Router application MUST use this family for each retained record table.
A failed save MUST keep the entered non-secret values, identify the affected
row, and provide a retry or cancel action. A failed delete MUST keep the row.
A delete confirmation MUST identify the record and the effects of removal,
and cancellation MUST return focus to the delete action. Loading more records
MUST keep the current selection and unsaved row values, and MUST announce the
new total. On a phone, the responsive presentation MUST keep each cell label,
value, state, and row action associated with its record without page-level
horizontal overflow.

## Calling-service administration

The Router MUST provide native backend API endpoints that let an authenticated
service manage its own assignments, workspaces, keys, calls, media jobs, and
accounting.

The Router MUST NOT host a service-user application, service administration
page, cross-origin frame, embed session, or browser grant. A calling service
MUST authenticate and authorize its human user. Its backend MUST call the
Router with a service API key.

A reusable React assignment component MUST live in `../opendle-ui`. Crewday's
assignment interface MUST be the preferred behavior reference. FJ2 MAY supply
other useful behavior, but its non-React implementation MUST NOT be a
component source.

A reusable React playground component MUST also live in `../opendle-ui`. It
MUST support an assignment or exact provider-model and show applicable
controls, input, output, selected route, latency, usage, cost, and a corrective
error. The Router global application and calling-service applications MAY use
the same components.
