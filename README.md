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
The command waits for the complete MVP runtime before it returns. No service
publishes a non-loopback host port. The `/ready` response reports the database,
administration, embed-session, and model-request components as `ready`.

The administration page asks for the generated local administrator secret.
Open `.local-development/administrator-session` with a local editor and paste
its value into the write-only password control. The ignored file and all other
generated local secrets have mode 0600. The startup command does not print the
secret, and the browser does not store it in a URL or local storage.

Run the deterministic proof after startup:

```bash
./scripts/local-development.sh e2e
```

The proof uses a generated write-only fake credential and an in-process
OpenRouter transport. It does not make an external provider call. It configures
an ordered failed-route-to-DeepSeek V4 Flash fallback through the protected
administration API. It then
proves request replay, non-stream and stream output, status, cancellation,
accounting, restart recovery, isolation failures, and the real administration
embed in a headless browser. Use this one command to run the proof from a clean
state and stop all services when it finishes:

```bash
./scripts/local-development.sh prove
```

Use these local addresses:

- Backend readiness: `http://127.0.0.1:8010/ready`
- Administration: `http://127.0.0.1:5174`
- Router frame: `http://127.0.0.1:5175`
- Embed example: `http://127.0.0.1:5176`

The protected external site is
[https://llmrouter.opendle.dev](https://llmrouter.opendle.dev). Agents MUST use
the local URLs. Do not use the protected external URL for agent tests. The
administration container joins the existing `traefik-proxy` network. Traefik
applies the existing Pangolin resource policy before it forwards a request.

The public administration site uses the shared Pocket ID issuer at
`https://auth.opendle.dev`. Put the Router client's ID and secret in the
ignored mode-0400 files `.local-development/pocket-id-client-id` and
`.local-development/pocket-id-client-secret`. Put its separate regular,
expiring Pocket ID account-state API key in
`.local-development/pocket-id-account-api-key`. The Router uses this key only
to read user and WebAuthn credential state for its five-minute check. Do not
use `STATIC_API_KEY` or reuse an Ontology credential. Empty files keep the
deterministic localhost-only mode.

After the three Pocket ID files contain their values, restart the deployment.
Create the initial ten-minute one-use Router grant URL from the backend:

```bash
docker compose -f docker-compose.dev.yml exec backend \
  /bin/sh -euc '
    export LLMROUTER_LOCAL_RUNTIME=0
    export LLMROUTER_DATABASE_URL="postgresql://llmrouter:$(cat /run/secrets/postgres_password)@postgres:5432/llmrouter"
    exec .venv/bin/python scripts/administrator-grant.py initial
  '
```

Open the returned URL and authenticate through Pocket ID. Pocket ID proves the
person's identity. The one-use Router URL gives that identity its first local
Router grant. Pocket ID membership alone does not give Router authority.

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

For the optional, bounded live proof, use a private subshell and the hidden
shell input. The subshell removes the inherited value when it exits:

```bash
(
  read -rs OPENROUTER_API_KEY
  printf '\n'
  export OPENROUTER_API_KEY
  ./scripts/local-development.sh live-openrouter
)
```

Press Enter after the hidden input. The proof first runs the complete offline
proof. It then checks the current OpenRouter key and model metadata without an
inference call. If all checks pass, it makes one small compatible non-streaming
request and one small compatible streaming request for
`deepseek/deepseek-v4-flash`. It has one route,
no retry, an output limit of 64 units, and a maximum cost of USD 0.001 for each
request. The command reports only the call count and bounded Router accounting.
It resets the local database and stops all services after success or failure.

DeepSeek V4 Flash stays the default supported MVP model. For an authorized
alternate live check, use `live-openrouter-mimo` in the same private subshell.
This action selects the fixed `xiaomi/mimo-v2.5` wire model. MiMo 2.5 has a
separate canonical model identity. You can also use `live-openrouter-granite`
to select the fixed `ibm-granite/granite-4.1-8b` wire model. Granite 4.1 8B
has its own canonical model identity. Each alternate action uses the same
two-call, one-route, no-retry, output, and cost limits. The model selector goes
only to the proof process. It does not go to Compose.

For the guarded Granite stream diagnostic, use
`live-openrouter-granite-stream` in the same private subshell. This action keeps
the no-cost preflight, protected configuration, isolation check, status and
accounting checks, administration embed check, and sensitive-data scans. It
makes exactly one paid stream request and does not retry. The default
`live-openrouter` action stays the two-call DeepSeek acceptance proof.

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
