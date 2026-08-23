# Use the Python, PostgreSQL, FastAPI, React, and Vite foundation

- Status: accepted
- Date: 2026-08-23
- Decision owner: user

## Context

The simplified Router needs one web application, one transactional store, one
backend stack, and one global administration frontend. The shared backend
library also needs one Python version across OpenDLE projects.

## Decision

Use Python for the Router backend and shared SDK or harness. Use PostgreSQL as
the one logical transactional store. Use FastAPI on ASGI for HTTP operations.
Use React with strict TypeScript and Vite for the global administration
application.

Use `uv` for Python environments and dependency work. Pin exact eligible
versions under repository policy. This choice does not create separate runtime
roles or a TypeScript SDK requirement.

## Alternatives

- A second backend language increases shared-library and operation work.
- A second transactional database increases consistency and backup work.
- A non-React Router frontend cannot use the shared OpenDLE UI package.

## Consequences

The simplified application has one main stack. Calling services still use the
native API and do not depend on the Router frontend framework.

## Review conditions

Review this decision if one selected tool cannot meet an accepted contract or
security requirement.
