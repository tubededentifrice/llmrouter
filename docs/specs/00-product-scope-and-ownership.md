# Product scope and ownership

Status: Accepted on 2026-08-23.

## Purpose

LLM Router MUST be a shared calling service for model, embedding, and media
operations. Its primary runtime operation MUST call the named assignment that
a service selects. It MUST use that assignment's ordered provider-model
fallback chain.

The first release MUST provide:

- synchronous and streaming model calls;
- synchronous text-embedding batches;
- asynchronous image, video, and audio generation jobs;
- named assignments and exact provider-model calls;
- global provider, model, credential, and price administration;
- service and workspace ownership;
- durable usage and cost accounting;
- bounded detailed request logs and basic activity events;
- one global administration application;
- a native versioned HTTP API;
- a Python SDK and shared caller-process harness.

## Calling-service ownership

A calling service MUST own its domain rules, prompts, workflows, durable
conversation state, user authorization, business data, and tools. The Router
MUST NOT authenticate a calling service's human users or decide which caller
tool a human can use.

The Router MUST NOT execute an agent loop or a caller tool. A calling service
MAY send tool definitions and tool-result messages as model input. The model
MAY return tool calls. The calling service MUST execute them outside the
Router.

Generic Python SDK and harness behavior MUST live in `../opendle-lib`.
Reusable React components and interaction patterns MUST live in
`../opendle-ui`. This repository MUST keep only Router policy, API routes,
data, and host composition.

## Product exclusions

The first release MUST NOT provide:

- Router-hosted agent runs or durable conversation storage;
- shared external-tool adapters or a business-tool gateway;
- a calling-service user interface, hosted frame, or cross-origin embed;
- service-owned provider connections or provider credentials;
- workspace assignment overrides or workspace cost limits;
- an OpenAI-compatible API;
- service-token exchange, short-lived service tokens, operation scopes,
  audiences, mutual TLS product behavior, or browser host grants;
- durable admission, status, cancellation, resume, replay, or results for
  normal model and embedding calls;
- split control-plane and data-plane roles, Router node discovery, local event
  spools, leased allowances, fleet health hints, or Router-controlled standby
  promotion;
- fine-grained service-key or global-administrator permissions.

## Isolation

Every model, embedding, and media request MUST authenticate one service and
identify exactly one workspace that the service owns. A normal service MUST
NOT read or change another service's configuration, keys, workspaces,
requests, media jobs, logs, or accounting.

A service MAY inherit assignments only through its one parent chain. Parent
inheritance MUST NOT give either service access to the other service's keys,
workspaces, request data, media, or accounting.

Global administration MUST use a separate human identity and authorization
path. A service API key MUST NOT authenticate a global administrator action.

## Public state and contract versions

A service, workspace, assignment, provider connection, model, provider-model,
credential, and other configuration resource MUST have current state only.
It MUST NOT expose a resource revision, state revision, configuration version,
draft, publication, rollout, rollback, restore, or history resource.

The native API MUST have an explicit contract version. Contract versioning
MUST define wire compatibility only. It MUST NOT create or imply a version of
a service, workspace, or configuration resource.
