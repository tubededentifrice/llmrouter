# Model stream protocol

<!-- contract:stream-protocol -->

`POST /v1/model-streams` and `POST /v1/admin/playground/model-streams` use
UTF-8 server-sent events. Each event has one `event` line and one `data` line.
The `data` value is one JSON object. A blank line ends the event.

The event order is:

1. One `start` event.
2. Zero or more `text_delta` or `tool_call` events.
3. One `completed` event, or one `error` event.

`start` data uses `StreamStart`. `text_delta` data uses `StreamTextDelta`.
`tool_call` data uses `StreamToolCall`. `completed` data uses
`StreamCompleted`. For the service operation, `error` data uses
`ErrorEnvelope`. These closed schemas are in `openapi.yaml`.

For the administrator operation, `start` data uses
`AdministratorStreamStart` and `completed` data uses
`AdministratorStreamCompleted`. Both identify the logical call. The completed
event also gives the selector, elapsed milliseconds, completed attempts, final
route, usage, and cost. The intermediate schemas are the same as for the
service operation. The administrator error data uses
`AdministratorErrorEnvelope`. After logical-call creation, it gives the
logical call, selector, elapsed milliseconds, and completed attempts. A
failure before logical-call creation uses the basic form of this administrator
schema. The administrator operation requires a current administrator session,
the session-bound CSRF token, and the exact allowed Origin at admission.

The Router can try the next assignment candidate until model output becomes
visible. Visible output is the first `text_delta` or `tool_call` event. The
Router sends `start` only when it is ready to send that visible event. If the
call has no visible output, it sends `start` immediately before `completed`.
After visible output, a failure ends the stream with `error`. The Router does
not replay, resume, or cancel a stream through another API operation.

The connection can end without a terminal event. A client must treat this as
an incomplete call. The Router records the attempt for statistics and logs.
An administrator call remains an administrator-only record after disconnect.
