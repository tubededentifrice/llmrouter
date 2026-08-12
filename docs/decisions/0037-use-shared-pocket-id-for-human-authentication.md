# Use shared Pocket ID for human authentication

- Status: accepted
- Date: 2026-08-12
- Decision owner: user
- Supersedes: the human identity part of decision 0003

## Context

LLM Router and Ontology need passkey-only administrator authentication. The
same small operator group administers both applications. Separate account and
passkey stores make each person enroll twice and make account disablement,
passkey changes, and recovery separate tasks.

Service trees, workspaces, administrator grants, and machine credentials are
product authorization data. They are not human authentication data.

## Decision

Use one self-hosted Pocket ID deployment as the shared external OpenID Connect
identity service for LLM Router and Ontology. Pocket ID owns human accounts,
passkeys, initial passkey enrollment, passkey changes, account disablement,
interactive authentication, and loss-of-passkey recovery.

Disable public sign-up, passwords, email sign-in and one-time access, social
sign-in, and permanent recovery secrets. A central identity administrator can
create accounts and one-use invitations. Trusted server-console access starts
recovery. A user manages all registered passkeys at the central Pocket ID
account page.

Configure LLM Router and Ontology as separate confidential OpenID Connect
clients. Each client has exact redirect URIs and a separate audience. Each
application uses the authorization code flow with Proof Key for Code Exchange
and creates its own server-side session. The immutable issuer and subject pair
links the same human to local administrator records in both applications.

Pocket ID authenticates humans only. LLM Router continues to own its service
tree, workspaces, administrator grants, permissions, machine credential
exchange, sessions, and application audit. Ontology owns the equivalent
Ontology records. Creating a Pocket ID account does not grant access to either
application.

Each application keeps a trusted server-console flow for its initial local
administrator grant and for loss of all local administrator grants. The person
redeems a short-lived, one-use application URL and authenticates with Pocket
ID. This flow binds the Pocket ID issuer and subject to a local grant. It does
not create or recover a Pocket ID account or passkey.

## Alternatives

- Keep a separate passkey store in each application. This reduces the shared
  failure scope, but it duplicates enrollment, recovery, account disablement,
  protocol code, and security maintenance.
- Build a custom shared identity service. This gives implementation control,
  but it makes the project responsible for WebAuthn, OpenID Connect, sessions,
  recovery, signing keys, and identity security fixes.
- Use a larger identity and access management product. This gives more
  federation and policy features, but it adds operation and configuration work
  that the current operator group does not need.

## Consequences

- A person enrolls once and uses the same account and passkeys for both
  applications.
- Account and passkey management has one central user interface.
- Each application still makes and audits its own authorization decisions.
- Pocket ID availability is required for new administrator sessions, recent
  authentication, and identity changes. Existing non-sensitive actions have
  only the bounded status-cache period in the product specification.
- A Pocket ID compromise can affect authentication to both applications. Its
  administration, signing keys, backups, network policy, and recovery process
  need the same protection as the two control planes.
- Implementation must select an exact Pocket ID version that is at least 14
  complete days old and must use an immutable image reference.

## Migration effect

There is no deployed LLM Router identity store to migrate. Implementation must
remove the planned LLM Router WebAuthn enrollment and recovery functions and
replace them with direct OpenID Connect integration and a link to the shared
identity account page. Local administrator grants continue to be LLM Router
records.

## Security effect

The design removes two custom WebAuthn implementations. It also creates one
high-value shared identity dependency. Conformance tests must cover issuer and
audience validation, Proof Key for Code Exchange, state and nonce validation,
recent authentication, account disablement, passkey recovery, local session
revocation, provider outage, and token confusion between the two clients.

Identity-service groups can restrict client admission. They do not replace
local authorization or expand a service or workspace boundary.

## Review conditions

Review this decision if Pocket ID cannot meet the required recent-
authentication and revocation behavior, if the deployment needs external
enterprise federation or public customer accounts, if identity-service
availability prevents control-plane objectives, or if more applications need
a different shared identity lifecycle.
