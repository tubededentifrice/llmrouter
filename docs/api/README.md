# API contracts

The accepted public-interface direction is:

- a native versioned HTTP and streaming API as the primary contract;
- an OpenAI-compatible interface for accepted common operations;
- official Python and TypeScript clients.

The accepted cross-service integration contracts are:

- [Service and workspace management](service-management.md) for service token
  exchange and idempotent router workspace life cycle.
- [Hosted administration embed protocol](embed-protocol.md) for the
  service-scoped router administration view.

The native model, run, shared-tool, stream, accounting, configuration, and
OpenAI-compatible wire schemas remain open. They MUST follow the accepted
behavior in `docs/specs/` and MUST NOT change the contracts above without a
version change.
