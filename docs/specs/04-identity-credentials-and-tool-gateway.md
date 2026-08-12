# Identity, credentials, and tool gateway

Status: Accepted sections only. Token formats, lifetimes, and mutual TLS
deployment profiles remain open.

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

LLM Router MUST support mutual TLS as an optional additional machine control.
A deployment MAY require mutual TLS for selected services or routes. A mutual
TLS identity MUST be bound to the same service identity and MUST NOT expand the
token scope.

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
