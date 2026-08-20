# LLM Router

LLM Router is a planned source-available service for internal applications. It
will give applications one shared interface for model routing, provider
failover, agent execution, common tools, request accounting, and
administration.

The first calling services are Crewday, FJ2, and Xbot. This repository does not
change those services during the specification phase.

## Status

This repository contains an accepted specification set and no product
implementation. The user accepted the specification set and enabled Beads
implementation planning on 2026-08-13. Open implementation decisions stay as
explicit blocker tasks and must close before affected implementation starts.

## Accepted product boundary

The first release provides these shared functions:

- ordered service and workspace configuration;
- provider, model, assignment, and fallback selection;
- synchronous, streaming, and agent-run request handling;
- a controlled catalog of common tools, such as web search and extraction;
- local routing nodes with failover to other nodes;
- request logs, usage accounting, cost records, audit records, and retention;
- a global administrator application;
- service-scoped administration views that work in React and non-React hosts.

The agent harness is complete but optional for each service. A service can use
routing, streaming, failover, accounting, and shared tools without using the
harness.

The calling service continues to own its domain rules, prompts, workflows,
user permissions, business tools, and the choice of which tools an agent can
use.

## Repository map

- [Agent instructions](AGENTS.md) define safety, planning, Git, and review
  rules.
- [Product direction](docs/product-direction.md) records the current problem,
  goals, and limits.
- [Architecture](docs/architecture.md) records a working architecture model.
- [Architecture interview](docs/interviews/architecture-interview.md) contains
  the decisions that need user review.
- [Specification index](docs/specs/README.md) controls normative documents.
- [Decision index](docs/decisions/README.md) controls accepted decisions.
- [Research](docs/research/README.md) records source-service evidence.
- [Repository checks](scripts/check-repository.sh) verify the base structure.

## Development site

Start the complete localhost development deployment from a clean checkout:

```bash
./scripts/local-development.sh start
```

The command creates mode-0600 local database credentials in the ignored
`.local-development/` directory. It starts PostgreSQL, applies all migrations,
starts the backend, starts the administration UI, and starts the embed example.
It also starts a local worker that renews the short-lived example host token.
The command waits for readiness before it returns. No service publishes a
non-loopback host port.

Use these local addresses:

- Backend readiness: `http://127.0.0.1:8010/ready`
- Administration: `http://127.0.0.1:5174`
- Router frame: `http://127.0.0.1:5175`
- Embed example: `http://127.0.0.1:5176`

The protected external site is
[https://llmrouter.opendle.dev](https://llmrouter.opendle.dev). Agents MUST use
the local URLs. Do not use the protected external URL for agent tests.

The [administration embed example](apps/embed-example/README.md) proves the
service-scoped frame from a distinct localhost origin. It keeps the host
service token in the example server process.

The local database uses a named volume. Normal stop and start operations keep
its data. A reset removes the local database volume and all local application
volumes. It does not remove the ignored secret files. Use these commands:

```bash
./scripts/local-development.sh status
./scripts/local-development.sh logs
./scripts/local-development.sh stop
./scripts/local-development.sh reset
```

The deployment does not read an OpenRouter key from the shell or store it in a
file. After administrator authentication is configured, enter the key only in
the write-only provider credential control. Do not put the key in Compose, a
command, a fixture, or a repository file.

## Work process

1. Use the repository `director` skill to complete one ready Beads task.
2. Split work that has independent acceptance results or rollback boundaries.
3. Resolve each blocker decision before dependent implementation starts.
4. Let workers edit and verify only their assigned files.
5. Use focused checks during edits and one final affected broad-suite run.
6. Let only the Director close tasks, commit exact files, and push to
   `origin/main`.
7. Complete and push a main task and its independent self-review before the
   next main task starts.

## License

The selected license is the Functional Source License, Version 1.1, ALv2
Future License (`FSL-1.1-ALv2`). Each software version becomes available under
Apache License 2.0 on the second anniversary of the date that version is made
available.

See [`LICENSE.md`](LICENSE.md) for the complete terms.
