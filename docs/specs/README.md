# Product specifications

Some specification sections are accepted. Other behavior remains open while
the architecture interview continues.

- [Product and boundaries](00-product-and-boundaries.md)
- [Configuration and inheritance](01-configuration-and-inheritance.md)
- [Routing, failover, and request lifecycle](02-routing-failover-and-request-lifecycle.md)
- [Agent harness and tools](03-agent-harness-and-tools.md)
- [Identity, credentials, and tool gateway](04-identity-credentials-and-tool-gateway.md)
- [Logging, accounting, and retention](05-logging-accounting-and-retention.md)
- [Administration and shared interface](06-administration-and-shared-interface.md)
- [Reliability, deployment, and operations](07-reliability-deployment-and-operations.md)
- [Public interfaces, clients, and packaging](08-public-interfaces-clients-and-packaging.md)

Keep these rules:

- Put normative product behavior only in this directory.
- Give each requirement one source location.
- Define ownership, scope, success, failure, limits, consistency, retention,
  and audit behavior.
- Separate a logical request from its provider attempts.
- Cover normal services, workspaces, global administration, agents, and tools
  where behavior differs.
- Link accepted implementation choices from `docs/decisions/`.
- Do not create Beads work items until the user accepts the specification set
  and explicitly enables Beads planning.
