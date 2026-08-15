# Use FSL 1.1 with an Apache 2.0 future license

- Status: accepted; applied
- Date: 2026-08-12
- Decision owner: user

## Context

The project owner wants to make the source available and permit broad future
use. The owner also wants to limit some competing production use for a fixed
initial period.

## Decision

Select the Functional Source License, Version 1.1, ALv2 Future License
(`FSL-1.1-ALv2`). Each released software version changes to Apache License 2.0
on the second anniversary of the date that version is made available.

Use the canonical license text without modification. The license notice is
`Copyright 2026 tubededentifrice`. Use the same license for Ontology in a
separate repository change.

The authoritative references are the [FSL site](https://fsl.software/) and the
[SPDX license record](https://spdx.org/licenses/FSL-1.1-ALv2.html).

## Alternatives

- Apache-2.0 immediately permits broad commercial use and includes patent
  terms. It does not include the initial competing-use limit.
- MIT is short and permissive. It does not include explicit patent terms or
  the initial competing-use limit.
- AGPL-3.0 requires source sharing for modified network services. It can block
  adoption by some organizations.

## Consequences

- The project is not open source during the initial FSL period. Each version
  becomes available under the open source Apache-2.0 license after that
  version's two-year period.
- Each version has its own change date.
- Release records preserve the date that each version became available.
- The repository grants FSL under the canonical text and the notice above.

## Migration effect

Existing files are covered by the repository license notice. Ontology has its
own license file and notice.

## Security effect

This decision does not change runtime security. Release integrity controls
prevent an attacker from changing license text, version dates, or source
artifacts.

## Review conditions

Review this decision if the licensor changes, if the future-license policy
changes, or before distributing a version under different terms.
