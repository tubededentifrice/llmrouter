# Product direction

Status: Accepted on 2026-08-23.

## Problem

Crewday, FJ2, Xbot, and future services need the same provider, model,
assignment, fallback, embedding, media, and accounting functions. Local copies
make behavior and cost difficult to keep consistent.

The earlier Router design also put agent execution, shared tools, durable
request recovery, embedded administration, and distributed coordination in
one product. That design had too many states and failure modes for the current
need.

## Outcome

LLM Router is one small shared calling service. It owns provider connections,
model availability, assignments, fallback calls, price data, request logs,
and accounting.

A calling service uses one backend API key. Each call identifies one owned
workspace. The service selects a named assignment or one exact provider-model.
The Router filters candidates by the call shape and tries the eligible
candidates in order before output becomes visible.

Calling services keep all domain behavior. Shared Python SDK and harness code
belongs in OpenDLE Lib. Shared React assignment and playground components
belong in OpenDLE UI.

## First-release goals

- Keep configuration direct and easy to inspect.
- Keep one parent service chain and no workspace configuration layer.
- Make assignments the central routing object.
- Support text, image input, structured output, embeddings, and generated
  image, video, and audio.
- Keep provider credentials under global administrator control.
- Record accurate usage and cost for each attempt.
- Keep complete detailed logs for a short global period.
- Use normal web application and PostgreSQL deployment patterns.
- Keep the public API native, versioned, and provider-neutral.

## Limits

The Router does not own agent runs, tool execution, conversation databases,
calling-service user authorization, or calling-service user interfaces. It
does not provide service-owned providers, workspace budgets, OpenAI
compatibility, token exchange, hosted service frames, durable model-request
status, or Router-specific high-availability coordination.

The first calling services are Crewday, FJ2, and Xbot. Their code and data
changes stay in their own repositories.
