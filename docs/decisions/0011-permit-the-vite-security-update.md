# Permit the Vite security update

- Status: accepted
- Date: 2026-11-19
- Decision owner: user

## Context

An affected Vite development server can execute code received in an HTTP
request when it is exposed to the network.
[Vite advisory](https://github.com/vitejs/vite/security/advisories/GHSA-p4f5-76q4-ffq7)

## Decision

Install Vite 8.3.7 and its required Rolldown packages before they reach 14
days old. Keep the normal 14-day minimum for other dependency selections.

## Alternatives

Waiting for the releases to reach 14 days would delay the security fix.

## Consequences

The locked releases contain the fix. The normal age rule applies to each
release after it reaches 14 days.
