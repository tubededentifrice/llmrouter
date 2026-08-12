# Ontology administration alignment

Date: 2026-08-12

Status: Research and follow-up note. This document does not change Ontology.

## Hosted interface

LLM Router uses the same base embed model as the accepted Ontology decision in
`../ontology/docs/decisions/0008-framework-neutral-ontology-view.md`:

- a service-hosted cross-origin frame;
- an exact-origin and source-window handshake;
- a short-lived scoped embed session;
- a one-use bootstrap token that is not in the frame URL;
- a narrow versioned message protocol;
- validated theme tokens;
- a headless HTTP alternative.

Each service needs its own protocol namespace because the data and actions are
different.

## Passkey administration

Ontology already requires passkey-only administration, no password, no email
sign-in link, no social sign-in, and a one-use server-console recovery process
in `../ontology/docs/specs/05-administration.md`.

Ontology does not yet define the exact initial passkey enrollment mechanism.
The recommended shared operator experience is:

1. Run a service CLI command on a trusted server console.
2. Receive a random, short-lived, one-use enrollment URL.
3. Redeem the URL into a short-lived enrollment ceremony.
4. Register the initial or recovery passkey with user verification.
5. Audit the command, redemption, result, and administrator identity when it
   becomes known.

The URL is a bootstrap mechanism. It is not an email magic-link sign-in method.
After URL redemption, a short-lived server-side ceremony can issue new
one-use WebAuthn challenges if the browser needs to retry registration.

A later Ontology specification change should add this initial-enrollment flow
without changing its accepted passkey-only identity decision.
