# Hosted LLM Router administration embed protocol

## Contract

This document defines message version `1` for the service-scoped hosted
administration view. The protocol name is `llmrouter-admin-embed`. It is
independent of the native HTTP API and the Ontology embed protocol.

Every message is a closed JSON object with this envelope:

```json
{
  "protocol": "llmrouter-admin-embed",
  "version": "1",
  "session_id": "opaque-session-id",
  "message_id": "opaque-message-id",
  "type": "frame.ready",
  "payload": {}
}
```

The host and frame MUST use `window.postMessage` with one exact target origin.
Each side MUST compare `event.origin`, `event.source`, session identity, and
message version before it acts. A wildcard target origin is invalid.

## Create an embed session

The host backend calls:

`POST /v1/services/{service_id}/administration/embed-sessions`

The bearer token needs audience `host_backend` and operation
`admin_embed.create`. The host obtains it through the service-token exchange in
[the service-management contract](service-management.md). The browser MUST NOT
call this route.

Request:

```json
{
  "host_user_subject": "opaque-host-user-id",
  "workspace_id": "opaque-router-workspace-id",
  "allowed_origin": "https://xbot.example",
  "permissions": ["configuration.read", "budget.read", "accounting.read"],
  "recent_auth_at": "2026-08-12T12:00:00Z",
  "theme": {
    "mode": "dark",
    "density": "compact",
    "corner_style": "rounded"
  }
}
```

`workspace_id` is optional only for a service-wide permission. Its omission
does not grant workspace data access. Permissions are a non-empty subset of:

- `configuration.read` and `configuration.write`;
- `budget.read` and `budget.write`;
- `accounting.read`;
- `request_status.read`;
- `health.read`;
- `diagnostic.run`.

No embed permission grants captured-content read, global administration,
service creation, workspace creation, credential-secret read, or another
service's data.

`budget.write` can set only a lower Router workspace or assignment limit. It
cannot raise, remove, or replace a host-set workspace ceiling. The host backend
uses the separate `budget_authority` audience for ceiling changes.

`recent_auth_at` is optional for a read-only session. It is required when the
permissions contain `configuration.write`, `budget.write`, or `diagnostic.run`.
The host asserts the time of its latest successful passkey authentication for
this user. It MUST be no more than five minutes old. A sensitive session MUST
expire no later than five minutes after `recent_auth_at`. The frame MUST
request a new host authorization instead of changing to a write permission in
the browser.

Success returns `201`:

```json
{
  "session_id": "opaque-session-id",
  "bootstrap_token": "one-use-secret",
  "frame_url": "https://router.example/service-administration",
  "expires_at": "2026-08-12T12:05:00Z",
  "message_version": "1"
}
```

The frame URL MUST NOT contain the bootstrap token. A read-only embed session
can live for no more than five minutes before renewal. A sensitive session can
live for less time because it cannot outlive the recent-authentication window.

## Bootstrap

1. The host creates the frame from `frame_url`.
2. The frame sends `frame.ready` with a one-use `frame_nonce`.
3. The host validates the exact origin, source window, session, and version.
4. The host sends `host.bootstrap` with `bootstrap_token`, `frame_nonce`, and
   `host_origin`.
5. The frame redeems the token once and sends `frame.bootstrapped` with
   `expires_at`, `service_id`, and optional `workspace_id`.

An uncertain bootstrap result needs a new embed session. The frame erases the
bootstrap token after the redemption attempt.

The frame redeems the bootstrap token with
`POST /v1/administration/embed-sessions/{session_id}/bootstrap`. It sends the
token, `frame_nonce`, and checked `host_origin` in the closed JSON body and
sends its exact Router frame origin in the `Origin` header. The Router returns
only the bounded session scope, permissions, theme, and expiry. It sets a
Secure, HttpOnly, SameSite=None, host-only session cookie. The cookie and the
exact Router frame origin are the authority inputs for later frame API calls.
The frame cannot read the cookie. A mutation from the embedded view MUST use
this origin-bound cookie authority and MUST fail before lookup when the exact
Router frame origin is absent or different.

The host backend can revoke a session with
`DELETE /v1/services/{service_id}/administration/embed-sessions/{session_id}`.
The bearer token needs the same `host_backend` audience and
`admin_embed.create` operation as session creation. Expiry or revocation makes
the embed session cookie invalid immediately.

## Messages

Frame-to-host message types are:

- `frame.ready`: `frame_nonce`;
- `frame.bootstrapped`: `expires_at`, `service_id`, optional `workspace_id`;
- `frame.height_changed`: `height_px`;
- `frame.navigation_changed`: `section`, optional safe `record_id`;
- `frame.configuration_changed`: `revision`, `distribution_state`;
- `frame.session_expired`: `expired_at`;
- `frame.error`: public `code`, `message`, and `retryable`.

Host-to-frame message types are:

- `host.bootstrap`: `bootstrap_token`, `frame_nonce`, `host_origin`;
- `host.navigate`: `section`, optional safe `record_id`;
- `host.theme_changed`: a valid theme object;
- `host.dispose`: an empty payload.

A message MUST NOT contain a service credential, unrestricted token, provider
credential, captured request content, model response, tool content, or hidden
record. The host MUST ignore an unknown message type.

## Workspace and permission change

A workspace switch, user switch, router-permission change, or loss of xbot
membership MUST dispose of the frame and revoke the current embed session. The
host creates a new session only after it repeats current authorization. An open
frame does not keep old authority.

## Errors and compatibility

The public frame error codes are `session_expired`, `origin_mismatch`,
`unsupported_message_version`, `permission_denied`, `workspace_unavailable`,
`revision_conflict`, `temporarily_unavailable`, and `internal_error`.

A compatible version `1` change can add an optional payload field or a new
message type. It cannot change a required field or the meaning of an existing
type. A breaking change needs a new message version. The service MUST support
the prior message version for at least 180 days after it announces a
replacement.
