# Administration and shared interface

Status: Accepted sections only. Detailed workflows and permissions remain
open.

## Global administration identity

LLM Router MUST provide a separate global administration application. It MUST
use a separate control-plane audience and authorization path. A service
credential MUST NOT authenticate to global administration.

Interactive global administrator authentication MUST use passkeys only. The
service MUST NOT provide public sign-up, password sign-in, email sign-in links,
social sign-in, or a permanent recovery secret.

An operator MUST create initial administrator enrollment from a trusted server
console with an LLM Router CLI command. The command MUST create a random,
short-lived, one-use enrollment URL. The service MUST store only a verifier for
the enrollment secret. The secret MUST NOT be in logs. The URL MUST be
redeemable once into a short-lived server-side enrollment ceremony. The URL
MUST be invalid after redemption or expiry. The ceremony can issue a new
one-use WebAuthn challenge when a registration attempt needs a retry.

The same CLI mechanism MUST support loss-of-passkey recovery. Recovery MUST be
one-use, time-limited, and audited. It MUST revoke applicable administrator
sessions and require new passkey enrollment before normal administration can
continue.

The initial enrollment and recovery model SHOULD remain aligned with the
Ontology service. Each service has its own relying-party identifier, origin,
administrator records, sessions, and audit records.

## Hosted service interface

LLM Router MUST host one React administration application. A service MUST be
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
global administration application can use the same React codebase, but it MUST
use the separate global administrator authority.
