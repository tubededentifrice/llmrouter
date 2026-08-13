# Logical request fingerprint version 1

## Contract

The fingerprint name is `rfc8785-sha256-v1`. Router resolves the authenticated
context and request fields into one closed provider-neutral document. It
canonicalizes that document with RFC 8785 and calculates SHA-256 before an
external effect starts.

The document contains:

- native or compatibility operation name and major version;
- authenticated service identity and optional workspace identity;
- `data_profile`;
- assignment name, or resolved exact route identity and diagnostic permission
  scope;
- complete messages, content parts, attachment identities, media types, and
  immutable attachment SHA-256 values;
- complete tool definitions and tool allow-list;
- request limits, budget controls, timeout, maximum output, and output controls;
- for an agent run, maximum steps, maximum tool concurrency, and allowed
  business-tool names;
- for a direct shared tool, tool kind and complete provider-neutral input;
- for a compatibility request, every accepted field after its lossless native
  mapping, including sampling, tool choice, response format, metadata, and
  caller `user` value.

The document does not contain:

- a bootstrap secret, bearer token, diagnostic grant secret, administrator
  session, cookie, CSRF value, provider credential, or tool grant secret;
- the logical request identity, authorization headers, or other transport
  headers;
- trace context, node identity, selected provider attempt, receipt time,
  current state, status location, or current accounting;
- an output, provider response, tool result, or another value created after
  admission.

The resolved diagnostic permission scope is fingerprinted. Its bearer value
and grant identity are not. A repeated request can present a renewed grant that
proves the same scope. It cannot change the exact route.

Each top-level OpenAPI request property uses `x-router-fingerprint: true` or
`false`. A `true` document or array property includes its complete recursively
validated value. Authenticated service and workspace context, operation name,
and contract version are required fingerprint context even when transport puts
them outside the JSON body.

## Binding and conflict

The durable binding stores request identity, service, optional workspace,
fingerprint version, and digest. A repeated matching digest returns the
existing admission. A different digest returns `request_identity_conflict`
without earlier request content.

Changing canonicalization, a field inclusion, or field meaning requires a new
fingerprint version. A deployment MUST keep the version needed by every
retained binding.
