# API errors

<!-- contract:errors -->

All non-stream failures use the `ErrorEnvelope` schema. A stream failure uses
the same object as the `error` event data.

Stable error names are:

- `authentication_required`: The request has no valid service key or administrator session.
- `permission_denied`: The authenticated actor cannot do the operation.
- `invalid_request`: The request does not match the contract or a field rule.
- `not_found`: The requested resource does not exist in the actor scope.
- `conflict`: The requested change conflicts with an existing resource or relationship.
- `assignment_cycle`: The assignment inheritance would contain a cycle.
- `provider_unavailable`: No eligible provider-model can accept the call.
- `upstream_failed`: The selected provider-model failed.
- `content_unavailable`: Media content is not ready or is no longer available.
- `rate_limited`: A configured request rate was exceeded.
- `internal_error`: The Router could not complete the operation.

The `message` is safe for the caller. The optional `details` value uses a
closed schema. It can identify a field and give a safe reason. It must not
contain a credential, prompt, response, tool value, or provider secret.
