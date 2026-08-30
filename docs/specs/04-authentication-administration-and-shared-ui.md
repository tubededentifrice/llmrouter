# Authentication, administration, and shared UI

Status: Accepted on 2026-08-23. The graph-first UI and administrator
playground amendments were accepted on 2026-08-24. The fixed compound-board,
administration-content, optional relationship-graph toolbar, compact shared
graph-inspector, shared table-alignment, and selectable configuration-command
amendments were accepted on 2026-08-29. The contextual child-creation amendment
and the full-height and edge-to-edge graph-page amendments were accepted on
2026-08-30. The Overview and no-top-bar shell amendment was accepted on
2026-08-30.

## Service API keys

A service API key MUST be a backend-only, revocable, long-lived bearer
credential. A backend MUST send it directly on each Router request. The
product MUST NOT exchange it for another token.

One service MAY have several named active keys. A new key MUST contain at
least 256 random bits and MUST be shown only once. The Router MUST store only
a verifier that is sufficient to authenticate it. Except for the one-time
creation response to an authenticated creator and its transient display to a
global administrator, the Router MUST NOT put the key in a URL, browser source
file or bundle, browser storage, log, activity detail, or later response.

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

- manage services, workspaces, and service API keys in the Services destination
  as specified for the
  [compact selected-service inspector](01-services-workspaces-and-assignments.md#compact-selected-service-inspector)
  and the
  [service-details route](01-services-workspaces-and-assignments.md#service-details-route);
- manage provider connections, credentials, models, capabilities, prices, and
  service assignments in one configuration board;
- preview and import model catalog entries from the model-create workflow;
- start price synchronization and inspect its results;
- open a contextual model, embedding, and media playground from an assignment
  or exact provider-route row;
- inspect accounting, detailed request logs, media, activity, cooldowns, and
  the small health summary.

The sidebar MUST NOT contain separate workspace-and-key, provider, model,
assignment, or playground destinations. The Services destination MUST own
service, workspace, and key administration as specified in
[Services, workspaces, and assignments](01-services-workspaces-and-assignments.md#service-details-route).
The three-column configuration board MUST own provider, model, provider-model,
assignment, and contextual playground administration. Direct application paths
for removed destinations MAY redirect to the applicable retained board, but
MUST NOT keep a second configuration page.

### Application shell, Overview, and local controls

The authenticated administration shell MUST keep the persistent left sidebar
on desktop and narrow desktop layouts. It MUST NOT render a persistent top bar
on any route. It MUST omit the top-bar region, wrapper, border, shadow, and
reserved block size in normal, loading, empty, error, and unavailable states.
The shell MUST NOT contain a global service selector, global refresh action, or
an empty replacement for either control.

The sidebar MUST contain these destinations in this order: `Overview`,
`Services`, `LLM configuration`, `Logs`, `Usage & cost`, and `Activity &
health`. It MUST keep the application identity, administrator identity, and
account actions. It MAY show non-interactive global-administrator context, but
it MUST NOT contain a service selection control. The link for the current
destination MUST use `aria-current="page"` and the shared active state without
color alone. A service-details route MUST select the `Services` destination.

`/overview` MUST remain the `Overview` dashboard destination and the default
authenticated landing destination. Its visible `h1` and sidebar label MUST be
`Overview`, and its document title MUST start with `Overview`. The authenticated
root path `/` MUST replace its history entry with `/overview` before it renders
route content. It MUST NOT render a duplicate landing page or add a second Back
step. A successful sign-in with no explicit valid return target MUST open
`/overview`. A direct `/overview` request MUST render the same dashboard without
another redirect.

Overview MUST keep global resource totals, the small health summary, and the
current provider-model cooldown summary. It MUST NOT require a selected service
or a primary workflow action. It MUST remain the dashboard composition point
for later accepted statistics and operational summaries. A later accepted
dashboard summary MUST extend Overview and MUST NOT require a second overview
destination.

Each route MUST own the service context and refresh behavior that it uses. A
route-local control MUST load or change only that route's context and data. The
shell MUST NOT own, change, or expose a shared selected-service state. Each
route MUST determine applicable service context from its own location and
control. Sidebar and phone-navigation links MUST NOT copy a service query to a
route that does not use it. Browser Back and Forward MAY restore context that
belongs to the restored route. The routes MUST use this ownership:

| Route | Service context | Refresh ownership |
| --- | --- | --- |
| `/overview` | None | `Refresh overview` in the page-heading action region reloads only dashboard summaries. |
| `/services` | The selected graph node and its route-local `service` query | `Refresh services` in the graph-wide action region reloads the service tree. |
| `/services/{serviceApiName}` | The route service; no selector | The page and its sections own their existing load, retry, and refresh actions. |
| `/configuration` | `Service context` in the graph toolbar; its empty value is `All services` | `Refresh configuration` in the graph-wide action region reloads the global catalog and the selected service's assignments. |
| `/logs` | Its local Logs filters only; no general service context | `Refresh Logs` and the existing Logs retry actions remain in the Logs view. |
| `/statistics` | Its local statistics service filter; no general service context | Submitting its local statistics filters reruns the report; no separate shell refresh exists. |
| `/operations` | None | `Refresh operations` in the page-heading action region reloads the health, retention, cooldown, and activity data; section retry and activity actions remain local. |

The configuration `Service context` value MUST use the route-local
`/configuration?service={serviceApiName}` location state. `All services` MUST
remove that query value and show the no-selected-service assignment state. The
Services selection MUST use the same query through its Services route location
rules. Navigation between Services and configuration MAY carry one valid
`service` query. The destination's own graph selection or `Service context`
control MUST show and own that value. This carried location value MUST NOT
create a shell control or apply to Overview, Logs, statistics, or operations.

A local refresh action MUST preserve applicable confirmed route context,
filters, graph search, selection, and focus. While pending, it MUST identify
its busy state, prevent a duplicate request, and keep focus on the action. A
failed refresh MUST keep confirmed data, identify it as stale when applicable,
show a corrective route-local error, and permit retry. A successful refresh
MUST update only current route data. A response for a route or service context
that is no longer active MUST NOT change visible state or focus.

Application navigation MUST move focus to the destination `h1`. The two graph
pages MUST use their programmatic heading and focus rule. Other routes MUST use
their visible `h1`. The heading MUST be a programmatic focus target and MUST NOT
be a normal Tab stop. On desktop, normal Tab order MUST pass through the skip
link, sidebar destinations and account actions, and the current route's local
controls in rendered order. It MUST NOT include a removed top-bar tab stop.
After route-entry focus moves to the heading, the next Tab MUST move to the
first route-local control or to the next application control when the route has
none. Activating a sidebar destination MUST push one history entry and move
focus to the destination heading after the route is ready to identify itself.

On a phone, the left sidebar MUST use the shared responsive replacement by the
persistent bottom navigation. That navigation MUST keep `Overview` as its first
destination and use the same current-page state. The shell MUST NOT add a phone
top bar. The route content MUST start at the dynamic viewport block-start edge
and end before the bottom navigation and its safe-area inset. Activating a
phone destination MUST close any open navigation surface, change the route,
and move focus to the destination heading. At 200% text size, local context and
refresh controls MUST wrap in their route-owned region without document-level
horizontal overflow.

Once the administrator session is known, route loading and failure MUST keep
the navigation shell and current-route heading available. The route MUST show
its labelled loading, partial, stale, or corrective failure state in its own
content region. Overview initial loading MUST say `Loading Overview.` An
initial Overview failure MUST say `Overview is unavailable.` and provide
`Retry Overview`. A partial Overview failure MUST keep confirmed summary
regions, identify each failed summary, and provide a local retry. The shell and
another route MUST NOT show the missing global refresh action as recovery.

Focused authenticated browser tests MUST cover `/` and `/overview` at `1440 ×
1000` desktop, `1100 × 800` narrow desktop, and `390 × 844` phone sizes with
device scale factor 1. They MUST prove the replace redirect, no extra Back
entry, default post-sign-in landing, exact heading and document title, active
Overview navigation state, retained totals, health and cooldown summaries, and
normal, loading, partial, stale, initial-failure, retry, and refresh states.

At each size, tests MUST visit every retained route and a service-details route
and prove that no top-bar element, content, border, shadow, tab stop, or reserved
block size exists. They MUST prove that no global service selector or refresh
action exists; each named local control is in its required route region; route
context follows the exact carry and removal rules through sidebar, phone, Back,
and Forward navigation; and stale responses do not change the active route.
Keyboard tests MUST verify the
exact desktop and phone navigation order, current-page state, route-heading
focus, local-control order, pending duplicate prevention, focus retention, and
corrective retry. Desktop and narrow measurements MUST prove that the sidebar
starts at the dynamic viewport block-start edge, reaches its block-end edge,
and ends at the `main` inline-start edge. Phone measurements MUST prove that the
left sidebar is absent, the `main` region starts at both viewport start edges,
and the bottom navigation reserves its exact block size and safe area.
Graph-page measurement tests MUST prove that removal of the top bar makes each
desktop and narrow graph page start at the dynamic viewport block-start edge
and adds no replacement row, while all accepted full-height, edge-to-edge,
inspector, local-scroll, safe-area, and no-document-overflow rules still pass.
Each normal and conditional surface MUST pass Axe and have a reviewed
screenshot at desktop, narrow, and phone size.

### Administration content

Visible static helper text includes page and section descriptions, field
notes, and instructional paragraphs. This term does not include headings,
labels, actions, record values, corrective errors, state messages, live-region
messages, or accessible names. One exact keep rule applies: visible static
helper text MUST explain a required action, a non-obvious effect, a safety
fact, a security boundary, a retention effect, a loading, empty, unavailable,
or failure state, a destructive impact, or an accessibility state. The
application MUST remove visible static helper text that does not meet this
rule.

Corrective errors, required actions, non-obvious effects, safety facts,
security facts, retention facts, loading states, empty states, unavailable
states, failure states, destructive impacts, and accessibility states MUST
remain available. This rule MUST NOT remove a live region or an accessible
name. It MUST NOT use helper text in place of a required accessible name or
state message.

A health-component message is optional. When it is absent, the application
MUST omit the message and MUST NOT show `No corrective message` or another
placeholder. When it is present, the application MUST show it if it gives a
corrective error, required action, non-obvious effect, safety fact, security
boundary, retention effect, or applicable state.

The activity panel MUST NOT show `This is a basic activity record. It is not
immutable configuration history.` or a replacement disclaimer with the same
purpose. This removal does not change the basic-activity requirements in
[Accounting, logs, retention, and operations](05-accounting-logs-retention-and-operations.md#basic-activity-log).

The content inventory MUST cover these retained routes:

| Path                         | View                           | Content that the inventory MUST check                                    |
| ---------------------------- | ------------------------------ | ------------------------------------------------------------------------ |
| `/overview`                  | overview dashboard             | totals, health, cooldown summaries, and local refresh states              |
| `/services`                  | service administration         | the service tree and compact and create inspectors                       |
| `/services/{serviceApiName}` | service-details administration | the heading, facts, form, workspace, key, delete, and conditional states |
| `/configuration`             | configuration administration   | all three board columns, inspectors, forms, and playground entry points  |
| `/logs`                      | retained-log inspection        | filters, retained records, selected details, media, and retention states |
| `/statistics`                | accounting statistics          | filters, query states, and accounting results                            |
| `/operations`                | operations                     | health, retention, cooldowns, and activity                               |

For each route, the test inventory MUST list each static helper text item, its
expected presence or absence, and one allowed keep reason for each retained
item. An automated browser test MUST compare the rendered text with this
reviewed inventory at desktop and phone widths. The test inventory MUST
include fixtures for corrective errors, required actions, non-obvious effects,
safety facts, security facts, retention facts, loading states, empty states,
unavailable states, failure states, destructive impacts, and accessibility
states. It MUST confirm the two named removals, live regions, and accessible
names. Each route MUST pass Axe at both widths and MUST have a reviewed
snapshot at both widths. Focused snapshots MUST also cover each conditional
state that is not present in the route's normal snapshot.

This content rule MUST NOT remove or hide a retained page heading except for
the two graph-page exceptions below. The other retained pages MUST keep their
visible page headings.

### Full-height graph pages

The `/services` and `/configuration` pages MUST NOT render a visible page
heading block. They MUST remove the complete visible eyebrow, title, and
subtitle or description block. They MUST NOT replace it with another visible
page title, graph title, introduction, or equivalent vertical spacer. This
rule does not remove an inspector heading, column heading, control label,
state message, corrective error, or accessible name.

Each graph page MUST contain one programmatic `h1` that does not occupy layout
space. Its exact text MUST be `Services` for `/services` and `LLM
configuration` for `/configuration`. It MUST stay in the accessibility tree
and MUST NOT use `hidden`, `display: none`, `visibility: hidden`, or
`aria-hidden`. That heading MUST label the page's `main` region. The document
title MUST start with the same text. The labelled local graph viewport MUST
have the exact accessible name `Services and parent relationships` on
`/services` and `LLM configuration relationships` on `/configuration`. A
toolbar or graph MUST NOT repeat either page name as a visible title.

When application navigation or an initial direct load opens either graph page,
focus MUST move to its programmatic `h1`. The heading MUST have
`tabindex="-1"` and MUST NOT enter the normal Tab order. The next Tab MUST move
to the first applicable graph-page control. This control MUST be the first
toolbar, retry, or empty-state control in rendered order, or the graph's one
active roving tab stop when no earlier control exists. A browser-history
restoration that has a valid graph or inspector focus target MUST use the
applicable graph or inspector restoration rule instead. When a loading state
has no graph-page control, the next Tab MUST move to the next application
control in document order. Loading, error, and empty states MUST keep the same
page heading, `main` label, and route focus entry.

Each graph page MUST fill the dynamic viewport block size that remains after
persistent shell navigation. It MUST use a definite block size, not only a
minimum block size. An inline sidebar MUST reduce the available inline size
but MUST NOT reduce the available block size. Because the shell has no top bar
or other persistent block-start control, the graph page MUST start at the
dynamic viewport block-start edge on desktop, narrow desktop, and phone
layouts. On a phone, the persistent bottom navigation and its reserved
safe-area inset MUST reduce the available block size. The page MUST use the
dynamic viewport so a change to mobile browser chrome recalculates the
available size. It MUST NOT use a fixed `100vh` substitute.

The graph-page layout MUST use rows for programmatic context, graph-wide
controls, and the graph workspace. The programmatic context row MUST occupy no
layout space. The control row MUST use only its rendered block size. The graph
workspace MUST receive all remaining block size and MUST use `min-block-size:
0` or equivalent overflow containment. The toolbar, search, filters, service
context controls, create controls, refresh controls, and other graph-wide
actions MUST remain outside the labelled scrolling graph viewport. A
contextual action in the graph roving focus group MUST remain in that viewport
with its selected graph control.

The document MUST NOT gain horizontal or vertical scroll only because of a
graph page, graph content, graph-wide control, or open graph inspector. Graph
content that is wider or taller than the remaining stage MUST scroll only in
the labelled local graph viewport. The viewport MUST use both horizontal and
vertical local overflow when required. Its complete content MUST remain
reachable. The toolbar and other graph-wide controls MUST remain visible when
that viewport scrolls. Opening, closing, or changing the mode of an inspector
MUST NOT change the graph-page block size or make the document scroll.

On desktop and narrow layouts, split and overlay inspectors MUST stay inside
the full-height graph workspace and follow the shared inspector rules. On a
phone, the graph page MUST end at the block-start edge of the persistent bottom
navigation. It MUST NOT extend behind that navigation or into its reserved
safe-area inset. A bottom-sheet inspector and its backdrop MUST keep the shared
viewport insets and stacking rules. They MUST be above the bottom navigation,
and that navigation MUST be inactive while the sheet is open. The mobile
navigation, safe-area inset, sheet, or backdrop MUST NOT cover a graph-page
control that remains active.

A graph loading state MUST use the full remaining graph workspace, identify
the graph that is loading, and keep its live status available. An error state
MUST use the same workspace, show its corrective error and retry action, and
keep the retry action in the normal page Tab order. An allowed empty state MUST
use the same workspace, identify what is empty, and keep each permitted
empty-state action reachable. The Services page MUST use the permanent-root
loading and failure rules and MUST NOT show a normal empty state after a
successful load. The same labelled local graph viewport MUST remain present in
each state. State content that does not fit MUST scroll only in that viewport.
It MUST NOT make the graph workspace or document scroll.

Focused authenticated browser tests MUST run at `1440 × 1000` desktop, `1100
× 800` narrow, and `390 × 844` phone CSS-pixel viewports with device scale
factor 1. For each route and size, a measurement test MUST verify all of these
results within one CSS pixel:

1. The graph page's block-start edge equals the dynamic viewport block-start
   edge and has no shell control or reserved space above it.
2. Its block-end edge equals the dynamic viewport edge on desktop and narrow
   layouts, or the block-start edge of the bottom navigation on a phone.
3. Its measured block size equals the difference between those two edges.
4. The labelled graph viewport receives the space that remains after the
   rendered graph-wide controls and shared gaps.
5. The document scrolling element's `scrollWidth` and `scrollHeight` do not
   exceed its client dimensions because of the graph-page layout.

The tests MUST verify that the complete visible eyebrow, title, and subtitle
blocks are absent and reserve zero space. They MUST verify the exact
programmatic headings, document-title prefixes, `main` labels, graph names,
route-entry focus, Tab entry, toolbar access, action access, and one graph
roving tab stop. An oversized fixture MUST increase both local graph overflow
dimensions, allow both local scroll positions to change, keep graph-wide
controls fixed, and leave the document dimensions unchanged. Tests MUST repeat
the measurements for normal, loading, corrective-error, retry, and allowed
empty states when applicable. They MUST also repeat the normal-state
measurements with the applicable split, overlay, or bottom-sheet inspector
open. The phone tests MUST verify dynamic-viewport resize, safe-area
reservation, bottom-navigation stacking and inactive state, sheet focus
containment, and no covered active control. The safe-area test MUST use a
non-zero reserved inset. At 200% text size, focused tests MUST verify that
graph-wide controls, state actions, graph content, and inspector controls stay
reachable through local scrolling without document overflow. Each normal and
conditional surface MUST pass Axe and have a reviewed screenshot at its
applicable desktop, narrow, and phone size.

### Edge-to-edge graph stages

The `/services` and `/configuration` page surfaces MUST use the edge-to-edge
mode. Each graph stage and its labelled local scrolling viewport MUST have no
page gutter. Before a split inspector takes its space, the graph stage's
inline-start and inline-end border edges MUST equal the `main` content box
edges. On desktop, this area is all space beside the sidebar. On a phone, it is
the complete dynamic viewport width.

Graph-wide controls MUST use one shared responsive inset. This rule applies to
toolbars, searches, filters, service-context controls, graph-wide create
controls, refresh controls, primary actions, and state actions. The inset MUST
be `var(--od-page-gutter)` at each inline edge. On a device with a non-zero
inline safe area, each side MUST instead use the larger of
`var(--od-page-gutter)` and that physical side's safe-area inset. Thus, the
left content edge MUST use
`max(var(--od-page-gutter), env(safe-area-inset-left))`, and the right content
edge MUST use
`max(var(--od-page-gutter), env(safe-area-inset-right))`. The Router MUST NOT
define another graph-page inset, control gutter, maximum width, or
negative-margin compensation. A control-row background or separator MAY span
the complete stage width, but its control content MUST use this inset.

The graph viewport MUST start immediately after the graph-wide control rows. It
MUST extend to the graph-region inline edges and the graph-stage block-end edge.
It MUST NOT inherit the control inset. Graph layout padding that places nodes
and connectors inside the scrollable canvas remains part of the graph content.
It MUST scroll with that content and MUST NOT act as a page gutter. Loading,
error, retry, and allowed empty-state content inside the graph viewport MUST use
the same shared responsive inset without reducing the viewport's outer width.

The stage and graph viewport MAY use block-start, block-end, or internal
separator borders. A border MUST use border-box sizing and MUST NOT reduce the
edge-to-edge outer width or cause document overflow. At an edge that meets the
`main` content box, the stage and viewport MUST NOT use an outer inline border,
margin, border radius, or shadow. Graph nodes, cards, internal panels, overlay
inspectors, and bottom sheets MAY keep their shared borders, radii, and
shadows.

In split mode, the inspector MUST occupy the shared exact `21rem` at the
stage's inline-end edge. It MUST NOT receive a page gutter or control inset.
The remaining graph region MUST start at the `main` inline-start edge and end
at the inspector boundary. Its toolbar content MUST use the shared responsive
inset within that remaining region, and its local graph viewport MUST use the
complete remaining region width. The separator between the graph and inspector
MAY use one border that is included in the inspector's `21rem` border box.

In overlay mode, the graph stage, graph-wide control rows, and local viewport
MUST keep their complete edge-to-edge width. The inspector MUST keep the shared
exact `21rem` width and `0.875rem` inline-end inset from the stage edge. The
control inset MUST NOT be added to that overlay inset. The shared reachability
rule MUST account for the overlay without changing the stage, viewport, or
document width.

In phone bottom-sheet mode, the graph stage and local viewport MUST remain the
complete dynamic viewport width above the reserved bottom navigation and its
safe area. Graph-wide control content MUST use the responsive safe-area inset
above. The bottom sheet MUST keep its shared `0.75rem` browser-viewport insets;
it MUST NOT align to or add the graph control inset. The backdrop MUST cover
the complete browser viewport. Neither the sheet nor the bottom navigation MAY
add an inline page gutter or change the graph-page width.

OpenDLE UI MUST own the host-neutral application of the shared control inset
inside `GraphToolbar`, `GraphWorkspace`, and `RelationshipGraph`. The Router
MUST select the edge-to-edge page composition and supply its domain controls.
It MUST NOT copy the shared control-row layout or add Router-only toolbar,
search, filter, action, safe-area, or inspector alignment rules. The shared
components MUST coordinate so that each control row receives the inset exactly
once. Nested shared components MUST NOT add the inset again. The shared edge
behavior MUST apply when these components are inside a `PageSurface` whose
`edgeToEdge` value is true. A false or omitted `edgeToEdge` value MUST keep the
existing shared non-edge geometry, including its gutter, borders, radii, and
maximum-width behavior. OpenDLE UI MUST keep `PageSurface` and
`PageSurfaceProps` as package-root exports, and `PageSurfaceProps.edgeToEdge`
MUST remain an optional boolean.

The edge-to-edge width rule and the full-height rule MUST apply together. A
graph-wide control that wraps MUST use more of the control row's block size and
leave less block size for the local graph viewport. It MUST NOT widen the
document, add document scroll, add a page gutter to the graph viewport, or
increase the full-height page. At 200% text size, controls and actions MUST wrap
inside the shared inset, and graph content MUST remain reachable through its
local scroll.

Focused authenticated measurement tests MUST use the full-height test sizes:
`1440 × 1000` wide desktop, `1100 × 800` narrow desktop, and `390 × 844`
phone, with device scale factor 1. Within one CSS pixel, both graph pages MUST
prove:

1. The stage border box starts at the `main` inline-start edge and ends at the
   `main` inline-end edge before split-inspector allocation.
2. The local viewport has zero page-gutter offset from its graph-region inline
   edges.
3. Each graph-wide control content edge equals its graph-region edge plus the
   computed shared responsive inset for that side.
4. No graph-page wrapper, stage, viewport, or state surface has a second inset,
   smaller maximum width, negative-margin compensation, outer inline border,
   or outer inline radius.
5. The document scrolling element's dimensions do not exceed its client
   dimensions because of the edge-to-edge layout.

Wide tests with a split inspector MUST verify the exact `21rem` inline-end
inspector border box, its included separator, its full inspector-host block
size, the remaining edge-to-edge graph region, and the control inset inside
that region. Narrow tests with an overlay inspector MUST verify its exact
`21rem` width, `0.875rem` inline-end and block-end insets, and `4.75rem`
block-start inset from the stage edge. They MUST verify that it stays below
graph-wide controls, receives no added control inset, and does not change the
stage or viewport width. Phone tests MUST verify a stage and viewport from
inline coordinate zero through the dynamic viewport width, control insets with
zero and unequal non-zero physical left and right safe areas, the independent
`0.75rem` sheet insets, the shared sheet maximum height, complete backdrop
coverage, and the reserved bottom-navigation area. The unequal safe-area test
MUST prove that the left and right values are not exchanged. Tests MUST repeat
width measurements for loading, error, retry, allowed empty, oversized, and
200%-text fixtures. They MUST verify fixed and uncovered graph-wide controls,
two-axis local graph scrolling, no document overflow, Axe results, and reviewed
screenshots at all three sizes.

Focused OpenDLE UI tests MUST import `PageSurface`, `PageSurfaceProps`,
`GraphToolbar`, `GraphWorkspace`, and `RelationshipGraph` from the built
package root. They MUST cover true, false, and omitted `PageSurface`
`edgeToEdge` values. They MUST prove one shared control inset in each standalone
and nested composition, zero graph-viewport page gutter, the physical safe-area
maximum on both sides, wrapped controls, unchanged non-edge geometry, and no
local consumer override.

The application shell and each retained page MUST use the complete available
width after the sidebar. Retained pages other than the two graph pages MUST use
one responsive gutter system for page headings, filters, panels, graphs, and
tables. A page MUST NOT use one maximum width for its controls and another
width for its result, and the page container MUST NOT set a smaller maximum
width. The two graph pages MUST use the edge-to-edge stage and shared control
inset above. On a phone, the shell MUST use the complete viewport width and
MUST prevent page-level horizontal overflow. A graph or dense data region MAY
scroll in its own labelled viewport when its content cannot reflow. Its
heading, filters, and graph-wide primary actions MUST remain outside that
scrolling region. A contextual action in a graph's roving focus group MUST stay
with its selected control and MAY be inside the labelled graph viewport.

The service tree MUST use nodes and inspectors as its graph interaction
surface. Service management outside the graph MUST follow
[Services, workspaces, and assignments](01-services-workspaces-and-assignments.md#service-details-route).
The three-column configuration board MUST use compound cards, nested rows, and
inspectors as its complete interaction surface. A graph or board
toolbar MUST NOT show a visible surface title such as `Service tree` or repeat
the page title. The programmatic page name and the graph or board accessible
name MUST provide the necessary context. A tree that is smaller than its
viewport MUST be centered in the available graph stage. A larger tree MUST
keep its layout origin and MUST be reachable with bounded graph-viewport
scrolling.

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
an expanded node. The selected-node child-service action MUST join this roving
focus order only as specified in
[Services, workspaces, and assignments](01-services-workspaces-and-assignments.md#contextual-child-service-creation).
That action MUST NOT add a second graph Tab stop.

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
change MUST scroll the focused control into the labelled local viewport. The
service-node keyboard behavior MUST follow
[Services, workspaces, and assignments](01-services-workspaces-and-assignments.md#compact-selected-service-inspector).
For each other graph or board control, Enter or Space MUST open the same
inspector or modal as a pointer action. An assignment rung action MUST open its
assignment inspector and identify that rung. Escape MUST close the inspector or
modal and return focus to the opening control. If that control no longer exists,
focus MUST follow the unavailable-record rule below.

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

### Selectable configuration controls and commands

The configuration board MUST use its compound controls to separate resources
from stored relationships. These controls MUST be selectable:

- a provider-connection card, which selects one provider connection;
- a canonical-model card header, which selects one canonical model;
- a provider-route nested row, which selects one provider-model mapping;
- an assignment card header, which selects the effective assignment with that
  name for the selected service; and
- an assignment rung, which selects one ordered assignment candidate link in
  the effective assignment.

An assignment rung is a relationship control. Its accessible name and visible
content MUST identify the assignment, the provider route, its one-based
fallback position, and whether the link is `Direct` or `Inherited`. `Direct`
means that the candidate link is in a direct-chain definition on the selected
service. `Inherited` means that the rung comes through an assignment-name
reference or a definition on an ancestor service. A provider-route row is a
resource control. It MUST NOT describe its provider-model mapping as an
assignment candidate link.

The drawn connector between a provider and a provider-route row and the drawn
connector between a provider-route row and an assignment rung MUST remain
presentational. A connector stroke MUST NOT be a pointer target, tab stop, or
separate accessibility-tree item. Its connected controls MUST contain the full
relationship text. A credential, price row, capability value, state label,
column heading, non-actionable group header, loading or error placeholder, and
empty-state message MUST NOT become a selectable graph item. An action in one
of these regions MAY keep its normal independent control.

Pointer click, pointer tap, and touch tap on a selectable control MUST select
that control and open the same inspector as Enter or Space. The assignment
inspector MUST identify a selected rung and its fallback position. A visible
inspector action MUST provide the pointer and touch equivalent of each
available keyboard command. The application MUST NOT require a long press,
double tap, context menu, hover, or precise selection of a connector stroke.

OpenDLE UI MUST extend the current host-neutral `RelationshipGraph` API with
these package-root exports:

```tsx
export type RelationshipGraphCommand = "delete" | "disable" | "enable";

export interface RelationshipGraphNode {
  readonly commands?: readonly RelationshipGraphCommand[];
  readonly commandPending?: boolean;
  readonly commandPendingLabel?: string;
}

export interface RelationshipGraphNodeCommandContext
  extends RelationshipGraphNodeContext {
  readonly command: RelationshipGraphCommand;
}

export interface RelationshipGraphProps {
  readonly onNodeCommand?: (
    context: RelationshipGraphNodeCommandContext,
  ) => void;
  readonly statusMessage?: string;
}
```

`commands` MUST contain the commands that are available for the current item
and current host state. Omission or an empty list MUST mean that the item has
no keyboard command. Duplicate command values MUST be invalid. When one item
has a command, the host MUST supply `onNodeCommand`. The shared component MUST
put only the applicable keys in `aria-keyshortcuts`. It MUST dispatch the
focused item, command, column, group, and control element. It MUST NOT infer a
mutation from a node state, label, column, relationship, or command key.
`commandPending` MUST make the control busy, remove all key shortcuts, and show
`commandPendingLabel` as visible state text. It MUST set `aria-busy` to true
and MUST keep the control selected and focusable. A true `commandPending`
value MUST require a non-empty label and an empty or omitted `commands` list.
A false or omitted value MUST NOT show the pending label.
`statusMessage` MUST let the host send domain result text to the graph's shared
polite live region. The component MUST announce each non-empty changed value
once and MUST keep its internal search and unavailable-item announcements.
When a host message and an internal message are pending at the same time, the
component MUST queue and announce both in their arrival order. It MUST NOT
replace, combine, or omit either message.
The host MUST clear the value before it uses the same text for a later result.

OpenDLE UI MUST map the unmodified keys as follows:

| Key | Host-neutral command |
| --- | --- |
| `Delete` | `delete` |
| `d` | `disable` |
| `e` | `enable` |

When more than one command is available, `aria-keyshortcuts` MUST list its
applicable `Delete`, `d`, and `e` tokens in that order with one space between
tokens.

The letter keys are case-sensitive. `D`, `E`, Backspace, and a key used with
Control, Alt, Meta, or Shift MUST NOT run a command. A command MUST apply to
the focused selectable control. The engine MUST select that control before it
dispatches the command. This rule also applies when arrow-key focus movement
left another control selected.

The engine MUST ignore a command when the event was prevented, is an input
method composition event, is an automatic key repeat, or starts in an input,
textarea, select, content-editable region, editor, inspector, or open dialog.
It MUST also ignore the command when focus is not on a selectable graph
control, the command is absent from that control, or the host marks the item
pending by removing its commands. An ignored command MUST make no selection,
request, confirmation, state change, or announcement.

The Router MUST set `commandPending` with the applicable operation label and
remove an item's commands synchronously when it starts a preview or mutation.
It MUST keep that state until the operation succeeds or fails. Thus, a second
physical key press while work is pending MUST NOT queue or send another
request. After success, the Router MUST supply commands from the new state. A
second `d` after a successful disable and a second `e` after a successful
enable MUST do nothing because that same command is no longer available. The
opposite command can become available. One command MUST create at most one
initial preview or direct mutation request. A later explicit confirmation is a
separate user action. It MUST create at most one confirmation request or
assignment write.

The Router MUST supply command eligibility as follows:

| Selected control | `Delete` | `d` | `e` |
| --- | --- | --- | --- |
| Live provider connection | yes | when its stored enablement is on | when its stored enablement is off |
| Live canonical model | yes | when its stored enablement is on | when its stored enablement is off |
| Live provider-route row | yes | when its stored enablement is on | when its stored enablement is off |
| Service-local assignment-definition header, including a definition that names another assignment | yes | no | no |
| Rung stored in a local direct-chain assignment | yes | no | no |
| Rung resolved through an assignment-name reference or ancestor service | no | no | no |
| Assignment header from an ancestor service | no | no | no |
| Empty implicit root `default` | no | no | no |
| Unresolved, deleted, loading, error, or placeholder item | no | no | no |

A service-local assignment-definition header MUST keep `Delete` when its
stored definition names another assignment. That command targets the local
definition that the header represents. It MUST NOT target the named assignment
or any resolved rung. The rungs shown through that local assignment-name
reference are inherited relationship controls and MUST NOT get `Delete`.

An unavailable live provider, canonical model, or provider route MUST keep the
enablement command that matches its own stored value. A dependency state does
not change that stored value. Thus, `d` MUST remain available when the live
record is stored as enabled but is unavailable because of a credential,
provider, canonical model, cooldown, or other dependency. `e` MUST remain
available when the live record itself is stored as disabled, even when it will
be unavailable after enablement. A missing referenced record is not a live
record and MUST have no command.

`d` and the matching inspector action MUST change only the selected provider,
canonical-model, or provider-model enablement to off. `e` and the matching
inspector action MUST change only that stored enablement to on. They MUST use
the normal direct configuration write, validation, activity, routing, and
concurrent-write rules in
[Providers, models, prices, and configuration](02-providers-models-prices-and-configuration.md#direct-configuration-changes).
They MUST NOT delete a record, change another stored enablement value, or
silently repair an unavailable dependency. Enable and disable MUST NOT require
a confirmation.

`Delete` MUST only request a deletion. It MUST NOT apply a change before the
administrator confirms it. For a provider connection, canonical model,
provider-model mapping, or service-local assignment-definition header, the
Router MUST create and show the exact reviewed cascade preview and MUST confirm
it through the operations in
[Reviewed configuration deletion](02-providers-models-prices-and-configuration.md#reviewed-configuration-deletion).
The confirmation MUST show the target, each delete or change effect, each
retained shared credential, and each routing or inheritance effect that the
reviewed operation requires.

For a rung stored in a local direct-chain assignment, `Delete` MUST open a
confirmation that identifies the assignment, provider route, fallback
position, and effective chain before and after removal. Confirmation MUST
replace that local direct-chain assignment definition through its existing
administrator assignment write with only the selected candidate link removed.
It MUST close the gap and keep the other links in their prior relative order.
Removal of the last link MUST keep an empty direct-chain assignment. It MUST
NOT delete the assignment or the provider-model mapping. A resolved rung MUST
NOT become a stored local link or permit removal from its source. The normal
complete-state validation, concurrent-write, and activity rules for a direct
configuration change MUST apply.

Canceling a reviewed-cascade confirmation MUST NOT send its confirmation
request. Canceling an assignment-rung confirmation MUST NOT send its
assignment write. Each cancellation MUST return focus and selection to the
control that opened it. While a preview or confirmation dialog is open, all
graph commands MUST remain suppressed.

After a successful enable or disable, the board MUST keep the changed control
selected and in view and MUST show its new state in visible text. When the
operation started from a graph key, focus MUST stay on that control. When it
started from an inspector action, the inspector MUST stay open, focus MUST
stay at the same action position, and closing the inspector MUST return focus
to the selected control. The action at that position MAY change from Disable
to Enable or from Enable to Disable. The board MUST announce
`{record} enabled.` or `{record} disabled.`. If an enabled record is still
unavailable, the visible state and announcement MUST also give `Unavailable`
and the corrective reason.

After successful removal of an assignment rung, the board MUST select and
focus its assignment card header and announce the removed provider route and
the new number of candidates. After successful removal of a provider-route
row, the board MUST select and focus its canonical-model card header when that
header remains available. After successful deletion of a service-local
assignment definition that exposes an inherited definition or the empty
implicit root `default`, the board MUST select and focus that replacement
control and announce its source. This rule applies when the deleted local
definition had a direct chain or named another assignment. For another deleted
selected record, or when the named fallback control is not available, the
board MUST select and focus the first available control in rendered board
order. When no control is available, it MUST clear graph selection and focus
its empty-state action. The success announcement MUST identify the deleted
record and MUST state that the shown board contains the applied result.

A failed preview, confirmation, enable, disable, or candidate-link removal
MUST keep the selected board control and current non-secret inspector values.
It MUST make no success change. A failed preview MUST keep or return focus to
the opening control and show a corrective error. A failed confirmation, or a
failed mutation for which an applicable dialog or inspector is open, MUST keep
that surface open, move focus to or keep focus on the corrective error, and let
the administrator retry or cancel. Canceling after such a failure MUST return
focus and selection to the opening control. When a graph key starts a failed
enable or disable while no applicable inspector or confirmation dialog is
open, the board MUST keep focus on the selected control, show a corrective
alert associated with that control, and let the administrator retry the
command or dismiss the error. The error MUST identify the selected record and
MUST use an alert. A polite live announcement MUST report a successful state
change or removal. It MUST NOT announce success before the server confirms the
write, announce one operation twice, or put a credential value or confirmation
token in a live region.

An expired, used, or stale reviewed-cascade preview MUST keep the old
confirmation content visible with its exact conflict error. Its corrective
action MUST create and show a new preview. It MUST NOT submit the old token or
silently replace the old preview. A storage failure with an unused token MAY
retry the same confirmation after the administrator requests the retry. A
candidate-link validation failure MUST keep the proposed chain visible so the
administrator can correct it or cancel.

Selected, focused, pending, enabled, disabled, unavailable, inherited, and
direct state MUST each have a visible shape, border, icon, text, or state label
when it applies. Color, an accessible name, `aria-selected`, `aria-pressed`, or
`aria-keyshortcuts` alone is not sufficient. Each available inspector action
MUST show its command key. An inherited item MUST show its source. An
unavailable item MUST show its corrective reason. A pending item MUST show the
operation in progress and MUST not look enabled for another action.

OpenDLE UI MUST own semantic compound controls, selection, one-tab-stop and
arrow-key behavior, command-key recognition, event suppression, repeat
suppression, `aria-keyshortcuts`, generic pending and state presentation,
generic focus fallbacks, and host-neutral live-region primitives. It MUST keep
connector strokes presentational. The Router MUST own the control projection,
resource and relationship identity, current service, direct and inherited
status, stored enablement, readiness, command eligibility, inspector actions,
confirmation content, API calls, mutation lock, result refresh, domain error
text, domain announcements, and activity effects. OpenDLE UI MUST NOT import a
Router type or call a Router API. The Router MUST NOT copy the shared keyboard
or focus engine.

Focused OpenDLE UI component tests MUST import
`RelationshipGraphCommand`, `RelationshipGraphNodeCommandContext`, and the
extended `RelationshipGraphNode` and `RelationshipGraphProps` from the built
package root. They MUST cover every command key, exact callback context,
selection before dispatch, command omission, duplicate-command rejection,
pending-state validation and presentation, `aria-keyshortcuts`, host status
messages, internal announcement preservation, simultaneous host and internal
messages in arrival order, case and modifier handling, prevented events, input
method composition, automatic and physical repeated keys, editable controls,
inspectors, dialogs, pending items, and unsupported items. They MUST also prove
that connector strokes have no pointer, focus, or accessibility target and
that assignment rungs remain complete semantic relationship controls.

Router command-projection tests MUST prove the exact command list for each row
in the eligibility table. They MUST cover stored enablement separately from
dependency readiness, incomplete data, pending projection, a local direct-chain
definition, a local definition that names another assignment, its resolved
rungs, an ancestor definition, and the implicit root `default`.

Router localhost browser tests MUST use `http://127.0.0.1:5174`. They MUST
cover provider, canonical-model, provider-route, service-local direct-chain
assignment, service-local definition that names another assignment, and direct
assignment-rung selection by keyboard, pointer, and touch. They MUST prove that
`Delete` is available on both service-local assignment-definition headers and
is not available on a rung resolved through the assignment-name reference.
They MUST cover each other eligible and ineligible `Delete`, `d`, and `e` case;
unavailable records; ancestor assignments; the implicit root `default`;
confirmation cancel, success, stale-preview conflict, validation failure, and
storage failure; a graph-key mutation failure with no open inspector; rapid
repeated keys; focus and selection after each result; visible non-color state;
exact live announcements; and retained non-secret values. Desktop and phone
tests MUST cover keyboard and touch-equivalent actions, local board scrolling,
200% text, no page-level overflow, and Axe results. The tests MUST prove that
one user command creates no more than one initial preview or direct mutation
request, that one later confirmation action creates no more than one
confirmation request or assignment write, and that a connector stroke is not
an action target.

### Shared relationship-graph toolbar

OpenDLE UI MUST export this optional toolbar API for `RelationshipGraph` from
its package root:

```tsx
export interface RelationshipGraphToolbarOptions {
  readonly leading?: ReactNode;
  readonly actions?: ReactNode;
}

export interface RelationshipGraphProps {
  readonly toolbar?: RelationshipGraphToolbarOptions;
}
```

When `toolbar` is omitted or `undefined`, `RelationshipGraph` MUST keep its
current standalone search rendering and MUST NOT render a `GraphToolbar`. This
rule keeps existing consumers compatible. When `toolbar` is present,
`RelationshipGraph` MUST render one shared `GraphToolbar`. An empty object, or
an object whose host slots add no rendered child, MUST render a search-only
toolbar. An absent host slot or one that adds no rendered child MUST NOT render
an empty wrapper or reserve space.
The graph MUST render its search control in the center slot. The host MAY put
context in `leading` and controls in `actions`. Host content MUST NOT replace,
remove, or add content to the center slot. Neither `RelationshipGraph` nor a
host slot MAY add a visible title for the graph surface, or a heading that
repeats the page title. The programmatic page name and the graph accessible
name MUST continue to give the graph context.

The graph MUST own the center search rendering and style. When `searchQuery`
is defined, that value alone MUST control the rendered input and graph result.
Typing or clearing MUST call `onSearchQueryChange` with the requested value,
and the graph MUST NOT change the effective query before the host supplies a
new `searchQuery`. `defaultSearchQuery` MUST only initialize uncontrolled
search. In uncontrolled mode, typing or clearing MUST change the internal
query and MUST also call `onSearchQueryChange` when the callback is present.
Adding, removing, or changing `toolbar` or either host slot MUST NOT reset the
effective query, graph selection, or search origin.

The clear action MUST be available for a non-empty effective query and MUST
request the empty value. After the effective query becomes empty, the graph
MUST restore focus and selection to the control that was selected before
search when it still exists. Otherwise, it MUST move focus and selection to the
first available graph control. When the graph has no control, it MUST clear
selection and focus the graph-owned search. A slash key MUST focus the
graph-owned search unless the event was prevented or started in an editable
control. When focus is in one of several relationship graphs, that graph MUST
own slash. When no graph contains focus, the first relationship graph in
document order MUST own slash.

The toolbar option MUST also preserve complete and partial no-result states,
search context, live announcements, search labels, accessible names,
selection, graph keyboard movement, and the graph's one active graph-control
tab stop.

Keyboard order MUST follow the rendered toolbar slots. Focus MUST move through
focusable leading-slot content, the graph search and its conditional clear
action, focusable action-slot content, the graph's one active graph-control tab
stop, and the following page content. Focus order within each host slot MUST
follow its rendered order. An absent or non-focusable slot MUST add no tab stop.
Without a toolbar, the existing standalone search, conditional clear action,
active graph-control tab stop, and following page content MUST keep this same
relative order. A slash-key search focus action MUST move focus to the same
graph-owned search control that is in the applicable tab order.

The shared toolbar MUST be a sibling before the labelled scrolling graph
viewport. It MUST NOT be inside that viewport, and it MUST remain in place when
the viewport scrolls. The viewport MUST keep its bounded local horizontal and
vertical scrolling when its graph content cannot reflow. On a wide screen, the
rendered slots MUST use the shared `GraphToolbar` layout in leading, center,
and actions order. On a phone, the shared component MUST reflow the rendered
slots in that same order. An absent slot MUST collapse without an empty row,
column, gap, or container.

Long leading content, search labels, search values, placeholders, and action
labels MUST wrap, clip safely, or reflow in their applicable slots. At 200%
text size and at phone width, they MUST NOT cover the search, hide a control,
change the toolbar order, or cause page-level horizontal overflow. The toolbar
MUST NOT scroll with the graph viewport or make the page scroll horizontally
to reach graph content.

OpenDLE UI MUST own toolbar composition, slot layout, search rendering and
behavior, responsive reflow, keyboard order, and separation from the graph
viewport. The host MUST own context copy, action controls, permissions, action
behavior, domain labels, and search-label values. A host MUST NOT copy the
shared toolbar or search layout.

Focused OpenDLE UI tests MUST import `RelationshipGraph`,
`RelationshipGraphProps`, and `RelationshipGraphToolbarOptions` from the
package root. They MUST cover an omitted and explicitly `undefined` toolbar,
an empty search-only toolbar, host slots that add no rendered child, actions
with the graph-owned center search, and all three rendered slots. They MUST
confirm that a host slot that adds no rendered child has no wrapper and reserves
no layout space.

The tests MUST cover controlled and uncontrolled search, callback values, a
delayed controlled update, toolbar changes without state reset, slash focus in
one and several graphs, prevented and editable-target slash events, clear and
each focus-return fallback, complete and partial no-result states, live
announcements, one active graph-control tab stop, the specified keyboard order,
and accessible names. They MUST also cover long content in all three slots,
200% text, wide-screen and phone reflow, stable toolbar geometry during local
viewport scrolling, bounded local scrolling, and no page-level overflow. The
absent, search-only, and all-slot surfaces MUST pass Axe at wide-screen and
phone widths. Each OpenDLE UI change MUST keep React Doctor at score 100 with
zero diagnostics.

A Router integration fixture MUST supply configuration-graph data, test-only
host context, and test-only host actions to a `RelationshipGraph` with the
shared toolbar. It MUST prove that the toolbar does not add a second visible
graph title and does not change Router domain behavior. The optional API alone
MUST NOT require a production host to add context copy or an action that its
own accepted specification does not define.

Source-consumer checks MUST use the built package root and prove that Router,
Ontology, and Xbot continue to compile and render. They MUST cover Router's
omitted `RelationshipGraph` toolbar and actions-only `GraphToolbar`, Ontology's
leading-and-actions toolbar, Xbot's center-and-actions toolbar, and the Router
integration fixture with all three rendered slots. They MUST prove that a
consumer does not copy the shared toolbar or search layout. Each changed
consumer surface MUST pass Axe at wide-screen and phone widths and keep React
Doctor at score 100 with zero diagnostics.

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

### Shared compact graph inspector

OpenDLE UI MUST own one compact inspector system for `GraphWorkspace`,
`RelationshipGraph`, and other host-neutral graph surfaces. The inspector host
MUST be the `GraphWorkspace` content box. When `RelationshipGraph` renders an
inspector directly, its root content box MUST be the inspector host. Another
host-neutral graph surface MUST use `GraphWorkspace` for this behavior. The
system MUST select one of three modes from the inspector-host content-box width:

- At `69rem` or more, it MUST use split mode. The inspector MUST occupy the
  inline-end with a border-box width of exactly `21rem`. The graph region and
  its controls MUST occupy the remaining width. At the exact `69rem` boundary,
  the graph area MUST be `48rem`.
- Above `48rem` and below `69rem`, it MUST use a non-modal right overlay. The
  overlay MUST have a border-box width of exactly `21rem`, stay below the
  toolbar, and leave the graph region and its controls at their full width.
- At `48rem` or less, it MUST use a modal bottom sheet. The sheet MUST have a
  `0.75rem` inset from each inline side of the browser viewport and from its
  bottom. Its border box MUST extend between the two inline insets, and its
  maximum height MUST be `calc(100dvh - 1.5rem)`. The `21rem` width applies only
  to split and overlay modes.

The measured inspector-host content box is the complete width inside its
border, if it has one, before space is assigned to the inspector. The
measurement MUST include the `21rem` that split mode then assigns to the
inspector. It MUST NOT include the page gutter or sidebar. OpenDLE UI MUST own
the mode and boundary calculation. It MUST use `ResizeObserver`, a CSS container
query, or an equivalent container-size mechanism. It MUST NOT use the browser
viewport width as a substitute. Exactly `69rem` belongs to split mode. Exactly
`48rem` belongs to bottom-sheet mode.

In split mode, the inspector-host outer width MUST stay unchanged. The
`GraphWorkspace` stage and toolbar MUST end before the inspector. A
`RelationshipGraph` toolbar or standalone search and its labelled graph
viewport MUST also end before the inspector. The shared relationship graph MUST
keep its three `13rem` minimum columns and MAY reduce only its shared column
gaps and inline padding until the graph content reaches a `48rem` floor. Content
that needs more than that floor MUST use the labelled local graph viewport.
Split mode MUST NOT add page-level horizontal overflow or change the page
width.

In overlay mode, the inspector MUST use an inline-end inset of `0.875rem`, a
block-start inset of `4.75rem`, and a block-end inset of `0.875rem`. It MUST NOT
cover the toolbar. The graph region and its controls MUST keep their width, and
the graph region MUST keep its local scroll. When the selected control is
behind the overlay, the shared system MUST scroll that control into the visible
part of its local viewport and account for the overlay width. A host MUST NOT
calculate this scroll offset. The complete graph MUST remain reachable without
page-level horizontal scrolling.

In bottom-sheet mode, OpenDLE UI MUST open the inspector with native modal
dialog behavior or an equivalent accessible modal implementation. The
background MUST be inactive, and focus MUST stay in the sheet until it closes.
Split and overlay modes MUST stay non-modal. Their background MUST remain
active, and Tab and Shift+Tab MUST let focus move between the inspector and the
rest of the page.

The shared inspector header, content, and footer MUST each use `0.75rem`
padding. The header and footer MUST use a `0.5rem` gap. The content stack MUST
use a `1rem` gap. A section heading MUST have a `0.5rem` gap before its content.
The facts layout MUST use `5rem minmax(0, 1fr)` columns, a `0.5rem` column gap,
and `0.5rem 0.625rem` row padding. It MUST collapse to one column when the text
does not fit. This collapse MUST use the facts region's available inline size,
not the browser viewport width. A row list MUST use a `0.375rem` gap. Each row
MUST use `0.5rem 0.625rem` padding and a minimum block size of `2.75rem`. A
notice or corrective error MUST use `0.625rem` padding and a `0.5rem` internal
gap. Footer actions MUST wrap with a `0.5rem` gap. These compact rules MUST NOT
reduce an interactive control below `2.75rem` in either dimension.

OpenDLE UI MUST export `GraphInspector`, `GraphInspectorFacts`,
`GraphInspectorFact`, `GraphInspectorSection`, `GraphInspectorRows`,
`GraphInspectorRow`, and `GraphInspectorNotice` from the package root. It MUST
also export `GraphInspectorProps`, `GraphInspectorFactsProps`,
`GraphInspectorFactProps`, `GraphInspectorSectionProps`,
`GraphInspectorRowsProps`, `GraphInspectorRowProps`, and
`GraphInspectorNoticeProps` from that root. These components and types MUST be
host-neutral and MUST NOT contain a Router, Ontology, or Xbot data type.

`GraphWorkspaceProps` MUST provide this optional host-neutral reachability
extension:

```tsx
readonly selectedControlRef?: RefObject<HTMLElement | null>;
```

When the reference contains a connected selected graph control, the shared
workspace MUST use it for overlay reachability. A host that changes graph
selection MUST update the reference. `RelationshipGraph` MUST provide its
selected-control reference internally. A host MUST NOT provide a mode, width,
inset, or scroll-offset value.

`GraphInspector` MUST own the labelled header, optional eyebrow and icon,
heading, close action, local content region, and optional fixed action footer.
The host MUST supply its close behavior. The shared component MUST supply the
close control and its default accessible label. Its heading MUST be an `h2`.
It MUST wrap and MUST NOT use visible ellipsis as the only way to show a long
title. The component MUST keep host extension points for the eyebrow, icon,
content, fixed footer actions, close-control label, return focus, and visual
tone.
`GraphInspectorProps.onClose` MUST be required. The component MUST NOT expose a
host override that moves initial focus away from the heading.

`GraphInspectorFacts` MUST render a semantic description list. Each
`GraphInspectorFact` MUST render one associated term and description.
`GraphInspectorSection` MUST render a labelled section with an `h3` and an
optional count. `GraphInspectorRows` MUST render a semantic list. Each
`GraphInspectorRow` MUST support a label, a value, and optional sibling actions
without nesting one interactive control in another. A row that is one action
MAY use one semantic whole-row control instead. `GraphInspectorNotice` MUST
support neutral, warning, and error states without color alone. A dynamically
added corrective error MUST use an alert role. A static notice MUST NOT get a
live role only because it uses the shared primitive.

The shared primitives MUST own their semantic structure, spacing, wrapping,
focus style, borders, and state presentation. A host MUST own domain values,
labels, permissions, controls, forms, mutations, and error text. A host MAY put
domain-specific content in the primitive slots. It MUST NOT copy the shared
inspector structure or define a local inspector width, mode, padding, section,
facts, row, notice, overflow, or footer layout.

A host MUST use `GraphInspectorFacts` and `GraphInspectorFact` for an inspector
fact set, `GraphInspectorSection` for a titled inspector section,
`GraphInspectorRows` and `GraphInspectorRow` for a repeated inspector row list,
and `GraphInspectorNotice` for an inspector notice or corrective error. An
action for one row MUST stay in that row. An inspector-level action group MUST
use the fixed `GraphInspector` footer. A form action MAY stay with its form.
Other domain-specific media, tags, routes, forms, and controls MAY use the
content extension point when no shared primitive applies.

Only the inspector content region MAY scroll vertically. The header and action
footer MUST remain visible. The inspector MUST NOT scroll horizontally. Long
titles, labels, identifiers, URLs, values, notices, errors, and action text MUST
wrap or break safely without hiding information or controls. At 200% text size,
all content and controls MUST remain reachable, the reading order MUST stay the
same, and the inspector MUST NOT cause page-level overflow.

Opening an inspector MUST move focus to its heading. The heading MUST be a
programmatic focus target, and Tab MUST move to its first applicable control.
Escape and the close action MUST close the inspector in all three modes and
return focus to the control that opened it. If that control no longer exists,
focus MUST follow the unavailable-record rule. Opening or closing an inspector
MUST NOT change the page width. The selected graph control MUST stay selected
and reachable in the local graph viewport. `GraphInspector` MUST use a connected
`returnFocusRef` as the return target. When that reference is absent, it MUST
capture the connected focused control that opened the inspector. The return
target and the selected-control reachability target MAY be different controls.

Restoring an inspector that application history kept open MUST NOT count as a
new inspector open action. In split or overlay mode, the host MAY restore focus
to the graph control that opened it. In bottom-sheet mode, focus MUST move to
the inspector heading so that it stays in the modal sheet. The graph control
MUST remain the inspector return-focus target in all three modes.

If the inspector host crosses a mode boundary while the inspector is open, the
system MUST keep the same inspector DOM element, selected record, entered
non-secret values, and content scroll position. It MUST NOT close and reopen
the inspector or return focus to the graph. When focus is in the inspector, the
system MUST keep the same focused element if it still exists. If that element
no longer exists, it MUST move focus to the inspector heading.

When focus is outside the inspector, a change between split and overlay modes
MUST keep that focus. A change to bottom-sheet mode MUST make the background
inactive and start modal focus containment. If focus was outside the inspector,
the system MUST move it to the inspector heading. A change from bottom-sheet
mode MUST remove that containment, make the background active, and keep the
current inspector focus. A mode change MUST NOT cause a duplicate announcement.

A failed create, change, or delete MUST keep the applicable inspector open,
keep the entered non-secret values, and show a corrective error. It MUST NOT
show success or change the graph or board until the server confirms the write.

The Router MUST remove the local `42rem` configuration-inspector width and the
local `38rem` service-inspector width, including their phone-width rules. It
MUST use the shared `21rem` width and shared primitives for both surfaces. It
MUST NOT replace an override with an equivalent local width or inspector-layout
rule. OpenDLE UI MUST use Xbot's compact ontology inspector as the visual
hierarchy baseline for facts, sections, property rows, relationship rows, and
notices. It MUST implement that hierarchy in the shared primitives and MUST NOT
copy Xbot selectors, CSS, copy, or domain types. Xbot MUST consume the shared
primitives instead of keeping a second inspector layout. Ontology MUST use the
same shared geometry and mode rules.

Focused OpenDLE UI tests MUST import the inspector, all shared inspector
primitives, all named inspector prop types, and `GraphWorkspaceProps` from the
built package root. Container-boundary tests MUST cover one CSS pixel above
`69rem`, exactly `69rem`, one CSS pixel below `69rem`, one CSS pixel above
`48rem`, exactly `48rem`, and one CSS pixel below `48rem`. They MUST prove that
the same browser viewport can produce different modes for different host
container widths and that a container resize changes the mode.

The split tests MUST prove the `21rem` inspector width, the `48rem` remaining
graph floor, three `13rem` minimum relationship columns, reduced shared gaps
and padding, contracted `GraphWorkspace` stage and toolbar, contracted
`RelationshipGraph` toolbar or standalone search and viewport, unchanged host
width, local graph scrolling, and no page-level overflow. Overlay tests MUST
prove the exact insets and `21rem` border-box width, an active background, an
uncovered toolbar, unchanged graph width, local scroll, reachability through a
host `selectedControlRef`, and internal `RelationshipGraph` selected-control
reachability. Bottom-sheet tests MUST prove the exact viewport insets,
full width between those insets, maximum height, inactive background, focus
containment, and local content scrolling. They MUST prove that the sheet does
not keep the `21rem` split-and-overlay width.

Component tests MUST cover the exact header, content, footer, section, facts,
row, notice, and action spacing. They MUST cover empty and absent optional
regions, long data, long actions, forms, errors, notices, local overflow, 200%
text, and the `2.75rem` interactive-control minimum. They MUST confirm the
`h2`, labelled `h3` sections, associated description terms and descriptions,
semantic lists, sibling row actions, whole-row action option, and static and
dynamic notice roles without nested interactive controls. They MUST also cover
initial heading focus, Tab and Shift+Tab, Escape, close, exact focus return,
unavailable return targets, and each open-inspector mode change. Mode change
tests MUST cover focus inside and outside the inspector, a removed focused
element, retained form values, retained content scroll, modal entry, and modal
exit. Wide, narrow, and phone surfaces MUST pass Axe and have reviewed
screenshots. Each OpenDLE UI change MUST keep React Doctor at score 100 with
zero diagnostics.

Source-consumer checks MUST use the built OpenDLE UI package root. Router tests
MUST cover configuration and service inspectors after removal of the `42rem`,
`38rem`, and phone-width overrides. Router source checks MUST prove that no
local inspector rule duplicates shared width, mode, padding, facts, section,
row, notice, overflow, or footer layout. Xbot tests MUST cover the shared
compact visual hierarchy. Xbot source checks MUST prove that no local inspector
rule duplicates those shared layouts while allowing domain-specific media,
tag, route, and tone presentation. Ontology tests MUST cover the shared
primitives, geometry, and mode behavior. Ontology source checks MUST prove that
it does not keep a second shared inspector layout.
Router, Xbot, and Ontology MUST compile and render in split, overlay, and
bottom-sheet modes. Each changed inspector surface MUST pass Axe and have a
reviewed screenshot in each mode. Each changed React consumer MUST keep React
Doctor at score 100 with zero diagnostics.

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

### Shared DataTable alignment and status-pill sizing

OpenDLE UI MUST own the existing `DataTableColumn.align` API as the host-neutral
start, center, and end alignment control. One column value MUST apply to its
column-header content, each desktop data-cell content region, and each phone
card description-value region. For a sortable column, it MUST apply to the
visible header label and sort indicator as one content group.

The desktop alignment region MUST be the header-cell or data-cell content box
after removal of its inline padding. The phone alignment region MUST be the
description-value region's value grid area after removal of the card-row inline
padding, label grid track, and shared column gap. A center-aligned phone value
MUST be centered only in that value grid area. It MUST NOT be centered across
the complete label-and-value row.

The default `DataTable` column-header cell, desktop data cell, and phone card
value row MUST keep `0.75rem` inline padding. Compact density MUST keep
`0.5rem` inline padding. Centering MUST NOT consume this protected outer
padding. When rendered content fits after its allowed wrapping or breaking, its
inline bounds MUST have at least `0.75rem` clearance from each applicable outer
inline edge at default density and at least `0.5rem` at compact density. In the
alignment region, the free inline space before and after centered rendered
content MUST differ by no more than one CSS pixel. A host MUST NOT create this
alignment with an asymmetric margin, a translated position, or a local
alignment class.

OpenDLE UI MUST own `StatusPill` internal spacing and safe long-label behavior.
The pill MUST use border-box sizing, a maximum inline size of `100%`, and
exactly `0.5rem` internal padding at each inline side. It MUST NOT own an outer
margin or host alignment. Its status dot MUST NOT shrink, clip, or move outside
the pill. A long localized label with spaces and a long unbroken label MUST wrap
or break inside the pill without reducing the inline padding, hiding the dot,
clipping text, or increasing the page width. The text MUST stay accessible and
the dot MUST stay decorative. This behavior MUST remain safe at 200% text size.

A host MUST choose the column alignment and supply the pill text and tone. It
MUST use `DataTableColumn.align` when that API supplies the required alignment.
It MUST use the shared `StatusPill` for this status-pill pattern. A host MUST NOT
override the shared pill sizing, padding, wrapping, dot layout, margin, or
alignment. `DataTable` MUST NOT add a status-specific alignment API, and a host
MUST NOT add a status-specific shared API without a proved host-neutral gap.

Focused OpenDLE UI tests MUST import `DataTable`, `DataTableColumn`,
`StatusPill`, and `StatusPillProps` from the built package root. They MUST cover
start, center, and end alignment for a plain header, a sortable header, a
desktop cell, and a phone value region. Center tests MUST prove the exact
default and compact inline padding, their matching minimum outer clearance,
and the one-CSS-pixel maximum difference between the two free spaces. Phone
tests MUST prove that the value is centered in its value grid area and not
across the label-and-value row.

Tests MUST cover a short `StatusPill`, a long synthetic label with spaces, and
a long synthetic unbroken label at normal and 200% text size. They MUST prove
equal pill inline padding, safe wrapping or breaking, a visible non-shrinking
decorative status dot, accessible text, no clipping, and no page-level
overflow. They MUST preserve semantic header and cell associations and semantic
phone term and description associations. Desktop and phone fixtures MUST pass
Axe and have reviewed screenshots. Each OpenDLE UI change MUST keep React
Doctor at score 100 with zero diagnostics.

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
