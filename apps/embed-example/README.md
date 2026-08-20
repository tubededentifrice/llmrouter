# Administration embed example host

This localhost-only application proves the version 1 administration frame
protocol from a separate loopback origin. The host page uses
`http://127.0.0.1:5176`. The Router frame uses
`http://127.0.0.1:5175`.

The example server creates and revokes embed sessions. It reads the short-lived
`host_backend` token only from `LLMROUTER_EXAMPLE_HOST_TOKEN`. The browser does
not receive this token. Do not put a bootstrap credential in this variable.

The complete local deployment starts this example with:

```bash
./scripts/local-development.sh start
```

For a separate manual start, set these values in the shell that starts the
example:

```bash
export LLMROUTER_EXAMPLE_HOST_TOKEN='<short-lived host_backend token>'
export LLMROUTER_EXAMPLE_SERVICE_ID='<service ID>'
export LLMROUTER_EXAMPLE_WORKSPACE_ID='<first workspace ID>'
export LLMROUTER_EXAMPLE_SECOND_WORKSPACE_ID='<second workspace ID>'
export VITE_LLMROUTER_FRAME_ORIGIN='http://127.0.0.1:5175'
```

You can set `LLMROUTER_EXAMPLE_HOST_TOKEN_FILE` instead of
`LLMROUTER_EXAMPLE_HOST_TOKEN`. The local Compose deployment uses the
write-only file at `/run/secrets/example_host_token`. Do not configure both
sources.

Start the Router administration application on `127.0.0.1:5175`. Then start
the example host:

```bash
npm run dev --workspace @llmrouter/embed-example
```

Open `http://127.0.0.1:5176`. Use the example controls to switch the user or
workspace, change permissions, remove membership, restore membership, or renew
the session. Each authority change removes and revokes the old frame before a
new session starts.
