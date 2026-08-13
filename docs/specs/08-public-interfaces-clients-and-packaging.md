# Public interfaces, clients, and packaging

Status: Accepted on 2026-08-13.

## Native API

LLM Router MUST provide a native, versioned HTTP API as its primary public
contract. The repository MUST publish a formal OpenAPI contract for HTTP
operations and separate formal contracts for stream or frame protocols when
OpenAPI cannot express them completely.

The native API MUST represent assignments, exact diagnostic routes, logical
requests, provider attempts, agent runs, tool calls, status recovery,
cancellation, accounting, configuration revisions, and administration without
requiring provider-specific fields.

Before version 1.0, a release MAY make a documented breaking public-contract
change. It MUST include migration notes. After version 1.0, a breaking public
contract change MUST use a new major version.

## OpenAI-compatible interface

LLM Router MUST provide an OpenAI-compatible interface for accepted common
model operations and migration. The compatibility interface MUST use the same
authentication, service and workspace isolation, assignment resolution,
provider retry, fallback, budgets, accounting, and logging as the native API.

The compatibility `model` field MUST select a named assignment by default. An
exact provider-model value MUST use the explicit
`x_llmrouter_exact_route` extension and MUST also supply a short-lived,
write-only `x_llmrouter_exact_route_grant`. The `model` field MUST remain the
named assignment. Encoding an exact route in `model` MUST be invalid. The
compatibility interface MUST NOT make an exact route easier to access than the
native interface.
This extension follows [decision 0048](../decisions/0048-use-immutable-attachments-and-explicit-compatibility-diagnostics.md).

The contract MUST document supported endpoints, fields, stream events, tool
calls, errors, and extensions. It MUST reject an unsupported field when
ignoring it could change cost, safety, routing, or result meaning.

The native API remains the source contract for router-specific behavior. The
compatibility interface MUST NOT weaken or hide request identity, interrupted
state, or accounting.

## Official clients

The first release MUST provide official Python and TypeScript clients. The
clients MUST implement service-token exchange and renewal, local-first node
selection, client-generated UUIDv7 request identity, admission-receipt
handling, safe retry before and after admission, 24-hour terminal status
recovery, streaming, best-effort cancellation states, and protocol
compatibility checks.

The clients MUST create the UUIDv7 when an intentional logical request is ready
for its first submission. They MUST NOT reuse an expired identity. They MUST
send the same provider-neutral request fields when they repeat a submission.

The clients MUST support immutable attachment creation, content upload, scoped
read, and request references. They MUST verify the local byte length and SHA-256
digest before upload, preserve the same attachment identity and digest on a
repeat submission, and never replace an attachment after admission.

The clients MUST treat 120 seconds as the maximum provider-attempt timeout and
15 minutes as the maximum logical execution period. A client transport timeout
MUST NOT create a new logical request. The client MUST use the original request
identity for status recovery or a matching repeat submission.

The clients MUST NOT contain provider retry or fallback policy. LLM Router owns
that behavior after admission.

The TypeScript client MUST separate server and browser entry points. The
browser entry point MUST NOT accept a service bootstrap secret or unrestricted
service token. Browser operations MUST use an eligible short-lived browser,
embed, or administrator session.

The server clients MUST support idempotent service-workspace create, read,
disable, restore, and retire operations. They MUST preserve the caller
reference and idempotency key across an uncertain retry. They MUST NOT treat a
retire operation as deletion of retained router records.

After version 1.0, a normal server release MUST support the current and
previous minor versions of each official client. The repository MUST publish a
compatibility table.

## Deployment image

LLM Router MUST publish one immutable container image. The same image MUST run
in these configured roles:

- combined;
- control-plane;
- data-plane;
- worker.

Role selection MUST NOT change the public contract or artifact version. A
deployment MUST be able to disable routes and capabilities that do not belong
to the selected role.

The first stable deployment documentation MUST include a production Compose
configuration. Kubernetes manifests or a Helm chart MUST follow after the
first stable release. Container references in deployment examples MUST use an
immutable digest or an exact version tag according to repository policy.
