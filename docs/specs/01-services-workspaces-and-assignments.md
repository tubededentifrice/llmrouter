# Services, workspaces, and assignments

Status: Accepted on 2026-08-23. The graph-first UI amendment was accepted on
2026-08-24. The service-details and compact-inspector amendment was accepted on
2026-08-29. The empty-chain and assignment-deletion amendment was accepted on
2026-08-29.

## Names and identity

Each service and workspace MUST have an opaque internal identity and one
stable, readable `apiName`. An `apiName` MUST contain 1 through 63 lowercase
ASCII letters, digits, or hyphens. It MUST start with a letter and MUST end
with a letter or digit. A service `apiName` MUST be globally unique. A
workspace `apiName` MUST be unique in its owning service. The Router MUST NOT
reuse an `apiName` in the same collection while its resource exists.

A service or workspace MUST either exist or be absent. It MUST NOT have a
disabled, retired, restored, deleting, cleanup, revision, or version state.

## Service tree

A service MAY have one parent service. It MUST NOT have more than one parent.
A parent change MUST reject a cycle and MUST leave the existing tree unchanged
after any validation or storage failure.

Only a global administrator MAY create a service, change its parent, or delete
it. A service API key MUST NOT perform these operations. A service delete MUST
fail while one or more child services name it as their parent. The
administrator MUST first move or delete each child.

Deleting a service MUST delete its API keys, workspaces, local assignment
definitions, request logs, raw accounting, daily aggregates, media jobs, and
retained media. It MUST NOT delete a parent service or a child service. The
delete MUST make the service unavailable to new calls before dependent
records are removed.

The global administration application MUST use the service tree as the main
service-management entry. One primary-button click or one tap on a service node
MUST select that service and open its compact inspector without navigation. A
primary-button double-click on a service node MUST select that service and open
its service-details route. The first click of that double-click MAY open the
compact inspector before navigation.

For a service node, Space MUST perform the one-click action and Enter MUST open
the service-details route. This requirement replaces the general rule that
Enter and Space have the same graph activation result. The arrow, Home, End,
one-tab-stop, selection, and focus rules in
[Authentication, administration, and shared UI](04-authentication-administration-and-shared-ui.md#global-administration-application)
remain unchanged. On a touch device, one tap on the node MUST open the compact
inspector. The administrator MUST be able to tap the inspector's `Open service
details` action to navigate. The application MUST NOT require a double-tap,
long press, or pointer-only gesture.

### Compact selected-service inspector

The compact selected-service inspector MUST use `GraphInspector` and the
shared inspector primitives. Its header MUST use the service display name as
the title. Its eyebrow MUST be `Root service` when the service has no parent and
`Child service` otherwise. It MUST show these facts in this order:

1. `API name`: the service `apiName`.
2. `Parent`: `None` for a root service, or the parent display name and
   `apiName` for a child service.
3. `Created`: the service creation date and time.

The inspector header MUST contain the shared close action. Its fixed footer
MUST contain exactly one action: a primary action labelled `Open service
details`. The primary action MUST open `/services/{serviceApiName}`. The
inspector MUST NOT contain a display-name form, parent change, delete action,
workspace data, workspace action, API-key metadata, API-key action, or one-time
secret. It MUST NOT load workspaces or API keys.

The selected-service and create-service inspectors MUST use the complete shared
compact graph-inspector system in
[Authentication, administration, and shared UI](04-authentication-administration-and-shared-ui.md#shared-compact-graph-inspector).
They MUST use all shared widths, insets, mode boundaries, and scrolling rules.
The Router MUST NOT add a local width, padding, overflow, inset, breakpoint, or
mode rule. On a phone, the modal sheet and its modal backdrop MUST be in a
stacking layer above the application bottom navigation. The navigation MUST be
inactive and MUST NOT cover any part of the sheet. This stacking rule MUST NOT
change the shared sheet insets, width, maximum height, or mode boundary.

Closing the compact selected-service inspector with Escape or its close action
MUST keep the service selected and return focus to its graph node. Reopening the
selected service MUST move focus to the inspector heading as required by the
shared system. The create-service action MUST continue to open a create
inspector in the graph workspace. Opening it MUST focus its heading, and the
next Tab MUST move to the first applicable field. Closing it MUST return focus
to the create-service action. Service creation MUST NOT navigate to the details
page.

### Service-details route

The direct service-details route MUST be
`/services/{serviceApiName}`, with the path segment encoded as one service
`apiName`. For an available service, the route identity MUST select the same
service as the global administration context. It MUST render a service-details
page and MUST NOT fall back to Overview, the service tree, or another service.
This child route is the accepted service-management surface outside the tree
and its inspectors. It MUST stay in the retained Services navigation
destination and MUST NOT add a sidebar item. A workspace or API key MUST NOT
have a separate administration route. Workspace and key management MUST use
the service-details child route and MUST NOT use the compact selected-service
inspector. The service-details child route MUST be part of the `/services`
destination and MUST have its own content-inventory and accessibility-review
entry.

The service-details page MUST contain these regions in this order:

1. A page heading with a `Back to services` action, the service display name,
   and its `apiName`.
2. A service fact summary with the `apiName`, parent, creation date and time,
   and direct-child count.
3. A service form that can change the display name and parent. The parent list
   MUST exclude the service and all its descendants.
4. A `Workspaces` section with the current workspaces and create and delete
   actions.
5. A `Service API keys` section with active key metadata and create and revoke
   actions.
6. A `Delete service` section with a delete action, the child-service blocker,
   and the complete destructive effect.

The details page MUST use the route service as context. A form or action MUST
NOT ask the administrator to select that service again. The service tree MUST
remain the only graph of services. Apart from the parent selector, the details
page MUST NOT render a duplicate tree, table, or general index of services.

The workspace table MUST show the workspace display name, `apiName`, and
creation date and time. Workspace creation MUST ask for only the display name
and `apiName`. The key table MUST show the key name, creation date and time, and
last-used date and time. Key creation MUST ask for only the key name. The
service `apiName` and workspace `apiName` MUST stay read-only after creation.

The service form MUST have one primary `Save changes` action for its display
name and parent values. It MUST show pending state and MUST prevent a duplicate
submit. A failed save MUST keep the entered non-secret values and the last
confirmed service facts, show a corrective error, and MUST NOT change the tree.
The tree MUST change only after the server confirms the save.

The page MUST NOT enable `Save changes` until it has confirmed the parent
options for the route service. If those options fail to load or refresh, the
service form MUST show a corrective error and a retry action. It MUST keep the
confirmed service facts and MUST NOT remove or disable confirmed workspace or
key data.

The `Delete service` action MUST be unavailable while the service has a direct
child and the section MUST identify the blocker. Otherwise, the action MUST
open a confirmation that identifies the service and all effects of deletion.
Cancellation MUST keep the page and return focus to the delete action. A failed
delete MUST keep the page and confirmed service data and show a corrective
error. Only a confirmed successful delete MAY change the route and tree.

Opening the details page from a node double-click, Enter, or the inspector
action MUST push one browser-history entry. It MUST NOT replace the service-tree
entry. The tree entry MUST identify the selected service. Browser Back from a
details page that was opened from the tree MUST return to the same tree
location and keep the compact inspector open. It MUST restore the prior local
graph scroll when that position keeps the selected node reachable. Otherwise,
the shared reachability behavior MUST scroll the node into view. In split or
overlay mode, focus MUST return to the same service node. In bottom-sheet mode,
focus MUST move to the inspector heading, and closing the sheet MUST return it
to that node. Browser Forward MUST return to the details route and focus the
page heading without making a second history entry.

The `Back to services` action MUST use browser Back when the current history
entry records a same-origin service-tree source. For a direct link or another
source, it MUST replace the current entry with
`/services?service={serviceApiName}`. That fallback MUST open the compact
inspector, move focus to its heading, and use its service node as the
return-focus target. A normal browser Back from a direct link MUST continue to
use the browser's prior history. History state MUST NOT contain a service API
key secret, form value, workspace record, or other sensitive data.

Opening the details route MUST move focus to its page heading. A return that
opens or restores the compact inspector MUST use the applicable focus rule
above. A return without an inspector MUST focus the applicable service node. If
that node no longer exists, focus MUST move to the first available service node
or the create-service action. An in-progress write MUST block application
navigation and browser history restoration until it finishes. Unsaved
non-secret form values MUST require discard confirmation before application
navigation or browser Back or Forward can leave the page. A browser reload or
close attempt MUST use the standard leave-page confirmation while a write is
pending or unsaved values exist. A blocked navigation attempt MUST NOT resume
automatically after a write finishes. The administrator MUST request that
navigation again.

The route MUST load the exact service before it enables management actions. An
initial load MUST show a labelled loading state. An initial load failure MUST
show a corrective error with `Try again` and `Back to services` actions at the
same route. It MUST NOT show another service or stale data from another route.
A response for a service route that is no longer active MUST NOT change the
current page.

If a refresh fails after confirmed service data is visible, the page MUST keep
that data, identify it as stale, and show a retry action. It MUST disable parent
change, deletion, workspace writes, and key writes until a service refresh
succeeds. A workspace or key refresh failure MUST keep the confirmed records in
that section, mark only that section stale, and disable only that section's
writes. A failure in one section MUST NOT remove or disable confirmed data in
the other section.

If the service is absent on the initial load or becomes absent after a refresh,
the route MUST show an unavailable-service state with `Back to services`. It
MUST clear that service from the global selected-service context. It MUST NOT
silently open another service. In this state, `Back to services` MUST return to
`/services` without a selected-service query. If the administrator deletes the
service from its details page, a successful delete MUST replace the details
history entry with `/services`, select no service, and focus the first remaining
service node or the create-service action. Browser Back MUST NOT reopen the
deleted details entry.

If a service becomes unavailable while one of its API keys is being created or
its one-time secret is visible, the page MUST keep the protected key state. It
MUST show the unavailable-service state and let the request finish. If the
request returns a secret, or if a secret is already visible, that state MUST
keep the one-time-secret presentation and its copy and clear actions. The `Back
to services` action MUST remain unavailable until the request finishes and the
administrator clears any secret. The application MUST NOT discard, reload, or
expose the secret in another record.

## Workspaces

A workspace MUST belong to exactly one service. A service API key MUST be able
to create, list, read, and delete workspaces for its service. A global
administrator MUST have the same operations for any service.

A workspace MUST be an accounting label only. It MUST NOT own assignments,
provider connections, credentials, prices, policy, or limits.

The service-details page MUST present workspaces and keys as two labelled
sections with their current records and actions. It MUST use the route service
as context, and MUST NOT ask the administrator to select the same service
again. A create operation MUST ask only for fields that the route service and
the operation do not already supply. A new service key MUST keep the one-time
display and write-only rules in
[Authentication, administration, and shared UI](04-authentication-administration-and-shared-ui.md#service-api-keys).

In the global administration application, the one-time secret MUST appear only
in the service-details page's `Service API keys` section after the applicable
administrator create response. It MUST provide `Copy secret` and `Clear secret`
actions and state that the secret cannot be shown again. `Clear secret` MUST
remove the value from the rendered page and application state. The application
MUST NOT put it in the URL, browser history state, browser storage, a stale-data
snapshot, a log, or a later API response. While key creation is pending,
application navigation and browser history restoration MUST remain on the
details page. While the secret is visible, application navigation and browser
history restoration MUST remain on the page until the administrator clears the
secret. A browser reload or close attempt MUST use the standard leave-page
confirmation while the secret is visible. If the administrator confirms a
reload or close, the Router MUST NOT try to recover or show that secret again.

A failed key create MUST keep the entered key name, show a corrective error,
and MUST NOT show a secret or add key metadata. Revoking a key MUST require a
confirmation that identifies the key and states that each later request will
fail. Cancellation MUST return focus to the revoke action. A failed revoke MUST
keep the key metadata and show a corrective error. The page MUST remove the key
metadata only after the server confirms the revocation.

The workspace and key sections MUST use the shared table behavior in
[Authentication, administration, and shared UI](04-authentication-administration-and-shared-ui.md#global-administration-application).
Each section MUST have its own loading, empty, error, and bounded loading
state. A failure in one section MUST NOT remove records that are already
visible in the other section. Long names and key metadata MUST wrap or scroll
inside the shared table viewport and MUST NOT increase the page width.

Each workspace and key read or write MUST use the route service identity. A
late result for another service route MUST be ignored. A workspace result MUST
contain only workspaces that belong to that service. A key result MUST contain
only key metadata for that service and MUST NOT contain a verifier or secret.
These details-page rules MUST NOT give a service API key authority over another
service or global administration.

Authenticated browser tests MUST use `http://127.0.0.1:5174` and the local
test-session workflow. They MUST NOT print the session file values or a created
key secret. Tests MUST cover a split desktop workspace, a narrow overlay
workspace, and a phone bottom sheet. They MUST verify the exact shared width
and mode boundaries, local graph scrolling, phone-navigation stacking and
inactive state, and the absence of Router-only inspector geometry.

Browser tests MUST cover pointer click and double-click, Space, Enter, touch
tap, the explicit `Open service details` action, close, Escape, initial focus,
exact focus return, and one service-tree tab stop. They MUST cover the direct
route, route encoding, page regions, history push, Back, Forward, the direct-
link fallback, graph-scroll restoration and reachability, mode-specific history
focus, and deleted-node focus fallback.

Browser tests MUST also cover initial loading, initial failure and retry,
refresh failure with stale service data, independent workspace and key refresh
failures, an out-of-order result after a route change, an initially absent
service, concurrent service removal, successful deletion, a child-service
delete blocker, service-save success and failure, delete confirmation,
delete failure, parent-option failure and retry, and unsaved or pending
navigation guards for application navigation, Back, Forward, reload, and
close. Workspace and key tests MUST cover service isolation, create and delete,
key-create success and failure, revoke success, confirmation, and failure,
one-time secret display, copy, and clear, reload and navigation protection, and
the absence of a secret in later reads, history, URLs, browser storage,
stale-data snapshots, and logs. Long-content tests MUST prove that graph and
table viewports keep their local scrolling and do not cause page-level
overflow.

The compact inspector, details page, workspace table, key table, loading,
empty, stale, error, unavailable, one-time-secret, and confirmation states MUST
pass Axe in desktop, narrow, and phone tests. Each width MUST have a reviewed
screenshot of the normal service flow. Focused screenshots MUST also cover the
phone sheet above the bottom navigation and each conditional state that is not
visible in a normal screenshot. A one-time-secret screenshot MUST use a fixed
synthetic value that cannot authenticate. A screenshot, snapshot, or test
report MUST NOT contain a secret from a create response. Each React change MUST
keep React Doctor at score 100 with zero diagnostics.

Deleting a workspace MUST delete its detailed logs, raw accounting, daily
aggregates, media jobs, uploaded images, and retained generated media. It MUST
NOT change the owning service or its assignment definitions. The delete MUST
make the workspace unavailable to new calls before dependent records are
removed.

Service and workspace create, update, and delete operations MUST change the
current state directly. Their requests and responses MUST NOT contain a state
revision, resource version, or expected revision.

## Assignment names

An assignment name MUST contain 1 through 127 lowercase ASCII letters, digits,
dots, underscores, or hyphens. It MUST start with a letter or digit. An
assignment MUST represent one named service use case.

Each assignment definition MUST contain either:

- one direct ordered provider-model candidate chain; or
- the name of one other assignment to inherit.

It MUST NOT contain both. A direct chain MUST contain 0 through 16 unique
provider-model candidates. An assignment MUST NOT store a temperature or
output-limit default.

## Service inheritance

For one assignment name, the Router MUST search from the called service toward
the root. The nearest service definition MUST replace every definition with
the same name farther from the called service. Chains MUST NOT merge.

If the selected definition inherits another assignment name, the Router MUST
resolve that name from the called service through the same service parent
chain. A direct chain MUST replace the complete inherited assignment chain.
Configuration validation MUST reject a missing inherited name and any cycle
across assignment names and service inheritance. The direct assignment
deletion rule below is the only exception. It clears each direct child
reference instead of leaving a missing inherited name.

A workspace MUST NOT take part in assignment resolution.

## Default and automatic assignments

Each root service MUST have an implicit assignment named `default`. It MUST
exist even when it has no configured chain. Any service in the parent chain
MAY define its own `default` chain. The nearest definition MUST replace the
complete parent definition. A child with no local definition MUST inherit the
effective parent `default`.

When a service calls a valid assignment name that has no effective record,
the Router MUST create a local assignment for that service. The new assignment
MUST inherit `default`. The first call MUST then use the inherited effective
`default` chain without a separate registration operation.

Concurrent first calls for the same service and assignment name MUST create at
most one local assignment record. Each call MAY use that one record after its
creation transaction commits.

The call MUST fail before provider work when its effective chain is empty or
when no candidate supports the call shape. This rule applies to `default` and
each other assignment.

## Use evidence

Each assignment MUST store its last-used time. The Router MUST update this
time when a call passes service, workspace, assignment-name, and input
validation, even when no eligible candidate completes the call.

Each assignment MUST store the union of call capabilities and modalities that
validated calls requested. A global or service administrator MUST be able to
remove an observed item. Runtime candidate filtering MUST use the current
call's actual requirements. It MUST NOT use the stored union as the call
filter.

The administration interface MUST make an assignment with no direct chain
clear. It MUST show whether that assignment inherits `default` or another
assignment and MUST show its last-used time and observed requirements.

An authenticated service MAY create, change, or delete a local assignment
definition for itself. A global administrator MAY perform the same operation
for any service. The administrator MUST use the reviewed direct assignment
deletion in
[Providers, models, prices, and configuration](02-providers-models-prices-and-configuration.md#reviewed-configuration-deletion).
Provider, canonical-model, and provider-route deletion MUST NOT delete an
assignment.

Direct deletion of assignment A MUST delete A and A's own provider-route
candidate links. It MUST apply `SET NULL` to the assignment-parent reference
of each direct child assignment that points to A. It MUST keep each direct
child record and convert that child to an empty direct-chain assignment. It
MUST NOT change a grandchild reference that points to one of those direct
children. Deleting a local definition MUST also expose the next definition
with the same name in the service parent chain. Deleting a local root
`default` definition MUST expose the empty implicit root `default`.

An administrator or authenticated service MAY create or save an empty direct
assignment chain. A provider, canonical-model, or provider-route deletion MUST
remove only affected candidate links. Remaining links MUST close the gap and
keep their relative order. If the last link is removed, the Router MUST keep
the assignment as an empty direct-chain assignment. A call through that empty
effective chain MUST fail before provider work.

Direct assignment deletion and each child conversion MUST be atomic. A
validation or storage failure MUST keep the assignment, its candidate links,
and each direct child reference unchanged. Focused tests MUST cover service and
administrator deletion, direct-child `SET NULL`, empty-child conversion,
unchanged grandchildren, exposed service inheritance, the implicit root
`default`, empty-chain create and save, and failure before provider work.
