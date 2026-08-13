# Foundation milestone verification evidence

Date: 2026-08-13 UTC

This file records safe verification evidence for Bead `llmr-v01`. It contains
no test credential, private data, prompt, or model response.

## Verified source and tools

- Source base: `d7dc05b6eebc8958ddb5c4f67e06e4c70096843f`.
- Verification changes: the owned changes for defects
  `llmr-e11.1`, `llmr-e11.3`, `llmr-e11.5`, and `llmr-e11.7`.
- Python: 3.13.12.
- uv: 0.12.0.
- Node.js: 24.17.0.
- npm: 11.18.0.
- Docker: 29.5.1.
- PostgreSQL image: `postgres@sha256:5c855ad7b85e68e48a62f34662853f38b57c1c1d80f3a927ab58034fd6d31c5e`.

## Contract and dependency identities

- OpenAPI SHA-256: `7f953cd862595af40af74c2bb438de00a5b8bd711df583031717825dfdac54e5`.
- Contract policy SHA-256: `547312bf67b7645996f284786b3e7ac9178cdacdf85e6ae900cd2a09477f23f0`.
- Contract digest manifest SHA-256: `6084f50a2d910e70f66a9cf2e495814368cecb04876e456426af8630db8f87a6`.
- Ordered fixture-checksum list SHA-256: `7f9d2e225ed834c4afd6a92b355621c4dc7dd795c77a3ab77be9460d5fc14e56`.
- uv lock SHA-256: `d8af817482155158760ee8b2be9622b0f3f3287523943bf4ad7baa05b1846a97`.
- npm lock SHA-256: `de59055c555e6be7f3762545ffb60e989121308356ba6f9ae379944967ea8af3`.

The ordered fixture checksum is the SHA-256 of the output from
`sha256sum docs/api/fixtures/*.json` in shell path order.

## Migration identities

- Migration 0001, `control_foundation`: `1a23b671fb1afbc56e37b38970520fd63b0a2ec32f2d0f050049d78e86855dd3`.
- Migration 0002, `runtime_ledger`: `e48ad870632d589c16539b1b47d67fcdd284323906e505ba2979b6f673eb9e73`.
- 0001 up SQL SHA-256: `4f0806fc07147c9419b5f2a5db20e4457a4353395ce723b6500e027c56fb5626`.
- 0001 down SQL SHA-256: `ff294e5ba1efcda24189cd1b579605bcb14345b745ef61c91f4d85d2de4a1b51`.
- 0002 up SQL SHA-256: `db878eb90f5627d27a79e6179345f63b222354f7c14a6ba9e1d06f76940bf413`.
- 0002 down SQL SHA-256: `7b4c7569a87fccb5afe445e9f4ba6379ecf730f3691d029478595893d39f7332`.

The migration identity includes the up and down SQL in the repository migration
loader. The loader rejects a checksum change after a migration is applied.

## Matrix evidence

The verification used a detached worktree without an existing uv environment
or Node dependency tree. Each database case used a new random database on a
digest-pinned PostgreSQL service that listened on localhost only.

- Generated contracts and deterministic fixtures: passed.
- Empty migrate to versions 0001 and 0002: passed.
- Upgrade from version 0001 with retained control data: passed.
- Rollback from version 0002 to 0001: passed.
- Re-upgrade from version 0001 to 0002 with retained control data: passed.
- Final rollback to version 0000: passed.
- Failed state and audit transaction rollback: passed.
- Authorized state and linked audit transaction commit: passed.
- Service and workspace scope isolation: passed.
- Permission denial before the record lookup: passed.
- Closed stored audit authority class: passed.
- Real PostgreSQL suite: 27 passed.
- Python suite outside the database service: 91 passed and 26 expected database
  skips.
- TypeScript suite: 27 passed.
- React Doctor: score 100 with zero diagnostics.
- Python and npm security audits: no known vulnerability.

## Tracked failures

The clean matrix found and tracked these failures before a fix:

- `llmr-e11.1`: the database gate did not install backend dependencies.
- `llmr-e11.3`: Bandit treated two fixed public or test identifiers as secrets.
- `llmr-e11.5`: stored audit events did not contain the formal authority class.
- `llmr-e11.7`: the Node gate did not install its exact locked dependency tree.

The product, language, database, security, Beads plan, and repository checks
passed after these fixes. The final detached clean command used Node.js 24.17.0
through `npm exec`. It set `LLMROUTER_FULL_CHECKS` to `1`, ran
`./scripts/check-repository.sh`, and exited with status 0.
