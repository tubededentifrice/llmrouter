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
    assert "lock_deployment" in shell
    assert "flock --nonblock 9" in shell
    assert 'readlink "/proc/$$/fd/9"' in shell
    assert "created_admin_session=0" in shell
    assert "local-development.sh test-session" in shell
    assert "local-development.sh clear-test-session" in shell


def test_local_development_exposes_the_fixed_proof() -> None:
    """Keep the accepted local-development proof entry point."""
    source = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    assert "prove)" in source
    assert '[[ "$#" == "1" ]]' in source
    proof_action = source[source.index("    prove)") : source.index("    *)")]
    assert proof_action.index("lock_operation") < proof_action.index(
        'exec "${repository_root}/scripts/prove-simplified-product.sh"'
    )
    assert 'exec "${repository_root}/scripts/prove-simplified-product.sh"' in source
    assert "test-session)" in source
    assert "clear-test-session)" in source


def test_live_proof_covers_sdk_harness_and_native_operation_families() -> None:
    """Keep one live native and shared-library check for each product family."""
    source = LIVE_PROOF.read_text(encoding="utf-8")
    for evidence in (
        "ConversationHarness(",
        "RouterClient(",
        "ExactModelSelector(",
        '"/v1/model-calls"',
        '"/v1/model-streams"',
        '"/v1/embeddings"',
        '"/v1/media-jobs"',
        '"/v1/statistics"',
        '"/v1/admin/session/start"',
        '"/v1/admin/services/beta"',
        "foreign_model.status_code == 404",
        "foreign_embedding.status_code == 404",
        "foreign_media.status_code == 404",
        "hidden_content.status_code == 404",
        'bucket["dimensions"][0] == "(exact)"',
        'foreign_statistics.json()["buckets"] == []',
        "fake-stream-interruption-v1",
        'Path("/usr/bin/google-chrome")',
        '"Services\\n2"',
        '"Provider connections\\n1"',
        '"/v1/admin/playground/model-calls"',
        '"/v1/admin/playground/model-streams"',
        '"/v1/admin/playground/embeddings"',
        '"/v1/admin/playground/media-jobs"',
        '"/v1/admin/request-logs"',
        '"/v1/admin/statistics"',
        "expired_admin_session",
        "_prove_administrator_logout(",
        "_assert_axe(",
        "Accessibility.getFullAXTree",
        "forced-colors",
        "prefers-reduced-motion",
        "_prove_service_tree(",
        "_prove_configuration_graph(",
        "_prove_other_playground_operations(",
        "_prove_observation_pages(",
        "_prove_route_and_state_matrix(",
        '"/providers", "/models", "/assignments", "/playground"',
        '"loading" && listPaths.has(url.pathname)',
        'mode === "error"',
        'mode === "empty"',
        "A failed refresh did not retain and label the current configuration graph",
        'mode === "remove-text"',
        "Target unavailable",
        "Refresh target",
        "Prepare retained media download",
        "width=1440, mobile=False",
        "width=390, mobile=True",
        '"height": 844 if mobile else 900',
        '"Providers",\n            "Canonical models",\n            "Assignments"',
        "[aria-label='LLM configuration relationships']",
        "input[aria-label='Search configuration']",
        "[data-group-id='model:text-model']",
        "[data-group-id='assignment:workflow']",
        "[data-node-id='rung:workflow:1']",
        "[data-node-id='rung:workflow:2']",
        "data-source-node-id='mapping:text'",
        "data-target-node-id='rung:workflow:2'",
        "The assignment inspector did not restore rung focus",
        "The route search did not keep its connected context",
        "Text model with a deliberately long name for responsive proof",
    ):
        assert evidence in source
    assert "if opcode == 9:\n                self._send_frame(10, payload)" in source


def test_browser_proof_keeps_authentication_and_provider_work_safe() -> None:
    """Keep browser proof on localhost with the real session and fake routes."""
    source = LIVE_PROOF.read_text(encoding="utf-8")
    assert '"url": ADMIN_ORIGIN' in source
    assert '"httpOnly": True' in source
    assert '"sameSite": "Lax"' in source
    assert '"secure": False' in source
    assert '"--remote-debugging-address=127.0.0.1"' in source
    assert 'f"--remote-allow-origins=http://127.0.0.1:{port}"' in source
    assert 'f"Origin: http://{url.host}:{url.port}\\r\\n"' in source
    assert '"--remote-allow-origins=*"' not in source
    assert '"provider_model_api_name": "text"' in source
    assert '"provider_model_api_name": "embedding"' in source
    assert '"provider_model_api_name": "media"' in source
    assert 'Service API key" not in modal_text' in source
    assert 'Workspace" not in modal_text' in source
    assert 'Permission scope" not in modal_text' in source
    assert "https://llmrouter.opendle.dev" not in source
