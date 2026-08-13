# Public error and retry contract

## Envelope

All non-streaming `/v1` errors use this closed JSON object:

```json
{
  "error": {
    "code": "workspace_unavailable",
    "message": "The workspace cannot accept new work.",
    "retryable": false,
    "request_id": "opaque-request-id",
    "retry_after_ms": null,
    "field_errors": []
  }
}
```

`message` is safe for an administrator. It MUST NOT contain a prompt, model
response, tool value, credential, hidden identity, or provider secret.
`field_errors` contains closed objects with `path`, `code`, and `message`.
The server includes it only for safe request or configuration validation.

`retryable` states whether the same authenticated operation can be tried
again. It does not give permission to create a new logical request. When
`retryable` is true and `retry_after_ms` is present, the client MUST wait at
least that long and apply bounded jitter.

An HTTP `Retry-After` header uses whole seconds. When the response has both
forms, the header value MUST be the ceiling of `retry_after_ms / 1000`. A client
MUST use the longer wait if an invalid intermediary changes one value.

## Codes

| HTTP | Code | Retry | Meaning and client action |
| --- | --- | --- | --- |
| 400 | `invalid_request` | No | Fix the stated fields. |
| 400 | `unsupported_contract` | No | Use a supported API, stream, or message major version. |
| 400 | `unsupported_capability` | No | Remove the operation or field, or use a deployment that declares it. |
| 401 | `invalid_token` | No | Obtain a new scoped token. Do not repeat with the same token. |
| 401 | `recent_auth_required` | No | Complete a new passkey check and create a new session. |
| 403 | `insufficient_scope` | No | Request only through an authority that has the exact operation. |
| 403 | `service_scope_mismatch` | No | Do not retry across a different service. |
| 403 | `workspace_scope_mismatch` | No | Do not retry across a different workspace. |
| 403 | `policy_denied` | No | Change the policy or request. Provider fallback is not allowed. |
| 403 | `diagnostic_permission_required` | No | Use a current diagnostic grant or an assignment. |
| 404 | `not_found` | No | The record is absent or hidden from this scope. |
| 404 | `request_not_found` | No | The logical request is absent, hidden, or its status retention expired. |
| 404 | `workspace_not_found` | No | The workspace is absent or hidden from this service. |
| 409 | `idempotency_conflict` | No | Use the original body or a new identity for new work. |
| 409 | `state_revision_conflict` | No | Read current state and submit an intentional new change. |
| 409 | `configuration_revision_conflict` | No | Read the active revision before another write. |
| 409 | `request_identity_conflict` | No | The request identity is bound to a different fingerprint. |
| 409 | `stream_replay_unavailable` | No | Read request status because the requested stream events expired. |
| 409 | `terminal_state` | No | The operation cannot change a terminal request or retired record. |
| 409 | `budget_ceiling_conflict` | No | A subordinate limit exceeds the host-set ceiling. |
| 410 | `request_identity_expired` | No | Create a new UUIDv7 only for intentional new work. |
| 410 | `workspace_retired` | No | A retired workspace cannot return to service. |
| 422 | `assignment_unavailable` | No | Fix configuration or select another eligible assignment in a new request. |
| 422 | `workspace_unavailable` | No | Restore the authorized disabled workspace before new work. |
| 422 | `capability_mismatch` | No | The assignment cannot meet the required provider-neutral capability. |
| 422 | `budget_exhausted` | No | Wait for an authorized budget reset or change. Do not retry automatically. |
| 422 | `secret_detected` | No | Remove the control secret before submission. |
| 429 | `rate_limited` | Yes | Repeat the same operation after `retry_after_ms`. |
| 503 | `temporarily_unavailable` | Yes | Repeat the same operation or recover its status with the same identity. |
| 503 | `stale_configuration` | Yes | Wait until a valid revision reaches an eligible node. |
| 503 | `spool_capacity_exhausted` | Yes | Wait for ledger delivery and repeat the same operation. |
| 503 | `allowance_unavailable` | Yes | Wait for a valid budget allowance. |
| 500 | `internal_error` | Yes | For an admitted request, read status or repeat with the same identity. |

The server MAY add a code only in a compatible minor contract when its HTTP
status and retry rule match an existing class. A client MUST treat an unknown
code by its HTTP status and `retryable` value. A new code MUST NOT make an
unsafe retry appear safe.

## Provider and terminal errors

A terminal request can contain one of these normalized classes in its status:
`authentication`, `policy`, `budget`, `rate_limit`, `timeout`, `transport`,
`provider_unavailable`, `invalid_provider_response`, `incompatible_request`,
`cancelled`, `uncertain_effect`, or `router_internal`.

The status also contains the known affected scope: `attempt`,
`provider_model_route`, `provider_instance`, `credential`,
`assignment_candidate`, or `logical_request`. Provider codes and details are
optional and MUST be redacted. A terminal error is not an instruction to make
a new logical request.

## Retry ownership

Before admission, the official client can retry token exchange, endpoint
selection, and the same create operation when no response is known. It MUST
keep the same idempotency key and request body.

After admission, the client MUST use the same logical request identity. It can
repeat the identical submission, read status, reconnect to the event stream,
or request cancellation. It MUST NOT create a replacement identity
automatically. Router provider retry and fallback remain internal.
