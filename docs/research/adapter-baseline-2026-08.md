# Draft source-driven adapter baseline

Date: 2026-08-13

Status: Research. This document is not normative. Product-owner approval in
`llmr-b01` is necessary before this draft can become a provider specification
or start adapter implementation.

The review used these source revisions:

- Crewday `379a555581f56ad720a1a380570ce17e7bf374a6`;
- FJ2 `9d02518d244072e29f6c8e6929c565058c04954e`; and
- Xbot `b2096e5ebd36c470fa5a0967f08894fff77fa9ca`.

The listed evidence files did not have local source-repository changes during
the review. The review excluded unrelated source-repository changes.

## Result

The smallest source-driven baseline without migration loss has six model adapter kinds, nine
model configuration profiles, two price-source profiles, eight search adapter
kinds, one page-service adapter, and one local extraction adapter. The matrix
does not include a provider only because it is popular. Each row has current
source code, an accepted Xbot need, or both.

This report treats an integration as current when a source repository has an
implemented production path or a supported database configuration shape. It
does not use private deployment data to guess which optional row has a secret
today. The product owner can make the baseline narrower during `llmr-b01`, but
the removal effect in each row shows the migration loss.

The draft excludes source fakes, source business tools, prompts, domain
authorization, local content stores, and source-specific retry loops. Router
conformance fixtures replace the fake providers. Calling services keep their
business tools and domain rules.

The draft also excludes FJ2's unreachable legacy Cohere embedding and
Replicate image branches. Those types are not in the current provider choices,
and their provider software development kits are not pinned in the FJ2
dependency files.

## Source evidence

### Crewday

- `docs/specs/11-llm-and-agents.md` defines the supported provider types,
  operations, fallback behavior, and usage records.
- `docs/specs/16-deployment-operations.md` defines the OpenRouter credential
  and weekly price refresh.
- `app/api/factory.py` normally builds one `OpenRouterClient`; the development
  selector can build the in-process fake.
- `app/adapters/llm/ports.py` defines text completion, chat, tools, vision OCR,
  streaming, token usage, model identity, and finish reason.
- `app/adapters/llm/openrouter.py` implements OpenAI-compatible chat,
  server-sent events (SSE),
  tools, image input, audio transcription, usage parsing, safe errors, retries,
  and OpenRouter model and price metadata.
- `app/adapters/llm/ollama.py` implements Ollama chat, tools, image and audio
  input, transcription, and usage parsing. Its current adapter does not stream.
- `app/adapters/llm/fastembed.py` implements local fixed-dimension embeddings.
- `app/adapters/db/llm/models.py` has the closed provider types `openrouter`,
  `openai_compatible`, `ollama`, `fake`, and `local_embedding`. Its non-secret
  provider shape includes endpoint, credential reference, timeout, request
  rate, state, wire model, capabilities, thinking controls, and typed prices.
- `app/api/admin/llm.py` exercises OpenRouter, OpenAI-compatible, Ollama, and
  local embedding routes in administrator diagnostics.
- `tests/unit/adapters/test_llm_openrouter.py`,
  `tests/unit/adapters/test_llm_ollama.py`, and
  `tests/unit/test_fastembed_client.py` verify the adapter interfaces. The
  recorded OpenRouter shapes are in `tests/fixtures/llm/`.

### FJ2

- `docs/llm-providers/supported-providers.md` and
  `docs/llm-configuration.md` describe the supported provider and model
  configuration shapes.
- `docs/decisions/0030-bounded-sse-admin-agent-display-streaming.md` defines
  the bounded display stream.
- `docs/decisions/0031-codex-subscription-provider-contract.md` defines the
  isolated Codex app-server protocol, paused turns, accounting, and interrupt.
- `docs/decisions/0035-focused-admin-web-extraction.md` defines the bounded
  public-page extraction path.
- `apps/llm_providers/models.py` has Anthropic, OpenAI, OpenAI-compatible,
  OpenRouter, Z.AI, custom, WaveSpeed, and Codex app-server provider types.
  Its non-secret shape includes endpoint, encrypted-key presence, default
  model, timeouts, request limits, capabilities, provider-model settings,
  typed prices, and price-source identity.
- `apps/llm_providers/clients/chat.py` implements native Anthropic messages and
  OpenAI-compatible chat, tools, usage, and one reviewed OpenAI-compatible
  display stream.
- `apps/llm_providers/clients/vision.py`,
  `apps/llm_providers/clients/embedding.py`, and
  `apps/llm_providers/clients/image_generation.py` add vision with JSON Schema,
  embedding, OpenAI/OpenRouter image, and WaveSpeed image operations.
- `apps/llm_providers/clients/factory.py` supplies fallback, provider error
  classes, attempt usage, model capabilities, display streaming, and exact
  paused-turn rules.
- `apps/llm_providers/clients/codex_chat.py` and
  `apps/llm_providers/codex_gateway.py` implement an isolated Codex app-server
  session, native tools, exact paused turns, usage, and interruption.
- `apps/llm_providers/price_sync.py` implements one-fetch OpenRouter and
  WaveSpeed catalog price normalization.
- `apps/autopublish/services/web_search.py` implements Brave, Z.AI, Linkup,
  DuckDuckGo Lite, ScrapingDog Google, ScrapingDog Bing, Serper, and SearXNG
  search. It declares web, news, and image capabilities and records query,
  latency, results, credits, cost, and credential-redacted source errors. The
  source does not prove that all stored provider error text is safe content.
- `apps/autopublish/services/scrapingdog.py` implements the ScrapingDog scrape
  and screenshot operations with URL checks, quota handling, credit units,
  and usage records.
- `apps/admin_agent/url_fetch.py`, `extraction_sandbox.py`, and
  `url_extraction_worker.py` implement bounded public-URL fetch and local text
  extraction. `apps/admin_agent/web_extraction.py` can then use a normal model
  assignment for focused extraction.
- `tests/test_llm_providers/`, `tests/test_autopublish/test_web_search.py`,
  `tests/test_autopublish/test_web_search_tracking.py`,
  `tests/test_autopublish/test_scrapingdog_service.py`, and
  `tests/test_admin_agent/test_url_fetch.py` provide recorded conformance
  evidence. `tests/test_llm_providers/test_wavespeed_live.py` is the only
  provider-key live model test found, but its current constructor call has an
  unsupported `timeout` argument and cannot reach the provider. The Codex
  protocol smoke is gated in
  `tests/test_llm_providers/test_codex_contract.py`.

### Xbot

- `docs/specs/07-shared-llm-router-integration.md` assigns model routing,
  provider fallback, shared search, shared extraction, usage, pricing,
  cancellation, and workspace isolation to LLM Router.
- `docs/specs/01-agent-control.md` requires provider-neutral agent runs,
  current tool checks, pause, stop, cancellation, and durable handoffs.
- `docs/specs/03-content-engagement-and-media.md` requires eligible image
  analysis, image generation, and screenshot media.
- `docs/specs/05-platforms-operations-and-measurement.md` requires model,
  provider, media-tool, and workspace budget accounting.
- `docs/specs/06-security-privacy-and-quality.md` requires workspace and privacy
  approval before provider use.

Xbot has no provider adapter or production provider data. It adds capability
and isolation requirements but does not justify an additional vendor row.

## Proposed model and price matrix

The word **Add** describes the effect of approval. **Remove** describes the
exact loss if the product owner removes the row.

| ID | Proposed registered kind | Operations and current evidence | Credential and settings form | Usage, price, error, and cancellation shape | Add and remove effect |
| --- | --- | --- | --- | --- | --- |
| M01 | `openai_compatible.v1` | Text and multimodal chat, non-streaming tools, JSON output, SSE text, audio transcription, embeddings, and image generation, including OpenRouter image-to-image. Enable each operation only on a route that declares it. Crewday: `app/adapters/llm/openrouter.py`. FJ2: `apps/llm_providers/clients/chat.py`, `apps/llm_providers/clients/vision.py`, `apps/llm_providers/clients/embedding.py`, and `apps/llm_providers/clients/image_generation.py`. | HTTPS endpoint profile: `openai`, `openrouter`, `zai`, or `generic`. Bearer API key. Closed settings: profile, connect/read timeout, optional fixed OpenRouter attribution identity, and supported operation list. A generic endpoint uses the standard endpoint trust rules and cannot set arbitrary headers. The three vendor profiles use fixed vendor origins in the first baseline. | Input, output, reasoning, and cached tokens when reported; images or audio seconds for those operations. Manual prices or P01. Normalize authentication, rate, timeout, connection, invalid request, server, empty response, invalid response, refusal, and stream interruption. After Router accepts cancellation, it can close the provider stream; no source proves an upstream cancel API. | Add: migrates Crewday's normal model path and most FJ2 model paths with one capability-gated protocol adapter. A custom OpenAI-compatible endpoint can move to `generic`, but a current OpenAI, OpenRouter, or Z.AI vendor-profile endpoint override cannot migrate unchanged. Remove: Crewday cannot migrate its normal provider, and FJ2 loses OpenAI, OpenRouter, Z.AI, and custom-compatible routes. |
| M02 | `anthropic_messages.v1` | Text and multimodal messages, tools, schema-verified JSON, and reported token usage. FJ2: `apps/llm_providers/clients/chat.py` and `apps/llm_providers/clients/vision.py`. The source has no reviewed native streaming path. | Fixed Anthropic HTTPS origin with an API key. Closed settings: API version profile, connect/read timeout, and supported operation list. | Input and output tokens. Manual price in the first baseline. Use the same normalized errors as M01. After Router accepts cancellation, it can close the provider transport, but that does not confirm provider cancellation. | Add: keeps FJ2 native Anthropic routes without forcing an OpenAI compatibility gateway. Remove: those routes move to an approved M01 gateway or stay in FJ2. |
| M03 | `wavespeed_images.v1` | Text-to-image submission with bounded polling. FJ2: `apps/llm_providers/clients/image_generation.py`. The source accepts `source_image` only for OpenRouter, not WaveSpeed. | Fixed WaveSpeed HTTPS origin with a bearer API key. Closed settings: poll interval, poll limit, and accepted image sizes. | Image count and optional request unit. Manual price or P02. Normalize submit failure, poll failure, timeout, invalid result URL, and invalid image result. The source has no cancel request for a submitted task; cancellation stops polling and remains uncertain until reconciliation ends. | Add: migrates FJ2 WaveSpeed media routes that use the standard origin. A configured WaveSpeed-compatible proxy cannot migrate unchanged. Remove: FJ2 keeps this adapter or migrates the affected image assignments to M01. |
| M04 | `codex_app_server.v1` | Text chat, dynamic tools, exact paused-turn continuation, token usage, model discovery, and interrupt. FJ2: `apps/llm_providers/clients/codex_chat.py` and `apps/llm_providers/codex_gateway.py`. | No provider API key. Deployment-managed account session, fixed executable and protocol pin, isolated work directory, model allow-list, and health snapshot. This form does not fit the current public provider-instance requirement for an HTTP endpoint and credential record. | Input, output, and reasoning tokens. FJ2 records zero marginal provider cost, but the source does not define how to allocate the subscription cost. Normalize account unavailable, protocol mismatch, invalid model, invalid tool continuation, timeout, and interruption. The gateway has an explicit interrupt operation. | Add: gives Router one accounting and security point for FJ2 subscription turns, but needs an approved local-provider contract change, price treatment, and legal and operations review. Remove: FJ2 keeps the isolated gateway outside Router and its subscription attempts do not enter Router accounting. |
| M05 | `ollama_chat.v1` | Text chat, tools, image input, audio transcription through native multimodal chat, and usage. Crewday: `app/adapters/llm/ollama.py` and the provider diagnostic in `app/api/admin/llm.py`. This is an implemented diagnostic and supported registry path; the normal process factory does not select it. The current adapter does not stream. | Endpoint, timeout, and operation list. A non-loopback endpoint uses HTTPS with normal certificate-authority and hostname validation. Plain HTTP is valid only on loopback. The bearer key is optional in Crewday, but the current Router provider-instance contract requires a credential identity. | Prompt and generated token counts from Ollama. Manual price, normally zero. Normalize connection, timeout, HTTP, and invalid response. The source has no provider cancel operation. | Add: migrates Crewday's supported Ollama configuration without an OpenAI translation. A no-key instance needs an approved provider-instance contract change; a private plain-HTTP endpoint cannot migrate unchanged. Remove: an installed Crewday Ollama row stays local or uses an approved M01-compatible endpoint. |
| M06 | `fastembed_local.v1` | Atomic text embedding batches with fixed model space and dimension. The source strips leading and trailing whitespace and rejects a blank item before inference. Crewday: `app/adapters/llm/fastembed.py`, `app/api/admin/llm.py`, and `tests/unit/test_fastembed_client.py`. This is an implemented diagnostic and seeded registry path; the normal model-client factory does not select it. | No endpoint and no credential. Closed settings: approved model name, immutable model artifact digest, dimension, local cache path policy, thread limit, and offline mode. This form does not fit the current public provider-instance endpoint and credential requirements. | Input count and bytes, local compute duration, and manual zero provider price. Reject wrong count, dimension, non-numeric, non-finite, or zero-norm vectors. The source has no active-inference cancellation interface. | Add: permits Crewday local embedding migration without loss, but needs an approved local-provider contract change, immutable model-artifact control, and an explicit input-normalization rule. Remove: Crewday keeps local embedding outside Router or changes model space to a remote embedding route. |
| P01 | `openrouter_catalog.v1` | One immutable catalog fetch for model metadata and typed input, output, request, image, and audio prices. Crewday: `app/adapters/llm/openrouter.py`. FJ2: `apps/llm_providers/price_sync.py`. Crewday parses audio prices. FJ2 parses prompt, completion, image, and request values only. | The Router proposal uses the M01 OpenRouter instance credential. FJ2 currently fetches this public catalog without a bearer key. Closed settings: fixed catalog path, snapshot timeout, and source age limit. | Router would preserve raw decimal strings, source time, content hash, optional validator, missing values, explicit zero, and per-row status. FJ2 currently defaults an absent source value to zero and drops zero image and request prices. | Add: migrates both current weekly OpenRouter price sources and fixes their loss of source facts. Remove: every OpenRouter route needs a manual price. |
| P02 | `wavespeed_catalog.v1` | One immutable catalog fetch for per-request base prices. FJ2: `apps/llm_providers/price_sync.py`. | Uses the M03 credential. Closed settings: fixed catalog path, snapshot timeout, and source age limit. | Preserve the raw base-price string as a per-request component and the same snapshot and error fields as P01. | Add: keeps FJ2 WaveSpeed automatic price updates. Remove: every WaveSpeed route needs a manual per-request price. |

M01 is one adapter kind, not four adapters. The closed profile changes the
trusted origin and known protocol differences. A route cannot claim an operation
only because another profile supports it. Each exact provider-model route
needs its own capability proof.

Each M01 profile is a separate approval unit even though the profiles use one
adapter kind:

| Profile | Current source form | Add and remove effect |
| --- | --- | --- |
| `openai` | FJ2 `openai` provider type. | Add: permits FJ2 OpenAI routes on the fixed OpenAI origin. Remove: those routes stay in FJ2 or move to another approved profile. |
| `openrouter` | Crewday normal production path and supported graph rows; FJ2 `openrouter` provider type, including image-to-image. | Add: permits both services' OpenRouter routes on the fixed OpenRouter origin and P01. Remove: Crewday cannot migrate its normal model path, FJ2 OpenRouter routes stay local, and P01 cannot supply their prices. |
| `zai` | FJ2 `zai` provider type. | Add: permits FJ2 Z.AI model routes on the fixed Z.AI origin. Remove: those routes stay in FJ2 or move to another approved profile. |
| `generic` | Crewday `openai_compatible` provider type; FJ2 `openai_compatible` and `custom` provider types. | Add: permits supported compatible endpoints under the standard endpoint trust rules. Remove: those configured routes stay in their calling service. |

Approval of M01 needs to name its approved profiles. Approval of one profile does
not approve another profile or its operations.

## Proposed shared-tool matrix

| ID | Proposed registered kind | Operations and current evidence | Credential and settings form | Usage and failure shape | Add and remove effect |
| --- | --- | --- | --- | --- | --- |
| T01 | `brave_search.v1` | Web, news, and image search. FJ2: `BraveSearchBackend` in `apps/autopublish/services/web_search.py`. | Fixed Brave origins; subscription-token API key; minimum interval. | One search unit, latency, count, safe error, and bounded retry and circuit facts. | Add: preserves the Brave chain member. Remove: any Brave-configured search chain loses that candidate. |
| T02 | `zai_search.v1` | Web search and generic news through the same web-search operation; no source image capability. FJ2: `ZaiSearchBackend` in `apps/autopublish/services/web_search.py`. | Fixed Z.AI origin; bearer API key. | One search unit plus the common search facts. | Add: preserves the Z.AI chain member. Remove: any Z.AI-configured chain loses that candidate and image search remains unaffected. |
| T03 | `linkup_search.v1` | Web search, generic news search, and image search. FJ2: `LinkupSearchBackend` in `apps/autopublish/services/web_search.py`. | Fixed Linkup origin; bearer API key. | One search unit plus the common search facts. | Add: preserves the Linkup chain member. Remove: any Linkup-configured chain loses that candidate. |
| T04 | `duckduckgo_lite.v1` | HTML-backed web search and generic news search through the same operation. FJ2: `DuckDuckGoBackend` in `apps/autopublish/services/web_search.py`. | No credential; fixed DuckDuckGo Lite origin; strict rate interval. | One search unit, HTTP challenge state, latency, count, and safe error. No provider bill. | Add: preserves the current credential-free fallback under Router tracking and policy. Remove: the untracked database-failure fallback also ends; services need an eligible configured search provider. |
| T05 | `scrapingdog_google.v1` | Google web, news, and image search. FJ2: `ScrapingDogSearchBackend` in `apps/autopublish/services/web_search.py`. | Fixed ScrapingDog search origins; query API key. | FJ2-assumed credit units by operation, cost, quota circuit, latency, count, and safe error. FJ2 records zero units on failure. | Add: preserves the main ScrapingDog search modes. Remove: those configured chain rows and their credit accounting cannot migrate. |
| T06 | `scrapingdog_bing.v1` | Bing web search. FJ2: `ScrapingDogBingSearchBackend` in `apps/autopublish/services/web_search.py`. | Its own ScrapingDog query-key credential record; fixed Bing search origin. | Same FJ2-assumed credit shape and shared ScrapingDog quota circuit as T05. FJ2 records zero units on failure. | Add: preserves the separate Bing candidate and its operation limits. Remove: Bing-configured chain rows cannot migrate; T05 is not an equivalent result source. |
| T07 | `serper_search.v1` | Web, news, and image search. FJ2: `SerperSearchBackend` in `apps/autopublish/services/web_search.py`. | Fixed Serper origins; `X-API-KEY` secret. | One search unit, latency, count, and safe error. | Add: preserves the Serper chain member. Remove: any Serper-configured chain loses that candidate. |
| T08 | `searxng_search.v1` | Web, news, and image search. FJ2: `SearXNGSearchBackend` in `apps/autopublish/services/web_search.py`. | Base URL and optional query key. A non-loopback endpoint uses HTTPS with normal certificate-authority and hostname validation. Plain HTTP is valid only on loopback. | One search unit, latency, count, and safe error. Manual zero or deployment cost. | Add: preserves self-hosted search and local data control. A current non-loopback plain-HTTP URL cannot migrate unchanged. Remove: any SearXNG-configured chain stays in FJ2 or moves to another search provider. |
| T09 | `scrapingdog_page.v1` | Static page scrape and full-page or viewport screenshot. FJ2: `apps/autopublish/services/scrapingdog.py`. | Same credential record as T05; fixed scrape and screenshot origins. Closed settings disable dynamic and premium scrape modes in the first profile. | FJ2 assumes one credit for scrape and five credits for screenshot and records zero credits on failure. Record returned media type, bytes, latency, quota state, and safe error. | Add: migrates both current protected-page operations. Remove: protected-page fallback and provider screenshots stay in FJ2; search rows do not replace them. |
| T10 | `public_http_extract.v1` | Public URL fetch, bounded HTML, plain-text, and PDF extraction, and optional query-focused model extraction through a normal model assignment. FJ2: `apps/admin_agent/url_fetch.py`, `extraction_sandbox.py`, `url_extraction_worker.py`, and `web_extraction.py`. Xbot: `docs/specs/07-shared-llm-router-integration.md`. | No external-provider credential. Closed settings: allowed schemes and ports, redirect limit, response byte limit, content types, address checks, and extraction process limits. | Request count, fetched bytes, extracted bytes, latency, terminal URL class, and safe error. Focused model extraction is a separate model attempt with normal token accounting. | Add: supplies the shared extraction operation that Xbot requires and migrates the FJ2 public-page path. Remove: Xbot lacks an accepted shared extraction adapter and FJ2 keeps URL extraction local. |

No separate `extract` vendor row is present in the sources. T10 uses a local,
bounded extractor. The FJ2 path explicitly rejects an archive, paid, or
emergency fetch fallback. This matrix does not add a hidden T09 fallback or a
new adapter-composition contract. A search result is never proof that its URL
was fetched.

## Draft registered provider specification

This section is a working proposal for the later normative provider
specification. It does not change accepted behavior.

### Registry

Each matrix kind would have these closed documents at major version 1:

- `adapter.<kind>.settings.v1` for non-secret instance settings;
- `adapter.<kind>.capabilities.v1` for exact operation and limit claims;
- `adapter.<kind>.<operation>.request.v1` for the provider-facing normalized
  request;
- `adapter.<kind>.<operation>.result.v1` for the normalized result; and
- `adapter.<kind>.error.v1` for safe normalized failure facts.

Unknown kinds, versions, fields, operations, result kinds, capability claims,
price units, and credential forms would fail before an external call.
Provider-specific dictionaries would not pass through the public model or
shared-tool API.

These documents would be internal adapter and control-plane contracts. Public
model and shared-tool request and result documents would stay provider-neutral.
They would not expose an adapter kind, product-specific field, provider product
name, credential, or fallback path. An approved diagnostic can show the opaque
selected route under its existing permission and audit rules.

The common adapter identity would contain the kind, schema major, instance,
route, operation, configuration revision, endpoint trust profile, credential
identity, attempt timeout, and capability proof revision. It would not contain
secret material.

### Model operations

The closed model operation names would be:

- `chat.complete` for text, image, audio, or file content that the route proves;
- `chat.stream` for provider text streams that can map to Router stream version
  1 without loss;
- `tool.request` as part of non-streaming chat or a proved stream profile;
- `audio.transcribe` for audio-to-text routes;
- `embedding.batch` for the accepted atomic embedding contract; and
- `image.generate` for bounded image output.

Tool definitions would use registered input schemas. An adapter would send
only definitions in the effective allow-list. Provider tool output would be
data, not execution authority. A paused tool continuation would bind to the
exact instance, route, attempt, provider turn, call identity, and expiry.

M01 streaming would map text, usage, finish state, and safe error events. The
first released output remains the Router commit boundary. A source transport
that cannot map tool-call deltas safely would reject streaming tools for that
route. M02, M05, and M06 would not claim streaming in this baseline. M04 would
keep its provider turn private and expose only Router events.

### Shared-tool operations

The provider-neutral registered inputs would be:

- `shared.search.input.v1`: query, type (`web`, `news`, or `images`), result
  limit, optional locale, and safe-search level;
- `shared.extract.input.v1`: public URL and optional focused query;
- `shared.scrape.input.v1`: public URL;
- `shared.screenshot.input.v1`: public URL and `full_page` boolean.

The result schemas would have bounded items. A text search item would contain
title, URL, snippet, and source rank. An image item would contain image URL,
page URL, title, and source rank. Extraction would contain terminal public URL,
title when found, media type, bounded text, and truncation state. Scrape would
contain terminal public URL, status, media type, immutable content attachment,
and truncation state. Screenshot would contain page URL, image attachment,
media type, dimensions, and capture scope.

Queries, page text, and screenshots remain captured content. Usage and safe
errors would not contain those values. Every URL operation would validate the
scheme, port, host, DNS result, redirects, response type, and byte limit. A
redirect or DNS change to a private or disallowed address would fail.

### Usage and pricing

Each attempt result would report the raw provider quantities that are
available and one normalized set:

- input, output, cached, and reasoning tokens;
- audio seconds;
- image count;
- search count;
- provider tool or credit units;
- request count; and
- fetched and returned bytes for operational limits, not price unless a price
  component names that unit.

Missing usage would remain missing and estimated. It would not become zero.
Billable usage from a failed, refused, interrupted, or uncertain attempt would
remain eligible for accounting.

P01 and P02 would fetch one immutable snapshot for a synchronization run. A
normalizer would preserve raw decimal strings and distinguish explicit zero,
not applicable, missing, malformed, and rejected values. This is Router
behavior that corrects source loss; it is not a claim that FJ2 preserves these
facts now. Manual routes would not call a price source.

### Safe errors and fallback scope

Each adapter would map provider detail to a safe class and affected scope:

| Provider condition | Safe class | Usual affected scope |
| --- | --- | --- |
| Invalid, expired, or revoked provider credential | `authentication` | credential or provider instance |
| Provider rate limit | `rate_limit` | provider-model route or provider instance, from evidence |
| Provider account or quota unavailable | `provider_unavailable` | provider instance or credential, from evidence |
| Bounded provider timeout | `timeout` | attempt or provider-model route |
| Connect, TLS, or DNS failure | `transport` | provider instance or attempt |
| Provider 5xx | `provider_unavailable` | attempt or provider-model route |
| Malformed provider result | `invalid_provider_response` | provider-model route or attempt |
| Provider-specific policy refusal | `policy` | provider-model route or provider instance |
| Request value unsupported by one route | `incompatible_request` | provider-model route |
| Request invalid for every candidate | `incompatible_request` | logical request |
| Local or confirmed provider cancellation | `cancelled` | attempt |
| Unconfirmed submitted work or external effect | `uncertain_effect` | attempt |

Bad or missing adapter settings would fail configuration validation before an
external call. They would not become a provider attempt error.

The adapter would preserve private provider detail only under the accepted
capture policy. It would never place a key, authorization value, query text,
prompt, response, tool value, or raw page content in the safe error.

### Cancellation

The baseline has three adapter stop levels:

- M04 can issue the source interrupt operation and wait for a terminal state.
- After Router accepts a cancel request, an adapter can close its active
  provider transport. This stops local provider I/O but does not prove that a
  provider stopped billable work.
- M03 can stop polling, but the submitted remote image task stays uncertain
  because the source has no cancel operation.

A client stream disconnect does not cancel Router or provider work. Search,
scrape, screenshot, and extract stop before the next external effect and close
active transports after Router accepts cancellation. A completed provider call
or stored screenshot is not undone. Late usage still enters reconciliation.

## Live conformance plan

Recorded fixtures and unit tests are necessary but do not prove current
provider behavior. Each approved external row needs an opt-in live suite. It
uses a dedicated restricted test credential, non-private input, a hard cost
limit, and a tagged test account. It does not run in the normal repository
gate.

| Rows | Minimum live proof before release |
| --- | --- |
| M01 with `openai` | One text result, one tool request, one image input, one embedding batch, one text stream, usage, safe 4xx, timeout, and stream close. Test image generation only when an approved route claims it. |
| M01 with `openrouter` | The M01 proofs plus image-to-image with file and URL inputs, audio transcription, model/price snapshot P01, explicit-zero price, missing price, rate limit when a safe test mechanism exists, and provider-model identity different from wire model. Test only operations that approved routes claim. |
| M01 with `zai` or `generic` | Text, tools, capability rejection, usage, safe 4xx, and timeout for each approved profile. A generic endpoint test uses an isolated conformance server and one separately approved real endpoint. |
| M02 | Text, tools, image input, structured result validation, usage, authentication failure, rate response when safely available, timeout, and local close. |
| M03 and P02 | One low-cost text-to-image result, one invalid request, one stopped poll that reaches `uncertain`, one price snapshot, and one price normalization failure. The current FJ2 live WaveSpeed test has a constructor error and is not valid Router conformance evidence. |
| M04 | Exact pinned protocol and executable, account readiness, model discovery, text, tool pause and resume, duplicate or expired continuation denial, interrupt, restart, safe error redaction, usage, and the approved subscription-price treatment. Run only after the product owner approves the session credential form and legal and operations review. |
| M05 | Text, tool request, image input, audio transcription, usage, invalid model, timeout, accepted endpoint trust, and provider-transport close against an isolated approved Ollama instance. |
| M06 | Offline model-artifact digest, fixed vectors for public inputs, leading and trailing whitespace behavior, multi-item order, wrong dimension, non-finite vector, zero-norm vector, active-inference cancellation behavior, no-network proof, and repeatability on supported CPU architecture. |
| T01-T03 and T07 | One web query, each claimed news or image operation, result bounds, no-result case, invalid credential, rate or quota response when safely available, timeout, safe logs, and cost unit. |
| T04 | One public web query, challenge or rate behavior, result bounds, timeout, user-agent policy, and proof that no call occurs when the row is not selected. |
| T05-T06 | Each claimed search operation, exact credit count, quota circuit, HTTP 410 behavior where applicable, invalid credential, timeout, result bounds, and safe logs. |
| T08 | Web, news, and image queries against an isolated approved SearXNG instance; optional-key and no-key modes; standard endpoint trust; timeout; and result bounds. |
| T09 | One static public scrape and one low-cost screenshot, server-side request forgery and redirect denial, media validation, exact credit count, quota response, timeout, cancellation, and safe logs. |
| T10 | Public HTML, plain text, and PDF, redirects, decompression and byte bounds, unsupported type, DNS rebinding simulation, private-address denial, extraction sandbox limits, focused extraction accounting, cancellation, and no-call behavior without a valid assignment. |

All live suites would also prove that the Router records separate logical and
attempt identities, uses the selected configuration and price revisions,
redacts the credential, applies service and workspace scope, and can account
for a failed attempt.

## Approval choices

`llmr-b01` needs one named selection. The selection needs to name every approved
kind and every approved M01 profile. The proposed name for the complete matrix
above is `source-lossless-2026-08`.

Two narrower choices are evidence-backed but have explicit migration costs:

- `http-provider-core-2026-08`: M01 with all four profiles, M02-M03, P01-P02,
  and T01-T10. FJ2 keeps the Codex app-server, and Crewday keeps Ollama and
  FastEmbed outside Router.
- `configured-vendor-minimum-2026-08`: a product-owner supplied subset based
  on reviewed non-secret deployment inventory. The subset needs to identify M01
  profiles as well as matrix row IDs. This option needs that inventory because
  source code alone cannot prove which optional provider rows are configured.

Approval also needs explicit answers for M04, the no-key M05 form, and M06
because the accepted public provider-instance shape currently requires an
endpoint and credential identity. It also needs the M04 subscription-price
treatment. Decision 0052 already fixes endpoint trust. Approval of a row needs
to accept the migration loss that this rule causes. Alternatively, a separate
accepted change can change that rule. `llmr-b01` cannot close until the approved normative
provider specification and affected accepted contracts record those answers.
Placeholder secrets or fake HTTP endpoints do not make the rows compatible
with the accepted contract.
