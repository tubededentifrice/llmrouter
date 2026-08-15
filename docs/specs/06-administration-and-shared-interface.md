# Administration and shared interface

Status: Accepted on 2026-08-13.

## Global administration identity

LLM Router MUST provide a separate global administration application. It MUST
use a separate control-plane audience and authorization path. A service
credential MUST NOT authenticate to global administration.

LLM Router and the Ontology service MUST use one shared external OpenID Connect
identity service for interactive human authentication. The identity service
MUST own human accounts, passkeys, passkey enrollment and revocation, account
disablement, interactive authentication, and loss-of-passkey recovery. A
person MUST be able to use one identity-service account and its registered
passkeys to authenticate to both applications. The selected identity service
is recorded in decision 0037.

The shared identity service MUST permit passkey authentication only. Open
sign-up, passwords, email sign-in links, email one-time access, social sign-in,
and permanent recovery secrets MUST be disabled. Token-limited sign-up MUST
remain enabled only for a short-lived invitation that an identity
administrator issues for one account's first passkey. An identity
administrator MAY instead create the account directly. Recovery MUST start
from a trusted server console, MUST be one-use and time-limited, MUST revoke
applicable identity sessions, and MUST require a new passkey before normal
authentication resumes. The identity service MUST audit account, passkey,
invitation, disablement, and recovery operations.

The deployment MUST use an exact Pocket ID version and immutable image digest.
The version MUST be at least 2.6.0, MUST be at least 14 complete days old when
selected, and MUST contain all published critical and high-severity security
fixes that apply to this configuration. An upgrade MUST pass the OpenID
Connect, enrollment, recovery, revocation, and token-confusion conformance
tests before production use.

A person MUST be able to register more than one passkey, name each passkey, and
revoke one passkey from the shared identity account page after recent
authentication.

LLM Router MUST be a separate confidential OpenID Connect client with its own
exact redirect URIs and token audience. It MUST use the authorization code flow
with Proof Key for Code Exchange. It MUST validate the issuer, audience,
signature, expiry, state, nonce, and exact redirect URI. It MUST identify a
human by the immutable issuer and subject pair. It MUST NOT use an email
address, display name, or group name as the stable identity.

The shared identity service authenticates a human. It MUST NOT own the LLM
Router service tree, workspaces, administrator grants, permissions, provider
credentials, budgets, or application audit records. LLM Router MUST keep its
own administrator record, authorization grants, server-side sessions, and
audit records. A new shared account MUST have no LLM Router authority until an
eligible LLM Router administrator grants it. An identity-service group MAY
limit who can start LLM Router authentication, but it MUST NOT expand a local
grant or workspace boundary.

An operator MUST create the initial LLM Router administrator grant from a
trusted LLM Router server console. The CLI command MUST create a random,
short-lived, one-use grant URL and MUST store only its verifier. The person who
redeems the URL MUST authenticate through the shared identity service. LLM
Router MUST bind the returned issuer and subject pair to the local global-
administrator grant. The same flow MAY recover local administration when no
eligible LLM Router administrator remains. The URL MUST NOT create or recover
an identity-service account or passkey. Creation, redemption, success, and
failure MUST create LLM Router audit events.

An administrator session MUST have an idle expiry of no more than 15 minutes
and an absolute expiry of no more than 8 hours. A sensitive action MUST require
an identity-service authentication no more than five minutes old. The service
MUST validate the authentication time and current account state. Account
disablement, passkey recovery, or central session revocation MUST make each
applicable LLM Router administrator session unusable within five minutes and
before its next sensitive action. If the identity service is unavailable, LLM
Router MUST reject new administrator sessions and sensitive actions. An
existing session MAY continue non-sensitive actions only while its last
account-state check is no more than five minutes old. Local logout MUST revoke
the local administrator session immediately. Account disablement, passkey
revocation, and recovery MUST also revoke applicable identity-service sessions.

The local administrator session cookie MUST use `Secure`, `HttpOnly`,
`SameSite=Lax`, a `__Host-` name, and no `Domain` attribute. Each administrator
write MUST require a session-bound CSRF token in `X-CSRF-Token` and an exact
allowed `Origin`. The server MUST reject a missing or mismatched token or
origin before it changes state. An OIDC callback MUST validate the stored
state, nonce, Proof Key for Code Exchange verifier, issuer, and exact redirect
URI before it creates the local session.

The global administration application MUST provide a clear link to the shared
identity account page for passkey and account management. It MUST NOT copy the
identity service's account or passkey management functions into LLM Router.
Service bootstrap credentials and short-lived service tokens remain under the
rules in specification 04 and MUST NOT become human identity credentials.

LLM Router MUST authorize each administrator action through an explicit local
grant. A grant MUST identify the human issuer and subject, authority class,
allowed service and workspace scopes, allowed operations, creation and expiry
times, and revision. A global administrator MAY delegate only an operation and
scope that its current grant permits. A service administrator MUST NOT create a
global grant or expand authority beyond its service and eligible descendants.

The formal contract MUST classify sensitive actions. Secret management,
content reads and exports, global grant changes, service parent changes,
promotion, failback, restore, and security-policy changes MUST require recent
authentication. Grant creation, change, revocation, denial, and use of a
sensitive action MUST create audit events.
This model follows [decision 0046](../decisions/0046-use-least-privilege-grants-and-global-secret-custody.md).

## Hosted service interface

LLM Router MUST host one administration application. A service MUST be
able to embed its service-scoped administration view in an isolated,
cross-origin frame. A service MUST also be able to build its own interface with
the headless, versioned API.

The frame integration MUST use the same base security model as the Ontology
hosted explorer:

- an exact host and frame origin allow-list;
- a short-lived embed session scoped to one service, eligible workspaces, host
  user subject, permitted actions, origin, and expiry;
- a one-use bootstrap token that is not in the frame URL;
- an origin and source-window handshake before bootstrap redemption;
- a narrow, versioned message protocol;
- no service credential or unrestricted token in browser code;
- validated theme tokens and no arbitrary host CSS or script;
- independent versions for the frame protocol and HTTP API.

LLM Router MUST use its own frame protocol name and version. It MUST NOT reuse
Ontology message types for different actions.

The embedded service view MUST NOT expose global administration functions. The
global administration application can use the same frontend codebase, but it
MUST use the separate global administrator authority.

The host service owns authentication and authorization for a person who opens
its embedded service view. The person MUST NOT need a Pocket ID session only
to use that host-authorized service view. The host backend MUST mint the embed
session only after it checks the current host session and the separate router-
administration permission. Pocket ID remains the authentication path for the
global LLM Router application.

For a host that uses one current workspace, the embed session MUST contain
only that workspace. A host workspace switch MUST dispose of the current frame
and session before it creates a session for the new workspace. A service-wide
router administrator MAY receive a service-scoped session with no workspace
data access for service-level configuration. The service view MUST show the
current service and workspace scope on each page.

The first-release service view MUST expose only effective configuration,
assignments, provider and route status, budgets, accounting summaries,
request status, and safe diagnostics that its grant permits. It MUST NOT expose
captured request or tool content.

A host can create a read-only embed session from its current authorized user
session. A session with configuration-write, budget-write, or diagnostic-run
permission MUST contain a host-asserted passkey authentication time that is no
more than five minutes old. It MUST expire no later than five minutes after
that authentication. The frame MUST NOT expand a read-only session after a
browser-only action.

## Operational graph state

The provider and assignment graph MUST be the primary provider, model,
provider-model route, and assignment administration workflow. It MUST use
searchable graph navigation with a side inspector for view and edit actions.
It MUST show effective inherited state without
requiring an administrator to reconstruct it from parent scopes. For each
eligible provider, provider-model route, and assignment, it MUST show current
availability and normalized recent failure indicators when data is available.

The interface MUST also provide an accessible table representation with the
same permitted records, status, and actions. The table MUST support keyboard
navigation, narrow screens, sorting, filtering, and bulk inspection. A graph
layout MUST NOT be the only way to find or change an item.

Authentication, policy, budget, rate-limit, availability, and request-
compatibility failures MUST have different visible states. A detail view MUST
show whether the router retried, used the next fallback, or stopped the logical
request. It MUST show the affected service, workspace when permitted,
assignment, provider-model route, configuration revision, count, last event,
and a redacted diagnostic summary.

Persistent provider authentication failures and repeated assignment-wide
failures MUST produce an administrator alert. Provider-specific errors MUST
NOT make a healthy fallback appear unhealthy. A service administrator MUST
see only its service, descendants it can administer, and eligible workspaces.

Configuration forms MUST publish each valid save immediately. The interface
MUST show validation errors before it reports success and MUST show the new
active revision and distribution state after success. It MUST NOT require a
draft, approval, canary, or promotion workflow.

## Interface starting point

The visual administration prototype in [`apps/admin/`](../../apps/admin/) is
the starting point and reference for the global and service administration
application. It demonstrates the intended design direction and the graph,
table, inspector, scope, status, and responsive interaction patterns. The
prototype is not a complete product contract. Keep detailed interface work in
the implementation task and in the application as it develops.

## Headless operational administration

The versioned headless administration API MUST expose every operational action
that the hosted application can perform. This includes topology and node state,
node drain, provider-circuit probe and reset, replication and spool state,
promotion, failback, backup start, restore validation, and disaster-recovery
test results.

Each write MUST require the same local grant, recent authentication, expected
revision or operation precondition, CSRF and origin controls for a browser
session, idempotency behavior, and audit event as the hosted action. A headless
route MUST NOT create a second authority path or accept a service credential
for a global operation.
This API follows [decision 0049](../decisions/0049-proxy-protected-exports-and-version-operations.md).
