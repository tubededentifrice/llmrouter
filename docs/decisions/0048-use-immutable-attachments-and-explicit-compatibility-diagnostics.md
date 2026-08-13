# Use immutable attachments and explicit compatibility diagnostics

## Context

Requests need bounded binary input without mutable external references. The
OpenAI-compatible interface also needs exact diagnostic routing without
changing the normal meaning of `model`.

## Accepted choice

Upload attachments to authenticated Router endpoints. Bind each immutable
object to service and optional workspace scope and verify its media type,
length, and SHA-256 digest. Use opaque identities in requests. Allow plain
text, Markdown, JSON, PDF, JPEG, PNG, WebP, MP3, and WAV. Limit one attachment
to 25 MiB, one request to 20 attachments, and its total attachment content to
100 MiB. For compatible requests, keep `model` as the assignment and use
`x_llmrouter_exact_route` with a write-only
`x_llmrouter_exact_route_grant` for an approved exact route.

## Alternatives

- Accept arbitrary remote URLs or presigned object-store URLs.
- Put binary content directly in every JSON request.
- Encode an exact provider route in the compatible `model` value.

## Good effects

- Request fingerprints identify exact immutable bytes.
- Router access rules protect upload and read operations.
- Compatibility routing remains explicit and does not overload `model`.

## Bad effects

- Attachments require a separate upload lifecycle and cleanup.
- Compatible clients need Router-specific fields for diagnostic routing.

## Migration effect

Official clients must implement attachment upload and the two exact-route
extension fields. The formal contract must publish attachment limits.

## Security effect

Scope checks, digest checks, fixed limits, encryption, and opaque identities
reduce replacement, cross-service read, and resource-exhaustion risks.

## Review conditions

Review this choice if a supported provider needs streaming input that cannot
use the accepted immutable upload lifecycle.
