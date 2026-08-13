# Native stream protocol version 1

## Transport and negotiation

Model requests and agent runs expose their event location in the admission
receipt. The client reads it with HTTP `GET` and
`Accept: text/event-stream; llmrouter-stream=1`. The bearer token needs the
matching request or run read operation and exact service and workspace scope.

The response uses UTF-8 server-sent events. Each event has `id`, `event`, and
one `data` line whose value is one versioned JSON object. The server sends event
IDs in strictly increasing unsigned decimal order for one logical request.
It can send an SSE comment as a keepalive. A comment has no contract meaning.

The client can reconnect with `Last-Event-ID`. The server replays retained
events after that ID without changing their contents. A repeated event ID is
the same event and MUST be safe to ignore. If the replay position is older
than retained stream data, the server returns `409 stream_replay_unavailable`
with the normal error envelope. The client then reads request status. The
router does not promise replay of model output after stream retention ends.

## Envelope

Each `data` object contains:

```json
{
  "stream_version": "1",
  "request_id": "0198f3d4-6f62-7c71-a5a0-02d97f11e612",
  "sequence": 4,
  "occurred_at": "2026-08-12T12:00:00.123Z",
  "payload": {}
}
```

`stream_version`, `request_id`, `sequence`, `occurred_at`, and `payload` are
present in every event. No other envelope field is valid in version 1. `run_id`
is also present for an agent run. A client MUST ignore an unknown optional
payload field so that a compatible minor contract can add evidence. An unknown
event type MUST be ignored only when its event name starts with `extension.`.
Another unknown event type is incompatible.

## Events

| Event | Required payload | Meaning |
| --- | --- | --- |
| `request.admitted` | `state`, `state_revision`, `admission` | The identity is durably bound. This is always the first event. |
| `request.running` | `state_revision` | Provider or tool work started. |
| `request.waiting_for_tool` | `state_revision`, `tool_call_id`, `expires_at` | The run waits for one business-tool result. |
| `output.delta` | `output_index`, `content_type`, `delta` | Model output became visible. The first event of this type is the stream commit boundary. |
| `output.completed` | `output_index`, `content_type` | One output item is complete. It does not make the request terminal. |
| `tool.call` | `tool_call_id`, `tool_name`, `arguments_delta`, `complete` | A provider-neutral tool request is being assembled. It is not authority to run a business tool. |
| `tool.started` | `tool_call_id`, `tool_kind` | An authorized tool call started. It is an external-effect commit boundary only for a business tool. |
| `tool.completed` | `tool_call_id`, `result_summary` | A bounded safe result is available to the run. |
| `tool.failed` | `tool_call_id`, `error`, `uncertain_effect` | The call failed, or its business effect cannot be reconciled. |
| `usage.updated` | `usage`, `estimated` | Current usage or estimate changed. Final accounting can add later corrections. |
| `request.cancel_requested` | `state_revision` | Cancellation was durably accepted. |
| `request.terminal` | `state`, `state_revision`, `partial_output`, `committed_effects`, optional `error`, optional `result` | The logical request reached its immutable terminal state. This is always the final event. |

`delta`, tool arguments, and `result` can contain sensitive service data. A
client MUST apply the original service and workspace authorization. It MUST
not log these values by default.

## Interruption and completion

An orderly terminal response sends `request.terminal`, then the server closes
the HTTP response. A connection close without `request.terminal` is not a
terminal result. The client reconnects with `Last-Event-ID` or reads status.

After the first `output.delta` or a business-tool `tool.started`, Router MUST
NOT restart the logical request on another provider or repeat the business
effect. A shared external-tool attempt can still use its configured fallback
before its result becomes visible. A later provider or node failure produces
terminal `interrupted` or `uncertain` as applicable. Partial output MUST never
be marked as a complete result.

Cancellation uses the HTTP cancel operation. Closing the stream does not
cancel work. The stream can report `cancel_requested` for up to the accepted
10-minute reconciliation limit before it reports `cancelled` or `uncertain`.

## Limits and retention

One JSON event can contain no more than 1 MiB. A `delta` string can contain no
more than 256 KiB. Larger provider output MUST be split without breaking UTF-8.
The server SHOULD send a keepalive at least every 15 seconds while no contract
event is available.

Replay data MUST remain available while the request is nonterminal and for at
least 15 minutes after its terminal transition. Request status and idempotency
binding remain available for 24 hours after the terminal transition.
