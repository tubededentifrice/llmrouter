# Product specifications

Status: Accepted reset set on 2026-08-23.

These files are the normative product source:

- [Product scope and ownership](00-product-scope-and-ownership.md)
- [Services, workspaces, and assignments](01-services-workspaces-and-assignments.md)
- [Providers, models, prices, and configuration](02-providers-models-prices-and-configuration.md)
- [Model, embedding, and media calls](03-model-embedding-and-media-calls.md)
- [Authentication, administration, and shared UI](04-authentication-administration-and-shared-ui.md)
- [Accounting, logs, retention, and operations](05-accounting-logs-retention-and-operations.md)
- [Python SDK and shared harness](06-python-sdk-and-shared-harness.md)

The accepted [simplification decision source](../interviews/service-simplification-2026-08-23.md)
explains the reset. These specifications own the resulting requirements.
Formal files in `docs/api/` own HTTP shapes, stream events, and public errors.

Services, workspaces, and configuration resources have current state only.
They have no resource revision or version. Native API contract versions are
separate and do not create resource history.

Do not restore a removed agent-run, shared-tool, hosted-frame, token-exchange,
durable-request, distributed-control, or OpenAI-compatibility requirement
without a new accepted decision and specification change.
