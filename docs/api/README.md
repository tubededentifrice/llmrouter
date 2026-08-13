# API contracts

The accepted public-interface direction is:

- a native versioned HTTP and streaming API as the primary contract;
- an OpenAI-compatible interface for accepted common operations;
- official Python and TypeScript clients.

The accepted cross-service integration contracts are:

- [Service and workspace management](service-management.md) for service token
  exchange and idempotent router workspace life cycle.
- [Hosted administration embed protocol](embed-protocol.md) for the
  service-scoped router administration view.
- [Business-tool gateway protocol](business-tool-gateway.md) for registered
  service gateways, one-use calls, results, and reconciliation.
- [Native OpenAPI](openapi.yaml) for all first-release HTTP operations and
  closed JSON schemas.
- [Native stream protocol](stream-protocol.md) for model-request and agent-run
  server-sent events, replay, interruption, and completion.
- [Public errors](errors.md) for retry rules and normalized failure classes.
- [Request fingerprint](request-fingerprint.md) for exact idempotency input and
  transient-field classification.
- [Cross-service conformance](cross-service-conformance.md) for contract,
  identity, lifecycle, failure, recovery, deletion, and release tests.

`openapi.yaml` is the source for native HTTP wire shapes. The protocol and
error files supply behavior that OpenAPI cannot express completely. The
service-management and embed documents give readable cross-service guidance;
their HTTP shapes MUST stay equal to the OpenAPI source.

All contract envelopes are closed. A field whose schema is an explicit JSON
document can contain only the values allowed by that document's declared
schema. This rule does not make an envelope open to new sibling fields.

## Machine-token mapping

Each bearer-token route uses one audience and one exact operation. A token for
another row MUST fail before record lookup.

| Route group | Audience | Operation |
| --- | --- | --- |
| Model create, read or events, cancel | `data_plane` | `model.create`, `model.read`, `model.cancel` |
| Agent-run create, read or events, cancel | `data_plane` | `run.create`, `run.read`, `run.cancel` |
| Shared-tool create, read, cancel | `data_plane` | `tool.create`, `tool.read`, `tool.cancel` |
| OpenAI-compatible model operation | `data_plane` | `model.create` |
| Attachment create, upload, metadata, or content read | `data_plane` | `attachment.create` or `attachment.read` |
| Workspace create, read, disable, restore, retire | `service_management` | The matching `workspace.*` operation |
| Host workspace-ceiling read or write | `budget_authority` | `budget_ceiling.read` or `budget_ceiling.write` |
| Embed-session create | `host_backend` | `admin_embed.create` |
| Effective configuration and configuration list or read | `configuration` | `configuration.read` |
| Configuration create, replace, rollback, or price synchronization | `configuration` | `configuration.write` |
| Business-tool gateway read or replace | `configuration` | `configuration.read` or `configuration.write` |
| Exact-route diagnostic-grant create | `configuration` | `diagnostic.grant.create` |
| Budget read or subordinate-limit write | `configuration` | `budget.read` or `budget.write` |
| Service or workspace retention read, preview, or write | `configuration` | `retention.read`, `retention.preview`, or `retention.write` |
| Accounting summary | `accounting` | `accounting.read` |

`GET /v1/contracts`, the OpenID Connect callback, administrator-session start,
and `GET /v1/health` do not use a bearer token. Global administration routes
use the local administrator session, its local grant, and recent
authentication when required. An administrator session MUST NOT satisfy a
machine-token route. A machine token MUST NOT satisfy a global administration
route.

The first-release OpenAI compatibility surface is `POST /v1/chat/completions`
and `POST /v1/responses`. It accepts the common non-provider-specific text,
multimodal input, JSON output, tool-definition, tool-choice, sampling, output-
limit, and stream fields that map without loss to the native model-request
schema. The `model` value is an assignment name. An approved exact diagnostic
route uses `x_llmrouter_exact_route` and the write-only
`x_llmrouter_exact_route_grant` together. The response includes
`x_llmrouter_request_id`, `x_llmrouter_state`, and the native status location.
Unsupported fields that can change cost, safety, routing, or result meaning
return `400 unsupported_capability`. Compatibility streaming maps native
events to the applicable compatibility event and ends an interrupted request
with an error event; it MUST NOT emit a normal completion marker.

The compatibility adapter MUST map the complete accepted request to the native
schema before admission. The mapped native values, compatibility operation and
contract versions, authenticated scope, and compatibility extensions are part
of the request fingerprint. A compatibility retry MUST use the same UUIDv7 and
the same request body.

An attachment upload can contain no more than 25 MiB. One logical request can
reference no more than 20 attachments and no more than 100 MiB of attachment
content in total. The Router verifies each declared byte length and SHA-256
digest before it marks the attachment ready.

Global administration uses explicit local grants. The headless operations API
uses the same session, grant, recent-authentication, CSRF, origin, idempotency,
revision, and audit rules as the hosted application. Captured exports are
redeemed through the same-origin Router endpoint and never through a direct
object-store URL.

These global actions require Pocket ID authentication that is no more than
five minutes old: credential changes, captured-content reads, export creation
and redemption, grant changes, service-parent changes, promotion, failback,
restore validation, and security-policy changes. Other actions can use a
current administrator session unless their effective policy marks them as
more sensitive. Each permitted or denied sensitive action creates an audit
event.

At build time, release tooling MUST calculate SHA-256 for each contract
artifact and publish those values from `GET /v1/contracts`. A caller pins the
build inputs for provenance. At runtime, it checks the declared major versions
and required capabilities. It MUST NOT require a whole-file digest match for a
compatible minor deployment.
