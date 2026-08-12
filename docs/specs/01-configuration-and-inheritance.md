# Configuration and inheritance

Status: Accepted sections only. Publication, disablement, validation, and
rollback behavior remains open.

## Scope chain

Service configuration MUST use one ordered parent chain. A normal service
MUST NOT have multiple parents.

The effective assignment order is:

1. router defaults;
2. each service in the parent-to-child chain;
3. the workspace layer.

For one named assignment, the nearest layer that defines the assignment MUST
replace the complete inherited fallback chain. The service MUST NOT merge a
child chain with its inherited chain. The first release MUST NOT support a
partial edit of an inherited chain.

An effective assignment response MUST identify the source layer and source
configuration revision.

## Administration ownership

Only a global administrator can create, disable, retire, or restore a service,
or change a service parent.

A service administrator can manage the assignments and permitted settings of
that service. It can manage workspace overrides for workspaces that the
service owns. It MUST NOT change another service, another service's workspace,
or a parent link.

Each global and service-scoped change MUST create an audit event.
