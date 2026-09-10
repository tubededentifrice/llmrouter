---
name: repository-tooling
description: Build or improve durable repository tools, test fixtures, quality gates, and concise agent guidance. Use when repeated manual work, missing test access, authentication setup, an unreliable check, or another repository difficulty slows delivery or lowers confidence.
---

# Improve repository tooling

Turn repeatable friction into a repository capability. Keep one-off diagnosis
inside the current task, but do not leave a recurring workaround only in chat.

## Select the durable fix

Use the smallest applicable mechanism:

1. Add or improve a deterministic script for a repeated command or check.
2. Add a test or quality gate for a failure that automation can detect.
3. Add or update a skill for a reusable workflow that needs judgment.
4. Add a nested `AGENTS.md` only for directory-specific rules. Keep the root
   instructions concise and link to the skill for details.

Prefer an existing tool over a parallel tool. Make scripts safe to repeat,
non-interactive by default, and clear on failure. Add them to
`scripts/check-repository.sh` or the applicable CI gate when they protect all
future changes.

## Remove access and environment friction

When authentication or environment setup blocks tests, add a safe test path:

- use an isolated test identity, fixture, local bootstrap command, or test
  service adapter;
- make credentials short-lived or generated outside Git;
- keep production authentication and authorization enabled;
- never copy a production credential, bypass a permission check, or bind a
  test service to a public interface;
- document only the command and expected result that a later agent needs.

If the repository cannot safely automate the setup, improve the nearest skill
or instruction with the exact prerequisite and failure test.

### Test the Router administration UI without Pocket ID

Keep Pocket ID enabled. Do not add an authentication bypass to the backend.
For localhost browser automation:

1. Start the deployment with `./scripts/local-development.sh start`.
2. Run `./scripts/local-development.sh test-session`.
3. Read `.local-development/test-administrator-session.json` directly from the
   browser test process. Do not print or copy its values.
4. Set its named cookie for `http://127.0.0.1:5174`. Make it host-only,
   HttpOnly, SameSite=Lax, path `/`, and `secure: false` for loopback HTTP.
   Administrator writes use its CSRF value in `X-CSRF-Token` and its exact
   origin.
5. Run `./scripts/local-development.sh clear-test-session` after the test.

The session expires after 15 minutes. It uses the production session store,
verifier, encryption, CSRF, origin, expiry, and administrator authorization
path. The complete product proof uses this fixture and tests it on localhost.

### Check browser-proof changes without a reset

Use `uv run python scripts/tests/run_browser_proof.py` for controlled browser
checks. It builds the existing real-App fixtures from installed dependencies,
fulfills or rejects every browser request, and tests the actual Python proof
helpers. It writes synthetic images and measurements to one new private
`/tmp/llmrouter-browser-proof-*` directory. Review the images and collect all
results before reporting completion. Do not run unchanged complete UI suites
again when their exact source and dependency evidence remains valid.

For a live read-only check, run from the checkout that owns the running local
deployment and its installed Python environment. The session commands use the
current working directory. Import the proof without calling its `main()`:

```bash
(
set -e
trap './scripts/local-development.sh clear-test-session' EXIT
./scripts/local-development.sh test-session
uv run python - <<'PYTHON'
import runpy
import sys

sys.path.insert(0, "scripts")
proof = runpy.run_path("scripts/prove-localhost.py")
session = proof["read_development_administrator_session"]()
proof["_prove_read_only_administration"](session.cookie_value)
PYTHON
)
```

This path permits only GET requests to `http://127.0.0.1:5174`, blocks browser
WebSocket channels, and saves no live content or screenshots. It does not seed
data or run playground operations. A candidate may receive the parsed session
in memory from the real checkout; do not copy its ignored file. The full
`prove` command remains a separate destructive workflow and requires authority
to reset the deployment.

## Preserve quality

- Use LSP inspection before a behavior edit and LSP diagnostics after it when
  an applicable server is available.
- Add a regression test for the difficulty when practical.
- Run each affected focused check, then `./scripts/check-repository.sh`.
- Keep React Doctor at score 100 with zero diagnostics. Run the repository
  React Doctor gate after each React change, and add that gate before the
  first React surface when it does not exist.
- Run the dependency-age gate for dependency changes when the repository
  supplies one. Extend the gate when a new ecosystem is added.
- Use `uv` for all Python environments, dependencies, locks, commands, and
  tools. Use `uv add`, `uv sync`, `uv lock`, and `uv run`. Do not use `pip`,
  `pipx`, Poetry, or a manually created virtual environment.

Review the new tool itself: test success, expected failure, unsafe input, and
operation from a clean checkout when applicable.
