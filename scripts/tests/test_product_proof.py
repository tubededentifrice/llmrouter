"""Regression checks for the deterministic simplified product proof."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROOF_SCRIPT = REPOSITORY_ROOT / "scripts/prove-simplified-product.sh"
LIVE_PROOF = REPOSITORY_ROOT / "scripts/prove-localhost.py"
ARGUMENT_ERROR = 2
EXPECTED_STORAGE_RESTARTS = 2


def test_proof_uses_only_loopback_public_endpoints_and_the_fake_adapter() -> None:
    """Keep provider work offline and public checks on localhost."""
    shell = PROOF_SCRIPT.read_text(encoding="utf-8")
    live = LIVE_PROOF.read_text(encoding="utf-8")
    assert "http://127.0.0.1:" in shell
    assert "http://127.0.0.1:" in live
    assert "https://openrouter" not in shell + live
    assert "https://api.openai" not in shell + live
    assert "'fake-provider', 'Fake provider', 'fake', true" in live
    assert "local-development.sh reset" in shell


def test_proof_rejects_arguments_before_it_changes_deployment() -> None:
    """Reject an input that could change the fixed proof scope."""
    result = subprocess.run(  # noqa: S603 - Execute the fixed local proof path.
        [PROOF_SCRIPT, "unexpected"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == ARGUMENT_ERROR
    assert "does not accept arguments" in result.stderr


def test_proof_covers_restart_and_dependency_failures() -> None:
    """Keep each deployment fault proof in the durable workflow."""
    shell = PROOF_SCRIPT.read_text(encoding="utf-8")
    assert 'docker restart "${backend_container}"' in shell
    assert (
        shell.count('docker restart "${storage_container}"')
        == EXPECTED_STORAGE_RESTARTS
    )
    assert 'docker stop "${postgres_container}"' in shell
    assert "verify-object-storage.py failure" in shell
    assert "trap cleanup EXIT" in shell
    assert "wait_for_health" in shell
    assert "./scripts/check-database.sh" in shell
    assert "--no-cov" in shell
    assert "unset LLMROUTER_TEST_DATABASE_URL" in shell
    assert "[[ ! -x /usr/bin/google-chrome ]]" in shell


def test_local_development_exposes_the_fixed_proof() -> None:
    """Keep the accepted local-development proof entry point."""
    source = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    assert "prove)" in source
    assert '[[ "$#" == "1" ]]' in source
    assert 'exec "${repository_root}/scripts/prove-simplified-product.sh"' in source


def test_live_proof_covers_sdk_harness_and_native_operation_families() -> None:
    """Keep one live native and shared-library check for each product family."""
    source = LIVE_PROOF.read_text(encoding="utf-8")
    for evidence in (
        "ConversationHarness(",
        "RouterClient(",
        '"/v1/model-calls"',
        '"/v1/model-streams"',
        '"/v1/embeddings"',
        '"/v1/media-jobs"',
        '"/v1/statistics"',
        '"/v1/admin/session/start"',
        '"/v1/admin/services/beta"',
        "fake-stream-interruption-v1",
        'Path("/usr/bin/google-chrome")',
        '"Services\\n2"',
        '"Provider connections\\n1"',
    ):
        assert evidence in source
