# Authentication, administration, and shared UI

Status: Accepted on 2026-08-23. The graph-first UI amendment was accepted on
2026-08-24.

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
  service assignments in one configuration graph;
- preview and import model catalog entries from the model-create workflow;
- start price synchronization and inspect its results;
- open a contextual model, embedding, and media playground from an assignment
  or exact provider-model node;
- inspect accounting, detailed request logs, media, activity, cooldowns, and
  the small health summary.

The sidebar MUST NOT contain separate workspace-and-key, provider, model,
assignment, or playground destinations. The service tree MUST own service,
workspace, and key administration. The three-column configuration graph MUST
own provider, model, provider-model, assignment, and contextual playground
administration. Direct application paths for removed destinations MAY redirect
to the applicable retained graph, but MUST NOT keep a second configuration
page.

The application shell and each retained page MUST use the complete available
width after the sidebar. They MUST use one responsive gutter system for page
headings, filters, panels, graphs, and tables. A page MUST NOT use one maximum
width for its controls and another width for its result. On a phone, the shell
MUST use the complete viewport width with the phone gutter and MUST prevent
page-level horizontal overflow. A graph or dense data region MAY scroll in its
own labelled viewport when its content cannot reflow.

The service tree and the three-column configuration graph MUST use graph nodes
and inspectors as their complete interaction surface. A graph toolbar MUST NOT
show a visible graph title such as `Service tree`. The page heading and the
graph's accessible name MUST provide the necessary context. A tree that is
smaller than its viewport MUST be centered in the available graph stage. A
larger tree MUST keep its layout origin and MUST be reachable with bounded
graph-viewport scrolling.

Each actionable graph node MUST be a semantic control in the browser
accessibility tree. Its accessible name MUST identify the record and its
important state. Keyboard users MUST be able to reach each node, move through
related nodes with documented arrow-key behavior, open the same inspector or
modal as a pointer user, close it with Escape, and return focus to the opening
node. The graph MUST expose selected, expanded, inherited, disabled, empty,
error, and unavailable state without color alone. The application MUST NOT
render a duplicate service or assignment table or list. The graph nodes MUST
provide the complete accessible record and action surface.

The three-column configuration graph MUST make each provider-to-model mapping
and each ordered assignment route clear. Selecting a provider, canonical
model, provider-model, or assignment MUST open its details and applicable
actions without navigation. Create actions MUST use the current column and
selected node as context so the form does not ask for known references. The
assignment column MUST show inherited assignment sources, direct replacement
chains, default inheritance, empty default chains, last use, and observed call
requirements for the selected service.

The playground MUST open as a modal from an applicable provider-model or
assignment node. It MUST infer the exact provider-model or assignment target,
the available operations, and the controls that apply to its capabilities. It
MUST show the inferred target instead of asking the administrator to select it
again. It MUST show input, output, selected route, latency, usage, cost, media,
and a corrective safe error. The modal MUST fit the available desktop or phone
viewport, keep its long content in a local scroll region, move focus into the
dialog, and make the background inactive. Escape and the close action MUST
close it and restore focus to the node or action that opened it.

The playground modal is a global administration presentation and is not a
workspace-owned navigation page. This presentation change MUST NOT create a
new global call authority. The execution authority and accounting design
remain an explicit open item owned by `llmr-gui-playground`. Until that item
has an accepted specification change, each execution MUST follow the existing
service-key and workspace rules in
[Model, embedding, and media calls](03-model-embedding-and-media-calls.md#common-request-rules).
The interface MUST NOT invent, hide, or silently select an execution service,
workspace, or credential.

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
delete confirmation, search and filters, bounded incremental loading, empty
and unavailable state, optional detail rows, and accessible live status.
Optional tree and reorder behavior MAY be included when a host needs it. Host
applications MUST own records, permissions, API calls, domain copy, and
mutation policy.

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
