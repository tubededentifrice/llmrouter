# Use one currency per hard-budget scope

## Context

Hard budgets need deterministic comparisons. Foreign-exchange conversion adds
a time-dependent external rate and can change an admission result.

## Accepted choice

Use one configured accounting currency in each hard-budget scope. Require all
prices, reservations, costs, corrections, and aggregates in that scope to use
it. Do not perform foreign-exchange conversion in the first release.

## Alternatives

- Convert each provider price through a live exchange rate.
- Permit mixed currencies and enforce separate sublimits.
- Use one fleet currency without scoped configuration.

## Good effects

- Admission and accounting remain reproducible.
- A live exchange-rate service cannot change routing availability.
- Currency mismatches fail before a route becomes eligible.

## Bad effects

- An administrator must enter a price in the scope currency.
- Historical data across a currency change needs separate revisions.

## Migration effect

Each initial hard-budget scope and provider-model route needs a currency and a
price in that currency before budget enforcement starts.

## Security effect

This choice removes an external mutable input from hard-budget admission and
limits cost manipulation through stale or compromised rate data.

## Review conditions

Review this decision if a required provider cannot supply or support a stable
price in the configured scope currency.
