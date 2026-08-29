# Authentication, administration, and shared UI

Status: Accepted on 2026-08-23. The graph-first UI and administrator
playground amendments were accepted on 2026-08-24. The fixed compound-board,
administration-content, optional relationship-graph toolbar, and compact shared
graph-inspector amendments were accepted on 2026-08-29.

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
| `/overview`                  | overview                       | totals, health, and cooldown summaries                                   |
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

The service tree MUST use nodes and inspectors as its graph interaction
surface. Service management outside the graph MUST follow
[Services, workspaces, and assignments](01-services-workspaces-and-assignments.md#service-details-route).
The three-column configuration board MUST use compound cards, nested rows, and
inspectors as its complete interaction surface. A graph or board
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
repeats the page title. The page heading and the graph accessible name MUST
continue to give the graph context.

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
