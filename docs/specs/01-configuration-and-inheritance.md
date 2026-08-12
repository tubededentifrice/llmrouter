# Configuration and inheritance

Status: Accepted sections only. Detailed disablement and validation limits
remain open.

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

## Shared catalog and scoped provider instances

LLM Router MUST own one shared catalog of provider adapter types, canonical
models, capabilities, and normalized provider metadata. A catalog update MUST
NOT create a provider instance, credential, provider-model route, or assignment
automatically.

Only a global administrator MAY create, change, or retire a shared catalog
entry. A service administrator MAY create and manage provider instances and
provider-model routes owned by that service when all referenced catalog and
provider items are eligible for the service.

A provider instance MUST have one owning scope: global or service. It MUST
contain the endpoint, credential reference, operating limits, and applicable
provider settings. A global provider instance can be permitted to selected
services. A service-owned provider instance MUST be visible only to that
service and its eligible descendants.

A child service MUST inherit eligible provider instances and model entries
from its parent chain. Provider-model routes MUST also have a global or service
owner and follow the same eligibility and inheritance boundary. A child MAY
disable an inherited item for itself and its descendants. It MUST NOT edit the
owning scope's item. A workspace MUST NOT own provider credentials, provider
instances, or provider-model routes in the first release. It MAY select only
eligible provider-model routes through its assignments.

The effective configuration interface MUST show each catalog item, provider
instance, provider-model route, and assignment with its owner, source layer,
active revision, enabled state, and inherited or local state.

## Immediate publication

Each successful configuration write MUST validate the complete affected
configuration and atomically create and activate one immutable revision. A
save MUST NOT create a draft, staged rollout, approval queue, or canary. A
validation or storage failure MUST leave the active revision unchanged.

A write MUST use an expected active revision or an equivalent concurrency
condition. It MUST reject a stale edit instead of silently replacing a newer
save. Its response MUST include the new active revision and distribution
state.

Normal revision distribution remains asynchronous and follows the accepted
24-hour stale-configuration rule. An urgent security change MUST use the
separate urgent path. A rollback MUST validate and immediately publish a new
immutable revision whose content restores a selected earlier revision. It
MUST NOT make stored history mutable.
