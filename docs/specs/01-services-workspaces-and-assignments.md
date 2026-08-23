# Services, workspaces, and assignments

Status: Accepted on 2026-08-23.

## Names and identity

Each service and workspace MUST have an opaque internal identity and one
stable, readable `apiName`. An `apiName` MUST contain 1 through 63 lowercase
ASCII letters, digits, or hyphens. It MUST start with a letter and MUST end
with a letter or digit. A service `apiName` MUST be globally unique. A
workspace `apiName` MUST be unique in its owning service. The Router MUST NOT
reuse an `apiName` in the same collection while its resource exists.

A service or workspace MUST either exist or be absent. It MUST NOT have a
disabled, retired, restored, deleting, cleanup, revision, or version state.

## Service tree

A service MAY have one parent service. It MUST NOT have more than one parent.
A parent change MUST reject a cycle and MUST leave the existing tree unchanged
after any validation or storage failure.

Only a global administrator MAY create a service, change its parent, or delete
it. A service API key MUST NOT perform these operations. A service delete MUST
fail while one or more child services name it as their parent. The
administrator MUST first move or delete each child.

Deleting a service MUST delete its API keys, workspaces, local assignment
definitions, request logs, raw accounting, daily aggregates, media jobs, and
retained media. It MUST NOT delete a parent service or a child service. The
delete MUST make the service unavailable to new calls before dependent
records are removed.

## Workspaces

A workspace MUST belong to exactly one service. A service API key MUST be able
to create, list, read, and delete workspaces for its service. A global
administrator MUST have the same operations for any service.

A workspace MUST be an accounting label only. It MUST NOT own assignments,
provider connections, credentials, prices, policy, or limits.

Deleting a workspace MUST delete its detailed logs, raw accounting, daily
aggregates, media jobs, uploaded images, and retained generated media. It MUST
NOT change the owning service or its assignment definitions. The delete MUST
make the workspace unavailable to new calls before dependent records are
removed.

Service and workspace create, update, and delete operations MUST change the
current state directly. Their requests and responses MUST NOT contain a state
revision, resource version, or expected revision.

## Assignment names

An assignment name MUST contain 1 through 127 lowercase ASCII letters, digits,
dots, underscores, or hyphens. It MUST start with a letter or digit. An
assignment MUST represent one named service use case.

Each assignment definition MUST contain either:

- one direct ordered provider-model candidate chain; or
- the name of one other assignment to inherit.

It MUST NOT contain both. A direct chain MUST contain 1 through 16 unique
provider-model candidates. An assignment MUST NOT store a temperature or
output-limit default.

## Service inheritance

For one assignment name, the Router MUST search from the called service toward
the root. The nearest service definition MUST replace every definition with
the same name farther from the called service. Chains MUST NOT merge.

If the selected definition inherits another assignment name, the Router MUST
resolve that name from the called service through the same service parent
chain. A direct chain MUST replace the complete inherited assignment chain.
Configuration validation MUST reject a missing inherited name and any cycle
across assignment names and service inheritance.

A workspace MUST NOT take part in assignment resolution.

## Default and automatic assignments

Each root service MUST have an implicit assignment named `default`. It MUST
exist even when it has no configured chain. Any service in the parent chain
MAY define its own `default` chain. The nearest definition MUST replace the
complete parent definition. A child with no local definition MUST inherit the
effective parent `default`.

When a service calls a valid assignment name that has no effective record,
the Router MUST create a local assignment for that service. The new assignment
MUST inherit `default`. The first call MUST then use the inherited effective
`default` chain without a separate registration operation.

Concurrent first calls for the same service and assignment name MUST create at
most one local assignment record. Each call MAY use that one record after its
creation transaction commits.

The call MUST fail before provider work when the effective `default` chain is
empty or no candidate supports the call shape.

## Use evidence

Each assignment MUST store its last-used time. The Router MUST update this
time when a call passes service, workspace, assignment-name, and input
validation, even when no eligible candidate completes the call.

Each assignment MUST store the union of call capabilities and modalities that
validated calls requested. A global or service administrator MUST be able to
remove an observed item. Runtime candidate filtering MUST use the current
call's actual requirements. It MUST NOT use the stored union as the call
filter.

The administration interface MUST make an assignment with no direct chain
clear. It MUST show whether that assignment inherits `default` or another
assignment and MUST show its last-used time and observed requirements.

An authenticated service MAY create, change, or delete a local assignment
definition for itself. A global administrator MAY perform the same operation
for any service. Deleting a local definition MUST expose the next inherited
definition. Deleting a local root `default` definition MUST expose the empty
implicit root `default`. A delete MUST fail if it leaves another local
assignment with a missing inherited name.
