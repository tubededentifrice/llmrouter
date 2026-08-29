# Providers, models, prices, and configuration

Status: Accepted on 2026-08-23. The graph-first UI amendment was accepted on
2026-08-24. The fixed compound-board amendment was accepted on 2026-08-29.

## Global ownership

Only a global administrator MAY create, change, or delete provider
connections, canonical models, provider-model mappings, provider credentials,
model capabilities, and price sources. A service API key MUST NOT perform
these operations.

Every enabled provider-model mapping MUST be available to every service in
its assignment and exact-call surfaces. The product MUST NOT have a service or
service-tree provider-model allowlist. A service API key MUST NOT create,
change, delete, or read a provider credential value.

The first adapter set MUST include:

- OpenAI;
- OpenAI-compatible endpoints;
- OpenRouter;
- custom endpoints;
- WaveSpeed;
- Ollama;
- local embeddings;
- a fake test adapter.

The first adapter set MUST NOT include native Anthropic, Z.AI, or ChatGPT or
Codex subscription adapters. The Router MUST NOT migrate a dormant provider
branch only because old code exists.

## Fixed configuration board

The global administration application MUST manage providers, canonical
models, provider-model mappings, and assignments through one fixed
three-column relationship board. The board's accessible name MUST be
`LLM configuration relationships`. Its column headings, from left to right,
MUST be `Providers`, `Canonical models`, and `Assignments`.

The board MUST be the only primary configuration entry. Separate provider,
model, and assignment navigation pages MUST NOT be required. The columns MUST
keep their order. The board MUST NOT be a freeform canvas, and an administrator
MUST NOT drag or save record positions. Selection, filtering, responsive
stacking, and scrolling MUST NOT change the relationship order.

On a wide screen, the three columns MUST appear side by side. On a phone, they
MUST stack in the same `Providers`, `Canonical models`, and `Assignments`
order. A compound model card MUST keep its provider routes nested when the
columns stack. If a connector line cannot stay clear in the stacked layout,
the route row and assignment rung MUST show the full relationship as text. The
text MUST be the same as the applicable connector's accessible name. The phone
layout MUST keep every record and action available and MUST NOT cause page-level
horizontal overflow.

Provider connections, credentials, canonical models, provider-model mappings,
capabilities, and prices in the first two columns MUST always be global. When
no service is selected, the board MUST show the global catalog and MUST make
the service requirement for assignment configuration clear. When one service
is selected, the first two columns MUST stay global and the assignment column
MUST show that service's effective assignments, local definitions, and
inherited sources. Selecting a service MUST NOT create a service-owned copy or
allowlist of a provider, model, mapping, credential, capability, or price.

Changing the selected service MUST replace only the assignment column. It MUST
close an open assignment inspector or assignment playground. It MUST NOT
silently discard an unsubmitted service-assignment form. It MUST let the
administrator cancel the service change or confirm that the form will close.
A response for the previously selected service MUST NOT replace data for the
current selection. Each assignment write from the board MUST name the selected
service. When no service is selected, assignment create, change, and delete
actions MUST be unavailable.

### Records and technical identities

Each record MUST show its readable display name before its technical identity.
The board MUST use these exact visible forms:

| Record | Primary text | Secondary text |
| --- | --- | --- |
| Provider connection | provider `display_name` | `Provider ID: {api_name} · Adapter: {adapter label}` |
| Canonical model | model `display_name` | `Model ID: {api_name}` |
| Provider route | provider `display_name` | `Route ID: {api_name} · Wire model: {provider_model_name}` |
| Assignment | assignment `display_name` | `Assignment ID: {api_name}` |

The adapter labels MUST be `OpenAI`, `OpenAI-compatible`, `OpenRouter`,
`Custom`, `WaveSpeed`, `Ollama`, `Local embeddings`, and `Fake` for their
corresponding registered adapter values.

The interface MUST use `Provider route` as the visible product term for a
provider-model mapping. An inspector and technical help MAY also state
`provider-model mapping`. A normal card or row MUST NOT use `provider-model`,
`mapping`, an adapter type, or `canonical model` as its primary name.

Each canonical model MUST be one compound card. Its header MUST show the
canonical-model name, model identity, and applicable capability labels. A
nested group labelled `Provider routes` MUST contain every provider route that
names that canonical model. The board MUST NOT repeat the canonical model as a
peer node for each provider route. Selecting the card header MUST open the
canonical-model inspector. Selecting one nested route row MUST open that exact
provider-route inspector.

The model header MUST use the user-facing capability labels `Text input`,
`Image input`, `Text output`, `Structured JSON`, `Embeddings`, `Image output`,
`Video output`, `Audio output`, `Tool calling`, `Streaming`, and `Reasoning`
for the corresponding native values. It MUST show only applicable labels. A
provider route that narrows the canonical model MUST identify the narrowed
modalities, capabilities, or constraints in the row or its expanded details.
It MUST NOT imply that the route has a capability that it removed.

Each assignment MUST be one compound card. Its header MUST show the assignment
name, assignment identity, definition source, last-used time, and observed call
requirements. Its ordered effective chain MUST contain one rung for each exact
provider route. Position 1 MUST have the visible relationship label `Primary`.
Each later position MUST have `Fallback {position}`, where position starts at
2. Each rung MUST show the route's provider display name, canonical-model
display name, provider-route identity, and current route state. It MUST connect
to the exact nested provider-route row, not to the canonical-model card as a
whole. One route MAY connect to more than one assignment or position.

The last-use text MUST be `Last used: {time}` when `last_used_at` is available.
It MUST be `Last used: Never` when `last_used_at` is not available. Observed
requirements MUST use the same user-facing labels as the corresponding model
modalities and capabilities. An empty list MUST show
`No observed requirements.`

The visible provider-to-route relationship label MUST be `Provides`. The
visible route-to-assignment labels MUST be `Primary` and `Fallback {position}`.
The provider connector's accessible name MUST be
`{provider display_name} (Provider ID: {provider.api_name}) provides Route ID:
{provider_model.api_name} for {model display_name} (Model ID:
{model.api_name})`. The assignment connector's accessible name MUST be
`{relationship label}: Route ID: {provider_model.api_name} for {assignment
display_name} (Assignment ID: {assignment.api_name})`. Decorative connector
lines MAY be hidden from assistive technology only when these names and the
connected controls provide the same complete relationship.

### Readiness, state, and inheritance

A provider connection MUST show one of these board states:

- `Ready` when it is enabled, its closed adapter settings are valid, and it has
  an applicable credential when its adapter requires one;
- `Disabled` when its stored provider enablement is off;
- `Unavailable` when it is enabled but a required credential or required
  adapter setting is not available. The card MUST show the corrective reason.

A provider route MUST show `Enabled` when its stored route enablement is on and
its provider is ready. It MUST show `Disabled` when its stored route enablement
is off. It MUST show `Unavailable` when its stored route enablement is on but
its provider is disabled or unavailable, when it has an active cooldown, or
when the board cannot load a required referenced record. The row MUST show the
cause. For a cooldown, it MUST show `Cooldown until {time}`. The board MUST keep
a disabled or unavailable route in its model card and assignment rungs. It
MUST NOT remove the route or silently connect the rung to another route.

`Ready` and `Enabled` on the board mean configuration readiness. They MUST NOT
promise that a later provider call will succeed. Live provider health and
cooldown details MUST keep their separate labels and MUST NOT change stored
enablement.

A canonical model MUST show `Ready` when at least one nested provider route is
enabled and available. It MUST show `Unavailable` when it has no such route.
This state is a board summary. The current canonical-model contract has no
stored enablement field, so the board MUST NOT show a canonical-model
enablement control or describe the summary as stored lifecycle state.

An assignment MUST show `Ready` when its effective chain has at least one
enabled and available route. It MUST show `Unavailable` when its effective
chain is not empty but no rung is currently available. It MUST show `Empty`
when its effective chain is empty. These summary states MUST NOT replace the
ordered rungs. They MUST NOT claim that one route can satisfy every future
call. The board MUST compare each route with the assignment's stored observed
requirements and MUST label a mismatch `Does not meet observed requirements`.
This comparison is guidance only. Runtime filtering MUST continue to use the
current call's actual requirements.

A readiness summary MUST use all records that it needs. While an applicable
page or relationship is still loading, the board MUST show `Loading`. When the
server identifies more applicable records than the board has loaded, the board
MUST show `Partial`. It MUST keep known route states visible, and it MUST NOT
change a model or assignment summary to `Unavailable` until it has the complete
records for that summary.

An assignment card MUST use these exact source labels:

- `Local definition` for a definition stored on the selected service;
- `Inherited from {service display_name} ({service api_name})` when the
  effective definition comes from an ancestor service;
- `Inherits {assignment display_name} ({assignment api_name})` when the
  selected definition names another assignment;
- `Implicit root default` for the empty implicit root `default`.

The card MUST show `Local definition`, `Inherited from ...`, or
`Implicit root default` as its definition source. It MUST also show
`Inherits ...` when that definition names another assignment. The card MUST
show the resolved effective rungs after these labels. It MUST NOT merge parent
and child chains. An inherited card and its rungs MUST identify inherited state
with the word `Inherited`. They MUST NOT use only color. An administrator MUST
be able to inspect the source service or inherited assignment from this
context. The board MUST NOT invent a local definition when it presents
inherited state.

These board states only present current configuration and readiness. They MUST
NOT add delete effects, cascading changes, a canonical-model enablement field,
or another lifecycle rule.

### Search, empty state, and incomplete data

The board MUST have one search and filter surface for its three columns. Its
label MUST be `Search configuration`. `/` MUST move focus to this surface when
focus is not in an editable control. Search MUST match readable names,
technical identities, adapter labels, provider wire model names, capabilities,
states, assignment source labels, and observed requirements.

A result MUST keep each direct match and the connected records that are
necessary to understand the complete path. A provider match MUST keep its
routes, their canonical-model cards, and connected assignment rungs. A model
match MUST keep all its nested route rows, their providers, and their connected
assignment rungs. A route match MUST keep its canonical-model card, its
provider, and its connected assignment rungs. An assignment match MUST keep
its effective route rows, canonical-model cards, and providers. A connected
record that is not a direct match MUST show the label `Context`. Search MUST
NOT remove a route row from its canonical-model card or change fallback order.

Applying search or a filter MUST keep focus and selection when the selected
control remains in the result. If the selected control is not in the result,
the board MUST move focus and selection to the first direct match in rendered
order and announce the result count. If the complete result has no match, it
MUST move focus to `Clear search` and announce the no-result message. If the
loaded partial result has no match, it MUST move focus to the first available
`Load more` action in column order and announce the partial-result message.
Clearing search MUST restore focus and selection to the prior selected control
when that record still exists. Otherwise, it MUST move focus and selection to
the first available control.

After all applicable records are loaded, the no-result message MUST be
`No configuration matches this search.` Its action MUST be `Clear search`.
When more applicable records are available and no loaded record matches, the
message MUST be `No matches in loaded records.` The board MUST also show
`Partial`, `Load more`, and `Clear search`. Clearing search MUST restore the
complete loaded board and the prior selected service. Search, filtering, or
bounded incremental loading MUST NOT change global ownership, assignment
inheritance, or the selected service.

The board MUST identify when more records are available and MUST NOT present a
partial result as the complete configuration. Each partial column MUST show
`Partial` and a `Load more` action for its next bounded page. The action's
accessible name MUST be `Load more {column heading}`. Loading more records MUST
keep the search value, focus, selection, expanded compound cards, and selected
service. A failed referenced-record load MUST keep records that are safe to
show, label the affected relationship `Unavailable`, and provide a `Retry`
action. The board MUST NOT draw a connector to an assumed record.

If a loaded route's provider cannot load, the route row MUST stay in its model
card and show `Unavailable provider: {provider_api_name}` instead of a provider
display name. If a loaded route's canonical model cannot load, the
`Canonical models` column MUST put the route in a non-actionable group named
`Unavailable referenced records`. The row MUST show
`Unavailable model: {model_api_name} · Route ID: {api_name}`. It MUST also show
the provider display name or the unavailable-provider text. If an assignment
rung's route cannot load, the rung MUST show
`Unavailable route: {provider_model_api_name}`. An assignment with a source
service that cannot load MUST show
`Inherited from unavailable service ({defined_by_service_api_name})`. An
assignment that names another assignment that cannot load MUST show
`Inherits unavailable assignment ({inherits_assignment_api_name})`. Each
affected record MUST show `Unavailable` and `Retry`. These placeholders MUST
use only known API identities. They MUST NOT invent a display name or act as a
connector endpoint.

If an initial page for one column fails, that column MUST show
`Unable to load {column heading}.` and `Retry`. Safe records in the other
columns MUST stay visible. If a later page fails, the applicable column MUST
keep its loaded records and show `Unable to load more {column heading}.` and
`Retry`. The failure MUST keep the search value, selected service, expanded
cards, focus, and selection. If the failed action had focus, the replacement
`Retry` action MUST receive focus. The board MUST announce the error without
moving focus for a background failure.

The board MUST use these empty states:

- The `Providers` column MUST say `No providers are configured.` and offer
  `Add provider`.
- A canonical model with no provider routes MUST keep its card, say
  `No provider routes.`, and offer `Add provider route`.
- If no canonical model exists, the `Canonical models` column MUST say
  `No canonical models are configured.` and offer `Add canonical model`.
- With no selected service, the `Assignments` column MUST say
  `Select a service to view assignments.`
- With a selected service and no effective assignment records, the
  `Assignments` column MUST say
  `No assignments are configured for this service.` and offer `Add assignment`.

An empty implicit root `default` MUST appear as an assignment card with its
`Empty` state. It MUST NOT become the whole-column empty state.

### Board verification

Focused board tests MUST use at least two providers, two canonical models, two
routes nested in one model, one route shared by several assignment rungs, one
local assignment, one inherited assignment, an empty implicit `default`, a
long name, disabled state, unavailable state, and differing capabilities.

The comparison check MUST confirm the compound model-and-route organization
against the local Crewday reference and the compact three-column information
order against the local FJ2 reference. It MUST NOT require their colors,
product-specific lifecycle rules, or implementation code.

Router projection tests MUST confirm every primary name, secondary identity,
state label, capability label, inheritance label, fallback position, and exact
connector endpoint. Search tests MUST cover a display name, each technical
identity, a provider wire model, a capability, an inherited source, a direct
match with context, no results, clearing, and an incomplete loaded page.
Empty-state tests MUST cover each exact empty-state message and action. They
MUST include the state in which no service is selected.
Loading and error tests MUST cover an initial page failure, a later page
failure, retained safe data, retry, focus, selection, expanded cards, and
selected-service preservation.

Browser tests MUST use `http://127.0.0.1:5174`. They MUST check a wide viewport
at 1440 by 900 pixels and a phone viewport at 390 by 844 pixels. They MUST
check the fixed column order, compound cards, nested route rows, exact
route-to-rung connectors, long-name wrapping, local scrolling, no page-level
horizontal overflow, inspector focus return, and unchanged relationships
after search and service changes.

Keyboard tests MUST cover the one board tab stop, every arrow-key direction,
Home, End, Enter, Space, Escape, `/`, an unavailable target, search results,
and focus after a referenced record disappears. Accessibility tests MUST check
semantic controls, headings and groups, accessible names with state and
relationships, visible focus, live announcements, text alternatives for
state, connector treatment, reading order, and no duplicate record list.

## Provider connections and credentials

A provider connection MUST name one registered adapter type and contain only
the settings that its closed adapter schema permits. Unknown settings MUST
fail validation.

The provider editor MUST use a low-field setup for a standard adapter. Its
initial view MUST ask only for the connection identity, adapter type, and an
applicable credential. It MUST infer the registered standard endpoint and
safe adapter defaults. It MUST NOT ask the administrator to copy a standard
endpoint. Custom endpoints, adapter-specific limits, enablement, and other
closed-schema settings MUST be in an explicit advanced section. A custom or
OpenAI-compatible endpoint that has no registered standard value MUST remain
an explicit field and MUST keep the endpoint trust rules below.

Before submission, the editor MUST show the inferred endpoint and defaults in
a review summary or in the open advanced section. It MUST identify which
values will change. A change of adapter type MUST revalidate all fields and
MUST NOT silently keep or apply a setting that the new adapter schema does not
permit. An adapter that does not use a credential MUST NOT ask for one. If an
adapter requires a credential, the editor MUST make clear that the connection
is unavailable without an applicable credential and MUST show how to correct
the missing value. It MUST NOT report that connection as ready.

The editor MUST show a provider credential as write-only input and safe
metadata. It MUST make create, replace, and delete effects clear. It MUST NOT
put a credential value in the board, a model form, a request log, or a later
response.

Provider credentials MUST use a built-in encrypted store. The wrapping key
MUST stay outside the database and repository. Credential input MUST be
write-only after submission. The administration application MAY show safe
metadata, such as the credential name, provider type, creation time, and short
fingerprint. It MUST NOT show the stored secret.

A delete or replacement MUST make a credential unavailable to new attempts as
soon as its database transaction commits. An active attempt MAY finish with
the credential value that it already received in process memory.

An OpenAI-compatible or custom endpoint MUST use normal certificate-authority
and exact-hostname validation for non-loopback HTTPS. Plain HTTP MUST be valid
only for an explicit loopback endpoint. Configuration MUST NOT permit
arbitrary authorization headers outside the credential schema.

## Models and capabilities

A canonical model MUST declare the input and output modalities and call
capabilities that the Router can use. These declarations MUST cover, when
applicable:

- text and image input;
- text, structured JSON, image, video, audio, and embedding output;
- tool calling;
- streaming;
- reasoning;
- embedding dimension or media constraints.

A provider-model mapping MUST connect one provider connection to one canonical
model and its provider wire model. It MAY narrow the canonical capabilities.
It MUST NOT claim a capability that the adapter and provider route cannot
perform.

The Router MUST filter each call against the actual required modalities and
capabilities before provider work. It MUST fail before provider work when no
eligible candidate remains.

## Reasoning

An assignment MAY set one reasoning level for its complete effective fallback
chain. Supported common values MUST include reasoning disabled and at least
one enabled level. A reasoning-capable model MUST default to reasoning enabled
when the assignment has no setting.

Each provider-model mapping MUST define how each supported common reasoning
value maps to the provider request. Configuration validation MUST reject an
assignment level that one selected candidate cannot map.

## Price authority

Each provider-model price MUST name its ISO 4217 accounting currency. Price
storage and calculations MUST use fixed decimal values, not binary floating
point. The Router MUST NOT convert currencies. Statistics MUST keep costs in
different currencies in separate result groups.

Each canonical model MAY select a registered price source and a source model
identifier. Each provider-model mapping MAY replace both values. No selected
source MUST mean manual pricing.

A price MUST support the applicable typed units, including input tokens,
output tokens, cached tokens, images, video duration, audio duration, requests,
and other adapter-declared units. A provider-model mapping MUST have complete
manual or synchronized prices for the units that it can report before the
Router uses those units for cost.

OpenRouter MUST be a normal price source. Another registered source, such as
WaveSpeed, MAY supply media-model prices.

## Price synchronization

The Router MUST run one fixed daily price synchronization at 02:00 UTC. The
product MUST NOT have an editable synchronization schedule. A global
administrator MUST also be able to start a synchronization on demand.

One synchronization MUST fetch each selected source no more than once. It
MUST apply that source snapshot to all mapped provider-model rows. Each result
MUST show updated, unchanged, missing, and failed rows.

A missing row, source failure, invalid price, or unsupported source value MUST
keep the last accepted price. It MUST NOT replace a price with zero. Each
provider attempt MUST snapshot the typed prices that it used. A later change
MUST NOT rewrite raw accounting or daily aggregates.

## Catalog import

A global administrator MUST be able to create a canonical model and a
provider-model mapping manually.

The administration application MUST also provide an explicit on-demand import
preview from registered catalogs. A preview MUST NOT change current state.
The administrator MUST select the entries to import. The Router MUST validate
the selected entries and apply them directly. It MUST NOT add or change model
catalog entries in a background import.

For OpenRouter, the administrator MUST be able to enter one strict OpenRouter
model identifier or supported HTTPS `openrouter.ai` model URL from the
model-create action. The Router MUST reject an empty value, a malformed
identifier, a non-OpenRouter URL, a missing catalog model, an unavailable
catalog, and metadata that cannot map to the native model contract. The
preview MUST populate each native model and mapping field for which OpenRouter
supplies valid metadata. It MUST show the proposed canonical identity, display
name, input and output modalities, capabilities, reasoning support, context
and output bounds, applicable media or embedding constraints, price-source
identity, typed prices, and each proposed provider-model mapping. It MUST NOT
infer a capability that the catalog metadata does not support.

The preview MUST identify an existing canonical model or provider-model
mapping and MUST NOT silently replace it. The preview MUST let the
administrator select the applicable existing global provider connections for
new mappings. One confirmation MUST create the selected canonical model and
provider-model mappings in one database transaction. A validation, duplicate,
catalog, or storage failure MUST create none of them. Imported values MUST
remain editable through the same board inspectors after creation.

Catalog import through this create workflow MUST create new records only. An
existing proposed canonical model or provider-model mapping MUST block
confirmation and MUST direct the administrator to the existing board record.
The administrator MUST select one or more compatible global provider
connections for new mappings. Confirmation MUST use the native values that the
administrator reviewed. A catalog change after preview MUST NOT silently
change those values. The Router MUST validate the complete reviewed model,
every selected connection, every mapping, and the current database state again
in the confirmation transaction. A concurrent duplicate or deleted connection
MUST fail the complete import.

## Direct configuration changes

Each configuration write MUST validate the complete affected current state and
apply the change in one database transaction. A validation or storage failure
MUST leave current state unchanged.

Validation MUST reject an unknown reference, duplicate identity, assignment
cycle, missing assignment parent, duplicate assignment candidate, unavailable
provider-model, unsupported reasoning mapping, and capability mismatch. A
delete MUST fail if a remaining assignment or provider-model mapping still
requires the deleted record.

Configuration resources MUST NOT have a public revision or version. A write
MUST NOT require or return an expected revision. If two valid writes occur at
the same time, each transaction MUST be atomic and the last committed value
MUST be the current value. The product MUST NOT keep drafts, immutable
configuration revisions, publication state, rollout state, or rollback
operations.

The activity log MUST record each successful or failed configuration change.
It MUST NOT be configuration history and MUST NOT store old field values.
