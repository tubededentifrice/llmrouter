# Supply a hosted frame and headless administration API

- Status: accepted
- Date: 2026-08-12
- Decision owner: user

## Context

Crewday uses React and FJ2 does not. Copying the LLM graph and management pages
would create different behavior and repeated work.

## Decision

Host the common React administration interface in LLM Router. Let a service
embed a service-scoped view in an isolated cross-origin frame. Use the same
base security model as the planned Ontology explorer: exact origins, a
short-lived scoped embed session, a one-use bootstrap handshake, a narrow
versioned message protocol, validated theme tokens, and no client secret in
the browser.

Also provide a headless versioned API. Use an LLM Router protocol namespace,
not the Ontology protocol namespace.

## Alternatives

- Publish a custom element. This improves host-page integration but has more
  CSS, dependency, and upgrade risk.
- Build separate service interfaces. This gives full host control but
  duplicates fixes and causes release drift.

## Consequences

- One deployment supplies the shared interface to React and non-React hosts.
- Frame, origin, content-security-policy, accessibility, and token tests are
  required.
- Hosts that need a special interface can use the headless API.
