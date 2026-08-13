# Allow two early security-fix pins

## Context

The initial TypeScript scaffold needs ESLint and Vite build dependencies. On
2026-08-13, it selected two new transitive package versions to address
high-severity security advisories. The selected versions were
`brace-expansion` 5.0.9, released on 2026-07-30 at 10:00 UTC, and `nanoid`
3.3.17, released on 2026-08-03 at 10:39 UTC. These versions were less than 14
complete days old.

The user approved a narrow dependency-age exception on 2026-08-13. The normal
14-day dependency-age rule stays active for all other packages.

A later review found that `nanoid` 3.3.16, released on 2026-07-12, had already
fixed GHSA-28wg-ghj8-5hjv and was mature. A later audit found
GHSA-2v37-7h3g-55p8 in `nanoid` 3.3.17. The compatible 3.3.18 fix is not 14
complete days old. `nanoid` 5.1.16 was released on 2026-06-24 and is mature.
The Node tests and builds show that it is compatible with the pinned tree.
`brace-expansion` 5.0.9 is also now 14 complete days old.

## Accepted choice

Pin `brace-expansion` to 5.0.9 and `nanoid` to 5.1.16 as exact npm overrides.
Keep the machine-checked dependency exception list empty. Do not add an early
package through this decision.

Keep the complete npm lock audit active. Do not ignore the related advisories.

## Alternatives

- Keep the mature vulnerable packages and ignore exact advisories temporarily.
- Delay the repository scaffold until `nanoid` 3.3.18 is mature.
- Remove the accepted Vite or ESLint foundation.

## Good effects

- The complete dependency audit has no known high-severity finding.
- The overrides are limited to two exact transitive packages.
- The dependency gate rejects an added or changed exception.

## Bad effects

- The `nanoid` override crosses a major version because no secure compatible
  3.x version is mature.

## Migration effect

There is no product data migration. The npm lock changes to the secure
transitive versions. The age-exception record becomes empty.

## Security effect

This choice removes the known high-severity `brace-expansion` and `nanoid`
findings. Exact pins, immutable lock integrity values, full-lock audit, and the
normal age cutoff limit the added supply-chain risk.

## Review conditions

Review this decision if either pin has a new advisory, becomes incompatible,
or is replaced by a mature secure version. Do not extend this decision to a
third package.
