# Identity, credentials, and tool gateway

Status: Accepted on 2026-08-13.

## Service bootstrap and access

A global administrator MUST create a bootstrap credential for one service. The
administration application MUST show its secret once. LLM Router MUST store
only a verifier that is sufficient to check the secret.

The service MUST use the bootstrap secret only at a server-to-server exchange
operation. The exchange MUST return a short-lived access token with exact
service, workspace limits when applicable, operations, audience, expiry, and
credential-generation claims. The requested token scope MUST be equal to or
smaller than the bootstrap credential scope.

A bootstrap secret MUST NOT call a normal model, agent, tool, accounting, or
administration operation directly. The service client SHOULD cache and renew a
short-lived access token without sending the bootstrap secret on each request.

Credential creation, scope expansion, rotation, and revocation MUST require
recent global administrator authentication and MUST create audit events.
Rotation MUST support a bounded overlap period. Revocation MUST stop new token
exchanges immediately.

A bootstrap secret MUST contain at least 256 bits from a cryptographically
secure random source. Its printable value MUST use unambiguous base64url
encoding. The router MUST store an Argon2id verifier with a unique salt and
deployment-approved cost parameters. It MUST NOT store the secret or a
reversible form.

A short-lived service token MUST be an opaque bearer value with at least 256
random bits. The router MUST store only a keyed digest. The token lifetime
MUST be 5 minutes. The server MAY accept no more than 30 seconds of clock skew
for expiry checks. A token MUST contain or bind to one issuer, one audience,
one service, optional allowed workspaces, exact operations, issue and expiry
times, one token identity, and one bootstrap credential generation. It MUST
be invalid when its credential generation is revoked.

Bootstrap rotation MUST create a new generation and MAY keep the prior
generation valid for a selected overlap of 0 to 24 hours. The default overlap
is 24 hours. The administration interface MUST show the exact end time and MUST
let an administrator end the overlap early. A token from an old generation
MUST NOT outlive that generation. Revocation has no overlap.
This overlap follows [decision 0041](../decisions/0041-allow-24-hour-service-secret-rotation-overlap.md).

The wrapping key and token-digest key MUST be separate deployment secrets.
They MUST be at least 256 random bits, MUST NOT be stored in the database or
Git, and MUST support staged rotation. A key-custody loss MUST fail closed for
token exchange and credential decryption and MUST produce an operator-visible
security state.

LLM Router MUST support mutual TLS as an optional additional machine control.
A deployment MAY require mutual TLS for selected services or routes. A mutual
TLS identity MUST be bound to the same service identity and MUST NOT expand the
token scope.

The production mutual TLS profile MUST use TLS 1.3, a private deployment trust
anchor, server and client certificate validation, and revocation or a bounded
certificate lifetime of no more than 24 hours. A certificate identity MUST
map to one registered service and credential generation. The router MUST
reject a certificate for a different service before it checks operation scope.

## Built-in provider credential store

LLM Router MUST provide one built-in encrypted store for provider and shared
external-tool credentials. The first release MUST NOT support references to an
external credential manager. A provider instance MUST refer to a credential
record by stable identity and MUST NOT contain plaintext credential material.

The store MUST use envelope encryption. The wrapping key MUST come from a
deployment secret that is outside the database and repository. The system
MUST support wrapping-key rotation and credential rotation without exposing a
stored plaintext value. A backup without the applicable wrapping key MUST NOT
be sufficient to decrypt credentials.

Only a global administrator with an explicit secret-management grant and
recent authentication MAY create, replace, disable, or retire a credential.
A service administrator MAY select an eligible credential reference for a
service-owned provider instance, but it MUST NOT manage secret material.
This rule follows [decision 0046](../decisions/0046-use-least-privilege-grants-and-global-secret-custody.md).
Secret input MUST be write-only. The interface MUST NOT
echo the submitted value or show stored credential material. It MAY show safe
metadata, such as owner, provider, creation time, rotation state, and a short
fingerprint. Each change MUST require recent administrator authentication and
create an audit event.

A data-plane node MUST receive only credentials needed for its active routes.
It MAY keep decrypted material in process memory for a bounded time. It MUST
NOT write plaintext credentials to its spool, logs, diagnostics, configuration
snapshot, or object storage. Rotation, disablement, and revocation MUST use the
urgent distribution path and invalidate applicable cached material.

## Registered business-tool gateway

Each service MAY register one private business-tool gateway. A global
administrator MUST approve the endpoint origin and network policy. A service
administrator MAY update its gateway within the global policy.

An agent request MUST NOT supply an arbitrary callback URL. LLM Router MUST
call only the registered gateway for the authenticated service.

For each business-tool call, LLM Router MUST send a short-lived, one-use,
run-scoped tool grant. The grant MUST be bound to the service, workspace, run,
owner epoch, tool identifier, permitted operation, request fingerprint, and
expiry.

The calling service MUST check the grant and its current user, workspace,
approval, tool permission, and record state when the tool runs. A catalog entry
or earlier check MUST NOT replace this execution-time authorization.

The gateway MUST return a bounded result envelope. LLM Router MUST treat the
result as untrusted model input. It MUST apply size, type, time, and content
limits before the next model call.

The gateway registration, call, result, error, and reconciliation documents
MUST use a formal, closed, versioned contract. A registration MUST declare the
supported contract major versions and tool kinds. A call MUST identify the
contract version, operation identity, service, workspace when supplied, run,
owner epoch, tool, input schema, deadline, and one-use grant. A result MUST
identify the same operation, contract version, final state, result schema, and
effect state.

The gateway MUST reject an unknown field, version, tool kind, result kind, or
reused grant. An unconfirmed effect MUST use the formal reconciliation
operation. The router and gateway MUST NOT infer success from a transport
timeout or repeat an effect without a confirmed safe result.
This contract follows [decision 0045](../decisions/0045-use-source-driven-adapters-with-registered-contracts.md).
