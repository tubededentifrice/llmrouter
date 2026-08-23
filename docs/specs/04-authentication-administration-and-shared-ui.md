# Authentication, administration, and shared UI

Status: Accepted on 2026-08-23.

## Service API keys

A service API key MUST be a backend-only, revocable, long-lived bearer
credential. A backend MUST send it directly on each Router request. The
product MUST NOT exchange it for another token.

One service MAY have several named active keys. A new key MUST contain at
least 256 random bits and MUST be shown only once. The Router MUST store only
a verifier that is sufficient to authenticate it. It MUST NOT put the key in
a URL, browser code, log, activity detail, or response after its creation.

An authenticated service key MUST have all authority for its service's
assignments, workspaces, model calls, embedding calls, media jobs, key
management, and accounting. It MUST NOT read detailed request logs or manage
global services, provider connections, model availability, provider
credentials, or global prices.

A service or global administrator MUST be able to create and revoke a key for
that service. Revocation MUST reject each later request with that key. Key
replacement MUST use create, deploy, and revoke. The product MUST NOT have a
timed rotation or overlap state.

## Global administrator identity

The global administration application MUST use the shared Pocket ID service
through standard OpenID Connect. Deployment configuration MUST contain an
allowlist of Pocket ID subject identities. Only an allowlisted subject MAY
receive a Router administrator session. The Router MUST reject every other
subject.

Each allowed administrator MUST have unrestricted Router administration
authority. The Router MUST NOT have administrator tenants, grants, delegated
scopes, or fine-grained administrator permissions.

The Router MUST use the OpenID Connect authorization code flow with Proof Key
for Code Exchange. It MUST validate the issuer, audience, signature, expiry,
state, nonce, and exact redirect URI. It MUST use the immutable issuer and
subject pair as the human identity. It MUST NOT use an email address or display
name as authority.

An administration return target MUST be one local absolute path. It MUST NOT
be a network-path reference or an absolute URL.

After sign-in, the Router MUST use one server-side local session with logout
and a configurable expiry from 1 hour through 30 days. Activity MUST NOT
extend the absolute expiry. The product MUST NOT require recurring
identity-provider session checks, refresh-token rotation, or a
recent-authentication workflow.

The session cookie MUST be `Secure`, `HttpOnly`, host-only, and
`SameSite=Lax`. A browser write MUST require a session-bound CSRF token and an
exact allowed origin. Logout MUST invalidate the local session.

## Global administration application

The Router MUST host only one global administration application. It MUST let
an administrator:

- create, move, inspect, and delete services;
- inspect and delete workspaces;
- create and revoke service API keys;
- manage provider connections, credentials, models, capabilities, and prices;
- preview and import model catalog entries;
- start price synchronization and inspect its results;
- configure and inspect assignment graphs for a selected service;
- use a model and media playground with an assignment or exact
  provider-model;
- inspect accounting, detailed request logs, media, activity, cooldowns, and
  the small health summary.

Service hierarchy and assignment configuration MUST use a graph and side
inspector as the primary workflow. The application MUST also provide an
accessible table or list with the same records and actions. It MUST show
inherited assignment sources, direct replacement chains, default inheritance,
empty default chains, last use, and observed call requirements.

The application MUST use reusable components, layout, tokens, and interaction
patterns from `../opendle-ui`. Router-specific data and actions MUST stay in
this repository.

## Calling-service administration

The Router MUST provide native backend API endpoints that let an authenticated
service manage its own assignments, workspaces, keys, calls, media jobs, and
accounting.

The Router MUST NOT host a service-user application, service administration
page, cross-origin frame, embed session, or browser grant. A calling service
MUST authenticate and authorize its human user. Its backend MUST call the
Router with a service API key.

A reusable React assignment component MUST live in `../opendle-ui`. Crewday's
assignment interface MUST be the preferred behavior reference. FJ2 MAY supply
other useful behavior, but its non-React implementation MUST NOT be a
component source.

A reusable React playground component MUST also live in `../opendle-ui`. It
MUST support an assignment or exact provider-model and show applicable
controls, input, output, selected route, latency, usage, cost, and a corrective
error. The Router global application and calling-service applications MAY use
the same components.
