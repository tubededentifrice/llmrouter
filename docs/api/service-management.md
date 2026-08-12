# Service and workspace management API

## Contract

This contract uses HTTP JSON API `/v1`. It defines the service-management
operations that a calling service needs before it sends a model, tool, or agent
request. A bearer token for these routes MUST have audience
`service_management` and the exact operation. It MUST NOT have request,
content-read, accounting-read, or configuration-write permission.

All request and response bodies are closed JSON objects. An unknown field is
invalid. An identifier is opaque and has 1 to 200 characters. A caller
reference is opaque to LLM Router and has 1 to 200 characters.

## Service-token exchange

`POST /v1/service-token-exchanges`

Request:

```json
{
  "service_id": "opaque-service-id",
  "bootstrap_secret": "write-only-secret",
  "audience": "service_management",
  "operations": ["workspace.create", "workspace.read"]
}
```

`operations` is a non-empty unique list. Its service-management values are
`workspace.create`, `workspace.read`, `workspace.disable`,
`workspace.restore`, and `workspace.retire`. A request can contain a smaller
set than the bootstrap credential permits.

The same exchange route can issue a `host_backend` token with operation
`admin_embed.create` when the bootstrap credential permits it. That token can
call only the server-side embed-session route. It MUST NOT use a
service-management operation. Each audience and operation pair is a separate
least-privilege token request.

Success returns `201`:

```json
{
  "access_token": "secret-bearer-token",
  "token_type": "Bearer",
  "expires_in": 300,
  "service_id": "opaque-service-id",
  "audience": "service_management",
  "operations": ["workspace.create", "workspace.read"],
  "credential_generation": 3
}
```

The response token is secret and MUST NOT enter a browser, URL, log, or audit
detail.

## Create a workspace

`POST /v1/services/{service_id}/workspaces`

The request MUST have `Idempotency-Key`, which is an opaque value with 16 to
200 characters.

Request:

```json
{
  "caller_reference": "opaque-xbot-workspace-reference",
  "display_name": "Workspace label"
}
```

`display_name` has 1 to 200 Unicode characters. It is administrator metadata
and MUST NOT be used as identity.

Success returns `201` for the first commit and `200` for an identical replay:

```json
{
  "workspace_id": "opaque-router-workspace-id",
  "caller_reference": "opaque-xbot-workspace-reference",
  "display_name": "Workspace label",
  "state": "active",
  "state_revision": "opaque-state-revision",
  "operation_id": "opaque-operation-id"
}
```

The pair of service and caller reference is unique. A repeated matching
request returns the same workspace and operation. A different request with the
same idempotency key or caller reference returns `409 idempotency_conflict`.

## Read a workspace

`GET /v1/services/{service_id}/workspaces/{workspace_id}`

Success returns `200` with the same workspace object as create. A caller can
use this operation to reconcile uncertain cross-service provisioning.

## Change workspace state

These routes require `Idempotency-Key`:

- `POST /v1/services/{service_id}/workspaces/{workspace_id}/disable`
- `POST /v1/services/{service_id}/workspaces/{workspace_id}/restore`
- `POST /v1/services/{service_id}/workspaces/{workspace_id}/retire`

Request:

```json
{
  "expected_state_revision": "opaque-state-revision",
  "reason": "Short administrator or system reason"
}
```

`reason` has 1 to 500 Unicode characters. Success returns `200` with the
workspace object and a new `operation_id`. Disable changes `active` to
`disabled`. Restore changes `disabled` to `active`. Retire changes `active` or
`disabled` to `retired`. A retired workspace cannot change state.

An identical replay returns the prior result. A stale revision returns
`409 state_revision_conflict` with the current state and state revision. A
state change that is already true under a different idempotency key returns a
new no-change receipt and keeps the state revision.

## Errors

An error is:

```json
{
  "error": {
    "code": "workspace_not_found",
    "message": "The workspace is not available in this service scope.",
    "retryable": false,
    "request_id": "opaque-request-id"
  }
}
```

The public codes are `invalid_request`, `invalid_token`, `insufficient_scope`,
`service_scope_mismatch`, `workspace_not_found`, `idempotency_conflict`,
`state_revision_conflict`, `workspace_retired`, `rate_limited`,
`temporarily_unavailable`, and `internal_error`. A hidden workspace and an
absent workspace use `workspace_not_found`. `rate_limited`,
`temporarily_unavailable`, and `internal_error` can be retryable. A retry MUST
use the same idempotency key and body.

## Audit and retention

Create, disable, restore, and retire write an audit event. Retirement does not
delete request, capture, accounting, configuration, or audit records. Each
record expires under its applicable router retention rule.
