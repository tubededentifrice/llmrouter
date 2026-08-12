# LLM Router

LLM Router is a planned open source service for internal applications. It will
give applications one shared interface for model routing, provider failover,
agent execution, common tools, request accounting, and administration.

The first calling services are Crewday, FJ2, and Xbot. This repository does not
change those services during the specification phase.

## Status

This repository is in architecture discovery. It contains repository controls,
research, and draft design boundaries. It does not contain a product
implementation. No functional specification or architecture choice is
accepted until the user reviews it.

Beads is initialized, but it has no work items. Beads planning stays disabled
until the user approves the specifications and explicitly asks to enable it.

## Proposed product boundary

The router can provide these shared functions:

- ordered service and workspace configuration;
- provider, model, assignment, and fallback selection;
- synchronous, streaming, and agent-run request handling;
- a controlled catalog of common tools, such as web search and extraction;
- local routing nodes with failover to other nodes;
- request logs, usage accounting, cost records, audit records, and retention;
- a global administrator application;
- service-scoped administration views that work in React and non-React hosts.

The calling service should continue to own its domain rules, prompts, workflows,
user permissions, and the choice of which tools an agent can use.

These boundaries are proposals. The architecture interview will decide them.

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

## Work process

1. Review the architecture interview with the user.
2. Write and review the product specifications.
3. Record accepted architecture decisions.
4. Enable Beads only after the user approves the specification set.
5. Plan and implement the service in small, verified changes.

## License

The project is intended to be open source. The license is not selected. License
selection changes how other parties can use and distribute the service, so it
needs an explicit user decision.
