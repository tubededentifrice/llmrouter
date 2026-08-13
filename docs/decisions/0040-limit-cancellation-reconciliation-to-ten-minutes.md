# Limit cancellation reconciliation to 10 minutes

## Context

A provider or tool does not always confirm that active work stopped. The
Router needs a visible time limit before it reports a final uncertain result.
An unlimited wait leaves a user without a terminal state. A short wait creates
more uncertain results.

## Accepted choice

The user accepted a 10-minute cancellation reconciliation limit. Router reports
`cancel_requested` while it tries to confirm all active work. At 10 minutes, it
reports terminal `uncertain` when proof is still absent. An adapter can use a
lower limit when it cannot supply later evidence.

## Alternatives

- A 1-minute limit gives a faster result but creates more uncertain states.
- A 30-minute limit can obtain more confirmations but leaves users waiting for
  longer.
- An unlimited wait does not give a bounded user experience.

## Good effects

- A user receives a terminal result in a bounded time.
- Router gets enough time for normal provider and tool stop confirmation.
- Tests and operator alerts have one exact limit.

## Bad effects

- Slow confirmation after 10 minutes cannot change the terminal state.
- Some work that stopped successfully can still have an `uncertain` result.

## Migration effect

There is no runtime migration. Clients must accept `cancel_requested` for up
to 10 minutes and must handle terminal `uncertain`.

## Security effect

The limit does not authorize a repeated effect. An uncertain effect stays
blocked until the calling service reconciles it.

## Review conditions

Review this choice if measured adapter stop confirmation usually takes more
than 10 minutes or if users cannot act safely on uncertain results.
