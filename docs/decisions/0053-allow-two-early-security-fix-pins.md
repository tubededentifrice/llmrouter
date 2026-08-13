# Allow two early security-fix pins

## Context

The initial TypeScript scaffold needs ESLint and Vite build dependencies. On
2026-08-13, all mature compatible versions of two transitive packages have
high-severity security advisories. The first compatible fixes are
`brace-expansion` 5.0.9, released on 2026-07-30 at 10:00 UTC, and `nanoid`
3.3.17, released on 2026-08-03 at 10:39 UTC. These versions are less than 14
complete days old.

The user approved a narrow dependency-age exception on 2026-08-13. The normal
14-day dependency-age rule stays active for all other packages.

## Accepted choice

Pin `brace-expansion` to 5.0.9 and `nanoid` to 3.3.17 as exact npm overrides.
Record both pins in the machine-checked dependency exception file. Do not add
another early package through this decision.

Keep the complete npm lock audit active. Do not ignore the related advisories.
After both pins are 14 complete days old, a normal lock refresh can remove the
age-exception record while it keeps the secure versions or later mature
versions.

## Alternatives

- Keep the mature vulnerable packages and ignore exact advisories temporarily.
- Delay the repository scaffold until 2026-08-17 at 10:39 UTC.
- Remove the accepted Vite or ESLint foundation.

## Good effects

- The complete dependency audit has no known high-severity finding.
- The exception is limited to two exact transitive packages.
- The dependency gate rejects an added or changed exception.

## Bad effects

- The two fixes have less ecosystem observation time than normal.
- The repository must review the exception record during a later lock refresh.

## Migration effect

There is no product data migration. The npm lock changes to the two secure
transitive versions. A later mature lock refresh can remove only the exception
record and keep compatible secure versions.

## Security effect

This choice removes the known high-severity `brace-expansion` and `nanoid`
findings. Exact pins, immutable lock integrity values, full-lock audit, and the
normal age cutoff limit the added supply-chain risk.

## Review conditions

Review this decision if either pin has a new advisory, becomes incompatible,
or is replaced by a mature secure version. Do not extend this decision to a
third package.
