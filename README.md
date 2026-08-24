# LLM Router

LLM Router is a source-available calling service for the LLM workflows of
other services. It calls models, creates embeddings, and creates generated
media. It selects a named assignment or one exact provider-model and records
usage and cost.

## Status

The user approved the service simplification reset on 2026-08-23. The
specifications in `docs/specs/` define the accepted product. The native API
contracts in `docs/api/` define wire shapes after their interface review.

Implementation and Beads work from the earlier design can still exist during
the reset. They do not change the accepted product boundary.

## Product boundary

The first release has these functions:

- one service parent chain and service-owned workspaces;
- named model assignments with ordered provider-model fallback;
- synchronous and streaming model calls;
- synchronous embedding batches;
- asynchronous image, video, and audio generation jobs;
- global provider connections, model availability, credentials, and prices;
- durable usage and cost accounting;
- short-lived detailed request logs;
- one global administration application;
- unrestricted global administrator playground calls;
- reusable assignment and playground components in OpenDLE UI;
- a Python SDK and multi-turn harness in OpenDLE Lib.

The Router does not run agents or caller tools. It does not host a
calling-service administration page or frame. It does not publish an
OpenAI-compatible API. It does not provide split control-plane and data-plane
roles, durable request recovery for model or embedding calls, or Router-owned
high-availability coordination.

A calling service owns its prompts, workflows, durable conversations, domain
data, user permissions, and tools. Each service API model, embedding, and media
request identifies one workspace that belongs to the authenticated service.

The global playground is the one exception to service call ownership. An
allowlisted administrator session can call an exact global provider-model or
an assignment for one selected service. The selected service supplies only
assignment and inheritance configuration. The call uses no service key or
workspace, and its records remain global administrator-only records.

## Documents

- [Agent instructions](AGENTS.md) define repository safety and work rules.
- [Product direction](docs/product-direction.md) gives the problem, outcome,
  and limits.
- [Architecture](docs/architecture.md) gives the accepted working structure.
- [Specification index](docs/specs/README.md) lists normative product
  behavior.
- [Decision index](docs/decisions/README.md) lists accepted choices.
- [Research index](docs/research/README.md) lists non-normative evidence.
- [Simplification decision source](docs/interviews/service-simplification-2026-08-23.md)
  records the approved reset answers.

## Shared code

Framework-neutral SDK and harness behavior belongs in `../opendle-lib`.
Reusable React components and interaction patterns belong in
`../opendle-ui`. This repository contains Router policy, routes, data, and host
composition only.

## Development

Use `http://127.0.0.1:5174` for administration browser tests. Use local
addresses for all agent checks. The protected
[external site](https://llmrouter.opendle.dev) is for the user.

The simplification reset uses a clean database. The reset command deletes the
local PostgreSQL volume. It keeps the ignored Pocket ID client files,
administrator subject allowlist, and administrator encryption keys.

```bash
./scripts/local-development.sh reset
./scripts/local-development.sh start
./scripts/local-development.sh status
./scripts/local-development.sh logs
./scripts/local-development.sh stop
```

Pocket ID stays enabled for the normal application. For localhost browser
automation, create one 15-minute administrator test session through the real
session store:

```bash
./scripts/local-development.sh test-session
```

The command writes the cookie, CSRF token, origin, and expiry to the ignored
mode-0600 file `.local-development/test-administrator-session.json`. Browser
automation reads this file directly and sets a host-only, HttpOnly,
SameSite=Lax cookie with path `/` for `http://127.0.0.1:5174`. Local loopback
HTTP needs `secure: false`. Administrator writes use `X-CSRF-Token` and the
file's exact origin. The automation must not print or copy the file values. The
backend still applies normal session, CSRF, origin, expiry, and administrator
checks.
Revoke the session after the browser check:

```bash
./scripts/local-development.sh clear-test-session
```

Repository checks use:

```bash
./scripts/check-repository.sh
```

The complete offline product proof resets the local database, uses only the
fake provider adapter, verifies the native SDK and harness, and tests restart
and selected dependency failures. It does not call a paid provider. Run it
only when it is safe to delete the local PostgreSQL and object-storage volumes:

```bash
./scripts/local-development.sh prove
```

The proof checks the Pocket ID discovery, redirect, PKCE, session, and denial
code through the automated identity suite. It does not complete the human
passkey callback.

## Work process

Use the repository `director` skill for autonomous Beads delivery. Keep
calling-service changes in the applicable calling-service repository. Do not
put secrets, private prompts, model responses, or private runtime data in Git.

## License

The license is the Functional Source License, Version 1.1, ALv2 Future License
(`FSL-1.1-ALv2`). Each version changes to Apache License 2.0 on the second
anniversary of the date that version becomes available. See
[`LICENSE.md`](LICENSE.md).
