# Providers, models, prices, and configuration

Status: Accepted on 2026-08-23.

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

## Provider connections and credentials

A provider connection MUST name one registered adapter type and contain only
the settings that its closed adapter schema permits. Unknown settings MUST
fail validation.

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
