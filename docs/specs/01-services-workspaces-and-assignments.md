# Services, workspaces, and assignments

Status: Accepted on 2026-08-23. The graph-first UI amendment was accepted on
2026-08-24. The service-details and compact-inspector amendment was accepted on
2026-08-29. The empty-chain and assignment-deletion amendment was accepted on
2026-08-29. The permanent-root-service and contextual child-creation
amendments were accepted on 2026-08-30.

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

The Router MUST store exactly one permanent root service. Its `apiName` MUST be
`root`, its initial display name MUST be `Root`, and its parent MUST be null.
The root service MUST be a normal stored service for assignment ownership and
inheritance. It MUST NOT be a virtual client-only node. Its opaque internal
identity and creation time MUST stay unchanged after bootstrap.

Every other service MUST have exactly one parent service. A non-root service
MUST NOT have a null parent or more than one parent. Each service parent chain
MUST end at `root`. A create or parent change MUST reject the target service as
its own parent and MUST reject any descendant as its parent. The complete
validation and write MUST use one transaction. A cycle, missing parent,
concurrent parent change, validation failure, or storage failure MUST leave the
existing tree unchanged.

Before it serves an administration or calling request, the Router MUST run one
idempotent root bootstrap and migration transaction while service-tree writes
are locked. The transaction MUST apply these rules:

1. An empty service collection MUST receive the stored `root` service.
2. If no service has `apiName` `root`, the Router MUST insert it and set the
   parent of each existing parentless service to `root`.
3. If a service already has `apiName` `root`, that record MUST become the
   permanent root. The Router MUST clear its existing parent, if any, and set
   the parent of each other parentless service to `root`. Detaching a nested
   `root` leaves its former top-level ancestor parentless. The Router MUST then
   attach that ancestor below `root`.
4. The Router MUST validate the complete resulting service tree and assignment
   inheritance before commit. If the non-empty pre-migration graph has no
   parentless service and has no service named `root`, or if the result contains
   another cycle, a missing parent, a missing inherited assignment, or an
   assignment cycle, bootstrap MUST fail without changing any service. The
   Router MUST stay unavailable for administration and calling requests until
   an operator repairs the stored configuration and bootstrap succeeds.

Thus, zero existing services creates `root`; one existing parentless service is
attached to a new `root` unless it is already `root`; and multiple existing
parentless services are all attached to one new or existing `root`. A newly
inserted root MUST receive one generated opaque internal identity and one
creation time in the bootstrap transaction. Migration MUST keep the opaque
identity, display name, creation time, and descendants of an existing service
named `root`.

The bootstrap MUST make the implicit empty `default` assignment available for
the permanent `root` service in the same transaction. It MUST keep all existing
services, assignments, workspaces, keys, accounting, logs, jobs, media, and
activity. It MUST NOT create an administrator activity event because bootstrap
is a system migration and not an administrator configuration change. A
repeated bootstrap MUST make no data change.

Only a global administrator MAY create a service, change its parent, or delete
it. A service API key MUST NOT perform these operations. An administrator MUST
NOT create another service with `apiName` `root`. A create request MUST name an
existing parent. The root display name MAY change. Its `apiName`, root status,
and null parent MUST NOT change. A request to give the root a parent or to give
a non-root service a null parent MUST fail and MUST NOT change the service.

A service delete MUST fail while one or more child services name it as their
parent. The administrator MUST first move or delete each child. Deletion of
`root` MUST always fail, including when it has no child. The Router MUST NOT
offer a root delete action or describe root deletion as available.

Deleting a service MUST delete its API keys, workspaces, local assignment
definitions, request logs, raw accounting, daily aggregates, media jobs, and
retained media. It MUST NOT delete a parent service or a child service. The
delete MUST make the service unavailable to new calls before dependent
records are removed.

Each authenticated administrator service create, display-name change, parent
change, and delete operation that reaches service validation MUST create the
basic activity event defined in
[Accounting, logs, retention, and operations](05-accounting-logs-retention-and-operations.md#basic-activity-log).
The action MUST be `service.create`, `service.update`, or `service.delete`, the
resource type MUST be `service`, and the resource API name MUST identify the
target service. A rejected protected-root, parent, or cycle operation MUST
record `failed`. A successful operation MUST record `succeeded`. An activity
event MUST NOT contain an old or new display name, parent value, or other
service-tree snapshot.

### Administration service wire rules

An administrator service response MUST contain `api_name`, `display_name`,
`parent_service_api_name`, `is_root`, and `created_at`. The
`parent_service_api_name` value MUST be null and `is_root` MUST be true only for
`root`. Every other response MUST contain a non-null parent API name and
`is_root` MUST be false.

An administrator create request MUST contain `api_name`, `display_name`, and a
non-null `parent_service_api_name`. An administrator replace request MUST
contain `display_name` and `parent_service_api_name`. The replace value MUST be
null for `root` and MUST be non-null for every other service. These requests
MUST NOT contain `is_root` or another root flag that the caller can set.

The service operations MUST use these exact relationship errors. Contract
shape validation MAY use the normal `invalid_request` message before the
operation runs.

| Trigger | HTTP status | Error code | Message |
| ------- | ----------- | ---------- | ------- |
| A create request uses `api_name` `root` | 409 | `conflict` | `The root service already exists.` |
| A create request uses another existing service API name | 409 | `conflict` | `Service API name already exists.` |
| A named parent does not exist | 404 | `not_found` | `Parent service was not found.` |
| A replace request gives `root` a non-null parent | 400 | `invalid_request` | `The root service must not have a parent.` |
| A replace request gives a non-root service a null parent | 400 | `invalid_request` | `A non-root service must have a parent.` |
| A parent is the target service or one of its descendants | 409 | `conflict` | `The service parent would create a cycle.` |
| Concurrent tree state no longer matches the validated parent graph | 409 | `conflict` | `The service tree changed. Refresh and try again.` |
| A delete target is `root` | 409 | `conflict` | `The root service cannot be deleted.` |
| A delete target has one or more direct children | 409 | `conflict` | `Move or delete the child services first.` |

After contract-shape validation, a create operation MUST check the reserved
root name before another API-name conflict, the target as its own parent,
parent existence, and concurrent tree state, in that order. A replace operation
MUST check target existence, the root or non-root null-parent rule, parent
existence, a cycle, and concurrent tree state, in that order. A delete operation
MUST check target existence, root protection, and direct children, in that
order. This order MUST select one exact error when a request would otherwise
match more than one error.

A rejected request MUST use the same activity result rules as any other
administrator configuration attempt. It MUST NOT make a partial service-tree
or dependent-record change.

Focused contract and service tests MUST cover an empty bootstrap, one legacy
parentless service, multiple legacy parentless services, an existing top-level
`root`, an existing nested `root`, repeated bootstrap, and atomic bootstrap
failure for a remaining service cycle, an assignment cycle, a missing parent,
or a missing inherited assignment. They MUST cover required create and replace
parent fields, response root fields, a missing parent, the target as its own
parent, a descendant parent, a concurrent parent change, a duplicate API name,
a null non-root parent, a root parent, root creation, root deletion,
child-blocked deletion, validation precedence, and successful display-name and
parent changes. Each operation failure test
MUST verify the exact HTTP status, error code, message, unchanged tree, and
failed activity event. Each operation success test MUST verify the stored tree
and succeeded activity event. Bootstrap tests MUST verify that no administrator
activity event is created.

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

### Contextual child-service creation

The service graph MUST NOT put a create-service action in its toolbar or in
another graph-wide action area. When one service is selected, the graph MUST
show one visible button labelled `+ New service` directly below that selected
service node. It MUST NOT show the button below another node. The button's
accessible name MUST identify the selected service as the parent. Selecting a
different service MUST move the button below the newly selected node without
moving focus away from that node.

A primary-button click, pointer tap, or touch tap on `+ New service` MUST open
the create-service inspector. Enter or Space on the focused button MUST open
the same inspector. The button MUST participate in the service graph's roving
focus group and MUST NOT add a second graph Tab stop. Down from the selected
service node MUST move focus to its `+ New service` button. Up or Left from the
button MUST return focus to that selected node. Down from the button MUST move
focus to the next visible service node when one exists and otherwise MUST keep
focus on the button. Right from the button MUST keep focus on it. Home and End
from the button MUST move focus to the first and last visible service node.
These rules replace the general service-tree arrow rule only for movement to
or from this contextual button.

Tab MUST still enter the graph at the selected service node, or at the first
service node when there is no selection. The next Tab from either a service
node or the contextual button MUST leave the graph. When inspector closure
returns focus to `+ New service`, that button MAY temporarily be the graph's
one active roving Tab stop. When focus next leaves the graph, the selected
service node MUST again become the graph's active Tab stop. Pointer, touch, and
keyboard use MUST produce the same parent and create form.

Opening the create-service inspector MUST capture the selected service as the
required parent for that create attempt. The inspector MUST use
`GraphInspector`. Its heading MUST be `New service`, and it MUST show the
captured parent's display name and `apiName` as read-only context. Its form
MUST contain only `Display name` and `API name` fields and one primary `Create
service` action. It MUST NOT contain a parent picker, a null-parent option, or
another way to change the captured parent. The create request MUST send the
captured service `apiName` as `parent_service_api_name`.

Opening the inspector MUST move focus to its heading, and the next Tab MUST
move to `Display name`. Escape, the inspector close action, or an explicit
cancel action MUST cancel creation, discard the unsaved non-secret values,
keep the parent service selected, and return focus to its `+ New service`
button. Cancellation MUST NOT send a create request. While the create request
is pending, the inspector MUST show `Creating service`, prevent a duplicate
submit, disable its fields and actions, and block inspector closure and graph
selection changes until the request finishes.

In split or overlay mode, if the administrator tries to select another service
while a create inspector is open and no request is pending, the application
MUST treat that action as a request to cancel creation. When both fields are
empty, it MUST close the create inspector, select the requested service, open
that service's compact inspector, and focus its heading. When either field has
a value, it MUST first ask the administrator to discard the values. Confirming
MUST complete the same selection change. Declining MUST keep the original
parent selected, keep the entered values, and keep the create inspector open.
In bottom-sheet mode, the shared modal background MUST prevent a graph
selection change until the inspector closes.

A create failure MUST keep the captured parent and entered values, show a
corrective error through `GraphInspectorNotice`, and permit retry.
Field-validation failure MUST focus the first invalid field. Another failure
MUST keep focus on the create action after it announces the error. If the
captured parent becomes unavailable, the application MUST close the create
inspector, select the first available service, return focus to that node, and
announce that the parent is unavailable. It MUST NOT retry with a different
parent. A successful create MUST add the confirmed child below the captured
parent, select the new child, replace the create inspector with its compact
selected-service inspector, and move focus to that inspector's heading. It
MUST NOT navigate to the service-details route.

The graph MUST show a labelled loading state and MUST NOT show a create action
until it has loaded at least one confirmed service. After a successful root
bootstrap, an initial graph load with no restorable selection MUST select
`root` and show `+ New service` below it. A bootstrap failure, service-load
failure, or successful response with no stored root MUST show a corrective
error and retry action. It MUST NOT show a graph-wide create action or permit a
service with no parent. A refresh that removes the selected service MUST use
the shared unavailable-record focus rule, select the first remaining service,
and move `+ New service` below that service. Because successful bootstrap
always stores `root`, the normal service graph MUST NOT have an empty state
after loading.

### Compact selected-service inspector

The compact selected-service inspector MUST use `GraphInspector` and the
shared inspector primitives. Its header MUST use the service display name as
the title. Its eyebrow MUST be `Root service` for `root` and `Child service`
otherwise. It MUST show these facts in this order:

1. `API name`: the service `apiName`.
2. `Parent`: `None` for `root`, or the parent display name and
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
shared system. The contextual child-service action and create inspector MUST
follow the creation, focus, cancellation, and success rules above.

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
3. A service form that can change the display name and, for a non-root service,
   its parent. The parent list MUST exclude the service and all its descendants.
   For `root`, the form MUST show the fixed parent value `None` and MUST NOT
   provide a parent control.
4. A `Workspaces` section with the current workspaces and create and delete
   actions.
5. A `Service API keys` section with active key metadata and create and revoke
   actions.
6. A `Delete service` section with protected-root status or a delete action,
   the child-service blocker, and the complete destructive effect.

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

For a non-root service, the page MUST NOT enable `Save changes` until it has
confirmed the parent options for the route service. If those options fail to
load or refresh, the service form MUST show a corrective error and a retry
action. It MUST keep the confirmed service facts and MUST NOT remove or disable
confirmed workspace or key data. The root form MUST NOT load parent options.

For `root`, the `Delete service` section MUST identify the permanent-root
protection and MUST NOT contain a delete action. For another service, the
delete action MUST be unavailable while the service has a direct child, and
the section MUST identify the blocker. Otherwise, the action MUST open a
confirmation that identifies the service and all effects of deletion.
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
that node no longer exists, focus MUST move to the first available service
node. An in-progress write MUST block application
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
service node. Browser Back MUST NOT reopen the deleted details entry.

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
exact focus return, and one service-tree tab stop. At desktop and phone widths,
focused authenticated tests MUST also cover the absence of a toolbar create
action; the exact selected-node placement, label, and accessible parent name of
`+ New service`; pointer, touch, Enter, Space, and arrow access; one graph Tab
stop; create-inspector focus entry and return; and cancel with empty and entered
values. Split and overlay tests MUST cover selection changes with empty and
entered values. Phone bottom-sheet tests MUST prove that the modal background
prevents selection changes. The tests MUST verify that the inspector has only
the display-name and API-name fields, shows the captured parent without a
parent picker, and sends that parent in the create request. They MUST cover
pending state, duplicate-submit prevention, field and non-field failures,
retry, parent
unavailability, confirmed success, root selection after bootstrap, bootstrap
and load failure, an invalid empty response, and refresh selection fallback.
They MUST cover the direct route, route encoding, page regions, history push,
Back, Forward, the direct-link fallback, graph-scroll restoration and
reachability, mode-specific history focus, and deleted-node focus fallback.

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

The root service MUST have an implicit assignment named `default`. It MUST
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
