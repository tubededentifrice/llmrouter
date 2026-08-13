# Use Python backends and React TypeScript frontends

## Context

LLM Router needs one implementation language for its control plane, data
plane, workers, provider adapters, tool adapters, and administration backend.
It also needs one frontend stack for the global administration application and
the service-scoped hosted view.

The first calling services use Python or need an official Python client. The
administration surface needs a responsive web application and an isolated
embed that can also work in a host that does not use React.

## Accepted choice

The user accepted Python for all LLM Router backend processes. This includes
combined, control-plane, data-plane, and worker roles, HTTP and streaming
servers, provider and tool adapters, scheduled work, and administration
backend operations.

The user accepted React with strict TypeScript for all LLM Router frontend
code. The global administration application and service-scoped hosted view can
share this frontend code while they keep separate authority.

The versioned HTTP, streaming, OpenAI-compatible, and embed contracts remain
language-neutral. A calling service can use the hosted frame or headless API
without a React dependency. Official Python and TypeScript clients remain
required.

The implementation must use `uv` for Python environments and dependencies.
It must pin exact Python, Node.js, TypeScript, React, and build-tool versions
that satisfy the repository dependency-age rule. This decision does not select
a Python web framework, process server, or frontend build tool. Decision 0044
selects FastAPI on ASGI and Vite for the first-release foundation.

## Alternatives

- A TypeScript backend would use one language with the frontend, but it would
  give less direct alignment with the initial Python calling services and
  Python model ecosystem.
- A Go or Rust backend could reduce runtime overhead, but it would add another
  language before measurements show that Python cannot meet the accepted
  targets.
- Different backend languages for different roles could optimize individual
  components, but it would increase build, deployment, and operating work in
  the first release.

## Good effects

- All Router backend roles use one toolchain and one shared implementation
  model.
- Provider, model, agent, and data-processing libraries are available in the
  selected backend ecosystem.
- All Router user interfaces use one typed frontend stack.
- Public contracts do not expose the internal language or frontend framework.

## Bad effects

- CPU-bound or high-concurrency paths can need more processes or later native
  optimization.
- The repository must maintain both Python and TypeScript toolchains.
- A future measured bottleneck can require a new decision before one component
  moves to another language.

## Migration effect

There is no product implementation to migrate. The first implementation must
create the Python backend and React TypeScript frontend structure. Generated
client and contract checks must remain independent of the backend language.

## Security effect

The language choice does not change service, workspace, credential, session,
or embed isolation. Python and JavaScript dependencies remain subject to exact
pins, the 14-day age rule, security review, and release gates.

## Review conditions

Review this decision if measured latency, throughput, memory, startup time, or
isolation cannot meet the accepted targets after normal Python profiling and
optimization. Review the frontend choice if React cannot keep the required
accessibility, embed isolation, or React Doctor result.
