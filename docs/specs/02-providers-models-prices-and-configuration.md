# Providers, models, prices, and configuration

Status: Accepted on 2026-08-23. The graph-first UI amendment was accepted on
2026-08-24.

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

The global administration application MUST manage providers, canonical
models, provider-model mappings, and assignments through one three-column
configuration graph. The columns MUST show provider connections, canonical
models with their provider-model mappings, and assignments. The graph MUST be
the only primary configuration entry. Separate provider, model, and assignment
navigation pages MUST NOT be required.

Provider connections, credentials, canonical models, provider-model mappings,
capabilities, and prices in the first two columns MUST always be global. When
no service is selected, the graph MUST show the global catalog and MUST make
the service requirement for assignment configuration clear. When one service
is selected, the first two columns MUST stay global and the assignment column
MUST show that service's effective assignments, local definitions, and
inherited sources. Selecting a service MUST NOT create a service-owned copy or
allowlist of a provider, model, mapping, credential, capability, or price.

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

The editor MUST show a provider credential as write-only input and safe
metadata. It MUST make create, replace, and delete effects clear. It MUST NOT
put a credential value in the graph, a model form, a request log, or a later
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
mapping and MUST NOT silently replace it. The administrator MUST select the
applicable existing global provider connections before confirmation. One
confirmation MUST create the selected canonical model and provider-model
mappings in one database transaction. A validation, duplicate, catalog, or
storage failure MUST create none of them. Imported values MUST remain editable
through the same graph inspectors after creation.

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
