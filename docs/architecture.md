# Architecture

Status: Accepted working structure for the 2026-08-23 simplification reset.

Normative behavior is in `docs/specs/`. This document explains the structure.

## Application

LLM Router is one logical Python web application with PostgreSQL. A deployment
can run identical application replicas behind a normal load balancer. The
application does not have control-plane, data-plane, worker, or combined
product roles.

FastAPI supplies the HTTP application. The global administration application
uses React, strict TypeScript, and Vite. Large input images and generated media
use Router-controlled S3-compatible object storage.

## Main records

The main records are:

- services and their one-parent relationships;
- service-owned workspaces;
- service API keys;
- canonical models and provider connections;
- enabled provider-model mappings and prices;
- named assignments and ordered candidate chains;
- model and embedding call records;
- media jobs;
- raw attempt accounting and daily aggregates;
- detailed request logs and basic activity events.

Services, workspaces, assignments, providers, models, credentials, and other
configuration resources have current state only. They have no public resource
revision or version. A valid write changes the current state in one database
transaction. The product has no draft, publication, rollback, or history
workflow.

Native API contract versions are separate from resource state. An API version
defines wire compatibility. It does not create a version of a service,
workspace, or configuration record.

## Routing path

The application authenticates the service API key and verifies the workspace.
It resolves the named assignment or exact provider-model, checks the required
call capabilities, and creates one attempt record for each provider call.

An assignment call tries each eligible candidate no more than once. Fallback
can continue only before model output becomes visible. An exact call has one
candidate and no fallback.

Model and embedding calls are connection-lifetime operations. Their durable
logs and accounting do not make them resumable. Media generation uses a small
job record because provider work can take longer than one HTTP connection.

## Shared libraries and interface

The Python SDK and multi-turn harness run in the calling-service process and
belong in `../opendle-lib`. Caller tools also run there. The Router receives
tool definitions and tool-result messages only as model-call data.

Reusable assignment and playground components belong in `../opendle-ui`.
Calling services render those components and let their own backends call the
Router. The Router hosts only its global administration application.

## Data and operations

PostgreSQL stores current configuration, media-job state, durable accounting,
and aggregate data. Detailed request logs are best-effort diagnostic data and
can disappear before their maximum retention time. Router-controlled object
storage keeps uploaded images and generated media for the same rolling period.

Normal deployment tools provide replication, load balancing, backup, restore,
and process restart. Prometheus metrics provide operational detail. The
administration application shows only a small health summary.
