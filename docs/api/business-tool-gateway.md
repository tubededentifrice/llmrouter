# Business-tool gateway protocol

Version: 1.0.0.

## Registration

One service can register one gateway origin. The registration declares the
supported gateway major versions and registered tool kinds. A global
administrator approves the origin and network policy. A run cannot supply a
callback URL.

All envelopes are closed. Each tool input and result uses a registered document
with `schema_name`, `major_version`, and `document`. The gateway and Router
MUST reject an unknown field, schema, major version, tool kind, or result kind.

## Call

The Router sends an HTTPS `POST` to
`/llmrouter/business-tool-gateway/v1/calls`. The request uses the closed
OpenAPI `BusinessToolCall` schema. The call
contains these fields:

- `contract_version`, with the value `1`;
- `operation_id`;
- `service_id` and optional `workspace_id`;
- `run_id` and `owner_epoch`;
- `tool_name` and `permitted_operation`;
- the registered input document;
- `deadline`;
- `tool_grant`, which is write-only, short-lived, one-use, and bound to every
  identity and field above.

The service checks the grant and its current user, workspace, approval, tool
permission, and record state before it performs an effect. A successful HTTP
connection does not prove that an effect completed.

## Result

The gateway returns the closed OpenAPI `BusinessToolResult` schema from the
call operation. The result contains `contract_version`, `operation_id`, `state`,
`effect_state`, and either a registered result document or a safe error.
`state` is `succeeded`, `failed`, or `uncertain`. `effect_state` is `none`,
`committed`, or `unknown`.

The result must fit the registered size, type, and time limits. The Router
treats result content as untrusted model input. It does not repeat a committed
or unknown effect.

## Reconciliation

For an unconfirmed effect, the Router sends `POST` to
`/llmrouter/business-tool-gateway/v1/reconciliations`. The request and response
use the closed OpenAPI `BusinessToolReconciliationRequest` and
`BusinessToolReconciliationResult` schemas. The request contains
the same operation identity, service, workspace, run, owner epoch, and a new
one-use reconciliation grant. The gateway returns `not_started`, `committed`,
`failed`, or `unknown` with safe evidence metadata. It must not start the
business operation during reconciliation.

A transport timeout or missing result stays `unknown`. The Router does not
infer success and does not repeat the effect automatically.
